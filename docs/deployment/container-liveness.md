# Containers: PID 1, liveness, and shutdown

- **Status**: Guidance
- **Related**: RFC-013 (process-exit policy), [`worker-type-selection.md`](./worker-type-selection.md), [`systemd.md`](./systemd.md)

> **AgentFlow does not ship a health endpoint.** Everything in §4 is an
> **integration contract and a sketch** — the shape of a check you write
> in your own application, using properties AgentFlow does expose. There
> is no built-in HTTP server, no `/healthz`, and no metrics exporter in
> this library.

The container runtime is the containment layer, exactly as systemd is
on a VM. AgentFlow bounds every shutdown path it controls and
**never calls `os._exit()`** (RFC-013 §5 Option F), so a wedged
non-daemon ThreadWorker thread keeps the interpreter alive until
something outside the process kills it.

---

## 1. PID 1 and signal forwarding

This is the most common way container shutdown silently breaks.

**PID 1 is special in Linux:** it does not get default signal
handlers. A process running as PID 1 that has not explicitly installed
a `SIGTERM` handler **ignores `SIGTERM` entirely**. The runtime then
waits out the full grace period and `SIGKILL`s — so every shutdown
takes the maximum time, and no cleanup runs.

There are two failure shapes:

```dockerfile
# BROKEN — shell form: /bin/sh -c becomes PID 1 and does not
# forward SIGTERM to Python. The app never learns it should stop.
CMD python -m myapp.agent_host

# BETTER — exec form: Python is PID 1 and receives SIGTERM directly,
# but only acts on it if you installed a handler (see systemd.md §4).
CMD ["python", "-m", "myapp.agent_host"]

# BEST — a real init as PID 1: forwards signals and reaps zombies.
# Zombie reaping matters for ProcessWorker, whose children must be
# reaped; PID 1 inherits orphans and a non-init PID 1 leaks them.
ENTRYPOINT ["tini", "--", "python", "-m", "myapp.agent_host"]
```

Docker provides `tini` via `docker run --init`. In Kubernetes, bake an
init (`tini`, `dumb-init`) into the image — there is no `--init` flag.

**Verify rather than assume:**

```bash
docker exec <ctr> ps -o pid,ppid,comm
# PID 1 should be tini/dumb-init/python — never `sh` or `bash`.

time docker stop <ctr>
# Should return well under the grace period. If it always takes
# exactly the full timeout, SIGTERM is not reaching your app.
```

An `ENTRYPOINT` wrapper script must `exec` the final command
(`exec python -m myapp.agent_host`), otherwise the shell stays PID 1
and swallows the signal.

---

## 2. Grace period and the shutdown chain

```
t=0                              SIGTERM → PID 1 (must be forwarded)
                                 └─ app handler sets flag,
                                    calls agent.terminate() for each agent
                                    ├─ ProcessWorker: bounded ~8s, converges
                                    └─ ThreadWorker:  5s deadline; if wedged,
                                       stop()→False, state=STOP_TIMEOUT,
                                       ERROR logged, non-daemon thread alive
                                       → interpreter will NOT exit
t < grace                        process exits → container stops cleanly
t = terminationGracePeriodSeconds
                                 SIGKILL → every process in the container
                                 → container dies regardless of the wedge
```

```yaml
spec:
  terminationGracePeriodSeconds: 30    # default 30; Docker's is 10
  containers:
    - name: agent-host
      image: myapp/agent-host:1.0
```

Size it above your worst-case cooperative shutdown and keep it finite:

- ProcessWorker escalation is bounded at ~8 s (join 5 + terminate 2 +
  kill 1), per agent stopped sequentially.
- Add broker disconnect and final flush time.
- 30 s suits a handful of agents; raise it for many sequential agents.
- **Never set it to a very large value to "avoid" a hang.** The grace
  period is the containment guarantee. Making it huge converts a fast
  forced restart into a long outage.

`preStop` hooks run *before* `SIGTERM` and **consume the same grace
budget** — a `preStop: sleep 20` inside a 30 s grace leaves agents 10 s.

---

## 3. Liveness vs readiness — they are not interchangeable

Conflating these is the second common failure. They have opposite
consequences.

| | Liveness | Readiness |
| --- | --- | --- |
| Question | "Should this pod be **killed and restarted**?" | "Should this pod **receive traffic**?" |
| On failure | container killed, restart counted | removed from Service endpoints; **not** restarted |
| Right for | unrecoverable, restart-fixable state | temporary: warming up, broker reconnecting, backpressure |
| Wrong use | transient blips → restart loops | permanent wedge → pod hangs forever, silently |

**Liveness must be strict.** A liveness probe that fails on a
transient condition (broker briefly unreachable) creates a restart
loop that takes down every replica at once — the classic outage
amplifier. Only fail liveness for states a restart genuinely fixes.

**Readiness must not be the only signal for a wedged worker.** A pod
that is merely un-ready is never restarted; it sits out of rotation
indefinitely while the wedged thread holds the process. That is the
exact scenario `requires_process_restart` exists to surface.

For AgentFlow, the natural mapping is:

- **Readiness** — broker connected, agents started, queues below
  their high-water mark. Recoverable, so drop traffic and wait.
- **Liveness** — the main loop is responsive, and (if your deployment
  chooses) no agent is stuck with `requires_process_restart`. A
  restart is the only remedy for a wedged non-daemon thread, so this
  is legitimately a liveness concern.

Note the deliberate hedge: whether one stuck agent should recycle the
whole pod is a **policy decision for your deployment**, not something
AgentFlow decides for you. See §4's caveat.

```yaml
livenessProbe:
  httpGet: { path: /healthz/live, port: 8080 }
  initialDelaySeconds: 20
  periodSeconds: 10
  timeoutSeconds: 3
  failureThreshold: 3          # ~30s of sustained failure before kill
readinessProbe:
  httpGet: { path: /healthz/ready, port: 8080 }
  periodSeconds: 5
  timeoutSeconds: 2
  failureThreshold: 2
```

Serve probes from a thread that cannot be blocked by agent work —
otherwise a wedged handler makes *every* probe fail and you lose the
ability to distinguish the failure modes.

---

## 4. Integration contract: aggregating `requires_process_restart`

**Again: this endpoint does not exist in AgentFlow. You write it.**
What AgentFlow guarantees is the contract the properties satisfy.

### The contract you can rely on

- `ThreadWorker` exposes `requires_process_restart: bool`,
  `thread_alive: bool`, `thread_daemon: Optional[bool]`, and
  `worker_thread_ident: Optional[int]`.
- All four are **read-only**, computed fresh on every read. Assignment
  raises `AttributeError`.
- Reading them **never logs, never caches, never mutates state**, and
  is safe from any thread — so a probe may poll them at any frequency.
- `requires_process_restart` is `True` **iff, at this instant**:
  `state == STOP_TIMEOUT` **and** `thread_alive` **and**
  `thread_daemon is False`.
- It is **self-clearing**: if the blocker releases and the thread
  exits, or a retried `stop()` reaches `STOPPED`, it returns to
  `False` with no reset call. It is present tense, not a latch, and
  **not** a claim that the thread is permanently unrecoverable.
- `ProcessWorker` **does not define these properties**. Consumers must
  use `getattr(worker, 'requires_process_restart', False)` — absence
  means `False`, not a fault.

### Sketch

```python
# ILLUSTRATIVE — not shipped by AgentFlow.
from http.server import BaseHTTPRequestHandler, HTTPServer
import json, threading

def restart_required(agents):
    """Agents whose worker currently needs external containment."""
    stuck = []
    for a in agents:
        w = getattr(a, '_agent_worker', None)
        if w is None:
            continue
        # Capability-based: ProcessWorker lacks these → False.
        if bool(getattr(w, 'requires_process_restart', False)):
            stuck.append({
                'agent': a.name,
                'worker_type': type(w).__name__,
                'state': str(getattr(w, 'state', 'unknown')),
                'thread_ident': getattr(w, 'worker_thread_ident', None),
                'thread_alive': getattr(w, 'thread_alive', None),
                'thread_daemon': getattr(w, 'thread_daemon', None),
            })
    return stuck

class Health(BaseHTTPRequestHandler):
    def do_GET(self):
        stuck = restart_required(AGENTS)
        if self.path == '/healthz/live':
            # Policy choice — see the caveat below.
            ok, body = not stuck, {'stuck': stuck}
        elif self.path == '/healthz/ready':
            ok, body = broker_connected(), {'broker': broker_connected()}
        else:
            self.send_response(404); self.end_headers(); return
        self.send_response(200 if ok else 503)
        self.send_header('Content-Type', 'application/json')
        self.end_headers()
        self.wfile.write(json.dumps(body).encode())

    def log_message(self, *args):
        pass   # don't spam logs with probe traffic

threading.Thread(
    target=HTTPServer(('', 8080), Health).serve_forever,
    daemon=True,     # a probe server may be abandoned at exit; agent
                     # workers may NOT — that is RFC-009 §7.13
).start()
```

> **Caveat — this is policy, not a rule.** Failing liveness on any
> `requires_process_restart` means one stuck agent recycles the whole
> pod, dropping the healthy agents with it. Since the flag clears
> itself if the blocker releases, a `failureThreshold` of 3+ over ~30 s
> avoids restarting on a wedge that was about to resolve. Decide
> deliberately; for a host running many unrelated agents, exporting
> the condition as a metric and alerting may beat auto-restart.
>
> The sketch reaches into `_agent_worker`, which is internal. Prefer
> exposing a small health accessor from your own `Agent` subclass so
> deployment code does not depend on a private attribute.

---

## 5. What is and is not guaranteed

**Guaranteed by AgentFlow**

- ProcessWorker stop **returns** in bounded time: join(5 s) →
  `terminate()` + join(2 s) → `kill()` + join(1 s), reporting the
  child's exit code. It never enters `STOP_TIMEOUT`. Note the scope:
  what is guaranteed is the *caller's* bounded return. `SIGKILL`
  reclaims the child reliably in the normal case, but a child parked
  in uninterruptible kernel I/O (`D` state) is not freed until that
  I/O completes — which is why the runtime's grace-period `SIGKILL`
  of the whole cgroup remains the outer backstop.
- ThreadWorker cooperative stop is bounded by its deadline; on expiry
  `stop()` returns `False` and state becomes `STOP_TIMEOUT` — the
  caller is always told, never left guessing.
- `Agent.terminate` never raises, never calls `os._exit`, and logs at
  most one restart-required **`ERROR`** per invocation.
- The diagnostic properties above, with the semantics in §4.

**NOT guaranteed — by AgentFlow or by CPython**

- **That a wedged non-daemon worker thread will ever exit on its own.**
  A thread parked in an uninterruptible call (`D`-state disk I/O, a
  timeout-less socket read, a C call holding the GIL) cannot be
  reclaimed by anything inside the process. `SIGTERM` reaches only the
  main thread; there is no `Thread.kill()`; async exception injection
  does not fire inside a C call.
- **That the interpreter will exit while such a thread lives.**
  Non-daemon threads are joined at shutdown, so `sys.exit()` and
  returning from `main()` both block.
- Therefore: **the runtime's final `SIGKILL` is the only hard
  guarantee that the container stops.** Keep
  `terminationGracePeriodSeconds` finite, keep PID 1 forwarding
  signals, and treat the restart-required `ERROR` as a handler bug to
  fix — per [`worker-type-selection.md`](./worker-type-selection.md),
  by adding a timeout or moving the agent to ProcessWorker.

---

## 6. Pre-deployment checklist

- [ ] PID 1 is an init or an `exec`'d Python — never a shell.
- [ ] `time docker stop` returns well under the grace period.
- [ ] The app installs a `SIGTERM` handler that calls
      `agent.terminate()` (see [`systemd.md`](./systemd.md) §4).
- [ ] `terminationGracePeriodSeconds` exceeds worst-case cooperative
      shutdown and is finite.
- [ ] `preStop` hooks are budgeted inside the grace period.
- [ ] Liveness and readiness are distinct endpoints with distinct
      failure semantics.
- [ ] The probe server runs on a thread agent work cannot block.
- [ ] Restart-required `ERROR` lines are alerted on, not just logged.
- [ ] Agents with unbounded blocking calls use ProcessWorker.
