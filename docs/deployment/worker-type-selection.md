# Choosing a worker type: ThreadWorker vs ProcessWorker

- **Status**: Guidance
- **Applies to**: `agentflow.core.agent.Agent`, `agentflow.core.agent_worker`
- **Related**: RFC-008 (ProcessWorker lifecycle), RFC-009 (ThreadWorker lifecycle), RFC-013 (process-exit policy)
- **Companion docs**: [`systemd.md`](./systemd.md), [`container-liveness.md`](./container-liveness.md)

This document explains how to pick a worker type, and what AgentFlow
can and cannot guarantee once you have picked. The short version:

> **A wedged ThreadWorker cannot be forcibly reclaimed from inside the
> process. A wedged ProcessWorker child normally can, via signals.**
> If a handler can block uninterruptibly, that difference decides
> whether a stuck agent is a recoverable event or a process restart.

---

## 1. How the choice is expressed

Worker type is selected per Agent, either through config or through
the explicit start helpers:

```python
from agentflow.core import config

# Via config key — this is the durable, deployment-level choice.
agent = MyAgent({config.CONCURRENCY_TYPE: 'process'})   # ProcessWorker
agent = MyAgent({config.CONCURRENCY_TYPE: 'thread'})    # ThreadWorker
agent.start()

# Via explicit helpers — same effect, set at the call site.
agent.start_process()   # forces CONCURRENCY_TYPE='process'
agent.start_thread()    # forces CONCURRENCY_TYPE='thread'
```

`Agent.__create_worker` dispatches on exactly one condition: the
string `'process'` selects `ProcessWorker`; **any other value selects
`ThreadWorker`**. The shipped default in `config.default_config` is
`'process'`.

> Note the fallback direction: a typo such as `'processs'` or
> `'Process'` silently yields a **ThreadWorker**, not an error. If
> worker type matters for your deployment, assert it after start
> rather than trusting the string.

There is **no automatic fallback** between the two. AgentFlow will
never promote a ThreadWorker to a ProcessWorker on your behalf, and
never restarts a process for you — both are deliberate RFC-013
non-goals, because a library silently changing a process model (and
therefore memory sharing, pickling requirements, and fault domains)
would be a worse surprise than a stuck thread.

---

## 2. When ThreadWorker is the right choice

Use ThreadWorker when **all** of the following hold:

- Handlers are cooperative — they check `is_running()` / respond to
  the stop signal, or they are short enough that a 5-second stop
  deadline is never at risk.
- Blocking calls are bounded by an explicit, trusted timeout.
- The agent shares in-process state (caches, connection pools, model
  weights) with its host, and moving to a separate process would mean
  duplicating or re-pickling that state.
- Startup cost matters and the workload is fine-grained: threads
  start in microseconds, processes in tens to hundreds of
  milliseconds.
- The work is I/O-bound. CPU-bound work in a thread contends on the
  GIL and will not scale across cores.

ThreadWorker is the cheaper option and the right default for
well-behaved, I/O-bound, cooperative handlers.

## 3. When ProcessWorker is the right choice

Use ProcessWorker when **any** of the following hold:

- A handler can block in a way you cannot bound or interrupt (see
  §4).
- The handler calls third-party or native code whose timeout
  behaviour you do not control.
- The handler is CPU-bound and needs a real core.
- The handler may crash the interpreter (segfault in a C extension,
  `abort()`, unbounded allocation) — a crash in a child process is a
  reportable exit code; a crash in a thread takes the whole process
  with it.
- The agent is untrusted, experimental, or third-party supplied.
- **Fault isolation is a stated requirement for this agent** — this
  is AgentFlow invariant #1 ("a failure in one agent must not stop
  unrelated agents"), and ProcessWorker is the only worker type that
  can actually enforce it under a wedged handler.

ProcessWorker's stop path escalates on a bounded schedule —
cooperative `join(graceful_timeout_s=5.0)` → `terminate()` with
`join(terminate_timeout_s=2.0)` → `kill()` with
`join(kill_timeout_s=1.0)` — so `stop()` itself returns within ~8
seconds and reports the child's exit code, and in the ordinary case a
stuck child is reclaimed by `SIGKILL` without operator involvement.

`SIGKILL` cannot be caught or ignored, but it is not instantaneous in
every case: a child parked in uninterruptible kernel I/O (`D` state,
e.g. a stalled NFS mount) stays until that I/O completes, and a child
that has itself become a zombie needs reaping. So the honest claim is
**bounded caller return plus a reliable kill in the normal case** —
not a mathematical guarantee that the child's memory is freed within
8 seconds. This is still categorically stronger than ThreadWorker,
where no forced-reclaim mechanism exists at all.

---

## 4. High-risk blocking calls

Each of these can block a ThreadWorker past its stop deadline with no
way for AgentFlow to interrupt it. If a handler can reach one of
them, prefer ProcessWorker.

**Socket / network without a timeout**

- `socket.recv()` / `accept()` / `connect()` on a socket with no
  `settimeout()`
- `requests.get(...)` with no `timeout=` (the default is *no*
  timeout, not a short one)
- `urllib.request.urlopen(...)` with no `timeout=`
- DB driver calls where the connect/read timeout is unset
  (`psycopg2`, `pymysql`, `pymongo`, …)
- `paho.mqtt` blocking loops without a stop condition

**Synchronisation primitives with no timeout**

- `threading.Lock.acquire()` — no timeout
- `queue.Queue.get()` / `put()` — no timeout
- `threading.Event.wait()` — no timeout
- `Thread.join()` / `Process.join()` — no timeout
- `multiprocessing` primitives without timeouts

**Filesystem and device I/O (often not interruptible at all)**

- Reads/writes on NFS or any network filesystem during a server stall
  — these are frequently in uninterruptible sleep (`D` state), where
  even a signal will not free the thread
- `fcntl.flock()` on a contended file
- Reads from a FIFO/pipe with no writer
- Serial / device-node reads

**Subprocess**

- `subprocess.run(...)` / `communicate()` with no `timeout=`
- `os.system(...)` — no timeout parameter exists at all

**Native / C-extension code**

- Any long C call that does not release the GIL — this blocks *every*
  thread in the process, not just the worker
- `time.sleep()` with a very large argument
- Tight compute loops in a C extension with no callback into Python

**Rule of thumb:** if you cannot point to the parameter that bounds
the call, treat it as unbounded.

---

## 5. `STOP_TIMEOUT` and `requires_process_restart`

When a ThreadWorker's cooperative stop deadline expires:

- `stop()` returns `False`
- `state` becomes `WorkerState.STOP_TIMEOUT`
- the worker thread is still alive, and it is **non-daemon**

RFC-013 adds four read-only diagnostics on `ThreadWorker`:

| Property | Type | Meaning |
| --- | --- | --- |
| `thread_alive` | `bool` | worker thread exists and `is_alive()` |
| `thread_daemon` | `Optional[bool]` | the thread's `daemon` flag; `None` if no thread |
| `worker_thread_ident` | `Optional[int]` | OS thread id, for `py-spy` / `gdb` |
| `requires_process_restart` | `bool` | conjunction of the three conditions below |

`requires_process_restart` is `True` **iff all three hold right now**:

1. `state == WorkerState.STOP_TIMEOUT`
2. `thread_alive is True`
3. `thread_daemon is False`

### What `True` means

There is, at this instant, a live non-daemon worker thread that missed
its stop deadline. Because the thread is non-daemon, the Python
interpreter **will not exit while it runs** — `sys.exit()` and falling
off the end of `main()` both block in the interpreter's shutdown join.
Guaranteeing process exit therefore requires external containment
(a supervisor's `SIGKILL`) or a process restart.

### What `True` does **not** mean

It is **not** a verdict that the thread is permanently unrecoverable.
The blocking call may still return on its own, and a retried `stop()`
may still succeed. The property makes no prediction about either.

Concretely, it clears itself with no reset call and no latch:

- Blocker releases and the thread exits — `thread_alive` goes `False`,
  so the property goes `False` **even though `state` may still read
  `STOP_TIMEOUT`**. State is history; the property is present tense.
- A retried `stop()` reaches `STOPPED` — condition 1 fails → `False`.
- Worker never started — `thread_alive` is `False` → `False`.

Each read is computed fresh. The getter never logs, never caches, and
never mutates state, so it is safe to poll from a health endpoint or
any other thread.

### Reading it from a generic worker reference

`ProcessWorker` does not define these properties. Callers must not
assume they exist:

```python
needs_restart = bool(getattr(worker, 'requires_process_restart', False))
```

A worker that does not expose the property is treated as `False` —
absence is not a fault. `Agent.terminate` uses exactly this
capability-based pattern.

### What `Agent.terminate` reports

When `worker.stop()` returns `False`, `Agent.terminate` logs one
diagnostic — at most one per invocation:

- `requires_process_restart` is `True` → **`ERROR`**, including worker
  type, state, thread ident, alive, daemon, the phrase *"external
  process restart or supervisor containment required"*, and an
  RFC-013 reference.
- otherwise → the pre-existing **`WARNING`** path.

`terminate()` never raises, never calls `os._exit`, and its signature
and return type are unchanged. Diagnostic reads are themselves guarded,
so a misbehaving worker property cannot break the never-raise contract.

---

## 6. Unsafe "solutions" — and why they are rejected

These appear in blog posts and StackOverflow answers. AgentFlow
rejects all of them; none is a supported remedy.

**`daemon=True` on the worker thread** — Rejected (RFC-013 §5 Option
B). It would let the process exit, but daemon threads are frozen
abruptly at interpreter shutdown: `finally` blocks and `atexit`
handlers are **not guaranteed to run**. That trades a visible hang for
silent data loss and half-released broker state — corrupting the
cleanup semantics RFC-009/RFC-010 depend on. The
`daemon=False,       # RFC-009 §7.13` marker in `ThreadWorker.start`
is pinned by tests precisely so this cannot be changed casually.

**`ctypes.pythonapi.PyThreadState_SetAsyncExc`** — Rejected (RFC-013
§5 Option J). It only raises at Python bytecode boundaries, so it does
nothing for a thread parked in a C-level `recv()` or in
uninterruptible disk sleep — i.e. it fails in exactly the cases you
would reach for it. It can also corrupt interpreter state.

**`os._exit()` from library code** — Rejected (RFC-013 §5 Option F).
It bypasses `atexit`, buffer flushing, and every `finally` block in
the process. Deciding to abandon a process is an application and
operator decision, not a library's. Tests pin that AgentFlow core
never calls it.

**`Thread.kill()`** — Does not exist. POSIX thread cancellation is not
exposed by CPython, and would be unsafe with the GIL if it were.

**`signal` to interrupt a worker thread** — Python delivers signals
only on the main thread, so this cannot target a worker. It also does
not free a thread in uninterruptible (`D`-state) I/O.

The honest summary: **once a non-daemon Python thread is wedged in an
uninterruptible call, nothing inside the process can reclaim it.**
That is a CPython property, not an AgentFlow limitation, and it is the
entire reason ProcessWorker exists.

---

## 7. Migration decision tree

```
Can a handler block in a call you cannot bound with an explicit timeout?
│  (check the §4 list — unsure counts as yes)
│
├── NO ──▶ Is the work CPU-bound?
│          ├── NO ──▶ Does it need in-process shared state?
│          │          ├── YES ─▶ ThreadWorker ✅
│          │          └── NO ──▶ ThreadWorker ✅ (ProcessWorker also fine)
│          └── YES ─▶ ProcessWorker ✅  (GIL contention)
│
└── YES ─▶ Can you add a timeout / make the call cooperative?
           ├── YES ─▶ Add the timeout, re-run this tree.
           │          Prefer fixing the handler over changing worker type.
           └── NO ──▶ Is a stuck agent tolerable until the next restart?
                      ├── YES ─▶ ThreadWorker + supervisor containment
                      │          (systemd.md / container-liveness.md)
                      │          + alert on the restart-required ERROR
                      └── NO ──▶ ProcessWorker ✅  ← fault isolation
```

### Migrating ThreadWorker → ProcessWorker

The switch is one config key, but the process model changes with it.
Check each of these before flipping it:

1. **The Agent must be picklable.** ProcessWorker spawns a child and
   sends the Agent across; see the Agent pickle protocol (RFC-008).
   Unpicklable attributes — open sockets, file handles, locks, DB
   connections, lambdas, local classes — must be excluded from
   `__getstate__` and rebuilt in the child.
2. **Shared mutable state stops being shared.** The child gets a
   copy. Module-level caches, counters, and singletons diverge
   silently. Route anything that must be shared through the broker or
   external storage.
3. **Rebuild connections in the child.** Never inherit a socket, MQTT
   client, or DB connection across the spawn boundary.
4. **Logging changes.** Child stdout/stderr needs collecting; handlers
   configured in the parent may not exist in the child.
5. **Budget the startup cost** — process spawn is orders of magnitude
   slower than thread start. It matters for short-lived or
   frequently-restarted agents.
6. **Stop semantics change, and improve.** `stop()` returns an exit
   code rather than a bool, escalates join → terminate → kill within
   ~8 s, and **never enters `STOP_TIMEOUT`** — so
   `requires_process_restart` stops being a concern for that agent.

### Staying on ThreadWorker

If you keep ThreadWorker for a handler that could wedge, then the
containment must come from outside the process. That is not optional:

- Configure a supervisor that will `SIGKILL` after a bounded grace
  period — [`systemd.md`](./systemd.md) or
  [`container-liveness.md`](./container-liveness.md).
- Alert on the `requires_process_restart` **ERROR** from
  `Agent.terminate`. It is the signal that a shutdown will not
  complete on its own.
- Capture `worker_thread_ident` and take a `py-spy dump --pid <pid>`
  before the restart — after the restart the evidence is gone, and a
  wedged-thread bug is very hard to reproduce on demand.
