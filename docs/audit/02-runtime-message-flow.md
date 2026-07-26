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
- Return value is always `None`. paho's `MessageInfo` returned by `MqttBroker.publish` (`mqtt_broker.py:106–107`) is discarded — see Risk R-13.
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
- Duplicate subscribe on the same topic only warns and overwrites the handler; no rejection.
- `data_type` is a string ("str" by default) and is passed through to `MessageBroker.subscribe`. `MqttBroker.subscribe` ignores it (`mqtt_broker.py:109–110`). Only `RosNoeticBroker` uses it — but that broker is unregistered.
- `__topic_handlers` is a plain dict (`agent.py:45`) with no synchronization — see Risk R-14.

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
- One new `threading.Thread` per received message, no daemon flag, no pool, no upper bound → Risk R-04.
- `Parcel.from_payload` raises `TypeError` on unknown HEAD (`parcel.py:89`); the raise happens on the broker's paho loop thread, then it is caught by `MqttBroker._on_message`.
- **BinaryParcel triggers `pickle.loads` on wire bytes** (`parcel.py:146`) → Risk R-01.
- If the handler returns `None` (typical for `on_message` overrides) and `p.topic_return` is set, `data_resp = None` is published to `topic_return`. That means any parcel that arrives with a `topic_return` set will cause a reply — even if the receiver has no intention of replying. See Risk R-05.

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

### Suspected reply loop (Confidence: Medium — not verified)

`handle_response` (`agent.py:338–341`) returns `None`. The parcel received on `return_topic` still carries `pcl.topic_return` = the same `return_topic` (round-tripped through `_get_managed_data` / `_set_managed_data`, `parcel.py:95–107`). In `_on_message` (`agent.py:543–555`), whenever `p.topic_return` is truthy, the framework publishes `data_resp` back to `p.topic_return`. So:

```
Requester subscribes return_topic
Responder publishes to return_topic (topic_return echoed in parcel)
Requester._on_message receives → handle_response returns None → data_resp = None
Requester publishes None to return_topic (because topic_return is still set on the parcel it just received)
Requester._on_message receives its own publish → handle_response returns None → publishes again
… loop
```

**Whether this loop actually triggers depends on**:
- Whether the responder actually copies `topic_return` into the response parcel it emits. In current code the request-side sets `pcl.topic_return` before publish; the responder is expected to *reply on that topic*, not to re-emit a parcel with that topic set. But the current dispatch code (`agent.py:555`) does `self.publish(pcl.topic_return, data_resp)` where `data_resp` may be `pcl` itself (line 552 sets `data_resp = p` on exception), which still has `topic_return` set. So on error paths at least, the round-trip loop is highly plausible.

Filed as Risk R-05 with Confidence Medium and a specific reproduction plan.

---

## 2.8 Publish result / error propagation

| Layer | Failure signal | Propagation |
|---|---|---|
| paho `client.publish` | Returns `MessageInfo(rc, mid)` | Discarded by `MqttBroker.publish` (`mqtt_broker.py:106–107`) |
| `MqttBroker.publish` | Returns whatever paho returned | Ignored by `Agent.publish` (`agent.py:309`) |
| `Agent.publish` | Wraps in try/except → logs | Caller receives `None` in all cases |
| `Agent.publish_sync` | Wait on `Event` | Timeout → `TimeoutError`; broker-side failure is invisible |

**Consequence**: no code path can observe a lost publish. See Risk R-13.

---

## 2.9 Unknowns

- **U-2.1**: Actual behaviour of the suspected reply loop in Risk R-05 — needs a scripted reproduction with a real MQTT broker.
- **U-2.2**: Behaviour of MQTT topics containing `.` under paho v2 — assumed to be a normal character (only `+ # / $` are special), but not empirically tested against a broker.
- **U-2.3**: Whether `handle_response` ever sees its own publish echoed back — depends on broker semantics (MQTT typically does deliver self-published messages).
