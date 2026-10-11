# RFC-015 — Delivery guarantees: QoS, message ids, reported losses

- **Status**: **Implemented** (2026-10-12, issue #4)
- **Author**: Audit follow-up
- **Depends on**:
  - [RFC-002](RFC-002-publish-error-propagation.md) (publish errors) and [RFC-012](RFC-012-mqtt-publish-result-contract.md) (MQTT publish result);
  - [RFC-004](RFC-004-bounded-message-dispatch.md) (bounded dispatcher) and [RFC-005](RFC-005-mqtt-subscription-recovery.md) (resubscribe);
  - [RFC-014](RFC-014-request-reply-correlation.md) (correlation ids).
- **Scope**:
  - MQTT QoS / retain on the broker interface.
  - QoS 1 for holon control and `publish_sync` traffic.
  - A message id on every message, and deduplication on receipt.
  - `Agent.publish` reporting its outcome.
  - An error reply for requests the dispatcher drops.
- **Explicitly out of scope**:
  - Persistent sessions and stable client ids (#17), without which the broker does not keep messages for a disconnected subscriber.
  - Bounding the outbound buffer (#35).
  - Decode-failure observability (#15).
  - Retained / last-will messages and membership expiry (R-18).
  - The envelope redesign (P2-1).

---

## 1. Problem

Every publish and subscribe used QoS 0, retain=False and a clean session, so a message in flight when a connection dropped was lost. That included holon control messages and RPC requests and replies.

The envelope had no message id, so nothing could recognise a message delivered twice. That ruled out any at-least-once delivery (CLAUDE.md invariant 3).

Two loss points were silent:
- `Agent.publish` swallowed broker errors and returned `None`.
- A request the dispatcher dropped (queue full) left its requester waiting for its whole timeout.

## 2. Design

### 2.1 Broker interface

```python
MessageBroker.publish(topic, payload, *, qos: int = 0, retain: bool = False)
MessageBroker.subscribe(topic, data_type, *, qos: int = 0)
```

- **MqttBroker** hands `qos` and `retain` to paho.
  - With the defaults, the paho call keeps its pre-RFC-015 shape (RFC-012 pins it).
  - `check_qos()` refuses anything but the ints 0, 1 and 2 with `ValueError`, before the RFC-012 gate.
  - The RFC-005 registry maps `topic -> (data_type, qos)`, so the resubscribe after a reconnect keeps each topic's QoS. Recovery reads each topic's live entry, so a `subscribe()` made while it runs is not undone.
  - For QoS ≥ 1, paho's `MQTT_ERR_NO_CONN` (socket already gone, disconnect not reported yet) counts as accepted: paho keeps the message and sends it after the reconnect.
- **Other brokers**: Empty, Redis, ROS, ROS-Noetic and DDS accept the keywords and ignore them. Brokers are a closed set (`BrokerMaker`).

### 2.2 Which traffic uses QoS 1

| Traffic | Publish QoS | Subscription QoS |
|---|---|---|
| Holon control messages (`register_child`, `register_parent`, `parent_online`, `terminate`) and other holon messages | 1 | 1 (`to_parent.*`, `to_child.*` and their `{agent_id}.` forms) |
| `publish_sync` requests | 1 | — (the service's own subscription) |
| Auto-replies, including the dropped-request error (§2.5) | 1 | 1 (`publish_sync` reply topics) |
| Application `Agent.publish` / `Agent.subscribe` | `qos=` keyword, default 0 | `qos=` keyword, default 0 |

- MQTT delivers at the lower of the publish and subscription QoS. A service that wants its requests at QoS 1 end to end subscribes with `qos=1`.
- An invalid `qos` on `Agent.publish` / `subscribe` raises `ValueError` up front, before any handler is registered.

### 2.3 Message ids

- `Parcel.message_id: Optional[str]` follows the rules of RFC-014's `correlation_id`. It is written to the envelope only when set, declared on the class too, and decoded only as a `str` of at most 128 characters (`MAX_MESSAGE_ID_LENGTH`), otherwise `None`.
- `Agent._publish_or_raise` stamps a fresh `uuid4().hex` on every message it sends. That covers `publish`, `publish_sync` requests and auto-replies.
- The id is set on a copy, so the caller's Parcel is not modified, and a Parcel published twice is sent as two messages. A message resent by paho keeps its bytes, and so its id.

### 2.4 Deduplication

`Agent._on_message` drops a message whose id it received recently, before any handler runs.

- The window holds the last 4096 ids (`_DEDUP_WINDOW`), in an `OrderedDict` behind a lock; both are runtime-only (RFC-008).
- Messages without an id, from senders older than RFC-015, are never deduplicated.

### 2.5 Reported losses

- `Agent.publish(...)` returns `True` when the broker accepted the message and `False` when it did not. It still never raises for broker errors (RFC-002 / RFC-012 fire-and-forget), and callers that ignore the result are unaffected.
- When the dispatcher drops a message (`enqueue()` returns `False`: queue full or stopping) that would have been auto-replied to (RFC-003), `_on_message` publishes an error reply at once. The reply has `error` set to `"overloaded: the request was dropped before it was handled (dispatcher queue full or stopping)"`, no content, and the request's correlation id. `publish_sync` returns that error Parcel quickly instead of raising `TimeoutError`.
  - It runs on the broker callback thread. `Agent.publish` never raises there, and `MqttBroker.publish` only checks paho's immediate return code (RFC-012), so it never waits for the broker.

## 3. What is guaranteed now

| Situation | Before | Now |
|---|---|---|
| Connection drops while a QoS 1 message (control / RPC) is in flight from the publisher | lost | paho resends it after the reconnect (with DUP); the receiver handles it once |
| Duplicate delivery of a message carrying an id | handled twice | handled once (within the 4096-id window) |
| Publishing while disconnected | `publish` → `None`, logged | `publish` → `False`, logged; `publish_sync` raises at once (RFC-002 / RFC-012) |
| Publishing in the moment between paho losing the socket and reporting the disconnect | QoS 0: lost and reported as failed | QoS 0: still reported as failed. QoS ≥ 1: paho keeps the message and sends it after the reconnect, so it is reported as accepted; reporting a failure would make callers retry and the receiver run the request twice |
| Subscriber disconnected while the broker holds a message for it | lost | still lost: a clean session keeps nothing for a disconnected client (#17) |
| Request dropped by a full dispatcher | requester times out | requester gets an "overloaded" error reply at once |

Delivery is therefore **at least once with deduplication** for QoS 1 traffic between connected peers. It is not end-to-end exactly-once: duplicates beyond the window, or from senders without ids, still reach handlers. Handlers with side effects should stay idempotent; the holon operations are (#5, #6).

## 4. Compatibility

- **Wire**: every message now carries `message_id`. Receivers older than RFC-015 ignore the key. Older senders send none and are never deduplicated.
- **API**:
  - `Agent.publish` returns a bool instead of `None`.
  - `Agent.publish` / `subscribe` and `MessageBroker.publish` / `subscribe` gain keyword-only `qos` / `retain` with the old defaults. Custom subclasses of the built-in brokers must accept them.
- **Brokers**: QoS 1 needs an MQTT broker that supports it; every MQTT 3.1.1 broker does.

## 5. Verification

- `tests/unit/test_mqtt_broker_qos.py`:
  - paho call shapes with the defaults, and with `qos` / `retain`;
  - resubscribe keeping QoS;
  - invalid QoS;
  - the keyword-only signature on every built-in broker.
- `tests/unit/core/test_delivery_guarantees.py`:
  - QoS 1 for holon control (every control subject, every holon subscription) and for `publish_sync` requests, replies and reply-topic subscriptions;
  - application defaults and the `qos` keyword, and invalid QoS;
  - message ids: fresh per publish with the caller's Parcel untouched, round trip, malformed values;
  - deduplication, its bounded window, and no deduplication without ids;
  - `publish` True / False;
  - the error reply for a dropped request, and none for a dropped plain message;
  - a `publish_sync` caller failing fast against an overloaded service.
- The RFC-002 / RFC-012 tests that pinned `publish` returning `None` now assert `True` / `False`. RFC-012's ABC-signature test pins the positional parameters plus the new keyword-only ones.
