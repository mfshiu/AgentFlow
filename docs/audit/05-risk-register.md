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
  - **Concurrent same-`topic_wait` race** — two `publish_sync` calls using the same explicit `topic_wait` still collide (`Agent.subscribe` silently overwrites). The new identity guard prevents this fix from making the collision *worse*, but does not resolve the underlying race. Tracked for a future RFC.

---

## R-03 — MQTT reconnect and subscription recovery are not implemented

- **Severity**: High
- **Category**: Fault Isolation / Message Reliability
- **File / Function / Line**:
  - `src/agentflow/broker/mqtt_broker.py:17–18` (`reconnect_on_failure=False`)
  - `src/agentflow/broker/mqtt_broker.py:52–53` (`_on_disconnect` only logs)
  - `src/agentflow/core/agent.py:509–512` (`_on_connect` early-return via `_connected_once`)
- **Trigger**: MQTT broker restart or transient network loss.
- **Impact**: Even if paho reconnected, `Agent._on_connect` would refuse to re-subscribe. Agent silently becomes deaf; parent/child registration cannot be re-established.
- **Confidence**: High
- **Recommended verification test**: Use toxiproxy to sever the MQTT TCP connection for 3 seconds, restore it, and verify whether messages published to previously-subscribed topics still arrive at the agent.

---

## R-04 — Per-message unbounded thread creation

- **Severity**: High
- **Category**: Concurrency / Resource
- **File / Function / Line**: `src/agentflow/core/agent.py:562` inside `Agent._on_message`
- **Evidence**: `threading.Thread(target=handle_message, args=(topic_handler, topic, pcl)).start()`
- **Trigger**: High-rate message stream (single burst or sustained load).
- **Impact**: Thread count grows unbounded; program exit may block on non-daemon threads; native-thread limits may be reached.
- **Confidence**: High
- **Recommended verification test**: Publish 10k messages at high rate to a single subscribed topic; monitor `threading.active_count()` and process behaviour on shutdown.

---

## R-05 — Suspected reply loop on error paths and on default handlers

- **Severity**: High
- **Category**: Correctness / Message Reliability
- **File / Function / Line**: `src/agentflow/core/agent.py:543–555` in `_on_message.handle_message`; `parcel.py:95–107` (`topic_return` survives round-trip via `_get_managed_data` / `_set_managed_data`)
- **Evidence**:
  - The dispatch always calls `self.publish(pcl.topic_return, data_resp)` when `p.topic_return` is truthy.
  - On exception, `data_resp = p` — and `p.topic_return` is still set → the reply parcel itself has `topic_return`, potentially triggering another cycle at the requester side where `handle_response` returns `None` and re-enters the same branch.
- **Trigger**: Any `publish_sync` where the responder handler raises, or where the requester's `handle_response` closure allows re-entry.
- **Impact**: Infinite reply loop, broker flood.
- **Confidence**: Medium — depends on whether the broker echoes self-published messages (MQTT usually does).
- **Recommended verification test**: Run a scripted `publish_sync` where the responder raises; observe broker traffic on the return topic.

---

## R-06 — Process-mode pickling of Agent + Worker + Process handle

- **Severity**: High
- **Category**: Process / Correctness
- **File / Function / Line**: `src/agentflow/core/agent_worker.py:56–65`, `ProcessWorker.start`
- **Evidence**: `multiprocessing.Process(target=self.initiator_agent._activate, args=(cfg,))` requires pickling the bound method → the Agent → its `_agent_worker` → the `work_process` field it just set.
- **Trigger**: Default configuration (`CONCURRENCY_TYPE='process'`, `agent.py:75–76`) combined with any user callback that is a closure or a lambda (e.g., `unit_test/test_parcel.py:57` places a closure into `EventHandler.ON_CONNECTED`).
- **Impact**: `spawn` fails with `AttributeError: Can't pickle local object` or `TypeError: cannot pickle …`.
- **Confidence**: Medium — the Agent-worker cycle may or may not be resolvable by pickle; not empirically tested.
- **Recommended verification test**:
  ```python
  Agent(name='x', agent_config={
      'broker_type': 'empty',
      EventHandler.ON_ACTIVATE: lambda: None,
  }).start()  # defaults to process
  ```
  observe whether `start()` succeeds and whether the child process runs `_activate`.

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

- **Severity**: High
- **Category**: Fault Isolation / Resource
- **File / Function / Line**:
  - `src/agentflow/core/agent_worker.py:75` `ProcessWorker.stop`
  - `src/agentflow/core/agent_worker.py:110` `ThreadWorker.stop`
- **Trigger**: Any handler that blocks (deadlock, sleep, blocking I/O).
- **Impact**: `Agent.terminate()` blocks the caller forever.
- **Confidence**: High
- **Recommended verification test**: Register an `on_message` handler that runs `while True: pass`; publish one message; call `agent.terminate()`; assert it returns within N seconds.

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

- **Severity**: Medium
- **Category**: Message Reliability / Observability
- **File / Function / Line**:
  - `src/agentflow/broker/mqtt_broker.py:106–107` `MqttBroker.publish` returns paho `MessageInfo`
  - `src/agentflow/core/agent.py:305–314` `Agent.publish` returns `None` unconditionally
- **Trigger**: Broker overload; disconnected client; QoS mismatch.
- **Impact**: Caller cannot distinguish success from failure.
- **Confidence**: High
- **Recommended verification test**: Mock paho `client.publish` to return `MessageInfo(rc=1, mid=…)`; verify caller has no way to observe this.

---

## R-14 — Shared dictionaries without locks

- **Severity**: Medium
- **Category**: Concurrency
- **File / Function / Line**:
  - `__topic_handlers` — `agent.py:45, 360–362, 541`
  - `_children` — `agent.py:42, 369`
  - `_parents` — `agent.py:42, 380`
- **Trigger**: Concurrent subscribe and message delivery, or concurrent parent/child registrations.
- **Impact**: Compound TOCTOU (`if topic in d: warn; d[topic] = h`) can lose a warning or overwrite unexpectedly; rare visibility issues.
- **Confidence**: Medium — dict single-op atomicity mitigates many cases but not all.
- **Recommended verification test**: Stress test with N threads registering distinct then colliding topics; observe warning counts vs actual final state.

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
| R-03 | High | High | Open | No MQTT reconnect / no re-subscribe |
| R-04 | High | High | Open | Unbounded per-message thread creation |
| R-08 | High | High | Open | Parent-process `publish` silently fails in process mode |
| R-09 | High | High | Open | Topic derived from unsanitised `Agent.name`; naming collisions |
| R-10 | High | High | Open | `Worker.stop()` join without timeout |
| R-18 | High | High | Open | No child/parent unregister / heartbeat |
| R-05 | High | Medium | Open | Suspected reply loop |
| R-06 | High | Medium | Open | Process-mode pickling |
| R-07 | High | Medium | Open | BaseException handlers |
| R-11 | Medium | Medium | Open | `_on_connect` `setattr(...None...)` overwrites methods |
| R-13 | Medium | High | Open | publish result discarded |
| R-14 | Medium | Medium | Open | Shared dicts without locks |
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
