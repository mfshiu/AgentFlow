# 02 — Runtime Message Flow

**Scope**: How a message travels from `Agent.publish` to a subscriber's handler, and how `publish_sync` layers a synchronous request/response on top of that.
**Rule**: Analysis only.

---

## 2.1 High-level component pipeline

```mermaid
flowchart LR
  user[User code<br/>agent.publish or agent.publish_sync] --> agent_pub[Agent.publish<br/>agent.py:305]
  agent_pub --> parcel_wrap[Parcel.from_content<br/>parcel.py:77]
  parcel_wrap --> broker_pub[MessageBroker.publish<br/>broker impl]
  broker_pub -->|paho| mqtt_wire[(MQTT wire)]

  mqtt_wire -->|paho loop thread| broker_recv[MqttBroker._on_message<br/>mqtt_broker.py:56]
  broker_recv --> agent_recv[Agent._on_message<br/>agent.py:536]
  agent_recv --> parcel_unwrap[Parcel.from_payload<br/>parcel.py:84]
  agent_recv --> lookup[Look up handler in<br/>__topic_handlers or on_message<br/>agent.py:541]
  agent_recv --> spawn_thread[threading.Thread<br/>handle_message<br/>agent.py:562]
  spawn_thread --> handler[topic_handler topic, pcl]
  handler -->|if pcl.topic_return| agent_pub
```

---

## 2.2 Publish

`Agent.publish` (`src/agentflow/core/agent.py:304–314`):

```python
@final
def publish(self, topic, data=None):
    pcl = data if isinstance(data, Parcel) else Parcel.from_content(data)
    try:
        if self._broker:
            self._broker.publish(topic, pcl.payload())
        else:
            logger.error("Cannot publish: _broker is None.")
    except Exception as ex:
        logger.exception(ex)
```

Observed behaviour:
- Return value is always `None`. paho's `MessageInfo` returned by `MqttBroker.publish` (`mqtt_broker.py:106–107`) is discarded. Fire-and-forget contract is intentional and preserved by [RFC-002](../rfc/RFC-002-publish-error-propagation.md); callers who require raise-on-failure semantics use the internal `Agent._publish_or_raise` (see §2.8).
- If `self._broker` is `None`, the call is silently logged and returns; caller cannot detect this. See Risk R-08.
- Any exception is swallowed via `logger.exception`.

`Parcel.from_content` (`src/agentflow/core/parcel.py:77–81`) chooses:
- `BinaryParcel` if `content` is `bytes` or `bytearray`
- `TextParcel` otherwise

Serialisation:
- `TextParcel.payload()` — `b"text/json|" + utf-8 json` (`parcel.py:179–181`)
- `BinaryParcel.payload()` — `b"application/pickle|" + pickle.dumps(managed_data, HIGHEST_PROTOCOL)` (`parcel.py:155–156`)

The serialized envelope contains `version`, `content`, `topic_return`, `error` (`parcel.py:95–101`).

---

## 2.3 Subscribe

`Agent.subscribe` (`src/agentflow/core/agent.py:352–364`):

```python
@final
def subscribe(self, topic, data_type:str="str", topic_handler=None):
    ...
    if topic_handler:
        if topic in self.__topic_handlers:
            logger.warning(...)
        self.__topic_handlers[topic] = topic_handler
    return self._broker.subscribe(topic, data_type) if self._broker else None
```

Observed:
- Duplicate subscribe on the same **NORMAL-owned** topic still warns and overwrites the handler; no rejection (RFC-006 §7.11 / RFC-007 §7.5 — legitimate rebind).
- `data_type` is a string ("str" by default) and is passed through to `MessageBroker.subscribe`. `MqttBroker.subscribe` ignores it (records it in the RFC-005 registry only). Only `RosNoeticBroker` would use it — but that broker is unregistered.
- ~~`__topic_handlers` is a plain dict with no synchronization~~ — **Post-RFC-007 (2026-07-27)**: `__topic_handlers` values are now `_HandlerRecord(owner_type, handler)` where `owner_type ∈ {NORMAL, PUBLISH_SYNC}`. All mutations and reads happen under `_handlers_lock` (a `threading.RLock` introduced by RFC-006 and generalised by RFC-007). Direct `Agent.subscribe` / `Agent.unsubscribe` on a `PUBLISH_SYNC`-owned topic raise `TopicWaitCollisionError`; `publish_sync` on a `NORMAL`-registered topic also raises. `_on_message` reads `__topic_handlers` under `_handlers_lock` in a single snapshot, closing the pre-RFC-007 TOCTOU. Broker I/O (`broker.subscribe`, `broker.unsubscribe`, `dispatcher.enqueue`, handler invocation) always happens **outside** the lock. See [`docs/audit/05-risk-register.md` R-14](05-risk-register.md#r-14--shared-dictionaries-without-locks) for the full contract, collision table, and lock-hygiene tests. `_children` / `_parents` remain unlocked (R-14 residual, deferred to a future RFC). `Agent.subscribe` / `Agent.unsubscribe` public signatures are unchanged.
- **Broker-side subscription recovery (post-RFC-005, 2026-07-27)**: `MqttBroker.subscribe` now maintains a thread-safe `_registry: dict[topic, data_type]`. When connected, the call forwards to `client.subscribe` as before; when disconnected, only the registry is updated. On the next successful `_on_connect`, the broker snapshots the registry and re-emits `client.subscribe(topic=topic)` for each entry (per-topic try/except; recheck `_stopping` and live-registry membership between iterations). `MqttBroker.unsubscribe` mirrors this: it deletes the registry entry so recovery does not resurrect it. `stop()` sets `_stopping=True` before `client.disconnect()`, so any late `_on_connect` callback observes the flag and skips both recovery and notifier delegation. See [`docs/audit/05-risk-register.md` R-03](05-risk-register.md#r-03--mqtt-reconnect-and-subscription-recovery-are-not-implemented) for the full contract and race analysis; `Agent.subscribe` / `Agent.unsubscribe` public signatures are unchanged.

---

## 2.4 Receive path (MQTT)

paho callback → `MqttBroker._on_message` (`mqtt_broker.py:56–60`):

```python
def _on_message(self, client, db, message):
    try:
        self._notifier._on_message(message.topic, message.payload)
    except Exception as ex:
        logger.exception(ex)
```

This is the correct boundary — a raised handler exception does not kill the paho loop thread.

`Agent._on_message` (`src/agentflow/core/agent.py:536–562`):

```python
@final
def _on_message(self, topic:str, data):
    pcl = Parcel.from_payload(data)
    topic_handler = self.__topic_handlers.get(topic, self.on_message)

    def handle_message(topic_handler, topic, p:Parcel):
        if p.topic_return:
            try:
                data_resp = topic_handler(topic, p)
            except Exception as ex:
                logger.exception(ex)
                p.error = str(ex)
                p.content = None
                data_resp = p
            finally:
                self.publish(pcl.topic_return, data_resp)
        else:
            try:
                topic_handler(topic, p)
            except Exception as ex:
                logger.exception(ex)

    threading.Thread(target=handle_message, args=(topic_handler, topic, pcl)).start()
```

Observed:
- ~~One new `threading.Thread` per received message, no daemon flag, no pool, no upper bound~~ — **RESOLVED 2026-07-26 (RFC-004)**. `_on_message` now enqueues `handle_message` onto a bounded `MessageDispatcher` (`src/agentflow/core/dispatcher.py`) drained by a fixed pool of **daemon** consumer threads (default `workers=8`, `queue_capacity=1024`, overflow policy `drop_newest`). `enqueue()` is linearized under `_state_lock`; the paho callback thread never sees `queue.Full` (RFC-004 §7.3 broker-callback safety invariant). See [`docs/audit/05-risk-register.md` R-04](05-risk-register.md#r-04--per-message-unbounded-thread-creation) for the full contract, race fixes (enqueue check-then-put linearization + concurrent-stop `_stop_complete_event`), and runtime evidence.
- `Parcel.from_payload` raises `TypeError` on unknown HEAD (`parcel.py:89`); the raise happens on the broker's paho loop thread, then it is caught by `MqttBroker._on_message`.
- **BinaryParcel triggers `pickle.loads` on wire bytes** (`parcel.py:146`) → Risk R-01.
- Auto-reply eligibility (**post-RFC-003, 2026-07-26**): an auto-reply is emitted only when both (a) the incoming `pcl.topic_return` is truthy AND (b) the dispatched handler was a **specifically registered** entry in `__topic_handlers` (not the fall-through to `on_message`). See [RFC-003](../rfc/RFC-003-auto-reply-contract.md) and R-05 in the risk register. The pre-RFC-003 behaviour where a fall-through `on_message` also generated an implicit reply is no longer the case.

---

## 2.5 End-to-end sequence — one-shot publish

```mermaid
sequenceDiagram
    autonumber
    participant U as User code
    participant A as Agent (publisher)
    participant BR as MqttBroker
    participant Mqtt as MQTT wire
    participant BR2 as MqttBroker (subscriber)
    participant A2 as Agent (subscriber)
    participant H as topic_handler

    U->>A: publish(topic, data)
    A->>A: Parcel.from_content(data)
    A->>BR: broker.publish(topic, pcl.payload())
    BR->>Mqtt: MQTT PUBLISH
    Mqtt-->>BR2: MQTT deliver (paho loop thread)
    BR2->>A2: _on_message(topic, payload)
    A2->>A2: Parcel.from_payload(payload)
    A2->>A2: __topic_handlers.get(topic, on_message)
    A2->>A2: threading.Thread(handle_message).start()
    A2->>H: handler(topic, pcl)
    alt pcl.topic_return is set
        H-->>A2: return data_resp (may be None)
        A2->>BR2: publish(topic_return, data_resp)
    else no topic_return
        H-->>A2: return (ignored)
    end
```

---

## 2.6 Topic naming rules used by Agent

Extracted from `src/agentflow/core/agent.py`:

| Purpose | Topic template | Line |
|---|---|---|
| Any child → any parent with this name | `to_parent.{self.name}` | 519 |
| A specific parent (targeted) | `{agent_id}.to_parent.{self.name}` | 520 |
| A parent → all its child-name group | `to_child.{self.name}` | 462 (default in `_notify_children`) |
| A parent → children of a specific name | `to_child.{target_child_name}` | 475 |
| A parent → one specific child | `{child_id}.to_child.{self.name}` | 447 |
| A child subscribes: all-parents-of-me | `to_child.{self.parent_name}` | 524 |
| A child subscribes: same-name siblings group | `to_child.{self.name}` | 525 |
| A child subscribes: me-only | `{agent_id}.to_child.{self.parent_name}` | 526 |
| Sync return topic | `{tag}-{10 rand alnum}/{topic}` | 316–319 |

`self.tag` is the first 4 hex chars of `agent_id` (`agent.py:34`). `self.parent_name` is derived by `name.split('.', 1)[1] if '.' in name else None` (`agent.py:37`).

Observations:
- Because subscription uses **agent name** (user-provided), two independently constructed agents that happen to share a name receive each other's parent/child messages. This is by design in `unit_test/test_parents_children_count.py`, but is a **cross-org collision** in any deployment sharing an MQTT broker. See Risk R-09.
- Return topic uses `/` as separator (`agent.py:319`), while all other topics use `.`. If the underlying original `topic` contains `+`, `#`, or `/`, MQTT wildcard semantics take over. No topic sanitisation exists. See Risk R-09.

---

## 2.7 Sync request/response — `publish_sync`

> **Update (2026-07-26)**: sections 2.7's "Explicit lifecycle gaps" #1 and #2 were **Resolved by [RFC-001](../rfc/RFC-001-publish-sync-subscription-lifecycle.md)** — see [`docs/audit/05-risk-register.md` R-02](05-risk-register.md#r-02--publish_sync-leaks-handler-entries-and-broker-subscriptions). Gaps #3 and #4 remain open. The sequence diagram and narrative below have been updated to reflect the resolved cleanup path.

`Agent.publish_sync` (`src/agentflow/core/agent.py:321–353`):

```mermaid
sequenceDiagram
    autonumber
    participant Caller as Caller code
    participant AS as Agent (requester)
    participant BR as MqttBroker (requester)
    participant AR as Agent (responder)
    participant HR as Response handler
    Note over AS: publish_sync(topic, data, topic_wait=None, timeout=30)
    AS->>AS: generate return_topic = tag-<10 alnum>/topic
    AS->>AS: create DataEvent(worker.create_event())
    AS->>AS: subscribe(return_topic, handle_response)<br/>__topic_handlers[return_topic] = handler
    rect rgba(220, 245, 220, 0.5)
      Note over AS: try:
      AS->>BR: publish(topic, pcl with topic_return)
      BR-->>AR: _on_message(topic, payload)
      AR->>AR: handle_message thread<br/>topic_handler → business logic
      AR->>BR: publish(return_topic, data_resp)
      BR-->>AS: _on_message(return_topic, payload)
      AS->>HR: handle_response(topic, pcl_resp)
      HR->>HR: if data_event.event.is_set(): return<br/>data_event.data = pcl_resp<br/>data_event.event.set()
      AS->>Caller: return data_event.data
    end
    rect rgba(255, 235, 220, 0.7)
      Note over AS: finally:
      AS->>AS: if __topic_handlers[return_topic] is handle_response:
      AS->>AS: __topic_handlers.pop(return_topic)
      AS->>BR: broker.unsubscribe(return_topic)
    end
```

### Explicit lifecycle gaps

1. ~~**No handler cleanup**~~ — **RESOLVED 2026-07-26 (RFC-001).** `publish_sync` now runs an identity-guarded `finally` that pops `__topic_handlers[return_topic]` when it is still the specific `handle_response` this call installed. Verified by `tests/unit/core/test_agent_publish_sync.py::test_topic_handlers_cleaned_after_successful_publish_sync` and `_timed_out_publish_sync`.
2. ~~**No broker unsubscribe**~~ — **RESOLVED 2026-07-26 (RFC-001).** `MessageBroker` now declares `unsubscribe(topic) -> None` as a non-abstract default no-op; `MqttBroker.unsubscribe` delegates to `self._client.unsubscribe(topic)`. Verified by `tests/unit/core/test_agent_publish_sync.py::test_broker_subscribe_and_unsubscribe_grow_together_on_success` and `_on_timeout`, plus `tests/unit/test_mqtt_broker_lifecycle.py::test_unsubscribe_delegates_topic_to_client`.
3. **No correlation id** (still open): return topic is the only demultiplexer. Concurrent requests to the same `topic` with `topic_wait=None` get distinct return topics via random suffix (10 base-36 chars → ~40 bits); with `topic_wait` reused explicitly, they still race — `Agent.subscribe` silently overwrites. RFC-001 added an identity guard so the cleanup path does not make this race worse, but the race itself is unresolved.
4. **Timeout only on `event.wait`** (still open): publish/subscribe I/O has no timeout.

### Post-resolution observable invariants

For every `publish_sync` call, regardless of exit path (success, `TimeoutError`, or broker-`publish` exception):

- `return_topic not in agent._Agent__topic_handlers`
- `broker.unsubscribe_calls` grew by exactly one entry equal to `return_topic`
- Any late or duplicate response arriving after cleanup is routed to the default `Agent.on_message` (no-op) via fall-through, not to the completed `handle_response`.

The one exception is the **identity-guard bypass**: if a foreign handler races onto the same topic between `subscribe` and `finally`, the guard skips both the pop and the unsubscribe. In that case the foreign handler is preserved and the (already lost) subscription is not torn down. Verified by `test_identity_guard_preserves_foreign_handler_on_same_topic`.

### Reply loop — RESOLVED 2026-07-26 (RFC-003)

> **Update (2026-07-26)**: the suspected reply loop was **confirmed by runtime evidence** and then **Resolved by [RFC-003](../rfc/RFC-003-auto-reply-contract.md)** — see [`docs/audit/05-risk-register.md` R-05](05-risk-register.md#r-05--suspected-reply-loop-on-error-paths-and-on-default-handlers).

Three loop patterns were runtime-confirmed under a bounded self-echo broker in `tests/unit/core/test_agent_reply_behavior.py`:

1. **Handler exception + self-echo**: on exception, `agent.py` used to set `data_resp = p` with `p.topic_return` preserved. The echoed error re-arrived and re-raised → loop (previously bounded at 20 dispatches; now terminates in ≤ 3).
2. **Handler returns Parcel-with-topic_return + self-echo**: the reply parcel preserved `topic_return` on the wire → re-arrival triggered another auto-reply → loop (previously bounded at 20; now ≤ 3).
3. **Two-agent mutual reply**: each side's handler produced a Parcel-with-topic_return targeted at the other's topic → ping-pong (previously bounded at 30 via a hub broker; now ≤ 3).

Post-RFC-003 dispatch (`agent.py:_on_message`, `@final`, public signature unchanged) enforces three rules that break every one of these patterns:

- **R-fallback-silent**: auto-reply is emitted only when `topic in self.__topic_handlers` (i.e. a specifically registered handler) AND `pcl.topic_return` is truthy. A subscribed-but-unhandled topic no longer generates implicit replies.
- **R-strip-topic_return**: if `data_resp` is a `Parcel` whose `topic_return` is truthy, the framework reconstructs a fresh parcel of the same subclass (`type(data_resp)(data_resp.content)`), copies `.error`, and uses it as the reply. The handler's original returned object is not mutated.
- **R-exception-fresh**: on handler exception the reply is a brand-new `Parcel.from_content(None)` with `.error = str(ex)`. The incoming `p` is not mutated; handlers holding a reference to `p` observe its original `content`, `error`, and `topic_return`.

`publish_sync` is unaffected: `handle_response` returns `None`, so `Parcel.from_content(None)` produces a reply with `topic_return=None` and R-strip-topic_return is a no-op for the request-response path. `publish_sync`'s specific handler on `return_topic` also prevents R-fallback-silent from ever applying during its lifetime. Verified by `test_publish_sync_happy_path_still_works_under_RFC_003` and `test_publish_sync_late_reply_after_cleanup_is_silently_dropped`.

---

## 2.8 Publish result / error propagation

> **Update (2026-07-26)**: `Agent.publish` → `publish_sync` masking layer was **Resolved by [RFC-002](../rfc/RFC-002-publish-error-propagation.md)** — see [`docs/audit/05-risk-register.md` R-13](05-risk-register.md#r-13--publish-result-is-discarded-at-every-layer). The paho `MessageInfo` (rc/mid) at the broker adapter layer is still discarded; that residual observability gap is deferred to a future RFC on broker-level observability.

| Layer | Failure signal | Propagation (post-RFC-002) |
|---|---|---|
| paho `client.publish` | Returns `MessageInfo(rc, mid)` | Still discarded by `MqttBroker.publish` (out of RFC-002 scope). |
| `MqttBroker.publish` | Returns whatever paho returned | Still ignored by `Agent.publish` / `Agent._publish_or_raise` (out of RFC-002 scope). Broker-raised exceptions, however, do propagate. |
| `Agent._publish_or_raise` (new, internal) | Raises broker exceptions unchanged; raises `RuntimeError("Cannot publish: no broker attached")` when `_broker is None` | Reaches its direct caller (currently `publish_sync`; available to any internal caller that needs raise-on-fail semantics) |
| `Agent.publish` | Wraps `_publish_or_raise` in `try/except Exception: logger.exception(...)` | **Unchanged**: fire-and-forget, returns `None` on every outcome. Preserves the pre-RFC-002 contract byte-for-byte. |
| `Agent.publish_sync` | Calls `_publish_or_raise`; broker exceptions propagate through the RFC-001 `try/finally` (cleanup still runs) | **Original broker exception object** propagates to the caller (same type, message, traceback). True-timeout case (broker accepted the publish but no response) continues to raise `TimeoutError`. |

**Consequence**:
- Fire-and-forget publish (`Agent.publish`) callers see zero behavioural change — success and every failure still return `None`.
- `publish_sync` callers now fast-fail on publish errors with the original exception; only genuine "no response in time" produces `TimeoutError`.
- The residual broker-side observability gap (paho `MessageInfo` rc/mid) is documented and deferred.

Verified by `tests/unit/core/test_agent_publish_errors.py` (46 tests) and the four R-02 crossover tests in `tests/unit/core/test_agent_publish_sync.py`.

---

## 2.9 Process-mode worker lifecycle (post-RFC-008, 2026-07-28)

> **Update (2026-07-28)**: `ProcessWorker` is fully functional under `CONCURRENCY_TYPE='process'`. Agent is pickle-safe via `__getstate__` / `__setstate__`; `ProcessWorker` runs on a `WorkerState` machine with a bounded stop-escalation ladder. See [RFC-008](../rfc/RFC-008-process-worker-lifecycle.md) and [`docs/audit/05-risk-register.md` R-06](05-risk-register.md#r-06--process-mode-pickling-of-agent--processworker-lifecycle).

### Parent-side Agent contract

Under `CONCURRENCY_TYPE='process'`, the parent-side `Agent` instance is a **lifecycle controller stub**:

- Effective runtime state — `_broker`, `_dispatcher`, `__topic_handlers` dispatch — lives in the **child** process, established by `_activate` after unpickling.
- Parent-side `_broker` / `_dispatcher` remain `None`; parent-side `_children` / `_parents` remain `{}`. Parent-side `Agent.publish` / `subscribe` / `publish_sync` are **not proxied** to the child — they operate on empty local state and produce no broker traffic (per R-02 + RFC-002 `_broker is None` fast-fail).
- Callers that need to interact with the running child Agent must construct a **separate Agent** that connects to the same broker (from the same or a different process).

A transparent parent-side publish/subscribe proxy is explicitly out of RFC-008 scope; deferred to a future RFC that would introduce an IPC-based proxy or a `ProcessDispatcher`.

### Pickle contract (Agent)

`Agent.__getstate__` / `__setstate__` (`src/agentflow/core/agent.py`) implement the pickle protocol:

- **Runtime-only fields excluded** from `__getstate__`: `_handlers_lock`, `_dispatcher_init_lock`, `_dispatcher`, `_broker`, `_agent_worker`, `_message_broker`, `_children`, `_parents`.
- **Picklability probe** in `__getstate__`: `config` as a whole; every `_HandlerRecord.handler` in `__topic_handlers` individually. On failure, raises `TypeError` naming the offending topic (or `Agent.config`) and suggests registering the handler / binding the callback inside `on_activate()` (which runs in the child, avoiding the pickle boundary).
- `__setstate__` reinstates every runtime-only field fresh in the child: new `threading.RLock` × 2, `None` for the broker / dispatcher / worker back-references, empty `{}` for `_children` / `_parents`.
- The RFC-006 / RFC-007 `_HandlerRecord(owner_type, handler)` shape survives pickle intact; only the guarding `_handlers_lock` is rebuilt.

### ProcessWorker state machine

`WorkerState(Enum)` = `NEW → STARTING → RUNNING → STOPPING → STOPPED` (or `NEW → STARTING → START_FAILED` on start error). All transitions under `_state_lock: threading.RLock`. Read-only `state` / `exitcode` properties.

```mermaid
stateDiagram-v2
    [*] --> NEW
    NEW --> STARTING: start()
    STARTING --> RUNNING: Process.start() OK
    STARTING --> START_FAILED: pickle / spawn error<br/>→ _cleanup_after_start_failure
    RUNNING --> STOPPING: stop() (first caller)
    STOPPING --> STOPPED: escalation ladder complete<br/>_exitcode cached<br/>_stop_complete_event.set()
    STOPPED --> STOPPED: stop() idempotent replay
    NEW --> NEW: stop() (no-op — subsequent start() still allowed)
    START_FAILED --> START_FAILED: stop() (no-op)
    RUNNING --> RUNNING: start() raises RuntimeError
    STOPPING --> STOPPING: concurrent stop() waits on _stop_complete_event<br/>returns same _exitcode
```

Key implementation notes:

- **`agent.config` is NOT mutated**: `start()` builds `child_config = dict(self.initiator_agent.config)` locally and puts the `work_queue` reference on the copy. `Process(daemon=False)`.
- **`stop()` before `start()`** is a pure no-op; state stays `NEW` so a subsequent `start()` is still allowed.
- **Restart is not supported**: `start()` after any of `STOPPING` / `STOPPED` / `START_FAILED` raises `RuntimeError("ProcessWorker cannot be restarted … construct a fresh worker")`.

### Shutdown escalation ladder

`stop(graceful_timeout_s=5.0, terminate_timeout_s=2.0, kill_timeout_s=1.0) -> Optional[int]`:

1. **Cooperative** — `send_data('terminate')` on the child's work queue.
2. `join(graceful_timeout_s)`.
3. If alive → **`Process.terminate()`** (SIGTERM on POSIX / TerminateProcess on Windows).
4. `join(terminate_timeout_s)`.
5. If alive → **`Process.kill()`** (SIGKILL / TerminateProcess with force).
6. `join(kill_timeout_s)`. If still alive after this, log ERROR and abandon.

Total wall time strictly bounded by `graceful + terminate + kill = 8.0s` at defaults; per-call configurable.

**Idempotence**: `STOPPED` state → cached-exitcode replay.
**Concurrent callers**: only the first caller executes the escalation; others block on `_stop_complete_event` and return the same cached exitcode. `finally` sets `STOPPED` and signals the event **even if the escalation body raised** — so concurrent waiters never hang.

### End-to-end sequence — process-mode start / stop

```mermaid
sequenceDiagram
    autonumber
    participant P as Parent process
    participant PW as ProcessWorker (parent)
    participant Py as multiprocessing.Process
    participant C as Child process
    participant CA as Child-side Agent
    P->>PW: pw = ProcessWorker(agent)
    P->>PW: pw.start()
    PW->>PW: state NEW → STARTING (under _state_lock)
    PW->>PW: child_config = dict(agent.config)<br/>child_config['work_queue'] = mp.Queue()
    PW->>Py: mp.Process(target=agent._activate, args=(child_config,))
    Py->>Py: pickle(target + args)<br/>→ Agent.__getstate__<br/>(probe handlers + config)
    Py->>C: fork/spawn
    C->>CA: Agent.__setstate__<br/>fresh locks, None broker, empty _children/_parents
    C->>CA: _activate(child_config)<br/>→ broker init, on_activate, work-queue loop
    PW->>PW: state STARTING → RUNNING
    Note over P,CA: agent.config in parent is unchanged (child_config was a copy)
    Note over P,CA: parent Agent._broker / _dispatcher stay None
    P->>PW: pw.stop(5.0, 2.0, 1.0)
    PW->>PW: state RUNNING → STOPPING
    PW->>C: send 'terminate' via work_queue
    PW->>PW: join(5.0)
    alt child cooperates
        C-->>Py: exit(0)
        PW->>PW: cache _exitcode=0
    else child ignores 'terminate'
        PW->>Py: Process.terminate() → SIGTERM
        PW->>PW: join(2.0)
        alt child ignores SIGTERM
            PW->>Py: Process.kill() → SIGKILL
            PW->>PW: join(1.0)
        end
    end
    PW->>PW: state STOPPING → STOPPED<br/>_stop_complete_event.set()
    PW-->>P: return _exitcode
```

`stop()` runtime evidence: `test_stop_returns_exitcode_zero_when_child_cooperates` (cooperative), `test_stop_escalates_to_terminate_when_child_ignores_terminate_message` (SIGTERM path), `test_stop_escalates_to_kill_when_child_ignores_sigterm` (SIGKILL path), `test_concurrent_stop_all_callers_return_same_result_single_escalation` (concurrent coordination), `test_no_orphan_process_after_stop` (`os.kill(pid, 0) → ProcessLookupError`).

### R-06 sub-risk status

| Sub-risk | Status |
|---|---|
| R-06.1 spawn pickle failure | **Resolved** by pickle protocol above |
| R-06.2 unbounded `Process.join()` | **Resolved** by escalation ladder |
| R-06.3 heartbeat / liveness / automatic restart | **Deferred / Open** (out of RFC-008 scope) |
| R-06.4 parent-child state divergence | **Documented architectural constraint** (§ above) |
| Child exception forwarding | **Deferred / Open** — parent observes only exit code |

---

## 2.10 Unknowns

- ~~**U-2.1**: Actual behaviour of the suspected reply loop in Risk R-05~~ — **Resolved**: reproduced against a bounded self-echo FakeBroker in `tests/unit/core/test_agent_reply_behavior.py`, then fixed by RFC-003 (§2.7 update above).
- **U-2.2**: Behaviour of MQTT topics containing `.` under paho v2 — assumed to be a normal character (only `+ # / $` are special), but not empirically tested against a broker.
- **U-2.3**: Whether `handle_response` ever sees its own publish echoed back — depends on broker semantics (MQTT typically does deliver self-published messages).
