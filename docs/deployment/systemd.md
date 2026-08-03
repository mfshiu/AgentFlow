# Running AgentFlow under systemd

- **Status**: Guidance
- **Related**: RFC-013 (process-exit policy), [`worker-type-selection.md`](./worker-type-selection.md), [`container-liveness.md`](./container-liveness.md)

AgentFlow bounds every shutdown path it controls, but it deliberately
**never calls `os._exit()`** (RFC-013 §5 Option F). A wedged non-daemon
ThreadWorker thread therefore keeps the interpreter alive indefinitely,
and no in-process mechanism can reclaim it.

**systemd is the containment layer.** The unit below is what converts
"this process will hang forever" into "this process is killed and
restarted within a bounded window".

---

## 1. Reference unit file

```ini
[Unit]
Description=AgentFlow agent host
After=network-online.target
Wants=network-online.target
# If the broker is a local unit, order after it but do NOT use
# Requires= unless the agent genuinely cannot run without it.
After=mosquitto.service

[Service]
Type=simple
User=agentflow
Group=agentflow
WorkingDirectory=/opt/agentflow
Environment=PYTHONUNBUFFERED=1
ExecStart=/opt/agentflow/.venv/bin/python -m myapp.agent_host

# --- Restart policy -------------------------------------------------
Restart=on-failure
RestartSec=5s

# Do not let a crash-loop hammer the broker: if the unit restarts
# more than 5 times in 60s, stop trying and enter `failed` so an
# alert fires instead of silently looping.
StartLimitIntervalSec=60
StartLimitBurst=5

# --- Bounded shutdown (the containment guarantee) -------------------
TimeoutStopSec=15
KillMode=mixed
KillSignal=SIGTERM
SendSIGKILL=yes
FinalKillSignal=SIGKILL

[Install]
WantedBy=multi-user.target
```

> `StartLimitIntervalSec` / `StartLimitBurst` belong in `[Unit]` on
> systemd < 229. On current systemd both placements work; `[Service]`
> is shown here for locality.

---

## 2. `Restart=on-failure` vs `Restart=always`

Both restart after a crash. They differ on **clean exits**, and that
difference decides whether you can ever stop the service normally.

| | `on-failure` | `always` |
| --- | --- | --- |
| Non-zero exit | restart | restart |
| Killed by signal (incl. `SIGKILL` after `TimeoutStopSec`) | restart | restart |
| Exit code 0 | **no restart** | restart |
| `systemctl stop` | no restart | no restart |

**Choose `on-failure` when** the agent host can legitimately decide to
exit — a drain mode, a one-shot batch, a config-reload supervisor loop,
or any path that exits 0 on purpose. `always` would fight that decision
and restart it in a loop.

**Choose `always` when** the process should never be down for any
reason, and every exit — including exit 0 — is by definition
unexpected. This suits a long-lived daemon with no legitimate
self-termination path. Be aware you lose the ability to shut down by
exiting cleanly; `systemctl stop` becomes the only way out.

**Recommendation for a typical agent host: `on-failure`.** It restarts
on the cases that matter (crash, and the `SIGKILL` that follows a
wedged-thread stop timeout) while preserving intentional exit as a
usable signal. Note that a `SIGKILL`ed process counts as a failure
under both settings, so **hang containment restarts work either way** —
this choice is only about clean exits.

---

## 3. The shutdown timing chain

With `TimeoutStopSec=15` and `KillMode=mixed`, `systemctl stop` runs:

```
t=0s     SIGTERM → main process only (KillMode=mixed)
         └─ your handler calls agent.terminate()
            ├─ ProcessWorker: bounded join(5s) → terminate(2s) → kill(1s)
            │                 caller returns in ~8s worst case;
            │                 SIGKILL reclaims the child in the normal case
            └─ ThreadWorker:  cooperative stop, 5s deadline
                              wedged → stop() returns False,
                              state=STOP_TIMEOUT, ERROR logged,
                              non-daemon thread still alive
                              → interpreter will NOT exit
t≤15s    process exits cleanly  → done, no SIGKILL
t=15s    TimeoutStopSec expires
         SIGKILL → main process AND all remaining members of the
                   cgroup (this is the "mixed" part)
         → process is gone; unit restarts per Restart=
```

### Why these settings

**`TimeoutStopSec=15`** — must exceed your worst-case *cooperative*
shutdown, so healthy stops are never truncated. ProcessWorker's
escalation is bounded at ~8 s (5 + 2 + 1), so 15 s leaves headroom for
broker disconnect and final flushes. Raise it if you run many agents
sequentially; keep it finite. `TimeoutStopSec=infinity` **removes the
containment guarantee entirely** and must never be used with
ThreadWorker.

**`KillMode=mixed`** — `SIGTERM` goes only to the main process, so your
application controls the shutdown sequence and children are stopped in
the right order by AgentFlow's own escalation. The final `SIGKILL`
still goes to *every* process in the cgroup, so an orphaned or wedged
ProcessWorker child cannot survive. `KillMode=control-group` would
`SIGTERM` children directly, racing AgentFlow's own child handling;
`KillMode=process` would leak children on the final kill. `mixed` is
the correct middle.

**`SendSIGKILL=yes`** — this is the default, and it is written
explicitly here because it is the setting that makes hang containment
real. Setting it to `no` means a wedged process is **never** forcibly
killed and the unit hangs in `deactivating` forever. Never set it to
`no` for a ThreadWorker deployment.

**`FinalKillSignal=SIGKILL`** — also the default (systemd 240+), stated
explicitly for the same reason. Some hardening templates change it to
`SIGABRT` to force a core dump; that is a legitimate debugging choice
for a wedged-thread investigation, but `SIGABRT` can be caught or
ignored, so pair it with a supervisor that will still escalate.

---

## 4. `ExecStop` and shutdown signals

**Prefer handling `SIGTERM` in the application over adding
`ExecStop=`.** With `Type=simple`, systemd sends `SIGTERM` to the main
process on stop; a signal handler is the direct path and needs no extra
unit configuration.

```python
import signal, threading

_shutdown = threading.Event()

def _on_sigterm(signum, frame):
    # Only set a flag here. Signal handlers run on the main thread
    # between bytecodes; doing real shutdown work in one risks
    # re-entrancy and deadlock against a lock the interrupted code
    # already holds.
    _shutdown.set()

signal.signal(signal.SIGTERM, _on_sigterm)
signal.signal(signal.SIGINT, _on_sigterm)

_shutdown.wait()
for agent in agents:
    agent.terminate()   # bounded; never raises; never calls os._exit
```

Things that bite here:

- **`ExecStop=` does not replace `SIGTERM`.** systemd runs `ExecStop=`,
  waits for it, and *then* still applies `KillSignal` to whatever is
  left. An `ExecStop` that returns immediately does not shorten the
  timeout chain.
- **`ExecStop=` shares the `TimeoutStopSec` budget** with the main
  process shutdown. A slow `ExecStop` eats the window your agents
  needed.
- **Signals reach only the main thread.** A wedged worker thread cannot
  be interrupted by `SIGTERM`, which is exactly why the `SIGKILL`
  backstop is mandatory rather than optional.
- **Do not re-raise or exit from the handler** while agents are still
  terminating; let `terminate()` run its bounded course.
- **Do not call `os._exit()` in the handler** to dodge a hang. It skips
  `atexit`, buffered-output flushing, and every `finally` block —
  including broker cleanup. If you conclude a process must be abandoned,
  that is the supervisor's `SIGKILL`, not the application's shortcut.
  AgentFlow core never calls `os._exit()`, and tests pin that.

---

## 5. Driving restart from the health signal

`Agent.terminate` emits an **`ERROR`** containing
`PROCESS RESTART REQUIRED` and the phrase *"external process restart or
supervisor containment required"* when `requires_process_restart` is
`True` — i.e. a non-daemon worker thread is still alive after its stop
deadline. Treat that line as the actionable signal.

Two ways to act on it.

### 5a. Journal-driven alerting (recommended)

Let the `TimeoutStopSec` → `SIGKILL` chain handle containment, and use
the log line to alert so the underlying bug gets fixed:

```bash
journalctl -u agentflow -p err --since "1 hour ago" \
  | grep -c "PROCESS RESTART REQUIRED"
```

A non-zero count means an agent could not be stopped cooperatively and
the process only exited because systemd killed it. That is a handler
bug — a missing timeout — not a systemd misconfiguration. Fix the
handler or move that agent to ProcessWorker
([`worker-type-selection.md`](./worker-type-selection.md) §4).

Capture evidence *before* the restart wipes it — the `ERROR` includes
`thread_ident`, which pairs with:

```bash
py-spy dump --pid <pid>     # which call is the thread parked in?
```

### 5b. Watchdog-driven restart

For an agent host that must self-report liveness, `systemd`'s watchdog
turns a stuck main loop into a bounded restart:

```ini
[Service]
Type=notify
WatchdogSec=30
Restart=on-failure
```

```python
import sdnotify   # pip install sdnotify

n = sdnotify.SystemdNotifier()
n.notify("READY=1")

while not _shutdown.is_set():
    # Only ping while genuinely healthy. Aggregate whatever your
    # deployment considers fatal — for example, refuse to ping if any
    # agent reports requires_process_restart.
    if not any(
        bool(getattr(a._get_worker(), 'requires_process_restart', False))
        for a in agents
    ):
        n.notify("WATCHDOG=1")
    _shutdown.wait(WatchdogSec / 3)
```

If the ping stops, systemd `SIGABRT`s the service (then `SIGKILL`s per
`FinalKillSignal`) and restarts it per `Restart=`.

> Two caveats. First, withholding the ping is a **policy decision you
> are making** — a `requires_process_restart` agent is not necessarily
> fatal to the host, and the flag clears itself if the blocker
> releases. Deliberately choose whether one stuck agent should recycle
> the whole process. Second, `_get_worker()` is internal; prefer
> exposing a small health accessor from your own agent subclass rather
> than reaching into it from deployment code.

---

## 6. Verifying the containment actually works

Do not assume the configuration is correct — prove it, before you need
it:

```bash
# 1. Deliberately wedge an agent (a handler that sleeps forever),
#    then request a stop and watch the timing chain.
time systemctl stop agentflow
#    Expect: returns at ~15s, not immediately and not never.

# 2. Confirm the kill and the restart were recorded.
journalctl -u agentflow -n 50
#    Expect: "State 'stop-sigterm' timed out. Killing." then
#            "Main process exited ... status=9/KILL"

# 3. Confirm it came back.
systemctl is-active agentflow
```

If step 1 hangs instead of returning at ~15 s, containment is broken —
check for `TimeoutStopSec=infinity` or `SendSIGKILL=no` in a drop-in:

```bash
systemctl show agentflow \
  -p TimeoutStopUSec -p SendSIGKILL -p KillMode -p FinalKillSignal
systemd-delta   # reveals overriding drop-ins
```
