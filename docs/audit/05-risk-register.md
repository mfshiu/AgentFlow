# 05 — Risk Register

**Scope**: Consolidated risk list for Phase 1. Each entry includes file, function, line, trigger, impact, confidence, and a recommended verification test.
**Rule**: No refactor conclusions are proposed. Every risk that is not directly observable in code is marked with a Confidence level.

Severity legend: **Critical / High / Medium / Low**
Confidence legend: **High** = directly observable in code; **Medium** = plausible from code but needs runtime confirmation; **Low** = code smells / hypothetical.

---

## R-01 — `BinaryParcel.from_payload` executes `pickle.loads` on wire bytes

- **Severity**: Critical
- **Category**: Security / Message Reliability
- **File / Function / Line**: `src/agentflow/core/parcel.py:142–149`, `BinaryParcel.from_payload`
- **Evidence**:
  ```python
  raw = bytes(payload)[len(BinaryParcel.HEAD):]
  managed = pickle.loads(raw)
  ```
- **Trigger**: Any publisher on the MQTT broker sends a payload whose first bytes are `b"application/pickle|"` followed by a malicious pickle stream.
- **Impact**: Arbitrary code execution in the receiving agent process.
- **Confidence**: High
- **Recommended verification test**: Unit test that constructs a payload using a `__reduce__` gadget that writes a marker file, calls `Parcel.from_payload`, and asserts the marker file exists (in a sandbox).

---

## R-02 — `publish_sync` leaks handler entries and broker subscriptions

- **Status**: **RESOLVED** (2026-07-26) — see [RFC-001](../rfc/RFC-001-publish-sync-subscription-lifecycle.md)
- **Severity**: High
- **Category**: Resource / Message Reliability
- **File / Function / Line** (historical, at time of discovery): `src/agentflow/core/agent.py:321–349`, `Agent.publish_sync`
- **Evidence** (historical): `subscribe(pcl.topic_return, handle_response)` at line 343 wrote into `__topic_handlers`; no matching delete existed anywhere in the file. `MessageBroker` did not define `unsubscribe`; `MqttBroker` did not implement one.
- **Trigger** (historical): Any long-running agent that called `publish_sync` repeatedly.
- **Impact** (historical): Unbounded growth of `__topic_handlers`; unbounded growth of the broker's subscription table for that client.
- **Confidence at discovery**: High
- **Recommended verification test** (was): Loop `publish_sync` N times against a mock broker; assert `len(agent._Agent__topic_handlers)` does not grow.

### Resolution

- **Resolved on**: 2026-07-26
- **RFC**: [RFC-001 — publish_sync subscription lifecycle](../rfc/RFC-001-publish-sync-subscription-lifecycle.md) (Accepted)
- **Scope of change**:
  - `src/agentflow/broker/message_broker.py` — added `MessageBroker.unsubscribe(topic) -> None` as a **non-abstract** method with a default no-op body. Backward compatible with existing third-party subclasses.
  - `src/agentflow/broker/mqtt_broker.py` — added `MqttBroker.unsubscribe(topic)` that delegates to `self._client.unsubscribe(topic)`.
  - `src/agentflow/core/agent.py`:
    - Added `@final Agent.unsubscribe(topic)` — public method symmetric to `Agent.subscribe`. Pops the entry from `__topic_handlers` and calls `broker.unsubscribe` when a broker is attached. Idempotent.
    - `publish_sync` now wraps `publish` + `event.wait` in a `try/finally`. The `finally` block uses an **identity guard** so it only tears down the subscription if `__topic_handlers[topic_return]` is still the specific `handle_response` closure that this call installed. Cleanup exceptions are swallowed via `logger.exception` so they cannot mask the original return value or `TimeoutError`.
    - `handle_response` closure gained a `data_event.event.is_set()` early-return guard so a duplicate arriving before the finally cleanup cannot mutate `data_event.data`.
- **Runtime verification** (as of 2026-07-26):
  - Command: `PYTHONPATH=src /home/eric/anaconda3/envs/actbot/bin/python -m pytest tests/unit -v`
  - Result: **68 passed, 0 failed, 0 xfailed, 0 xpassed** in 1.90 s.
  - Behaviours directly asserted:
    - **Success cleanup** — `test_topic_handlers_cleaned_after_successful_publish_sync`, `test_broker_subscribe_and_unsubscribe_grow_together_on_success`, `test_broker_unsubscribes_specific_topic_after_success`.
    - **Timeout cleanup** — `test_topic_handlers_cleaned_after_timed_out_publish_sync`, `test_broker_subscribe_and_unsubscribe_grow_together_on_timeout`, `test_broker_unsubscribes_specific_topic_after_timeout`.
    - **Publish-exception cleanup** — `test_publish_sync_subscribes_and_then_unsubscribes_when_publish_raises`.
    - **Late response fallback** — `test_late_response_after_timeout_falls_through_to_on_message`.
    - **Duplicate response fallback** — `test_first_response_returned_and_duplicate_falls_through_to_on_message`.
    - **Identity guard** — `test_identity_guard_preserves_foreign_handler_on_same_topic`.
    - **Concurrent cleanup** — `test_concurrent_publish_sync_with_distinct_topic_wait_all_clean_up`.
  - Test files touching this fix: `tests/unit/core/test_agent_publish_sync.py` (27 tests), `tests/unit/test_mqtt_broker_lifecycle.py` (`unsubscribe` delegation × 2), `tests/unit/test_empty_broker.py` (default no-op path × 3).
- **Known issues NOT resolved by this fix** (tracked separately, out of RFC-001 scope):
  - **R-13** (this register) — `Agent.publish` still swallows broker exceptions; `publish_sync` surfaces them as `TimeoutError` rather than the original exception.
  - **R-04** (this register) — `Agent._on_message` still spawns one short-lived thread per received message.
  - **Concurrent same-`topic_wait` race** — two `publish_sync` calls using the same explicit `topic_wait` still collide (`Agent.subscribe` silently overwrites). The new identity guard prevents this fix from making the collision *worse*, but does not resolve the underlying race. **Resolved 2026-07-27 (RFC-006 + RFC-007)**: RFC-006 turned publish_sync-vs-publish_sync collision into a fail-fast `TopicWaitCollisionError`; RFC-007 extended the fail-fast policy to direct `Agent.subscribe` / `Agent.unsubscribe` racing against a `publish_sync` waiter. See [R-14](#r-14--shared-dictionaries-without-locks) for details.

---

## R-03 — MQTT reconnect and subscription recovery are not implemented

- **Status**: **RESOLVED** (2026-07-27) — see [RFC-005](../rfc/RFC-005-mqtt-subscription-recovery.md)
- **Severity**: High
- **Category**: Fault Isolation / Message Reliability
- **File / Function / Line** (historical): `src/agentflow/broker/mqtt_broker.py:17-18` (`reconnect_on_failure=False`); `mqtt_broker.py:52-53` (`_on_disconnect` only logs); `src/agentflow/core/agent.py:509-512` (`_on_connect` early-return via `_connected_once`)
- **Evidence** (historical):
  - MqttBroker was a pass-through with **no subscription registry**.
  - `_on_disconnect` only issued `logger.warning`; `_connect_ok` / `_connected_evt` never cleared.
  - Even if a second `_on_connect` fired, `Agent._connected_once` would return early — no path could re-issue prior `client.subscribe` calls.
  - `stop()` set no observable flag; a late `_on_connect` callback still delegated to the notifier.
- **Trigger** (historical): MQTT broker restart or transient network loss; also stop / late-callback race.
- **Impact** (historical): Agent silently deaf after any disconnect. Prior `subscribe(topic, handler)` bindings held in `Agent.__topic_handlers` still existed but no messages arrived because the broker-side subscription was gone. Stop / reconnect race could resurface the notifier after the caller had already asked to terminate.
- **Confidence at discovery**: High.
- **Recommended verification test** (was): Use toxiproxy to sever the MQTT TCP connection for 3 seconds, restore it, and verify whether messages published to previously-subscribed topics still arrive at the agent.

### Runtime confirmation (before fix)

R-03 was upgraded from static-code to runtime-confirmed via 32 characterization tests in `tests/unit/test_mqtt_broker_reconnect.py` (RFC-005 §2). Six aspirational tests were marked `xfail(strict=True)` and formed the acceptance-check set.

### Resolution

- **Resolved on**: 2026-07-27
- **RFC**: [RFC-005 — MQTT subscription recovery](../rfc/RFC-005-mqtt-subscription-recovery.md) (Implemented)

**Final implementation** (Option C from RFC-005 §5 — MqttBroker owns the desired-state registry):

- `MqttBroker` now maintains a thread-safe `_registry: Dict[str, Any]` (topic → data_type) protected by `_state_lock`.
- Lifecycle state: `_connected`, `_ever_connected`, `_stopping`, `_last_disconnect_was_planned` — all under the same lock.
- Recovery metrics: `_resubscribe_success_count`, `_resubscribe_error_count`, `_recovery_run_count`. Exposed via `recovery_metrics()` snapshot.
- **`subscribe(topic, data_type)`**: writes registry under lock; forwards to `client.subscribe` only when `_connected=True`. Returns `None` if disconnected or stopping.
- **`unsubscribe(topic)`**: symmetric — deletes registry entry; forwards only when connected.
- **`_on_connect(rc=0)`**: under lock, checks `_stopping` (returns early if True), classifies first vs subsequent via `_ever_connected`, marks `_connected=True`, and snapshots the registry for reconnect. Outside the lock: runs `_recover_subscriptions` on the snapshot (skipped on first connect), then notifies the notifier. `_connected_evt.set()` in a `finally`.
- **`_recover_subscriptions`**: iterates the snapshot **outside** the lock; before each `client.subscribe(topic)`, re-acquires the lock briefly to re-check `_stopping` (breaks if True) and re-check `topic in _registry` (skips if removed).
- **`_on_disconnect`**: under lock, classifies planned (rc==0 or `_stopping=True`) vs unexpected; clears `_connected`; sets `_last_disconnect_was_planned`. Outside the lock: clears `_connect_ok` and `_connected_evt`. Registry is **preserved**.
- **`stop()`**: flips `_stopping=True` **before** `client.disconnect()` so any inline callback observes the flag.
- **Observability surface** (additive): `last_disconnect_was_planned` property; `recovery_metrics()` method.

### Lifecycle summary

- **Connected**: `subscribe`/`unsubscribe` immediately forward to client + update registry.
- **Disconnected**: `subscribe`/`unsubscribe` only update the registry; no client call.
- **Reconnect**: `_on_connect` snapshots the registry, runs `_recover_subscriptions` (per-topic try/except; recheck `_stopping` and `topic in _registry` between iterations); then notifies notifier.
- **Stopped**: `subscribe`/`unsubscribe` return `None` without touching either the registry or the client; `_on_connect` skips both recovery and notifier delegation.

### Race handling

- **stop vs on_connect**: `stop()` acquires `_state_lock`, sets `_stopping=True`, releases, then calls `client.disconnect()`. Any callback that arrives inline or after observes the flag and short-circuits.
- **stop during recovery**: recovery re-checks `_stopping` between topics; a stop() during recovery causes the loop to break with a WARNING that records `queue_depth` / `active_workers` (via broker's own log wording — not the dispatcher's).
- **unsubscribe during recovery**: recovery re-checks `topic in _registry` between iterations; a topic unsubscribed after snapshot capture is skipped.
- **subscribe during recovery**: new subscribe writes to registry under lock and is preserved for the next reconnect (or immediately forwards if `_connected=True`).
- **Lock hygiene**: `_state_lock` is **never** held across `client.subscribe` / `client.unsubscribe` / `client.publish` / `client.disconnect` / `client.loop_stop`. Verified by three dedicated tests using a spy that would deadlock if the lock were held.

### Failure isolation

- Per-topic recovery wraps each `client.subscribe(topic)` in `try/except Exception`. Failures increment `_resubscribe_error_count` and log at ERROR; the loop **continues** with the next topic.
- `BaseException` subclasses (`KeyboardInterrupt`, `SystemExit`, `GeneratorExit`) propagate — consistent with RFC-004 §7.10.
- Direct `broker.subscribe` / `broker.unsubscribe` on the connected path: exception from `client.subscribe` propagates to the caller; registry has already been updated before the client call raised (verified).

### Runtime verification (as of 2026-07-27)

- Command: `PYTHONPATH=src /home/eric/anaconda3/envs/actbot/bin/python -m pytest tests/unit`
- Result: **216 passed, 0 failed, 0 xfailed, 0 xpassed** in 3.34 s.
- R-03 dedicated file `tests/unit/test_mqtt_broker_reconnect.py` — **48 passed** (16 categories per RFC-005 §7).
- The 6 aspirational strict xfails from the R-03 characterization all **converted to passing tests**:
  - `test_recovery_resubscribes_all_registered_topics_on_reconnect`
  - `test_recovery_does_not_resurrect_unsubscribed_topic`
  - `test_on_connect_after_stop_does_not_notify_notifier`
  - `test_on_disconnect_distinguishes_planned_from_unexpected`
  - `test_on_disconnect_clears_connect_ok_flag`
  - `test_broker_maintains_subscription_registry` (was `test_broker_should_expose_active_subscriptions_after_subscribe`)
- R-02 (27), R-04 (33), R-05 (21), R-13 (46) tests all pass **unchanged** — no regression from the broker refactor.

### Observable behavioural changes

Three narrow, all in the recovery / safety direction:

1. `broker.subscribe(topic, data_type)` and `broker.unsubscribe(topic)` return `None` when the broker is currently disconnected or has been stopped — previously they always attempted a client call (with undefined paho behaviour when disconnected).
2. After `stop()`, `broker.subscribe` / `broker.unsubscribe` are silent no-ops (return `None`, no registry write, no client call) — previously they forwarded to paho regardless.
3. `_on_disconnect` now clears `_connect_ok` and `_connected_evt` and records `last_disconnect_was_planned` — previously those flags were sticky-true after first successful connect.

Callers that read `broker._connect_ok` as "was ever successfully connected" would notice the change; nothing in the codebase relied on that reading.

### Known issues NOT resolved by this fix

- **R-01** — `BinaryParcel.pickle.loads` on wire bytes.
- **Custom retry / backoff policy**: `reconnect_on_failure=False` is unchanged. Recovery only runs when paho drives an `_on_connect` callback; AgentFlow does not itself reconnect.
- **Offline publish queue**: `publish()` while disconnected still calls `client.publish` (paho decides).
- **Stable `client_id`** management: paho auto-generates.
- **Persistent MQTT session** (`clean_start=False`): not enabled — RFC-005 §Appendix B lists this as a future fast-path optimisation.
- **Broker-cluster failover**: not implemented.
- **Connection generation / epoch counter** (RFC-005 Option E): not implemented; `_ever_connected` boolean suffices for first-vs-reconnect classification.
- **External metrics export** (Prometheus / OpenTelemetry): only in-process `recovery_metrics()` snapshot.

---

## R-04 — Per-message unbounded thread creation

- **Status**: **RESOLVED** (2026-07-26) — see [RFC-004](../rfc/RFC-004-bounded-message-dispatch.md)
- **Severity**: High
- **Category**: Concurrency / Resource
- **File / Function / Line** (historical): `src/agentflow/core/agent.py` inside `Agent._on_message`
- **Evidence** (historical): `threading.Thread(target=handle_message, args=(topic_handler, topic, pcl)).start()` — one fresh non-daemon `threading.Thread` per received message. No pool, no queue, no upper bound on concurrency, no framework-side ownership of the spawned thread.
- **Trigger** (historical): Any inbound message stream — a burst of N messages spawned N native threads simultaneously.
- **Impact** (historical): (a) thread count grew unbounded with inbound rate; (b) no bounded queue meant no backpressure; (c) non-daemon threads blocked interpreter exit; (d) `Agent.terminate()` returned immediately without waiting for or observing handler threads; (e) same-topic handlers ran concurrently with unspecified completion order.
- **Confidence at discovery**: High
- **Recommended verification test** (was): publish 10k messages at high rate to a single subscribed topic; monitor `threading.active_count()` and process behaviour on shutdown.

### Runtime confirmation (before fix)

R-04 was upgraded from static-code to runtime-confirmed via 16 characterization tests in `tests/unit/core/test_agent_message_threading.py` (RFC-004 §2). Specifically:

- 100 / 500 / 1000 deliveries each produced N `threading.Thread` objects (`test_N_messages_create_N_threads`).
- Slow handlers caused `active_count` to scale 1:1 with delivered N (`test_slow_handler_active_thread_count_scales_with_n`).
- `Agent.terminate()` returned in < 0.2 s regardless of live handler threads.
- Handler threads were non-daemon.
- Same-topic messages executed concurrently; completion order matched release order rather than delivery order.

### Resolution

- **Resolved on**: 2026-07-26
- **RFC**: [RFC-004 — bounded message dispatch](../rfc/RFC-004-bounded-message-dispatch.md) (Implemented)

**Final implementation** (RFC-004 first-phase scope):

- New file `src/agentflow/core/dispatcher.py` — `MessageDispatcher` (bounded) + `LegacyPerMessageDispatcher` (deprecated shim).
- `MessageDispatcher`:
  - Fixed pool of **daemon** consumer threads (default `workers=8`, configurable).
  - Bounded `queue.Queue(maxsize=queue_capacity)` (default `1024`, configurable).
  - Overflow policy `drop_newest` (only policy implemented in this phase): `put_nowait` on full → `dropped_message_count` +1 → rate-limited WARNING → `enqueue()` returns `False`. Never raises to the caller.
  - **Broker-callback safety invariant** (RFC-004 §7.3): `_on_message` invoked from the paho loop thread never sees `queue.Full`.
  - **Lazy initialization**: dispatcher created on the first `Agent._on_message` call under `_dispatcher_init_lock` (double-check lock). See "RFC vs implementation" below.
  - **Graceful bounded shutdown**: `stop(timeout_s)` sets `_accepting=False`, posts one sentinel per worker, joins each with the remaining budget from a single monotonic deadline. Consumers pull sentinel from tail of queue after processing all previously-enqueued tasks.
  - **Thread-safe metrics** via `_metrics_lock`: `active_workers`, `queue_depth`, `dropped_message_count`, `rejected_after_stop_count`, `processed_count`, `error_count`. `metrics_snapshot()` returns a coherent single-lock dict.
  - **Handler-exception isolation**: consumer's `except Exception` catches ordinary errors and increments `error_count`; `BaseException` (`KeyboardInterrupt`, `SystemExit`, `GeneratorExit`) propagates out and terminates that consumer (RFC-004 §7.10). `queue.task_done()` runs in a `finally` block for every dequeued item, including sentinels and BaseException paths.
- `Agent`:
  - `_on_message` now enqueues via `self._get_dispatcher().enqueue(handle_message, topic=topic)` instead of spawning a per-message Thread.
  - `terminate()` calls `self._dispatcher.stop()` before `self._agent_worker.stop()`.
  - Public signatures `publish` / `publish_sync` / `subscribe` / `unsubscribe` / `_publish_or_raise` unchanged.
  - Legacy escape hatch: `agent_config['dispatch']['mode']='per_message_thread'` restores byte-for-byte pre-RFC-004 behaviour and emits a `DeprecationWarning` at `Agent.__init__`.

### Race fixes (linearization)

Two additional races were identified and fixed after the first-phase implementation:

- **enqueue check-then-put race**: `_accepting` check and `queue.put_nowait` were originally outside `_state_lock`. A concurrent `stop()` could interleave: set `_accepting=False`, post the sentinel, and then the enqueue's `put_nowait` would land the task AFTER the sentinel — but `enqueue()` returned `True`. Consumers took the sentinel first, exited, and the task was never executed. **Fixed** by performing the check + `put_nowait` atomically under `_state_lock`. Deterministic reproduction: `test_race_stop_wins_between_enqueue_check_and_put_deterministic` (fails pre-fix, passes post-fix).
- **Concurrent-stop race**: two threads calling `stop()` simultaneously — the second thread saw `_stopped=True` and returned `bool(self._stop_result)` before the first thread had set `_stop_result`, yielding `False` while the first thread returned `True`. **Fixed** by adding `_stop_complete_event`: only the first caller executes the actual shutdown; subsequent callers wait on the event and return the coherent cached `_stop_result`. Deterministic reproduction: `test_concurrent_stop_calls_execute_actual_shutdown_only_once` (fails pre-fix with `[False, False, False, False, True]`, passes post-fix).

Post-fix linearization invariants (all runtime-verified):

- Every `enqueue()` returning `True` is executed by a consumer before a successful `stop()` returns.
- Every `enqueue()` returning `False` does NOT execute the task.
- Sentinel items also go through `queue.task_done()` — `_queue.unfinished_tasks == 0` after clean shutdown.
- Concurrent `stop()` callers post exactly `workers` sentinels total and all observe the same True/False outcome.

### Runtime verification (as of 2026-07-26)

- Command: `PYTHONPATH=src /home/eric/anaconda3/envs/actbot/bin/python -m pytest tests/unit`
- Result: **168 passed, 0 failed, 0 xfailed, 0 xpassed** in 3.27 s.
- R-04 dedicated file `tests/unit/core/test_agent_message_threading.py` — **33 passed**.
- 5 independent re-runs of the race stress suite — 4/4 passing every time; no flakes observed.
- R-02 (27), R-05 (21), R-13 (46) tests all pass **unchanged** — no regression from the dispatcher integration or the race fixes.

### RFC vs implementation difference

RFC-004 §7.16 recommended **eager dispatcher initialization at `_activate` time** (RFC-001 boundary, inside the worker context) for determinism. The implementation diverges to **lazy first-message initialization** for one reason:

- Existing R-02 / R-05 / R-13 tests bypass `_activate` and construct Agents directly with `_broker` / `_agent_worker` set manually. Eager `_activate`-time initialization would either require dispatcher construction in `__init__` (spawning consumer threads even for tests that never receive a message — noisy) or require rewriting ~90 existing tests to call `_activate`.

The two determinism guarantees the RFC cited (dispatcher exists before first message; no race between dispatcher construction and message dispatch) are preserved via **double-check locking** in `Agent._get_dispatcher()`:

```python
if self._dispatcher is None:
    with self._dispatcher_init_lock:
        if self._dispatcher is None:
            self._dispatcher = self.__create_dispatcher()
return self._dispatcher
```

The first `_on_message` call constructs the dispatcher under the lock; concurrent callers wait on the lock and observe the completed object. Subsequent calls hit the lock-free fast path. Symmetric with the broker in that both are created on first use; asymmetric with the broker in that the broker is still created inside `_activate` (RFC-001) — a follow-up RFC may unify these lifecycles as part of ProcessWorker rework.

### Known issues NOT resolved by this fix (tracked separately)

- **R-01** — `BinaryParcel.pickle.loads` on wire bytes.
- **R-03** — MQTT reconnect + re-subscribe.
- **`overflow_policy` variants**: `drop_oldest`, `block`, `raise` are **not implemented** in this phase (RFC-004 §7.3 opt-ins). Only `drop_newest` is supported.
- **Per-topic serial queue** (RFC-004 Option E): same-topic ordering is not enforced. Deferred to a follow-up RFC iff ordering becomes required.
- **ProcessWorker dispatcher semantics**: dispatcher is per-Agent-instance; in a hypothetical ProcessWorker future, dispatcher would live in the child process. Out of RFC-004 scope.
- **External metrics export** (Prometheus / OpenTelemetry): only in-process attributes + log lines are exposed.
- **Legacy `per_message_thread` mode removal**: escape hatch is deprecated but still functional; RFC-004 §7.13 targets removal in Release N+2 with a hard `ValueError`.

---

## R-05 — Suspected reply loop on error paths and on default handlers

- **Status**: **RESOLVED** (2026-07-26) — see [RFC-003](../rfc/RFC-003-auto-reply-contract.md)
- **Severity**: High
- **Category**: Correctness / Message Reliability
- **File / Function / Line** (historical): `src/agentflow/core/agent.py:543–555` in `_on_message.handle_message`; `parcel.py:95–107` (`topic_return` survives round-trip via `_get_managed_data` / `_set_managed_data`)
- **Evidence** (historical):
  - The dispatch always called `self.publish(pcl.topic_return, data_resp)` when `p.topic_return` was truthy — including on the fall-through-to-on_message path.
  - On exception, `data_resp = p` — and `p.topic_return` was still set → the reply parcel itself carried `topic_return`, seeding a cycle whenever the broker delivered the reply back to the same agent (paho self-echo) or to a peer whose handler also produced a Parcel-with-topic_return.
- **Trigger** (historical): Handler exception + broker self-echo; handler returns Parcel-with-topic_return + broker self-echo; two-agent mutual reply where both handlers return Parcel-with-topic_return.
- **Impact** (historical): Infinite reply loop, broker flood.
- **Confidence at discovery**: Medium.
- **Recommended verification test** (was): Run a scripted `publish_sync` where the responder raises; observe broker traffic on the return topic.

### Runtime confirmation (before fix)

R-05 was upgraded from "suspected" to "confirmed by runtime evidence" via `tests/unit/core/test_agent_reply_behavior.py`. Three concrete loop patterns were reproduced against a bounded FakeBroker with self-echo:

1. **Handler exception loop** — `test_scenario_9_handler_exception_creates_reply_loop_bounded_by_broker` hit the 20-publish bound.
2. **Handler-returns-loopy-Parcel loop** — `test_scenario_4_5_handler_returns_topic_return_parcel_creates_loop` hit the 20-publish bound.
3. **Two-agent mutual reply loop** — `test_scenario_6_two_agent_reply_loop_via_hub_broker` hit the 30-publish bound via a shared hub broker.

### Resolution

- **Resolved on**: 2026-07-26
- **RFC**: [RFC-003 — auto-reply contract](../rfc/RFC-003-auto-reply-contract.md) (Implemented)
- **Scope of change**: `src/agentflow/core/agent.py` — `Agent._on_message` (`@final`, public signature unchanged). Three rules now hold together:
  - **R-fallback-silent**: `is_specific_handler = topic in self.__topic_handlers` is computed at dispatch. Auto-reply is emitted **iff** `pcl.topic_return` is truthy AND `is_specific_handler` is `True`. Fall-through to `on_message` still runs the handler but never publishes an implicit reply.
  - **R-strip-topic_return**: before publishing an auto-reply, if `data_resp` is a `Parcel` whose `topic_return` is truthy, the framework reconstructs a fresh parcel via `type(data_resp)(data_resp.content)`, copies `.error`, and uses it as the reply. The handler's returned object is not mutated.
  - **R-exception-fresh**: on handler exception, the reply is a new `Parcel.from_content(None)` with `.error = str(ex)`. The incoming parcel `p` is not mutated; handlers that store a reference to `p` observe its original state.
- **Public API impact**: none. `Agent.publish` / `subscribe` / `unsubscribe` / `publish_sync` / `_publish_or_raise` all unchanged. `Parcel` / `TextParcel` / `BinaryParcel` / `HEAD` / envelope fields unchanged. Wire format unchanged. `MessageBroker` / `MqttBroker` API unchanged.
- **Observable behavioural changes** (three; all narrow, all in the loop-prevention direction):
  1. A topic that is subscribed but has no specific handler no longer produces an implicit auto-reply when a message with `topic_return` arrives; the message dispatches to `on_message` default (no-op) and no publish is emitted.
  2. Auto-reply parcels never carry `topic_return` on the wire. A handler that returns a `Parcel` with `topic_return` set observes the reply carrying `topic_return=None` after reconstruction, while the handler's own returned object is left intact.
  3. Handler exceptions no longer mutate the incoming parcel; handlers that stored a reference to their input parcel see its original `content`, `error`, and `topic_return`.
- **Runtime verification** (as of 2026-07-26):
  - Command: `PYTHONPATH=src /home/eric/anaconda3/envs/actbot/bin/python -m pytest tests/unit`
  - Result: **135 passed, 0 failed, 0 xfailed, 0 xpassed** in 2.73 s.
  - The three previously loop-forcing scenarios (`test_scenario_9_...`, `test_scenario_4_5_...`, `test_scenario_6_...`) were rewritten to assert termination and each observes ≤ 3 publishes instead of hitting the 20/30 bound.
  - Normal request-response paths verified: `test_scenario_1` (no topic_return), `test_scenario_2` (None), `test_scenario_3` (scalar), `test_scenario_4` (Parcel), `test_publish_sync_happy_path_still_works_under_RFC_003`.
  - Rule verification: `test_default_on_message_does_not_auto_reply_when_no_specific_handler` (R-fallback-silent), 5 tests under R-strip-topic_return, 3 tests under R-exception-fresh.
- **No regression**: RFC-001's 27 R-02 cleanup tests and RFC-002's 46 R-13 fast-fail tests both continue to pass unchanged. `test_publish_sync_late_reply_after_cleanup_is_silently_dropped` explicitly validates the RFC-003 × RFC-001 interaction.
- **Known issues NOT resolved by this fix** (tracked separately):
  - **R-01** — `BinaryParcel.pickle.loads` on wire bytes.
  - **R-03** — MQTT reconnect + re-subscribe.
  - **R-04** — `Agent._on_message` still spawns one short-lived thread per received message.
  - **Concurrent same-`topic_wait` race** — `Agent.subscribe` silent overwrite on duplicate registrations.
  - **Broker-side `MessageInfo`** — `MqttBroker.publish` still discards paho `MessageInfo` (rc/mid); deferred to a future RFC on broker observability.

---

## R-06 — Process-mode pickling of Agent + ProcessWorker lifecycle

- **Status**: **PARTIALLY RESOLVED** (2026-07-28) — R-06.1 / R-06.2 / R-06.4 addressed by [RFC-008](../rfc/RFC-008-process-worker-lifecycle.md); R-06.3 and child-exception forwarding remain Deferred / Open.
- **Severity**: High
- **Category**: Process / Correctness
- **File / Function / Line** (historical): `src/agentflow/core/agent_worker.py:56–65`, `ProcessWorker.start`
- **Evidence** (historical): `multiprocessing.Process(target=self.initiator_agent._activate, args=(cfg,))` required pickling the bound method → the Agent → its `_handlers_lock: threading.RLock` (introduced by RFC-006 / RFC-007). `RLock` is not picklable. Every `start_process()` call raised `TypeError: cannot pickle '_thread.RLock' object` at pickle time in the parent; no child was ever launched. Additionally `ProcessWorker.stop()` called `Process.join()` with no timeout — a wedged child hung the caller forever; `Process.daemon = False` (unchanged); no liveness / exitcode / restart API; parent-side Agent state (`_broker`, `_dispatcher`, `__topic_handlers`) never populated because `_activate` runs in the child.
- **Trigger** (historical): Default configuration (`CONCURRENCY_TYPE='process'`, `agent.py:75–76`) — every `start_process()` call, regardless of user handlers.
- **Impact** (historical): `spawn` failed at pickle time; process mode was effectively dead. Any user who selected process mode (the framework default) saw the framework fall over. Once a child would have started, `stop()` had no bounded shutdown path.
- **Confidence at discovery**: Medium (Agent-worker cycle + lock pickling both suspect). Upgraded to **High** on 2026-07-27 via 27 characterisation tests in `tests/unit/core/test_process_worker_lifecycle.py` (RFC-008 §2), including runtime confirmation that `Agent` was unpicklable due to `_handlers_lock`, that `ProcessWorker.start` raised at pickle time, and that `stop()` hung on a wedged child.

### Sub-risk breakdown

| ID | Sub-risk | Status |
|---|---|---|
| **R-06.1** | Spawn pickle failure (Agent's `_handlers_lock: RLock` is not picklable → `ProcessWorker.start` raises at pickle time; no child ever launches) | **RESOLVED** (RFC-008) |
| **R-06.2** | Unbounded `Process.join()` in `stop()` (wedged child blocks the caller forever; no `terminate` / `kill` fallback; no `Optional[int]` exitcode return) | **RESOLVED** (RFC-008) |
| **R-06.3** | No heartbeat / liveness / automatic restart on child crash (parent has no watchdog; a dead child is only observable via post-hoc `stop()` + `exitcode`) | **DEFERRED / OPEN** (out of RFC-008 scope) |
| **R-06.4** | Parent-side Agent state divergence (parent-side `_broker` / `_dispatcher` / `__topic_handlers` never populate because `_activate` runs in the child; parent-side `publish` / `subscribe` do not proxy) | **DOCUMENTED architectural constraint** (RFC-008 §6.5, §7.16, §7.17) |
| Child exception forwarding | No IPC surface for child-side handler exceptions; parent observes only exit code | **DEFERRED / OPEN** (out of RFC-008 scope) |

### Resolution (R-06.1 / R-06.2 / R-06.4)

- **Resolved on**: 2026-07-28
- **RFC**: [RFC-008 — ProcessWorker lifecycle](../rfc/RFC-008-process-worker-lifecycle.md) (Implemented)

**Final implementation** (RFC-008 first-phase scope):

**Agent pickle protocol** (`src/agentflow/core/agent.py`):

- `Agent.__getstate__` / `__setstate__` implement the pickle protocol. A `_RUNTIME_ONLY_FIELDS` whitelist excludes non-picklable / not-meaningful-in-child fields: `_handlers_lock`, `_dispatcher_init_lock`, `_dispatcher`, `_broker`, `_agent_worker`, `_message_broker`, `_children`, `_parents`.
- `__getstate__` **probes every remaining field for picklability**: `config` as a whole; every `_HandlerRecord.handler` in `__topic_handlers` individually. On failure, raises `TypeError` naming the offending topic (or `Agent.config`) and suggests moving the handler / callback into `on_activate()`. **Fail-fast** — no silent omission.
- `__setstate__` reinstates every runtime-only field fresh: new `threading.RLock` × 2, `None` for `_broker` / `_dispatcher` / `_agent_worker` / `_message_broker`, empty `{}` for `_children` / `_parents`. The RFC-006 / RFC-007 `_HandlerRecord` ownership shape survives pickle; only the guarding lock is rebuilt.

**ProcessWorker lifecycle** (`src/agentflow/core/agent_worker.py`):

- New `WorkerState(Enum)`: `NEW → STARTING → RUNNING → STOPPING → STOPPED` (or `START_FAILED` on start error). All transitions under `_state_lock: RLock`. Read-only `state` / `exitcode` properties.
- `start()`: single-shot state machine. Repeated `start()` on `STARTING`/`RUNNING`/`STOPPING`/`STOPPED`/`START_FAILED` raises `RuntimeError` — restart is not supported (construct a fresh worker). **`agent.config` is not mutated**: `start()` builds `child_config = dict(self.initiator_agent.config)` locally and puts the `work_queue` reference on the copy. `Process(daemon=False)`.
- `_cleanup_after_start_failure()`: bounded rollback — `terminate + join(1s)`, `kill + join(1s)` if still alive; queue `close + join_thread`; all in best-effort swallowers. Sets `work_process = None`, `work_queue = None`, state `START_FAILED`.
- `stop(graceful_timeout_s=5.0, terminate_timeout_s=2.0, kill_timeout_s=1.0) -> Optional[int]`: **bounded escalation ladder** — cooperative `terminate` via queue → `join(graceful)` → `Process.terminate()` → `join(terminate)` → `Process.kill()` → `join(kill)`. Total wall time ≤ `graceful + terminate + kill = 8.0s` at defaults. State-dispatch:
  - `NEW` → **no-op, state stays `NEW`, subsequent `start()` still allowed** (implementation refinement of RFC-008 §7.12).
  - `START_FAILED` → no-op (resources already cleaned).
  - `STOPPED` → cached-exitcode replay (idempotent, §7.11).
  - `STOPPING` → concurrent caller waits on `_stop_complete_event` and returns the same `_exitcode` (RFC-004 pattern).
  - `STARTING` → `RuntimeError`.
  - `RUNNING` → transitions to `STOPPING`, runs escalation, caches `_exitcode = proc.exitcode`, queue cleanup, `finally` sets `STOPPED` and signals `_stop_complete_event` so waiters unblock even if the body raised.
- `exitcode` property: `None` before `stop()`; cached `Optional[int]` after.

### Parent-child contract (R-06.4, documented architectural constraint)

- Under `CONCURRENCY_TYPE='process'`, the **parent-side `Agent` instance is a lifecycle controller stub**. Its role is to orchestrate `start()` / `terminate()` / observation of `is_active()` / `worker.exitcode`.
- Effective runtime state — `_broker`, `_dispatcher`, `__topic_handlers` dispatch — lives in the **child** process. The parent-side `Agent`'s corresponding fields stay at their `__init__` values (`None` / `{}`).
- Parent-side calls to `Agent.publish` / `subscribe` / `publish_sync` are **not proxied** to the child. They operate on the empty parent-side state and produce no broker traffic (`_broker is None`; publish is a silent no-op that logs an error per RFC-002).
- Callers that need to interact with the running child Agent must connect to the same broker (from the same or a different process) using a **separate Agent instance** — or wait for a future RFC that introduces a transparent parent-side proxy / IPC layer.

### Pickle contract (R-06.1)

- Runtime-only fields (`_handlers_lock`, `_dispatcher_init_lock`, `_dispatcher`, `_broker`, `_agent_worker`, `_message_broker`, `_children`, `_parents`) are **excluded** from `__getstate__` and reinstated fresh in `__setstate__`.
- `_children` / `_parents` are rebuilt as empty `{}` in the child. Populated by broker callbacks after the child's `_activate` runs.
- Non-picklable handler in `__topic_handlers` → `TypeError` at `start()` naming the offending topic.
- Non-picklable value in `config` → `TypeError` at `start()` naming `Agent.config`.
- Both errors carry a suggestion to **register the handler / bind the callback inside `on_activate()`** (which runs in the child, avoiding the pickle boundary).
- RFC-006 / RFC-007 ownership model — `_HandlerRecord(owner_type, handler)` — survives pickle unchanged; only the guarding `_handlers_lock` is rebuilt fresh in the child.

### Shutdown ladder (R-06.2)

- **Step 1** — cooperative terminate: `send_data('terminate')` on the child's work queue.
- **Step 2** — `join(graceful_timeout_s=5.0)`.
- **Step 3** — if still alive: `Process.terminate()` (SIGTERM on POSIX / TerminateProcess on Windows).
- **Step 4** — `join(terminate_timeout_s=2.0)`.
- **Step 5** — if still alive: `Process.kill()` (SIGKILL / TerminateProcess with force).
- **Step 6** — `join(kill_timeout_s=1.0)`. If still alive after this, log ERROR and abandon (potential zombie).
- Total wall time strictly bounded by `graceful + terminate + kill = 8.0s` at defaults; per-call configurable.
- **Idempotence**: `STOPPED` state → cached-exitcode replay.
- **Concurrent callers**: only the first caller executes the escalation; others block on `_stop_complete_event` and return the same cached exitcode. Verified via `test_concurrent_stop_all_callers_return_same_result_single_escalation` (5 threads through a `threading.Barrier`).
- **No orphan process**: verified via `os.kill(pid, 0) → ProcessLookupError` in `test_no_orphan_process_after_stop`.

### Runtime verification (as of 2026-07-28)

- Command: `PYTHONPATH=src /home/eric/anaconda3/envs/actbot/bin/python -m pytest tests/`
- RFC-008 dedicated file `tests/unit/core/test_process_worker_lifecycle.py` — **33 passed** in ~14 s (categories A pickle × 8, B state-machine × 5, C real-spawn × 3, D restart guards × 2, E escalation × 3, F concurrent × 2, G start-failure × 3, H observability × 2, I parent-child × 1, J baseline × 4).
- Full combined regression: **289 passed, 0 failed, 0 xfailed(strict-pass), 2 xfailed** in ~26 s.
- 2 xfails preserved from RFC-007 (correlation ID / multi-handler fan-out — deferred).
- R-02 (27), R-03 (48), R-04 (33), R-05 (21), R-13 (46), RFC-006 same-API (21), RFC-007 (19) — combined 215 tests pass **unchanged**. No regression from adding `__getstate__` / `__setstate__` on Agent or from rewriting `ProcessWorker`.

### Observable behavioural changes

Four narrow, all in the correctness / bounded-shutdown direction:

1. `ProcessWorker.start()` now **actually functions** end-to-end. Previously it raised at pickle time in every configuration; now a minimal Agent with `broker_type='empty'` (or an equivalent picklable broker config) spawns a live child that runs `_activate` and terminates cleanly on `stop()`.
2. `ProcessWorker.stop()` return type widened from `None` to `Optional[int]` — callers who ignored the return still ignore it; callers can now observe the child's exit code without a new API.
3. Lambda / closure handlers on `Agent.subscribe` before `start_process()` **now fail-fast** with a `TypeError` naming the offending topic. Previously they blocked at pickle in ProcessWorker.start with a generic `TypeError: cannot pickle '_thread.RLock' object`.
4. `Agent.config` is **no longer mutated** by `ProcessWorker.start()` — the `work_queue` reference is added to a shallow copy shipped to the child. Callers who inspected `agent.config` post-start would previously find a stray `work_queue` key; now they do not.

### Known issues NOT resolved by this fix (tracked separately)

- **R-06.3 heartbeat / watchdog / automatic restart** — no liveness monitoring; a dead child is only observable via post-hoc `stop()` + `exitcode`. Deferred to a future RFC.
- **Child exception forwarding** — parent observes only exit code; child-side handler exceptions are not marshalled back over an IPC channel. Deferred.
- **Transparent parent-side `publish` / `subscribe` proxy** — parent-side calls remain no-op / silent-log per R-08. A cross-process proxy would require IPC on every message. Out of RFC-008 scope; documented architectural constraint (R-06.4).
- **`ProcessDispatcher`** — a dispatcher purpose-built for cross-process work. Deferred (RFC-004 Appendix C).
- **`ThreadWorker.stop` bounded shutdown** — RFC-008 explicitly scoped to `ProcessWorker`; `ThreadWorker.stop` still uses blocking `join()` (R-10 partial residual). Deferred.
- **R-01** — `BinaryParcel.pickle.loads` on wire bytes.
- **R-07** — handler `BaseException` handling.
- **R-08** — parent-side `publish` silent failure (documented via R-06.4).

---

## R-07 — Handler exceptions kill their per-message thread silently

- **Severity**: High
- **Category**: Fault Isolation
- **File / Function / Line**: `src/agentflow/core/agent.py:558–560` and 545–553
- **Evidence**: `except Exception as ex: logger.exception(ex)` catches only `Exception`. `BaseException` subclasses (`KeyboardInterrupt`, `SystemExit`, `MemoryError`) are not caught.
- **Trigger**: Handler raises `KeyboardInterrupt`, `SystemExit`, or an OS-level fatal condition.
- **Impact**: Thread dies without notice; agent continues serving other traffic but the task is lost; parent is not informed.
- **Confidence**: Medium
- **Recommended verification test**: Force handler to raise each BaseException subclass; observe whether agent process reports and how.

---

## R-08 — Parent-process `publish` silently fails in process mode

- **Severity**: High
- **Category**: Correctness / Observability
- **File / Function / Line**:
  - `src/agentflow/core/agent.py:305–314` `Agent.publish` reads `self._broker`.
  - `src/agentflow/core/agent.py:193` — `_broker` is assigned only inside `__activating`, which runs inside the worker context.
  - `src/agentflow/core/agent.py:76` forces default `CONCURRENCY_TYPE='process'`.
- **Trigger**: Any code that holds a reference to an `Agent` in the parent process and calls `agent.publish(...)`, or reads `agent._children`.
- **Impact**: Publish emits `logger.error("Cannot publish: _broker is None.")` and returns `None`. Caller cannot detect the failure. `agent._children` returns empty in parent even after children register.
- **Confidence**: High
- **Recommended verification test**: Start an agent with process mode, call `agent.publish('test', 'x')` from the main thread of the parent, and observe with a second subscribed agent that no message arrives.

---

## R-09 — Topic construction from `Agent.name` without sanitization; naming collisions

- **Severity**: High
- **Category**: Security / Correctness
- **File / Function / Line**:
  - `src/agentflow/core/agent.py:316–319` `__generate_return_topic` uses `/` as separator.
  - `src/agentflow/core/agent.py:353` `subscribe` passes the topic to the broker unchanged.
  - `src/agentflow/core/agent.py:519–526` build topics from `self.name` and `self.parent_name`.
- **Trigger**:
  - A user names an agent `foo+bar`, `foo#`, `foo/bar` → MQTT wildcard semantics kick in.
  - Two independently constructed agents share a name → they receive each other's parent/child messages (design confirmed by `unit_test/test_parents_children_count.py`, which uses this to have two `AgentB` share `'bbb.aaa'`).
- **Impact**: Message cross-delivery to unintended agents; unauthorised message reception if names collide across security domains.
- **Confidence**: High (collision is by design). For `+ # /` wildcard interpretation, Confidence: Medium — depends on paho behaviour but consistent with the MQTT spec.
- **Recommended verification test**: Construct two agents with the same name in a shared broker; observe whether both receive `register_child` events. Try a name containing `+` and observe subscription behaviour.

---

## R-10 — `Worker.stop()` uses `join()` without timeout

- **Status**: **RESOLVED** (2026-07-28) — ProcessWorker via [RFC-008](../rfc/RFC-008-process-worker-lifecycle.md); ThreadWorker via [RFC-009](../rfc/RFC-009-thread-worker-lifecycle.md). Both worker strategies now have bounded shutdown paths. `Agent.terminate()` returns in bounded time regardless of handler / worker wedging. Two related residuals remain **Open**: broker-level wedged `stop()` (runtime-confirmed but out of RFC-009 scope) and non-daemon interpreter-exit blocking under `STOP_TIMEOUT` (documented architectural limitation).
- **Severity**: High
- **Category**: Fault Isolation / Resource
- **File / Function / Line** (historical):
  - `src/agentflow/core/agent_worker.py:75` `ProcessWorker.stop` (pre-RFC-008)
  - `src/agentflow/core/agent_worker.py:110` `ThreadWorker.stop` (pre-RFC-009)
- **Evidence** (historical):
  - `ProcessWorker.stop`: `self.work_process.join()` with no timeout after `send_data('terminate')` — a wedged child blocked the caller forever.
  - `ThreadWorker.stop`: `self.work_thread.join()` with no timeout after `send_data('terminate')` — a wedged worker thread blocked the caller forever. In particular, a wedged `broker.stop()` inside the worker thread's `__deactivating` never returned, and the `join()` waited indefinitely.
- **Trigger** (historical): Any handler that blocks; any broker whose `stop()` blocks; any deployment that constructs a `ProcessWorker` or `ThreadWorker` without a cooperative-terminate path.
- **Impact** (historical): `Agent.terminate()` blocks its caller forever — every deployment that treated `terminate()` as fire-and-forget cleanup could not shut down gracefully.
- **Confidence at discovery**: High.
- **Recommended verification test** (was): Register an `on_message` handler that runs `while True: pass`; publish one message; call `agent.terminate()`; assert it returns within N seconds. Both ProcessWorker (RFC-008) and ThreadWorker (RFC-009) test suites now cover the equivalent bounded-return assertion.

### Sub-risk breakdown

| ID | Sub-risk | Status |
|---|---|---|
| **R-10.1** | `ProcessWorker.stop` unbounded `Process.join()` — wedged child blocks caller forever | **RESOLVED** (RFC-008) — bounded escalation `send terminate → join(graceful) → terminate() → join → kill() → join`, total ≤ 8s at defaults |
| **R-10.2** | `ThreadWorker.stop` unbounded `Thread.join()` — wedged worker thread blocks caller forever | **RESOLVED** (RFC-009) — bounded cooperative `join(graceful_timeout_s)`, `stop() -> bool`, wedged → `STOP_TIMEOUT` state, retriable |
| **R-10.3** | `Agent.terminate()` unbounded wait chained through worker.stop | **RESOLVED** (RFC-008 + RFC-009) — bounded by `dispatcher.shutdown_timeout_s + worker.graceful_timeout_s` (≈ 10s at defaults); logs WARNING on worker timeout; never raises |
| **R-10.4** | `broker.stop()` itself wedges (root cause of R-10.2 trigger) | **RESOLVED** (RFC-010) — daemon helper-thread wrapper, `bool` return, `STOP_TIMEOUT` + `STOP_FAILED` states, single-helper retry, callback fencing |
| **R-10.5** | Non-daemon worker thread in `STOP_TIMEOUT` blocks Python interpreter shutdown | **OPEN / Documented architectural limitation** — RFC-009 §7.13 / §H explicitly does not resolve; `daemon=False` preserved by design to avoid mid-`__deactivating` corruption. RFC-010 §7.13 confirms the helper thread is `daemon=True` (so helper alone does not block exit) but explicitly notes the worker thread waiting on `broker.stop()` still does |

### Resolution (R-10.1)

- **Resolved on**: 2026-07-27 — see [RFC-008 ProcessWorker lifecycle](../rfc/RFC-008-process-worker-lifecycle.md) (Implemented 2026-07-28).
- **Scope**: bounded shutdown escalation ladder with SIGTERM / SIGKILL fallback; `WorkerState` state machine; concurrent-stop coordination via `_stop_complete_event`; cached exitcode.
- Detailed runtime evidence + observable behavioural changes are captured under [R-06](#r-06--process-mode-pickling-of-agent--processworker-lifecycle) above (R-06 and R-10.1 share the same RFC-008 fix).

### Resolution (R-10.2 + R-10.3)

- **Resolved on**: 2026-07-28 — see [RFC-009 ThreadWorker lifecycle](../rfc/RFC-009-thread-worker-lifecycle.md) (Implemented).

**Final implementation** (RFC-009 first-phase scope):

**WorkerState** (`src/agentflow/core/agent_worker.py`) — the RFC-008 enum extended with two ThreadWorker-only members:

- `STOP_TIMEOUT` — cooperative stop deadline expired; thread still alive; retriable via a subsequent `stop()`.
- `FAILED` — `_activate` raised an `Exception` (not `BaseException`); captured into `last_exception`; thread ended.

`ProcessWorker` never enters either state (it has SIGKILL and exitcode).

**ThreadWorker rewrite** (`src/agentflow/core/agent_worker.py`):

- Full state machine `NEW → STARTING → RUNNING → STOPPING → STOPPED / STOP_TIMEOUT / FAILED / START_FAILED`, protected by `_state_lock: threading.RLock`.
- `stop(graceful_timeout_s=5.0) -> bool` — cooperative send `'terminate'` → `join(graceful_timeout_s)` → under lock: alive → `STOP_TIMEOUT + False`; dead + `last_exception` → `FAILED + True`; dead → `STOPPED + True`. `finally` unconditionally sets `_stop_complete_event` so concurrent waiters never hang.
- **Concurrent stop waiter is bounded**: waits `graceful_timeout_s + 0.1s` coordination margin; on event timeout, reads `Thread.is_alive()` and returns `not alive` with WARNING log. **No `Event.wait()` without timeout anywhere in `stop()`**.
- **Thread reference retained on `STOP_TIMEOUT`** — `is_working()` continues to reflect real `Thread.is_alive()`; retry `stop()` re-joins the same thread with a fresh budget.
- New read-only properties: `state: WorkerState`, `last_exception: Optional[BaseException]`.
- `_run_target` wrapper catches **`Exception` only** (RFC-009 §7.11) — `BaseException` propagates and dies without state update (documented limitation, see R-10 residuals below).
- `stop()` before `start()` is a no-op returning `True`, state stays `NEW` — subsequent `start()` is allowed.
- Repeated `start()` from any non-`NEW` state raises `RuntimeError` (closes the pre-RFC-009 silent orphan-thread leak).
- `agent.config['work_queue']` in-place mutation preserved (§7.15) — shared-instance model is thread mode's contract.

**Agent.terminate** (`src/agentflow/core/agent.py`):

- Public signature unchanged; never-raise contract preserved.
- Calls `dispatcher.stop()` before `worker.stop()` (RFC-004 order preserved).
- `dispatcher.stop()` wrapped in `try/except Exception` — broken dispatcher does not block worker cleanup.
- `worker.stop()` wrapped in `try/except Exception` — misbehaving worker `.stop()` never propagates.
- Observes `worker.stop()` return value: **`False` → WARNING** with `state`, `work_thread`, and the daemon interpreter-exit caveat; `True` / `None` (legacy `FakeWorker`) → silent.
- Docstring explicitly states: bounded return of `terminate()` only guarantees this method returns; if worker ended at `STOP_TIMEOUT` and `daemon=False`, Python interpreter shutdown may still block on it.

### Stop contract summary

| Trigger | ProcessWorker (RFC-008) | ThreadWorker (RFC-009) |
|---|---|---|
| Cooperative | `send terminate` via mp Queue | `send terminate` via threading Queue |
| Escalation | SIGTERM → SIGKILL (bounded per-step) | **None** — no safe forced-cancel (see R-10.4 / §5 Option E rejected) |
| Timeout return | `Optional[int]` exitcode (cached) | `bool` — `False` on wedge |
| Timeout state | `STOPPED` (always reachable via SIGKILL) | `STOP_TIMEOUT` (thread still alive; retriable) |
| Restart | Not supported | Not supported |
| Concurrent stop | `_stop_complete_event.wait()` — bounded by underlying escalation | `_stop_complete_event.wait(graceful_timeout_s + 0.1)` — bounded coordination margin |
| Config-share | Copy (`child_config = dict(agent.config)`) | Share (in-place mutation of `agent.config`) |
| Daemon | `daemon=False` | `daemon=False` |
| Force-kill primitive | `Process.kill()` | **None** — Python threads cannot be safely cancelled |

### Runtime verification (as of 2026-07-28)

- Command: `PYTHONPATH=src /home/eric/anaconda3/envs/actbot/bin/python -m pytest tests/unit`
- RFC-009 dedicated file `tests/unit/core/test_thread_worker_lifecycle.py` — **35 passed** in ~6 s.
- RFC-008 file `tests/unit/core/test_process_worker_lifecycle.py` — **33 passed** unchanged.
- Full combined regression: **324 passed, 0 failed, 0 xfailed(strict-pass), 2 xfailed** in ~34 s.
- 2 xfails preserved from RFC-007 (correlation ID / multi-handler fan-out — deferred).
- Zero regression across R-02 (27) / R-03 (48) / R-04 (33) / R-05 (21) / R-13 (46) / RFC-006 (21) / RFC-007 (19) / RFC-008 (33) — combined **248 tests pass unchanged**.

### Observable behavioural changes

Four narrow, all in the bounded-shutdown / safety direction:

1. `ThreadWorker.stop()` now returns `bool` (was `None`). Return-type widening is source-compatible; no in-tree caller inspected the old return value.
2. `ThreadWorker.stop()` before `start()` is now a no-op returning `True` (was `AttributeError`). Fixes an unintended bug.
3. `ThreadWorker.start()` from any non-`NEW` state now raises `RuntimeError` (was silent rebind). Closes the orphan-thread leak.
4. `Agent.terminate()` now logs a WARNING when `worker.stop()` returns `False`; it did not do so before because the old `stop()` returned `None`. Signature unchanged, never-raise contract unchanged.

### Known residuals NOT resolved by RFC-009

- ~~**R-10.4 broker.stop() itself wedges**~~ — **RESOLVED 2026-07-28 (RFC-010)**. See "Resolution (R-10.4)" below.
- **R-10.5 non-daemon interpreter-exit blocking** — a `STOP_TIMEOUT` worker leaves a live non-daemon thread; Python interpreter shutdown will still block on it. RFC-009 §7.13 keeps `daemon=False` on purpose (daemonising would trade a visible hang for silent mid-`__deactivating` corruption). Documented in `ThreadWorker` docstring, `stop()` docstring, `Agent.terminate` docstring, and every WARNING log emitted by the timeout path. **RFC-010 does NOT resolve this** — the broker helper thread is `daemon=True` (so helper alone does not block exit), but the worker thread waiting on `broker.stop()` still does.
- **`BaseException` observability** — `_run_target` catches `Exception` only (RFC-009 §7.11); a `KeyboardInterrupt` / `SystemExit` / `GeneratorExit` from `_activate` leaves state unchanged and, after `stop()`, gets marked `STOPPED` — masking the crash. A signal-based cleanup layer or a separate tracker would be needed to observe this. Deferred. (RFC-010 fixed the equivalent broker-side variant via `STOP_FAILED` — worker-side parity is future work.)
- **Heartbeat / watchdog / automatic restart** — RFC-009 Out-of-scope (parity with RFC-008).
- **Config-key surface** for `graceful_timeout_s` — deferred; method-arg only in first-phase.
- **Lifecycle metrics** on `ThreadWorker` — deferred; parity with RFC-008 §7.18.

### Resolution (R-10.4)

- **Resolved on**: 2026-07-28 — see [RFC-010 Broker bounded shutdown](../rfc/RFC-010-broker-bounded-shutdown.md) (Implemented).

**Final implementation** (RFC-010 first-phase scope):

**WorkerState extension** (`src/agentflow/core/agent_worker.py`) — one new member added to the RFC-008 / RFC-009 enum:

- `STOP_FAILED` — MqttBroker-only. Helper thread exited abnormally without setting the completed-normally marker (e.g. `BaseException` propagated out of paho). Terminal; retry via `stop()` replays cached `False`.

**MqttBroker rewrite** (`src/agentflow/broker/mqtt_broker.py`):

- Full state machine `NEW → STARTING → RUNNING → STOPPING → STOPPED / STOP_TIMEOUT / STOP_FAILED / START_FAILED`, protected by the existing `_state_lock`.
- New read-only properties: `state`, `last_stop_exception`.
- `stop(graceful_timeout_s=5.0) -> bool` — cooperative bounded shutdown via a `daemon=True` helper thread that runs `disconnect + loop_stop` with per-call `except Exception` isolation. `join(graceful_timeout_s)` in the caller; under lock: alive → `STOP_TIMEOUT + False`; dead + `_stop_helper_completed_normally=True` → `STOPPED + True`; dead + not-normally-completed → `STOP_FAILED + False`. `finally` unconditionally sets `_stop_complete_event` so waiters never hang.
- **Single-helper retry (RFC-010 modification 1)**: same MqttBroker lifecycle → at most ONE helper thread → at most ONE (`disconnect + loop_stop`) pair to paho. `STOP_TIMEOUT` retry re-joins the SAME helper; does not spawn a new one; does not re-issue paho calls.
- **Concurrent stop coordination**: `_stop_complete_event.wait(graceful_timeout_s + 0.1s)` — coordination margin bounded; on event timeout, reads `helper.is_alive()` and returns `not alive` with WARNING log. No bare `.wait()` anywhere.
- **`STARTING.stop()` raises `RuntimeError`** (first-phase) — avoids disconnect/loop_stop on a half-initialised client.
- **State cleanup at stop linearization** — flipping `_stopping=True` at the RUNNING → STOPPING transition is accompanied by immediate `_connected=False`, `_connect_ok=False`, `_connected_evt.clear()` in the same lock section (RFC-010 §G modification 4).
- **Full callback fencing** (RFC-010 §F modification 4):
  - `_on_message` — silent drop when `_stopping`; notifier NOT invoked.
  - `_on_connect(rc=0)` — entire body gated; skip → no `_connect_ok` write, no `_connected=True`, no state transition, no recovery, no notifier call, no `_connected_evt.set()`.
  - `_on_connect(rc!=0)` — also gated: skip → no writes.
  - `_on_disconnect` — unchanged (RFC-005): still updates `_connected=False` + `_last_disconnect_was_planned` diagnostics; does NOT touch `_stopping`; does NOT trigger recovery.
- **Exception isolation** (RFC-010 §7.8-§7.9):
  - `disconnect` `Exception` does NOT prevent `loop_stop` (resource-leak fix vs the pre-RFC-010 behaviour).
  - First captured `Exception` retained in `_last_stop_exception` (earlier is more diagnostic).
  - `BaseException` propagates; helper dies with `completed_normally=False`; state → `STOP_FAILED` (not `STOPPED`).

**Agent.__deactivating** (`src/agentflow/core/agent.py`):

- `Agent.terminate` signature and behaviour **unchanged**; never-raise contract preserved.
- `__deactivating` observes `broker.stop()`'s new `bool` return:
  - `False` → log WARNING with `state`, `last_stop_exception`, and daemon interpreter-exit caveat.
  - `True` → silent.
  - `None` (legacy brokers) → treated as success via `stopped is False` guard.
- Wraps `broker.stop()` in `try/except Exception` — misbehaving broker `.stop()` never propagates.

### Stop contract summary (updated for RFC-010)

| Trigger | ProcessWorker (RFC-008) | ThreadWorker (RFC-009) | MqttBroker (RFC-010) |
|---|---|---|---|
| Cooperative | mp Queue `terminate` | threading Queue `terminate` | paho `disconnect` + `loop_stop` on daemon helper |
| Escalation | SIGTERM → SIGKILL | none (Python threads not cancellable) | none (helper is contained; caller returns bounded) |
| Timeout return | `Optional[int]` exitcode | `bool` | `bool` |
| Timeout state | `STOPPED` (SIGKILL) | `STOP_TIMEOUT` (retriable) | `STOP_TIMEOUT` (retriable — same helper re-joined) |
| Abnormal exit | (n/a — process exit code covers) | `FAILED` (Exception captured) | `STOP_FAILED` (BaseException / abnormal helper exit) |
| Restart | Not supported | Not supported | Not supported |
| Concurrent stop | `_stop_complete_event.wait()` | `_stop_complete_event.wait(t + 0.1)` | `_stop_complete_event.wait(t + 0.1)` |
| Daemon | `daemon=False` | `daemon=False` | helper `daemon=True` |
| Force-kill primitive | `Process.kill()` | none | none |
| Interpreter-exit blocking risk | none (SIGKILL) | **YES** (R-10.5) | helper OK; worker still R-10.5 |

### Runtime verification (as of 2026-07-28)

- Command: `PYTHONPATH=src /home/eric/anaconda3/envs/actbot/bin/python -m pytest tests/unit`
- RFC-010 dedicated file `tests/unit/test_mqtt_broker_shutdown.py` — **53 passed** in ~2 s.
- `tests/unit/test_mqtt_broker_reconnect.py` — **48 passed** unchanged (2 tests refactored to prime broker to RUNNING before stop; RFC-005 semantics preserved).
- `tests/unit/core/test_thread_worker_lifecycle.py` — **35 passed** unchanged (RFC-009 preserved).
- `tests/unit/core/test_process_worker_lifecycle.py` — **33 passed** unchanged (RFC-008 preserved).
- Full combined regression: **377 passed, 0 failed, 0 xfailed(strict-pass), 2 xfailed** in ~34 s.
- 2 xfails preserved from RFC-007 (correlation ID / multi-handler fan-out — deferred).
- Zero regression across R-02 (27) / R-03 (48) / R-04 (33) / R-05 (21) / R-13 (46) / RFC-006 (21) / RFC-007 (19) / RFC-008 (33) / RFC-009 (35) — combined **283 tests pass unchanged**.

### Observable behavioural changes

Four narrow, all in the bounded-shutdown / safety direction:

1. `MqttBroker.stop()` now returns `bool` (was `None`). Return-type widening is source-compatible; no in-tree caller (except `Agent.__deactivating`, which was updated) inspected the old return value.
2. `MqttBroker._on_message` after `stop()` is now a silent drop (was: still forwarded to notifier). Bug fix — no in-tree caller depends on late delivery.
3. `MqttBroker._on_connect(rc=0)` after `stop()` no longer writes `_connect_ok=True` and no longer sets `_connected_evt`. Bug fix — no in-tree caller reads either post-stop.
4. `Agent.__deactivating` now logs a WARNING when `broker.stop()` returns `False`. Signature unchanged, never-raise contract unchanged.

### Known residuals NOT resolved by RFC-010

- **R-10.5 non-daemon interpreter-exit blocking** (see above) — remains OPEN / Documented.
- **`MqttBroker.start()` / `client.connect()` bounded lifecycle** — `wait=True` `timeout` bounds the wait but not the connect syscall itself. Deferred to a future broker-start-lifecycle RFC.
- **`MessageBroker` ABC timeout contract** — RFC-010 §7.16 explicitly keeps the ABC as `stop(self)`; unifying across `EmptyBroker` / third-party subclasses requires a separate RFC.
- **`STARTING.stop()` coordination** — first-phase raises `RuntimeError`; deferred.
- **Other broker implementations** (`RedisBroker`, `RosBroker`, `DdsBroker`) — R-22 flagged as unregistered / broken; not in RFC-010 blast radius.
- **Reconnect policy / offline publish queue / broker clustering / failover** — RFC-005 owns reconnect; rest are deferred.
- **Publish result observability** — paho `MessageInfo (rc/mid)` still discarded (RFC-002 residual).
- **Paho helper resource completion guarantee** — a wedged paho socket may keep the helper alive even after `stop()` returns; helper is daemonised so it does not block interpreter exit, but the socket / file descriptor is not force-closed.

---

---

## R-11 — Dynamic `setattr` in `_on_connect` can overwrite methods with `None`

- **Severity**: Medium
- **Category**: Correctness
- **File / Function / Line**: `src/agentflow/core/agent.py:515–517` in `_on_connect`
- **Evidence**:
  ```python
  for event in EventHandler:
      attr_name = str(event).lower()[len('EventHandler.'):]
      setattr(self, attr_name, self.get_config(str(event), getattr(self, attr_name, None)))
  ```
- **Trigger**: User passes an `EventHandler.*` key with a `None` value in config.
- **Impact**: The corresponding method (e.g. `on_activate`, `on_message`) becomes `None`; subsequent invocation raises `TypeError: 'NoneType' object is not callable`.
- **Confidence**: Medium
- **Recommended verification test**: `Agent(config={EventHandler.ON_ACTIVATE: None}).start()` and observe.

---

## R-12 — Dead signature branch in `_activate`

- **Severity**: Low
- **Category**: Correctness
- **File / Function / Line**: `src/agentflow/core/agent.py:219–225` in `_activate`
- **Evidence**:
  ```python
  elif isinstance(sig.parameters.get('self'), Agent):
      self.on_activate(self)
  ```
  `sig.parameters['self']` is a `Parameter`, never an `Agent`. This branch is unreachable.
- **Trigger**: N/A (dead code).
- **Impact**: A user who defines `def on_activate(self, agent)` expecting `agent` receives `self.config` instead (falls through to else branch).
- **Confidence**: High
- **Recommended verification test**: Static verification by reading code.

---

## R-13 — publish result is discarded at every layer

- **Status**: **RESOLVED** (2026-07-26) — see [RFC-002](../rfc/RFC-002-publish-error-propagation.md)
- **Severity**: Medium
- **Category**: Message Reliability / Observability
- **File / Function / Line** (historical): `src/agentflow/broker/mqtt_broker.py:106–107` `MqttBroker.publish` returns paho `MessageInfo`; `src/agentflow/core/agent.py:305–314` `Agent.publish` returns `None` unconditionally.
- **Trigger** (historical): Broker overload; disconnected client; QoS mismatch.
- **Impact** (historical): Caller could not distinguish success from failure; publish failures were silently swallowed by `Agent.publish` and, when reached from `publish_sync`, surfaced only as a plain `TimeoutError` after the full timeout elapsed, with no reference to the underlying cause.
- **Confidence at discovery**: High
- **Recommended verification test** (was): Mock paho `client.publish` to return `MessageInfo(rc=1, mid=…)`; verify caller has no way to observe this.

### Resolution

- **Resolved on**: 2026-07-26
- **RFC**: [RFC-002 — publish error propagation](../rfc/RFC-002-publish-error-propagation.md) (Implemented)
- **Scope of change** (RFC-002 §6):
  - `src/agentflow/core/agent.py` — added `Agent._publish_or_raise(topic, data=None) -> None` as an **internal** (single-underscore) strict variant. Wraps `data` as a Parcel, forwards to the broker, propagates every broker exception unchanged, and raises `RuntimeError("Cannot publish: no broker attached")` when `self._broker is None`.
  - `src/agentflow/core/agent.py` — `Agent.publish` refactored to call `_publish_or_raise` inside its existing `try/except Exception: logger.exception(...)`. Public signature and fire-and-forget contract preserved: still returns `None` on every outcome, still swallows every `Exception`.
  - `src/agentflow/core/agent.py` — `Agent.publish_sync` now calls `self._publish_or_raise(topic, pcl)` in place of `self.publish(topic, pcl)`. The `try/finally` structure from RFC-001 is unchanged.
- **Behavioural changes** (public signatures unchanged):
  - `publish_sync` propagates the broker's **original exception object** (same type, same message, same traceback) instead of masking it as `TimeoutError`. Verified for `RuntimeError`, `ConnectionError`, `TimeoutError`, `OSError`.
  - `publish_sync` fast-fails on publish error (measured elapsed < 50 ms against a mocked broker that raises synchronously) instead of waiting the full `timeout`.
  - `publish_sync` with `_broker is None` raises `RuntimeError("Cannot publish: no broker attached")` immediately, not `TimeoutError` after the full timeout.
  - The true-timeout case (broker accepted the publish but no response arrived within the deadline) continues to raise `TimeoutError` with the existing message shape.
  - `Agent.publish` behaviour is byte-identical to the pre-RFC state — fire-and-forget callers see no difference.
- **Interaction with R-02 cleanup**: intact. RFC-001's `try/finally` in `publish_sync` runs on every exit path (success, true timeout, broker-publish exception, missing broker). Verified by `test_publish_sync_cleans_up_handler_when_broker_publish_raises`, `test_publish_sync_calls_broker_unsubscribe_when_broker_publish_raises`, `test_publish_sync_cleans_up_handler_when_broker_is_none`, `test_publish_sync_with_none_broker_does_not_crash_on_cleanup`.
- **Runtime verification** (as of 2026-07-26):
  - Command: `PYTHONPATH=src /home/eric/anaconda3/envs/actbot/bin/python -m pytest tests/unit`
  - Result: **114 passed, 0 failed, 0 xfailed, 0 xpassed** in 1.82 s.
  - Behaviours directly asserted:
    - **Original exception type preserved** (4 types) — `test_publish_sync_propagates_broker_exception_unchanged[exc*]`, `test_publish_or_raise_reraises_broker_exception[exc*]`.
    - **Original exception object preserved** (identity check) — `test_publish_sync_raises_original_broker_exception_object`.
    - **Original message preserved** — `test_publish_sync_preserves_broker_exception_message`.
    - **Fast fail** (< 50 ms with `timeout=1.0`) — `test_publish_sync_fast_fails_when_broker_publish_raises`, `test_publish_sync_fails_fast_when_broker_is_none`.
    - **Missing broker** — `test_publish_sync_raises_RuntimeError_when_broker_is_none`, `test_publish_or_raise_raises_RuntimeError_when_broker_is_none`.
    - **True timeout still surfaces as `TimeoutError`** — `test_publish_sync_TimeoutError_still_used_for_true_timeout`.
    - **Fire-and-forget contract preserved** — `test_publish_swallows_broker_exception_and_returns_none[exc*]`, `test_publish_never_reraises_broker_exception[exc*]`, `test_publish_returns_none_when_broker_is_none`, `test_publish_return_value_cannot_distinguish_success_from_failure`.
    - **Caller has an escape hatch** — `test_publish_or_raise_lets_caller_distinguish_success_from_failure`.
  - Test files touching this fix: `tests/unit/core/test_agent_publish_errors.py` (46 tests), `tests/unit/core/test_agent_publish_sync.py` (3 R-02 crossover tests updated to reflect the new exception type on the publish-failure path).
- **Known issues NOT resolved by this fix** (tracked separately):
  - **R-01** — `BinaryParcel` still `pickle.loads` on wire bytes.
  - **R-03** — MQTT reconnect + re-subscribe still not implemented.
  - **R-04** — `Agent._on_message` still spawns one short-lived thread per received message.
  - **R-05** — suspected reply loop still unverified.
  - **Concurrent same-`topic_wait` race** — `Agent.subscribe` silently overwrites when two callers pick the same explicit `topic_wait`. RFC-001's identity guard prevents cleanup from making this worse, but the underlying race is unchanged.
  - **Broker-side `MessageInfo`** — `MqttBroker.publish` and `_publish_or_raise` still discard the paho `MessageInfo` (rc/mid). RFC-002 explicitly deferred this to a future RFC on broker observability.

---

## R-14 — Shared dictionaries without locks

- **Status**: **PARTIALLY RESOLVED** (2026-07-27) — `__topic_handlers` fully synchronised via [RFC-007](../rfc/RFC-007-handler-registry-ownership.md); `_children` / `_parents` remain Open (not addressed in this RFC).
- **Severity**: Medium
- **Category**: Concurrency
- **File / Function / Line** (historical): `__topic_handlers`, `_children`, `_parents` in `agent.py`
- **Trigger** (historical): Concurrent subscribe and message delivery, or concurrent parent/child registrations.
- **Impact** (historical): Compound TOCTOU (`if topic in d: warn; d[topic] = h`) could lose a warning or overwrite unexpectedly; rare visibility issues on `__topic_handlers` reads by `_on_message`.
- **Confidence at discovery**: Medium.
- **Recommended verification test** (was): Stress test with N threads registering distinct then colliding topics; observe warning counts vs actual final state.

### Runtime confirmation of `__topic_handlers` residual risks (before RFC-007)

The `__topic_handlers` slice of R-14 was upgraded to runtime-confirmed via `tests/unit/core/test_agent_publish_sync_cross_api.py` (12 characterisation tests documenting three residual risks after RFC-006 landed):

- **R.6-1** — direct `Agent.subscribe('T', new_handler)` while a `publish_sync` waiter was active on `'T'` silently overwrote the waiter's handler. Waiter timed out; response was routed to the replacement.
- **R.6-2** — direct `Agent.unsubscribe('T')` while a `publish_sync` waiter was active popped the waiter's handler and called `broker.unsubscribe('T')`. Waiter timed out; late response fell through to `Agent.on_message` (RFC-003 R-fallback-silent).
- **R.6-3** — `Agent._on_message` read `__topic_handlers` with two independent operations (`in` check, then `.get()`) — a TOCTOU window between them could produce inconsistent dispatch decisions under concurrent mutation.

### Resolution (for `__topic_handlers` only)

- **Resolved on**: 2026-07-27
- **RFC**: [RFC-007 — handler registry ownership](../rfc/RFC-007-handler-registry-ownership.md) (Implemented)

**Final implementation** (RFC-007 A + C + D combined):

- New internal types in `src/agentflow/core/agent.py` (leading-underscore, not re-exported):
  - `_HandlerOwnerType(Enum)`: `NORMAL`, `PUBLISH_SYNC`.
  - `_HandlerRecord(frozen dataclass)`: `(owner_type, handler)`.
- `Agent.__topic_handlers` value type changed from `Callable` to `_HandlerRecord`.
- **All registry mutations and reads** now happen inside `with self._handlers_lock:` (the RLock introduced in RFC-006):
  - `Agent.subscribe`: check owner; if PUBLISH_SYNC → raise; else warn (rebind case) + register as NORMAL. `broker.subscribe` outside the lock.
  - `Agent.unsubscribe`: check owner; if PUBLISH_SYNC → raise; else pop. `broker.unsubscribe` outside the lock.
  - `Agent.publish_sync` register: check any existing record; if PUBLISH_SYNC → raise (`already awaited`); if NORMAL → raise (`would trample it`); else register as PUBLISH_SYNC. `broker.subscribe` outside the lock.
  - `Agent.publish_sync` finally: **triple check** — record exists AND `owner_type is PUBLISH_SYNC` AND `handler is handle_response` → pop. `broker.unsubscribe` outside the lock.
  - `Agent._on_message`: single-snapshot read under the lock decides `is_specific_handler` and `topic_handler` together. Dispatcher enqueue and handler invocation happen outside the lock.

### Collision contract (four scenarios; all raise `TopicWaitCollisionError`)

| Scenario | Message keyword |
|---|---|
| publish_sync vs publish_sync (RFC-006) | `"already awaited by another publish_sync"` |
| publish_sync vs NORMAL (RFC-007) | `"already registered by a normal subscribe handler; publish_sync would trample it"` |
| direct subscribe vs PUBLISH_SYNC (RFC-007) | `"reserved by an active publish_sync waiter; direct subscribe is refused"` |
| direct unsubscribe vs PUBLISH_SYNC (RFC-007) | `"reserved by an active publish_sync waiter; direct unsubscribe is refused"` |

### Compatibility

- **`Agent.subscribe` NORMAL rebind**: preserved — warn + overwrite as before (RFC-006 §7.11 / RFC-007 §7.5).
- **`Agent.unsubscribe` on NORMAL topic**: preserved — pop + broker.unsubscribe as before.
- **Public signatures** of `Agent.publish` / `publish_sync` / `subscribe` / `unsubscribe` / `_on_message`: unchanged.
- **Parcel, Message Schema, Broker API**: unchanged.
- **`_HandlerRecord` / `_HandlerOwnerType`**: internal names (leading underscore); not re-exported.

### Lock hygiene (RFC-007 §7.10)

`_handlers_lock` is never held across:

- `broker.subscribe` / `broker.unsubscribe` / `broker.publish`
- `dispatcher.enqueue`
- handler invocation
- `_publish_or_raise` / `event.wait`

Verified by four dedicated tests: `test_handlers_lock_is_reentrant_from_broker_subscribe_callback`, `test_handlers_lock_is_reentrant_from_broker_unsubscribe_callback`, `test_handler_can_safely_call_subscribe_from_within_dispatch`, `test_dispatcher_enqueue_happens_outside_handlers_lock`.

### Runtime verification (as of 2026-07-27)

- Command: `PYTHONPATH=src /home/eric/anaconda3/envs/actbot/bin/python -m pytest tests/unit`
- Result: **256 passed, 0 failed, 0 xfailed(strict-pass), 2 xfailed** in 12.03 s.
- RFC-007 dedicated file `tests/unit/core/test_agent_publish_sync_cross_api.py` — **19 passed** (4 categories: cross-API protection × 4, compatibility × 3, registry shape × 3, `_on_message` semantics + stress × 5, lock hygiene × 4).
- 3 independent stability re-runs — all 19/19 pass, no flakes observed.
- R-01 / R-02 (27) / R-03 (48) / R-04 (33) / R-05 (21) / R-13 (46) / RFC-006 same-API (21) — combined 196 tests pass **unchanged**.
- 2 xfails preserved (correlation ID / multi-handler fan-out — deferred to future RFC).

### Still open under R-14

The `__topic_handlers` slice is resolved. The following R-14 residuals **remain Open** (out of RFC-007 scope):

- `_children` — parent's child registry; still an unlocked dict.
- `_parents` — child's parent registry; still an unlocked dict.

Fixing these would require a similar lock + accessor policy for `Agent._notify_children` / `_notify_parents` / `_handle_children` / `_handle_parents`. Deferred to a future RFC.

### Future work (deferred from RFC-007)

- **Correlation ID** — a Parcel metadata field to distinguish concurrent same-topic requests; would allow multiple publish_sync callers to coexist on the same `topic_wait` and receive their own responses. Requires Parcel schema change (R-20 territory).
- **Multi-handler fan-out per topic** — `__topic_handlers` value becomes a list; delivery notifies every handler; needed for pub-sub with multiple observers.
- **Waiter queue** — publish_sync waiters chain rather than collide; needs correlation ID to route responses.
- **Parcel metadata** — broader schema evolution (R-20).
- **ProcessWorker** — cross-process registry synchronisation; requires shared-memory / IPC design.

---

## R-15 — `ConfigName` symbol referenced but does not exist

- **Severity**: Medium
- **Category**: Configuration / Documentation
- **File / Function / Line**:
  - Consumers: `exe_test/1pmc.py:8`, `exe_test/1psc.py:8`, `exe_test/1csp.py:8`, `exe_test/mp1c.py:8`, `exe_test/mpmc.py:8`, `exe_test/mpmc-sp.py:8`, `exe_test/test1.py:14`
  - Definition location that would satisfy: `src/agentflow/core/config.py` — only `CONCURRENCY_TYPE` (module-level string) exists (line 24). No `ConfigName` class.
- **Trigger**: Running any `exe_test/*.py`.
- **Impact**: `ImportError: cannot import name 'ConfigName' from 'agentflow.core.config'`.
- **Confidence**: High
- **Recommended verification test**: `python -c "from agentflow.core.config import ConfigName"`.

---

## R-16 — Unused `tkinter` import in Agent

- **Severity**: Medium
- **Category**: Correctness / Portability
- **File / Function / Line**: `src/agentflow/core/agent.py:8` `from tkinter import N`
- **Trigger**: Any environment without Tk (minimal Docker images, servers without X libraries).
- **Impact**: `ImportError: No module named 'tkinter'` → the whole framework fails to import.
- **Confidence**: Medium — depends on the deployment environment.
- **Recommended verification test**: `python -c "from agentflow.core.agent import Agent"` in a minimal Python container.

---

## R-17 — `dict[str, function]` annotation uses undefined `function` type

- **Severity**: Low
- **Category**: Correctness
- **File / Function / Line**: `src/agentflow/core/agent.py:45`
- **Evidence**: `function` is not a Python built-in type nor imported. Python does not evaluate PEP 604 annotations at runtime unless `typing.get_type_hints` is called.
- **Trigger**: Any tool that evaluates class-level type hints (e.g. `dataclasses.dataclass`, `pydantic`, static type checkers, `typing.get_type_hints(Agent)`).
- **Impact**: `NameError: name 'function' is not defined`.
- **Confidence**: High
- **Recommended verification test**: `typing.get_type_hints(Agent)`.

---

## R-18 — No unregister / heartbeat / liveness detection between parents and children

- **Severity**: High
- **Category**: Fault Isolation
- **File / Function / Line**: `src/agentflow/core/agent.py:367–386` (`__register_child`, `__register_parent`); no counterpart delete methods anywhere in the file (grep-confirmed).
- **Trigger**: A child process crashes, is killed, or loses broker connection.
- **Impact**: Parent's `_children` dict never shrinks; subsequent `_notify_child` publishes to a dead topic silently (nothing to detect it). No task reassignment path exists.
- **Confidence**: High
- **Recommended verification test**: Start child + parent; kill child; verify `_children` still contains the dead child id, and that `_notify_child` succeeds silently.

---

## R-19 — Parcel version field is never validated

- **Severity**: Medium
- **Category**: Message Contract
- **File / Function / Line**: `src/agentflow/core/parcel.py:6` (`VERSION = 3`), `parcel.py:103–107` (`_set_managed_data` accepts any version).
- **Trigger**: Older/newer agent version emits a parcel with a different `version`.
- **Impact**: Schema drift is silent; no error, no upgrade path.
- **Confidence**: High
- **Recommended verification test**: Feed a parcel with `version=1` — expect no error.

---

## R-20 — No message-level metadata for tracing (messageId, correlationId, timestamp, source, attempt)

- **Severity**: Medium
- **Category**: Observability
- **File / Function / Line**: `src/agentflow/core/parcel.py:95–107` (envelope contents are `version`, `content`, `topic_return`, `error` only).
- **Trigger**: Any production incident triage.
- **Impact**: Cannot correlate publish → subscribe → response; cannot deduplicate; cannot detect stale messages.
- **Confidence**: High
- **Recommended verification test**: Read the parcel schema.

---

## R-21 — `core/wrapper.py` references undefined `VERSION` symbol

- **Severity**: Low
- **Category**: Correctness / Dead code
- **File / Function / Line**: `src/agentflow/core/wrapper.py:32` inside `TextWrapper.wrap`
- **Evidence**: `VERSION` is not imported or defined in this module.
- **Trigger**: Any call to `TextWrapper.wrap(...)`.
- **Impact**: `NameError`. Suspected dead code — no import of `wrapper` found in the source tree.
- **Confidence**: High
- **Recommended verification test**: `TextWrapper.wrap("x")`.

---

## R-22 — Stub / broken broker implementations reachable via `BrokerType`

- **Severity**: Low
- **Category**: Correctness / Documentation
- **File / Function / Line**:
  - `src/agentflow/broker/redis_broker.py` (stub, only logs)
  - `src/agentflow/broker/ros_broker.py` (stub, only logs)
  - `src/agentflow/broker/dds_broker.py` (references undefined `_client`, `Client` not imported; not registered in factory)
  - `src/agentflow/broker/ros_noetic_broker.py:4` (`from .. import LOGGER_NAME` fails)
  - `src/agentflow/broker/broker_maker.py:20–27` — factory only has real cases for Redis, MQTT, ROS, Empty; DDS branch missing.
- **Trigger**: User selects `BrokerType.Redis` or `BrokerType.ROS` expecting real broker behaviour.
- **Impact**: Messages appear to publish but nothing crosses the wire.
- **Confidence**: High
- **Recommended verification test**: Configure `broker_type: 'redis'`; publish a message; verify no delivery.

---

## R-23 — Naming / documentation drift

- **Severity**: Low
- **Category**: Documentation
- **File / Function / Line**:
  - `README.md` mentions `logistics/`, `unittest/`
  - Reality: `src/agentflow/logistic/`, `unit_test/`
- **Trigger**: New contributor follows README paths.
- **Impact**: Confusion; commands like `python -m unittest discover -s unittest` fail.
- **Confidence**: High
- **Recommended verification test**: `ls src/agentflow/`.

---

## R-24 — Unexplained `time.sleep(1)` before `_connected_event.set()` and `on_connected()`

- **Severity**: Low
- **Category**: Correctness / Observability
- **File / Function / Line**: `src/agentflow/core/agent.py:529–533` in `_on_connect`
- **Evidence**: `def handle_connected(): time.sleep(1); self.__connected_event.set(); self.on_connected()`
- **Trigger**: Every successful broker connect.
- **Impact**: 1-second delay on startup; masks underlying subscribe race with no explanatory comment.
- **Confidence**: Medium — the intent is presumed to be waiting for paho subscribe ACKs.
- **Recommended verification test**: Remove sleep in a scratch branch, run parent-child tests, observe races.

---

## R-25 — `_activate` broker init default has inconsistent shape with subsequent access

- **Severity**: Medium
- **Category**: Correctness
- **File / Function / Line**: `src/agentflow/core/agent.py:180–186` in `__activating`
- **Evidence**:
  ```python
  broker_config_all = self.get_config("broker", {'broker_type': BrokerType.Empty})
  ...
  broker_name   = broker_config_all['broker_name']
  broker_config = broker_config_all[broker_name]
  ```
  The default `{'broker_type': BrokerType.Empty}` has no `'broker_name'` key; the very next line raises `KeyError` when `config["broker"]` is missing.
- **Trigger**: Agent constructed without a `broker` config entry.
- **Impact**: Agent activation fails with `KeyError` before broker retry loop begins.
- **Confidence**: High
- **Recommended verification test**: Construct `Agent(name='x', agent_config={})` and call `start()`; observe.

---

## Summary table (sorted by severity, then confidence)

| ID | Severity | Confidence | Status | Title |
|---|---|---|---|---|
| R-01 | Critical | High | Open | `pickle.loads` on wire bytes |
| R-02 | High | High | **Resolved 2026-07-26 (RFC-001)** | `publish_sync` handler / subscription leak |
| R-03 | High | High | **Resolved 2026-07-27 (RFC-005)** | No MQTT reconnect / no re-subscribe |
| R-04 | High | High | **Resolved 2026-07-26 (RFC-004)** | Unbounded per-message thread creation |
| R-08 | High | High | Open | Parent-process `publish` silently fails in process mode |
| R-09 | High | High | Open | Topic derived from unsanitised `Agent.name`; naming collisions |
| R-10 | High | High | **Resolved 2026-07-28 (RFC-008 + RFC-009 + RFC-010)** — worker layer bounded (RFC-008/009); broker layer bounded (RFC-010); `Agent.terminate` bounded return + WARNING on timeout. Only residual R-10.5 non-daemon interpreter-exit blocking remains **Open / Documented**. | `Worker.stop()` join without timeout |
| R-18 | High | High | Open | No child/parent unregister / heartbeat |
| R-05 | High | Medium | **Resolved 2026-07-26 (RFC-003)** | Suspected reply loop |
| R-06 | High | High | **Partially Resolved 2026-07-28 (RFC-008)** — R-06.1 pickle / R-06.2 unbounded join / R-06.4 parent-child contract done; R-06.3 heartbeat + child-exception IPC still Open | Process-mode pickling + ProcessWorker lifecycle |
| R-07 | High | Medium | Open | BaseException handlers |
| R-11 | Medium | Medium | Open | `_on_connect` `setattr(...None...)` overwrites methods |
| R-13 | Medium | High | **Resolved 2026-07-26 (RFC-002)** | publish result discarded |
| R-14 | Medium | Medium | **Partially Resolved 2026-07-27 (RFC-007)** — `__topic_handlers` done; `_children` / `_parents` open | Shared dicts without locks |
| R-15 | Medium | High | Open | `ConfigName` referenced but missing |
| R-16 | Medium | Medium | Open | `from tkinter import N` |
| R-19 | Medium | High | Open | Parcel `version` never validated |
| R-20 | Medium | High | Open | No message metadata for tracing |
| R-25 | Medium | High | Open | Broker config default shape mismatch |
| R-12 | Low | High | Open | Dead signature branch in `_activate` |
| R-17 | Low | High | Open | `dict[str, function]` |
| R-21 | Low | High | Open | `wrapper.py` `VERSION` undefined |
| R-22 | Low | High | Open | Stub / broken broker implementations |
| R-23 | Low | High | Open | README / directory drift |
| R-24 | Low | Medium | Open | Unexplained sleep in `_on_connect` |
