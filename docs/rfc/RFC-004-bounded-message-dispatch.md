# RFC-004 — bounded message dispatch

- **Status**: Draft
- **Author**: Audit follow-up
- **Depends on**: `docs/audit/05-risk-register.md` R-04; consistent with [RFC-001](RFC-001-publish-sync-subscription-lifecycle.md), [RFC-002](RFC-002-publish-error-propagation.md), [RFC-003](RFC-003-auto-reply-contract.md)
- **Scope**: `Agent._on_message` dispatch strategy — bounded concurrency, bounded queue depth, explicit backpressure, graceful shutdown
- **Explicitly out of scope**: asyncio full rewrite, ProcessWorker refactor (R-06/R-08), MQTT reconnect (R-03), Parcel schema, pickle security (R-01), distributed backpressure

---

## 1. Problem statement

`Agent._on_message` (`src/agentflow/core/agent.py`) currently spawns a fresh `threading.Thread` for every received message via:

```python
threading.Thread(target=handle_message, args=(topic_handler, topic, pcl)).start()
```

Consequences (all runtime-confirmed — §2):

- **Unbounded parallelism**: N incoming messages → N concurrent OS threads. Under a burst or a slow handler, thread count grows without limit.
- **No backpressure**: nothing tells a fast producer (or a paho loop that just received a QoS-0 burst) to slow down. Every message enqueues immediately as a fresh thread.
- **Non-daemon threads**: interpreter exit blocks on stragglers.
- **No join mechanism**: `Agent.terminate()` cannot wait for in-flight handlers. Handler threads are anonymous and leaked.
- **Ordering hazards**: two messages on the same topic race; user-visible order is undefined.
- **Silent slow-path coupling**: a slow `broker.publish` in the auto-reply path runs on the same handler thread, so one slow reply blocks one handler slot but does not throttle further inbound messages.

This RFC proposes a bounded, backpressure-aware, gracefully-shutdownable dispatcher that replaces the fire-and-forget thread pattern while preserving the R-02, R-05, and R-13 contracts.

---

## 2. Runtime evidence

Baseline: `PYTHONPATH=src pytest tests/unit` → **151 passed, 0 failed, 0 xfailed, 0 xpassed** in 2.86 s.

Confirmed by `tests/unit/core/test_agent_message_threading.py`:

| Behaviour | Test | Result |
|---|---|---|
| Every delivery spawns a distinct Thread OBJECT | `test_every_message_spawns_one_new_thread` | PASSED |
| N=100/500/1000 deliveries → N Thread objects | `test_N_messages_create_N_threads[N]` | PASSED × 3 |
| Slow handlers → active thread count == N (1:1) | `test_slow_handler_active_thread_count_scales_with_n[10/50/100]` | PASSED × 3 |
| `Agent.terminate()` does not wait for handler threads | `test_agent_terminate_does_not_join_handler_threads` | PASSED |
| Handler thread is non-daemon | `test_handler_thread_is_not_daemon` | PASSED |
| Handler exceptions cleanly terminate the spawned thread | `test_handler_exception_terminates_thread_cleanly`, `test_handler_exception_does_not_leak_thread_when_auto_reply_active` | PASSED × 2 |
| Message completion order across threads is not guaranteed | `test_message_processing_order_across_threads_is_not_guaranteed` | PASSED |
| Same-topic messages execute concurrently | `test_same_topic_multiple_messages_execute_concurrently` | PASSED |
| Framework holds no lock around handler dispatch | `test_framework_provides_no_lock_around_handler_dispatch` | PASSED |
| Auto-reply publish runs on the handler thread | `test_auto_reply_publish_runs_on_the_handler_thread` | PASSED |
| 1 broker.deliver ↔ 1 handler thread | `test_each_broker_deliver_causes_exactly_one_handler_thread` | PASSED |

These form the pin-set that will be updated when the fix lands.

---

## 3. Current behavior

```mermaid
sequenceDiagram
    autonumber
    participant B as Broker (paho loop thread)
    participant OM as Agent._on_message
    participant T as fresh Thread N
    participant H as Handler
    B->>OM: _on_message(topic, payload)  [always on paho loop thread]
    OM->>OM: Parcel.from_payload, is_specific_handler, should_auto_reply
    OM->>T: threading.Thread(target=handle_message, ...).start()
    Note over OM: _on_message returns immediately (~microseconds)
    T->>H: handle_message runs handler
    alt should_auto_reply
      H-->>T: return data_resp
      T->>T: strip topic_return (RFC-003)
      T->>B: self.publish(topic_return, data_resp)
      Note over T: broker.publish runs on THIS thread; slow publish blocks this handler slot
    end
    T->>T: exit; no join, no reference held
```

Per-message cost: 1 OS thread spawn (~1 ms), 1 stack (~8 KB), no upper bound.

---

## 4. Desired behavior

- A fixed pool of consumer threads processes incoming messages from a bounded queue.
- `_on_message` on the paho loop thread enqueues without blocking; on overload, applies a well-defined backpressure policy (default: drop newest with WARNING log and metric increment).
- `Agent.terminate()` requests shutdown; consumers drain the queue up to a bounded timeout, then abandon stragglers (Python has no safe thread-kill).
- The dispatch contract remains sync-handler friendly: handlers still receive `(topic, pcl)` and their return value drives auto-reply exactly as under RFC-003.
- Metrics: `active_workers`, `queue_depth`, `dropped_message_count`, per-handler duration (DEBUG log) are exposed as attributes on the dispatcher for external observation.
- Same-topic ordering is **not** preserved by the default design (Option D); a follow-up RFC could adopt per-topic serial queues (Option E) if that guarantee becomes required.

---

## 5. Options considered

### Option A — Per-message Thread + daemon flag or explicit join

Sketch: keep `threading.Thread(...).start()`; add `daemon=True` and/or record the thread in `Agent._handler_threads` for `terminate()` to join.

| Aspect | Analysis |
|---|---|
| Bounds parallelism | ✗ |
| Bounds queue depth | ✗ |
| Backpressure | ✗ |
| Graceful shutdown | Partial (join with timeout) |
| Preserves handler signature | ✓ |
| Preserves R-02/R-05/R-13 | ✓ |
| Complexity | Minimal (~10 LOC) |
| Migration risk | Low |
| Future ProcessWorker | Trivial (each process has its own dispatcher) |

**Verdict**: rejected — does not address the primary risk (unbounded thread growth).

---

### Option B — `concurrent.futures.ThreadPoolExecutor`

Sketch: create one `ThreadPoolExecutor(max_workers=8)` per Agent; `_on_message` calls `executor.submit(handle_message, ...)`.

| Aspect | Analysis |
|---|---|
| Bounds parallelism | ✓ (via `max_workers`) |
| Bounds queue depth | **✗** — default internal queue is unbounded (`queue.SimpleQueue`). Submits never block. Under overload, memory grows. |
| Backpressure | ✗ |
| Graceful shutdown | ✓ (`shutdown(wait=True, cancel_futures=...)` — cancel_futures needs Python ≥ 3.9) |
| Preserves handler signature | ✓ |
| Preserves R-02/R-05/R-13 | ✓ |
| Complexity | Low |
| Migration risk | Low-Medium |
| Future ProcessWorker | ✓ (`ProcessPoolExecutor` swap) |

**Verdict**: partial. Fixes parallelism but not queue depth or backpressure. Reject as sole fix; keep as component if combined with Option C.

---

### Option C — `ThreadPoolExecutor` + bounded submission

Sketch: wrap `ThreadPoolExecutor` with a `threading.Semaphore(max_inflight)` acquired before `submit()` and released in a callback attached to the returned `Future`.

| Aspect | Analysis |
|---|---|
| Bounds parallelism | ✓ |
| Bounds queue depth | ✓ (via semaphore capacity == max_workers + tolerance) |
| Backpressure | ✓ (semaphore.acquire blocks or fails) |
| Graceful shutdown | ✓ |
| Preserves handler signature | ✓ |
| Preserves R-02/R-05/R-13 | ✓ |
| Complexity | Medium — semaphore around executor is fiddly; race between submit and future callback release |
| Migration risk | Medium |
| Future ProcessWorker | ✓ |

**Verdict**: viable but adds subtle machinery on top of stdlib. Option D achieves the same guarantees more directly.

---

### Option D — Bounded queue + fixed consumer threads (**RECOMMENDED**)

Sketch: `MessageDispatcher` owns a `queue.Queue(maxsize=Q)` and `N` consumer threads that pull, invoke `handle_message`, and loop.

| Aspect | Analysis |
|---|---|
| Bounds parallelism | ✓ (N consumer threads) |
| Bounds queue depth | ✓ (Q capacity) |
| Backpressure | ✓ (explicit put policy: block/drop-newest/drop-oldest/raise) |
| Graceful shutdown | ✓ (sentinel + join with timeout) |
| Preserves handler signature | ✓ |
| Preserves R-02/R-05/R-13 | ✓ (dispatcher just calls the same `handle_message` logic) |
| Complexity | Medium — small dedicated class, no external deps |
| Migration risk | Low-Medium |
| Future ProcessWorker | ✓ (same dispatcher runs inside child process) |
| Observability | ✓ (all state in one owning object) |

**Verdict**: **Recommended.** Most direct answer to the four priority requirements (bounded parallelism, bounded queue, backpressure, shutdown), and the simplest object to own the metrics.

---

### Option E — Per-topic serial queue

Sketch: each topic gets its own bounded queue and its own consumer thread. Messages on the same topic are serialized; different topics run in parallel.

| Aspect | Analysis |
|---|---|
| Bounds parallelism | ✓ (parallelism = number of active topics; still unbounded across topics) |
| Bounds queue depth | ✓ per topic |
| Backpressure | ✓ per topic |
| Graceful shutdown | ✓ but N-fold more machinery |
| Preserves handler signature | ✓ |
| Preserves R-02/R-05/R-13 | ✓ |
| Same-topic order | **✓ guaranteed** (only guarantee any option provides) |
| Complexity | High — dynamic per-topic state, cleanup when a topic goes idle, GC of dead topics |
| Migration risk | Medium-High |
| Future ProcessWorker | Complex — per-topic consumers × per-process |

**Verdict**: solves same-topic ordering but at meaningfully higher complexity. Defer to a follow-up RFC iff ordering is required; not part of this RFC.

---

### Option F — asyncio rewrite

Excluded per RFC-004 §Scope. Would require rewriting `Agent`, `MessageBroker`, `Worker`, and all handler contracts. Handler signature change is inevitable (sync vs async). Migration burden is disproportionate to the R-04 problem.

**Verdict**: out of scope.

---

### Comparison summary

| Criterion | A | B | C | **D** | E | F |
|---|---|---|---|---|---|---|
| Bounds parallelism | ✗ | ✓ | ✓ | ✓ | ✓ (per topic) | ✓ |
| Bounds queue depth | ✗ | ✗ | ✓ | ✓ | ✓ | ✓ |
| Explicit backpressure | ✗ | ✗ | ✓ | ✓ | ✓ | ✓ |
| Graceful shutdown | Partial | ✓ | ✓ | ✓ | ✓ | ✓ |
| Preserves handler signature | ✓ | ✓ | ✓ | ✓ | ✓ | ✗ |
| No schema change | ✓ | ✓ | ✓ | ✓ | ✓ | ✗ |
| Preserves R-02/R-05/R-13 | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ |
| Complexity | Low | Low | Medium | Medium | High | Very High |
| Verdict | rejected | insufficient | viable | **chosen** | deferred | out of scope |

---

## 6. Recommended design (Option D)

### 6.1 Component

A new class `MessageDispatcher` in `src/agentflow/core/dispatcher.py` (new file). Owned by `Agent` as `self._dispatcher`; created lazily in `_activate` (RFC-001-style, inside the worker context).

Public surface (all internal to Agent):

```python
class MessageDispatcher:
    def __init__(
        self,
        workers: int = 8,
        queue_size: int = 1024,
        overflow_policy: str = 'drop_newest',   # or 'drop_oldest' | 'raise' | 'block'
        block_timeout_ms: int = 0,               # only used when overflow_policy='block'
        shutdown_timeout_s: float = 5.0,
    ) -> None: ...

    def enqueue(self, task: Callable[[], None]) -> bool:
        """Enqueue a ready-to-run task. Returns True on success, False
        if dropped. Never blocks broker callback thread beyond
        block_timeout_ms."""

    def stop(self, timeout_s: Optional[float] = None) -> bool:
        """Signal consumers to drain remaining queue then exit. Returns
        True if all in-flight tasks completed within timeout, False if
        stragglers were abandoned."""

    # Metrics (read-only attributes; sampled atomically)
    active_workers: int
    queue_depth: int
    dropped_message_count: int
    processed_count: int
```

### 6.2 Wiring into `Agent`

`Agent._on_message` becomes an enqueue-and-return:

```python
def _on_message(self, topic, data):
    pcl = Parcel.from_payload(data)
    is_specific_handler = topic in self.__topic_handlers
    topic_handler = self.__topic_handlers.get(topic, self.on_message)
    should_auto_reply = bool(pcl.topic_return) and is_specific_handler

    def task():
        # Same handle_message body as RFC-003, unchanged.
        ...

    self._dispatcher.enqueue(task)
```

The consumer thread body simply runs `task()` and moves on. All RFC-003 semantics are inside `task` (which is where `handle_message` lives today).

### 6.3 Config-driven

Sensible defaults; every knob configurable via `agent_config`:

```python
agent_config = {
    'dispatch': {
        'workers': 8,
        'queue_size': 1024,
        'overflow_policy': 'drop_newest',
        'block_timeout_ms': 0,
        'shutdown_timeout_s': 5.0,
    }
}
```

Missing keys fall back to defaults. Legacy escape hatch:

```python
agent_config = {'dispatch': {'mode': 'per_message_thread'}}
```

Restores exact pre-RFC-004 behaviour (spawn one Thread per message, non-daemon, no join). Intended for one release cycle so downstream users have a rollback path without a code change.

---

## 7. Concrete decisions (all 15)

### 7.1 Default worker count
**8.** Rationale: I/O-bound workload (broker publish is the slow op); 8 provides room for parallel handlers without saturating an average small VM. Configurable.

### 7.2 Queue max capacity
**1024.** Rationale: at ~1 KB per parcel payload, ≤ 1 MB memory ceiling. Deep enough to absorb short bursts, shallow enough that overflow signals are meaningful within seconds. Configurable.

### 7.3 Queue-full policy

**Default: `drop_newest`** — the dispatcher's `enqueue()` calls `queue.put_nowait`, catches `queue.Full`, increments `dropped_message_count` (under `_metrics_lock`), and emits a WARNING log entry (rate-limited to at most one entry per topic per second). `enqueue()` returns `False` to the caller; it never raises. Rationale: cheapest, non-blocking, and — critically — safe for the paho loop thread. Matches MQTT QoS-0 "at most once" semantics for the primary use case.

**Broker-callback safety invariant** (non-negotiable): `enqueue()` invoked from `Agent._on_message` — i.e. from the paho loop thread — MUST NOT raise. Regardless of the configured `overflow_policy`, the dispatcher catches every `queue.Full` on this path and converts it to a drop-with-metric. A raising queue-full policy on the paho loop would corrupt paho's own dispatch state and delay reconnect / keepalive. This invariant is enforced inside `enqueue()`'s wrapper (not left to a `try/except` in `_on_message`) and is exercised by a dedicated acceptance test (§11 item 13).

Opt-in policies (chosen via `agent_config['dispatch']['overflow_policy']`):

- **`drop_oldest`** — dispatcher acquires an internal lock, pops the head via `queue.get_nowait()`, then `queue.put_nowait()` the new item. Also counts the dropped head in `dropped_message_count`. Favors freshest state.
- **`block`** — `queue.put(msg, timeout=block_timeout_ms / 1000)`. Only recommended when `block_timeout_ms` is small (≤ 50 ms); larger values freeze the paho loop and defeat the purpose. If the timeout expires, the message is dropped-newest with metric + WARNING.
- **`raise`** — `queue.Full` propagates to the **direct caller** of `dispatcher.enqueue()`. **Only permitted on the direct dispatcher API path**: test code or a future non-broker enqueue site that owns its call frame and has an explicit `try/except queue.Full`. On the broker path (`Agent._on_message` → `enqueue()`), the safety invariant above overrides `raise` and the effective behaviour is `drop_newest`. `raise` is therefore appropriate only for reliability-first callers who own their enqueue call site; **it MUST NOT be relied upon inside broker callbacks**.

In every case: `dropped_message_count` is incremented under `_metrics_lock`, and a WARNING is logged (rate-limited to at most one entry per topic per second via a per-topic timestamp cache).

### 7.4 May broker callback be blocked?
**No.** `_on_message` is invoked from the paho loop thread. Blocking that thread freezes MQTT keepalive / PINGREQ handling and delays reconnect logic (R-03). Default policy therefore never blocks; `block` policy is available but discouraged and capped by `block_timeout_ms`.

### 7.5 Does `Agent.terminate()` wait for queue drain?

**Yes, up to a bounded `shutdown_timeout_s`. Never waits forever.** Sequence:

1. `terminate()` calls `dispatcher.stop(timeout_s)`. `stop()` first atomically sets `_accepting = False` under `_state_lock`.
2. From that moment on, every `enqueue()` — from `_on_message` or from any direct caller — is **rejected**: it increments `rejected_after_stop_count` (under `_metrics_lock`), emits a rate-limited WARNING, and returns `False`. Messages arriving after `terminate()` are neither queued nor executed.
3. Consumers continue to drain the queue items already enqueued before step 1.
4. `stop()` posts one shutdown sentinel per consumer thread. Each consumer that sees a sentinel exits its loop after finishing its current task.
5. `stop()` computes a single monotonic deadline (`deadline = time.monotonic() + shutdown_timeout_s`) and joins each consumer with the remaining budget (`t.join(max(0, deadline - time.monotonic()))`). **Consumers are never joined without a per-call timeout**; the deadline is enforced across all joins collectively.
6. Return value: `True` if all consumers exited before the deadline; `False` if any remained (see §7.7 for the straggler report).

### 7.6 Shutdown timeout

**5.0 seconds default.** Configurable via `agent_config['dispatch']['shutdown_timeout_s']`. Rationale: matches typical operations SLA for graceful stop; longer values delay process exit, shorter values increase risk of abandoning in-flight work.

**Python thread-kill limitation (explicit)**: Python provides **no safe primitive to force-kill a running thread**. `Thread.join(timeout)` waits for cooperative completion; there is no `Thread.kill()`, `Thread.terminate()`, or equivalent. A rogue handler that blocks indefinitely (e.g. an infinite loop or an ungated `socket.recv`) cannot be terminated by the dispatcher. The RFC accepts this as a Python-level constraint and does not attempt any of the unsafe workarounds (setting a stop-flag observable only in cooperative handlers, `ctypes`-based `PyThreadState_SetAsyncExc`, or process-level `os.kill`). Consumers that miss the deadline are logged as stragglers (§7.7) but are not forcibly ended. This is a foundational reason the RFC caps handler responsibility rather than promising to enforce a kill.

### 7.7 Handling of stragglers after timeout

On a timed-out `stop()`, the dispatcher emits **one consolidated WARNING** that records the shutdown state under a single `_metrics_lock` snapshot:

- number of stragglers (consumers still `alive()` past the deadline);
- current `queue_depth` (unfinished tasks still in the queue);
- current `active_workers` count;
- for each straggler thread: its thread name and, if available, the topic of the task it was last dispatched;
- final counters: `processed_count`, `dropped_message_count`, `rejected_after_stop_count`, `error_count`.

`stop()` returns `False` so the caller learns the outcome programmatically.

**Daemon flag rationale**: consumer threads are `daemon=True` **only as a last-resort process-exit safety net** — they exist so that a wedged handler does not prevent the Python interpreter from ever exiting. The `daemon` flag is **NOT** the normal shutdown mechanism; the graceful drain path in §7.5 is. Operators who observe frequent straggler warnings should treat this as a handler bug (or as an under-sized `shutdown_timeout_s`), not as normal behaviour to be masked by relying on daemon-kill at exit.

Python provides no safe thread-kill primitive; force-termination is not attempted (see §7.6).

### 7.8 Same-topic ordering
**Not preserved** under Option D. All consumers share one queue; any consumer picks up any topic. Delivery order across different messages is not guaranteed.
Callers that require per-topic ordering must either (a) publish serially and wait for confirmation before the next publish, or (b) file a follow-up RFC to adopt Option E per-topic serial queues.

### 7.9 Cross-topic parallelism
**Yes**, bounded by `workers`. Up to `workers` handlers on distinct topics execute simultaneously.

### 7.10 Handler exception isolation

Consumer's loop:

```python
def run(self):
    while True:
        task = self._queue.get()
        try:
            if task is _SHUTDOWN_SENTINEL:
                return
            try:
                task()
            except Exception:
                # Ordinary handler errors: log and continue.
                self._metrics.incr_error()
                logger.exception(
                    "Dispatcher consumer trapped handler exception"
                )
            # NOTE: no `except BaseException` here — see contract below.
        finally:
            self._queue.task_done()
```

Contract:

- **Ordinary handler errors** (any subclass of `Exception`) are caught, counted in `error_count`, logged via `logger.exception(...)`, and the consumer proceeds to the next task. The consumer thread survives.
- **`BaseException` subclasses are deliberately NOT swallowed** by the framework. Specifically:
  - `KeyboardInterrupt` — a user Ctrl-C signal must reach the interpreter, not be hidden inside a consumer thread. Swallowing it would break interactive interruption.
  - `SystemExit` — a handler's explicit `sys.exit()` must be visible; hiding it would silently continue past an intentional termination request.
  - `GeneratorExit` — must propagate for `contextlib` / generator cleanup to work correctly.
  - These are allowed to propagate out of the inner `try` and terminate the consumer thread. `stop()` on subsequent shutdown will observe one fewer active worker and log accordingly (the outer `finally` still runs `task_done()`; see below). Silently swallowing them at the framework level would violate Python's cooperative interruption contract.
- **`queue.task_done()` is invoked in a `finally` block** for **every** dequeued item — including the shutdown sentinel and including tasks that raised `BaseException`. This guarantees `queue.join()` (if the dispatcher ever uses it during drain) never hangs due to a leaked accounting slot. The `finally` sits OUTSIDE the `Exception` handler so that a `BaseException` still executes `task_done()` before propagating up.
- The consumer thread name embeds its consumer index so that straggler / crash reports (§7.7) can identify which slot terminated.

### 7.11 Auto-reply thread
**Runs on the consumer thread** (same as today). The handler and its auto-reply `broker.publish` share a consumer slot. A slow broker publish therefore blocks one consumer, not a fresh OS thread — this is a bound on total pending publish work.

### 7.12 Metrics / logging

Counters and gauges exposed on `MessageDispatcher`. All mutations acquire an internal `threading.Lock` (`_metrics_lock`) so that increments observed under GIL still constitute a consistent memory-model event; individual attribute reads under the same lock return a coherent snapshot. Counters are plain Python ints, mutated only inside `_metrics_lock`; no `atomic` package or C extension is introduced.

- `active_workers: int` — consumers currently executing a task (gauge; incremented before task invocation, decremented in the outer `finally` of §7.10)
- `queue_depth: int` — current queue length (delegates to `queue.qsize()`, snapshotted under `_metrics_lock` alongside the other counters)
- `dropped_message_count: int` — cumulative drops from any overflow policy
- `rejected_after_stop_count: int` — cumulative `enqueue()` rejections after `stop()` set `_accepting = False`
- `processed_count: int` — cumulative task completions (successful `task()` returns)
- `error_count: int` — cumulative `Exception`-catching events (does not include `BaseException` propagations — those terminate the consumer and are reported at the straggler level)

**Snapshot API** (recommended over reading individual attributes):

```python
snapshot = dispatcher.metrics_snapshot()  # taken atomically under _metrics_lock
# {'active_workers': 3, 'queue_depth': 12, 'dropped_message_count': 0,
#  'rejected_after_stop_count': 0, 'processed_count': 128, 'error_count': 0}
```

`metrics_snapshot()` guarantees that every counter in the returned dict was sampled at the same instant. Individual attribute reads MAY race with a concurrent increment (they return the value under a brief lock but do not coordinate across counters); callers that need cross-counter consistency MUST use the snapshot.

Log lines:

- INFO on dispatcher start: `"Dispatcher started: workers=8 queue_size=1024 policy=drop_newest"`
- WARNING on overflow drop: `"Dispatcher queue full; dropping message on topic X"` — rate-limited to at most one entry per topic per second (per-topic timestamp cache under `_metrics_lock`)
- WARNING on post-stop enqueue rejection: `"Dispatcher stopped; rejecting message on topic X"` — same rate-limit shape
- WARNING on shutdown timeout: `"Dispatcher stop timeout: N stragglers, queue_depth=Q, active_workers=W, processed=P, dropped=D, rejected=R, errors=E"` — the final snapshot (§7.7)
- INFO on clean shutdown: `"Dispatcher stopped cleanly: processed=P, dropped=D, rejected=R, errors=E"`
- DEBUG per handler completion: `"Dispatched topic=X duration_ms=Y"` (opt-in via LOGGER level)

### 7.13 Migration from per-message Thread

- **New default** on RFC-004 landing: bounded dispatcher with the settings in §7.1–7.7.
- **Escape hatch**: `agent_config = {'dispatch': {'mode': 'per_message_thread'}}` preserves the pre-RFC-004 behaviour byte-for-byte — one `threading.Thread(...).start()` per message, no pool, no queue, no backpressure, non-daemon, no graceful shutdown.
- **Explicit non-goals of the legacy mode**: legacy mode is a **frozen compatibility shim, not a supported alternative dispatcher**. It intentionally does NOT:
  - gain backpressure — it remains unbounded;
  - gain a graceful-shutdown path — `Agent.terminate()` still returns immediately without joining handler threads;
  - gain the `daemon=True` flag — threads remain non-daemon to preserve byte-for-byte behaviour;
  - populate the RFC-004 metrics — `dropped_message_count`, `active_workers`, etc. all read as zero.
  Bug reports against legacy mode are not accepted; users experiencing issues must migrate to the bounded default.
- **Deprecation lifecycle** (single release cycle):
  - **Release N** (this RFC lands): escape hatch available. First construction of an `Agent` with `mode='per_message_thread'` emits a `DeprecationWarning` referencing this RFC (`RFC-004`) and identifying the config key. The warning is emitted **at most once per `Agent` instance** to avoid log floods.
  - **Release N+1**: escape hatch still functional. The `DeprecationWarning` is promoted with `stacklevel=2` so the caller site is visible in the traceback. Release notes document N+1 as the **final release** that ships the escape hatch.
  - **Release N+2**: escape hatch removed. `mode='per_message_thread'` raises `ValueError` at `Agent.__init__` time. The `dispatch` config accepts only bounded-dispatcher keys.
- **Removal conditions**: the escape hatch is removed in Release N+2 **unconditionally**, regardless of adoption metrics. Users who cannot migrate must pin to Release N+1 and file a follow-up RFC before the deprecation window closes. This forcing function is intentional: keeping a broken-by-design dispatcher indefinitely would perpetuate R-04.

### 7.14 Rollback
See §12.

### 7.15 Acceptance criteria
See §11.

### 7.16 Dispatcher lifecycle

**Start** — **eager, at Agent `_activate` time** (RFC-001 boundary), inside the worker context. Chosen over lazy-on-first-message construction because:

- Deterministic: the dispatcher exists before any `_on_connect` callback can fire; the first message never races the dispatcher's own construction.
- Symmetric with the broker: both created in `_activate`, both stopped in `_terminate`.
- Lazy construction would require a nested lock around `_on_message` and would race against the paho loop.

The dispatcher's `__init__` starts all `workers` consumer threads immediately. `_activate` retry loops (RFC-001) that never reach a live broker still get a matching `stop()` call on the shutdown path.

**Stop** — `dispatcher.stop(timeout_s)` is called from `Agent._terminate` / `__deactivating`. Safe to call from any thread.

**Idempotence** — `stop()` is **idempotent**. Repeated calls (via repeated `Agent.terminate()`) are safe: subsequent calls observe `_accepting == False`, skip re-posting sentinels, and return immediately with the cached outcome (True / False) from the first invocation. This matches the R-02 / R-13 pattern of cleanup routines being safely re-callable.

**Post-stop `enqueue()`** — rejected. Returns `False`. Increments `rejected_after_stop_count` under `_metrics_lock`. Emits a WARNING (rate-limited per topic). The rejected message is neither queued nor executed. This applies whether the caller is the paho loop (via `_on_message`) or a direct dispatcher API caller.

**Restart** — **not supported.** Once a dispatcher instance has been stopped, calling `stop()` again is a no-op; calling `enqueue()` returns `False`. Restarting message processing requires constructing a new Agent (which will create a fresh dispatcher). Rationale: restart semantics would require rebuilding consumer threads, resurrecting the queue, and re-negotiating post-stop state with any in-flight callers; the complexity is not justified for a lifecycle event that the wider Agent already treats as terminal.

---

## 8. Backward compatibility

### Source compatibility

| Symbol | Before | After | Compatibility |
|---|---|---|---|
| `Agent.publish` / `subscribe` / `unsubscribe` / `publish_sync` / `_publish_or_raise` | Same | Same | Full |
| `Agent._on_message` (@final) | Signature same | Signature same; body now enqueues | Full |
| `Agent.on_message` / `on_connected` / etc. handler hooks | Same | Same | Full |
| `Parcel` / `TextParcel` / `BinaryParcel` | — | — | Untouched |
| `MessageBroker` / `MqttBroker` | — | — | Untouched |
| Wire format | — | — | Untouched |
| `agent_config['dispatch']` | not defined | new key with defaults | Additive |
| `MessageDispatcher` (`src/agentflow/core/dispatcher.py`) | Not defined | New internal class | Additive |

### Behavioural compatibility

The observable difference from the caller's perspective:

- **Bounded parallelism**: at most `workers` handlers run concurrently. Previously unbounded. Callers that relied on N handlers running truly simultaneously (e.g. tests that spawn 100 blocking handlers) must either use the escape hatch or reduce their expected concurrency.
- **Bounded queue**: at most `queue_size` messages in flight. Overflow drops (default). Callers that never overflow (typical) see no change.
- **Graceful shutdown**: `Agent.terminate()` now waits up to `shutdown_timeout_s` for in-flight handlers. Previously returned immediately regardless. Callers depending on instant return should either pass `shutdown_timeout_s=0` or use the escape hatch.
- **Non-daemon → daemon consumers**: consumer threads no longer block interpreter exit. Handlers that expected to run to completion during exit lose that guarantee unless `Agent.terminate()` was called before exit.

No callers in `src/agentflow/`, `tests/`, `unit_test/`, or `exe_test/` rely on any of the above beyond what the escape hatch preserves.

### Wire compatibility

None. No wire protocol, Parcel field, or broker interaction changes.

---

## 9. Interaction with RFC-001 / RFC-002 / RFC-003

- **RFC-001 (R-02 publish_sync cleanup)**: unaffected. `publish_sync` runs on its caller's thread (test main or user thread), not on a dispatcher consumer. Cleanup semantics unchanged. `test_publish_sync_late_reply_after_cleanup_is_silently_dropped` continues to work because the late reply now travels through the dispatcher but still falls through to `on_message` (silent per RFC-003).
- **RFC-002 (R-13 fast-fail)**: unaffected. `publish_sync` calls `_publish_or_raise` on the caller's thread; broker exceptions still propagate. Dispatcher is not in this path.
- **RFC-003 (R-05 auto-reply contract)**: fully preserved. The `handle_message` body (with R-fallback-silent / R-strip-topic_return / R-exception-fresh) becomes the body of the dispatched task. Consumer thread runs it exactly as the per-message thread did. All 21 R-05 tests should continue to pass unchanged.

The 94 tests spanning R-02 / R-13 / R-05 (27 + 46 + 21) form a regression floor for this RFC.

---

## 10. Test migration plan

### R-04 characterization tests to flip (in `test_agent_message_threading.py`)

| Test | Current | Post-RFC-004 |
|---|---|---|
| `test_every_message_spawns_one_new_thread` | Asserts 5 Thread objects | Rewrite: assert dispatcher created exactly `workers` consumer threads; 5 deliveries reuse them |
| `test_N_messages_create_N_threads[N]` | Asserts N Thread objects | Rewrite: for N > workers, thread count remains `workers` |
| `test_slow_handler_active_thread_count_scales_with_n[N]` | Asserts N alive threads | Rewrite: alive count capped at `workers`; queue grows to min(N-workers, queue_size); overflow dropped and metric increments |
| `test_agent_terminate_does_not_join_handler_threads` | Asserts terminate returns in < 0.2s | Rewrite: with default `shutdown_timeout_s=5`, terminate blocks until drain OR timeout |
| `test_handler_thread_is_not_daemon` | Asserts non-daemon | Rewrite: assert consumer threads ARE daemon |
| `test_handler_exception_terminates_thread_cleanly` | Asserts per-message thread ends | Rewrite: assert consumer thread survives handler exception |
| `test_message_processing_order_across_threads_is_not_guaranteed` | Asserts out-of-order | Keep as-is (still holds under Option D) |
| `test_same_topic_multiple_messages_execute_concurrently` | Asserts N=8 concurrent | Keep as-is IF workers ≥ 8; else rewrite N=workers |
| `test_framework_provides_no_lock_around_handler_dispatch` | Asserts concurrent access | Keep as-is |
| `test_auto_reply_publish_runs_on_the_handler_thread` | Asserts same thread | Keep as-is (auto-reply still runs on consumer thread) |
| `test_each_broker_deliver_causes_exactly_one_handler_thread` | Asserts 1:1 | Rewrite: 7 deliveries reuse consumer pool; assert consumer count == workers |

### New tests to add (RFC-004 acceptance-check set)

| Test | Purpose |
|---|---|
| `test_dispatcher_bounds_active_workers_at_configured_max` | N handlers block; assert alive == workers |
| `test_dispatcher_queue_depth_bounded_by_configured_max` | Enqueue > queue_size; assert depth == queue_size; drops == N - queue_size |
| `test_dispatcher_drop_newest_policy_drops_latest_when_full` | Fill queue; enqueue 1 more; assert last is dropped, earlier preserved |
| `test_dispatcher_broker_callback_does_not_block_on_overflow` | Handler slow, queue full; time `_on_message`; assert bounded |
| `test_dispatcher_metrics_count_processed_and_dropped_correctly` | Deliver N; assert `processed_count + dropped_count == N` |
| `test_agent_terminate_drains_queue_within_shutdown_timeout` | Enqueue k tasks each 10 ms; terminate; assert all processed within timeout |
| `test_agent_terminate_abandons_stragglers_past_timeout` | Enqueue blocking handler; terminate(timeout=0.1); assert returns; assert WARNING logged |
| `test_dispatcher_consumer_survives_BaseException_in_handler` | Handler raises `KeyboardInterrupt`; assert consumer still alive |
| `test_legacy_per_message_thread_mode_restores_old_behaviour` | `agent_config['dispatch']={'mode': 'per_message_thread'}` → 100 deliveries → 100 Thread objects; DeprecationWarning emitted |
| `test_r02_r13_r05_all_pass_under_new_dispatcher` | Meta test that imports and re-runs a representative subset — or, equivalently, the full regression must be green |

### Legacy tests

`unit_test/*` and `exe_test/*` remain quarantined; not affected. FakeBroker helpers (`enable_self_echo`, `subscribed_topics`, `max_publish_dispatches`) remain useful for RFC-003 tests; no RFC-004-specific extension needed.

---

## 11. Acceptance criteria

Before this RFC's implementation PR can merge:

1. `PYTHONPATH=src pytest tests/unit` reports **all-pass** with **0 xfailed, 0 xpassed, 0 failed**.
   - Baseline before implementation: 151 passed.
   - Target after implementation: ~155 passed (11 R-04 tests rewritten, ~10 new tests, ~94 tests unchanged from R-02/R-05/R-13).
2. `MessageDispatcher` exists in `src/agentflow/core/dispatcher.py` with the surface described in §6.1.
3. `Agent._on_message` public signature unchanged; body enqueues via dispatcher.
4. Default configuration values match §7.1–7.7.
5. R-02 (27), R-13 (46), R-05 (21) tests all pass **unchanged** (no update needed for those files).
6. FakeBroker and FakeWorker unchanged (dispatcher is per-Agent, not per-broker).
7. Legacy escape hatch `dispatch={'mode': 'per_message_thread'}` restores byte-for-byte pre-RFC-004 dispatch behaviour, verified by re-running the R-04 characterization tests with that config.
8. No changes to:
   - `src/agentflow/core/parcel.py`
   - `src/agentflow/broker/*` (except possibly a docstring note about `_on_message` semantics)
   - Wire format
   - `Agent.publish` / `subscribe` / `unsubscribe` / `publish_sync` / `_publish_or_raise` signatures
9. This RFC file has status changed from `Draft` to `Accepted` in the same PR.
10. `docs/audit/05-risk-register.md` R-04 status changed to `Resolved` in the same PR.
11. **Handler `Exception` does not kill the consumer**: a test enqueues a handler that raises `ValueError`; asserts the consumer thread remains alive, `error_count` increments, and the very next enqueued task is executed by the same consumer.
12. **Handler `SystemExit` is not silently swallowed**: a test enqueues a handler that raises `SystemExit(0)`; asserts the consumer thread terminates (per §7.10), the `SystemExit` is observable in the thread's exit path (e.g. captured via `sys.excepthook` or by inspecting the alive count drop), the outer `finally` in §7.10 still calls `task_done()`, and remaining consumers keep processing. An equivalent test covers `KeyboardInterrupt`.
13. **Queue full does not break the broker callback thread**: a test fills the queue past capacity, then invokes `_on_message` via `broker.deliver(...)` (which simulates the paho loop thread call site); asserts `_on_message` returns without raising `queue.Full` (or any exception), `dropped_message_count` incremented, a WARNING was logged. Repeated for each opt-in `overflow_policy` value, including `'raise'` (which on the broker path MUST NOT raise per §7.3).
14. **`terminate()` is idempotent**: a test calls `agent.terminate()` twice in succession; asserts the second call returns within the same shutdown budget, does not double-count any metric, does not re-emit shutdown log lines, and returns the same outcome as the first call.
15. **Post-terminate enqueue is rejected**: a test calls `agent.terminate()`, then invokes `broker.deliver(...)`; asserts `rejected_after_stop_count` incremented, a WARNING was logged, the handler was NOT executed, and no consumer thread came back to life.
16. **Legacy mode emits `DeprecationWarning`**: a test constructs `Agent(agent_config={'dispatch': {'mode': 'per_message_thread'}})` inside `pytest.warns(DeprecationWarning)`; asserts the warning message references `RFC-004`, that per-message-thread behaviour is otherwise byte-for-byte preserved (11 R-04 characterization tests pass with this config), and that only one warning is emitted per Agent instance across many messages.

Out of scope (deferred to future RFCs):
- Per-topic ordering (Option E).
- asyncio dispatcher (Option F).
- ProcessWorker refactor.
- Broker-level backpressure signal.
- Metrics export to Prometheus / OpenTelemetry.

---

## 12. Rollback plan

Rollback trigger — any of:

- Any RFC-002 (R-13) or RFC-003 (R-05) regression.
- Deadlock or livelock in the dispatcher (would show as full queue with no consumer progress).
- `shutdown_timeout_s` too small causes routine straggler warnings in production.
- User workloads report unacceptable dropped-message rates under `drop_newest`.
- Compatibility issue with a deployment that relied on the per-message-Thread semantics not covered by the escape hatch.

Rollback procedure — single `git revert` of the merge commit. Because:

- `MessageDispatcher` is an additive new file.
- `Agent._on_message` body change is a localised replacement; revert restores the original `handle_message`-inside-thread pattern.
- `agent_config['dispatch']` is a new key; ignoring it is safe.
- No Parcel / wire / Broker changes to reconcile.
- Tests revert alongside; R-04 characterization tests re-mark the current behaviour.

Not rollback-safe: any change bundled into the same PR that modifies Parcel, Broker ABC, or Message Schema. This RFC forbids bundling.

Post-rollback state: R-04 returns to "Confirmed by runtime evidence, unresolved". Escape hatch reverts. The 11 R-04 tests re-assert per-message thread creation.

Interim mitigation (available without revert): switch a specific Agent to the legacy mode via `agent_config['dispatch']={'mode': 'per_message_thread'}`. This lets a canary deployment continue while the root cause is diagnosed.

---

## Appendix A — Why not just add `daemon=True`?

Setting `daemon=True` on the per-message thread would fix one symptom (non-daemon exit blocking) without addressing the primary R-04 concern (unbounded parallelism, no backpressure, no observability). Under a message burst, the process would still spawn thousands of daemon threads, exhausting OS resources; on exit, in-flight handlers would be silently truncated, potentially mid-publish.

Option A is included in §5 for completeness and rejected on those grounds. The escape hatch in §7.13 explicitly does NOT set `daemon=True` — it preserves the exact pre-RFC-004 behaviour (non-daemon) so that rollback is truly transparent.

## Appendix B — Why default worker count is 8 rather than `os.cpu_count()`

Handler workload is dominated by I/O (broker publish, subscriber callbacks). Scaling with CPU count is misleading: a 16-core server does not benefit from 16 consumers for I/O-bound work. A small fixed default (8) provides room for typical concurrent request-response patterns without the risk of accidentally over-parallelising on a large box. Users tune via `agent_config['dispatch']['workers']`.

## Appendix C — Interaction with future ProcessWorker

`MessageDispatcher` is a per-Agent object created inside `_activate`. In process mode, `_activate` runs inside the child process, so the dispatcher is entirely local to that process. No shared state between parent and child dispatchers is required. The design generalises cleanly to a hypothetical `ProcessDispatcher` (Option D with `multiprocessing.Queue` + child processes) as a future variant, but that variant is out of scope here.
