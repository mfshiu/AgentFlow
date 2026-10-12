# RFC-015 — Delivery guarantees: QoS, message ids, reported losses

- **Status**: **Implemented** (2026-10-12, issue #4; §6 issue #17, §7 issue #35)
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
  - Persistent sessions and stable client ids were out of scope at first; issue #17 added them (§6).
  - Bounding the outbound buffer was out of scope at first; issue #35 added it (§7).
  - Decode-failure observability (#15; since resolved, see the risk register's R-19).
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
- When the dispatcher drops a message (`enqueue()` returns `False`: queue full or stopping) that would have been auto-replied to (RFC-003), `_on_message` publishes an error reply at once. The reply has `error` set to `"overloaded: the request was dropped before it was handled (dispatcher queue or byte budget full, or stopping)"` (the byte budget since #34), no content, and the request's correlation id. `publish_sync` returns that error Parcel quickly instead of raising `TimeoutError`.
  - It runs on the broker callback thread. `Agent.publish` never raises there, and `MqttBroker.publish` only checks paho's immediate return code (RFC-012), so it never waits for the broker.

## 3. What is guaranteed now

| Situation | Before | Now |
|---|---|---|
| Connection drops while a QoS 1 message (control / RPC) is in flight from the publisher | lost | paho resends it after the reconnect (with DUP); the receiver handles it once |
| Duplicate delivery of a message carrying an id | handled twice | handled once (within the 4096-id window) |
| Publishing while disconnected | `publish` → `None`, logged | `publish` → `False`, logged; `publish_sync` raises at once (RFC-002 / RFC-012) |
| Publishing in the moment between paho losing the socket and reporting the disconnect | QoS 0: lost and reported as failed | QoS 0: still reported as failed. QoS ≥ 1: paho keeps the message and sends it after the reconnect, so it is reported as accepted; reporting a failure would make callers retry and the receiver run the request twice |
| Subscriber disconnected while the broker holds a message for it | lost | default (clean session): still lost. With `clean_session: false` (§6), the broker keeps QoS ≥ 1 messages for the client and delivers them when it reconnects, including after a restart with a stable `client_id` (see §6 "Persistence" and "Early messages") |
| Request dropped by a full dispatcher | requester times out | requester gets an "overloaded" error reply at once |
| Publishing faster than the link drains (slow or stalled link) | QoS 0: paho queued it without limit, so memory grew | refused once `max_pending_bytes` (default 64 MiB) are pending: `publish` → `False`, counted; `publish_sync` raises (§7) |

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
- §6 early messages: `tests/unit/core/test_early_messages.py` covers the gate: held and released, the handler registered meanwhile, never holding messages with a handler or on topics subscribed in this process (wildcards included), waiting for every hook, the deadline, the bound, the throttled release, replay past deduplication, a held request answered, and no gate outside the lifecycle. `tests/wire/test_early_messages_wire.py` checks messages pushed right after the CONNACK reaching handlers registered in `on_activate` or `on_connected`, and `on_connected` receiving a message it subscribed to and waited for.
- §6: `tests/unit/test_mqtt_broker_session.py` covers the default and templated client ids, clean session, invalid options, refusal classification and the Agent not retrying. `tests/wire/test_mqtt_session_wire.py` checks the id and clean-session flag a real Agent sends, and a rejected id failing fast without retries.

## 6. Client identity and persistent sessions (issue #17)

Before #17 the paho Client had no client id, so every connection sent a zero-length one. Brokers assigned a new id each time, so persistent sessions and per-agent ACLs were impossible, and broker logs could not be matched to agents.

**Options** (`broker` section, MQTT):

| Option | Default | Meaning |
|---|---|---|
| `client_id` | `'{name}-{agent_id:.12}'` | Template with `{name}` and `{agent_id}`, the Agent's. Write literal braces as `{{ }}`. The result must be 1 to 65535 UTF-8 bytes. |
| `clean_session` | `true` | `false` keeps the broker-side session across reconnects of the same client id: its subscriptions, and QoS ≥ 1 messages that arrive while it is disconnected. |

- **Uniqueness.** MQTT disconnects the older of two connections that share a client id. The default includes the agent id, so it is unique per Agent instance. A fixed id, or a template such as `'{name}'`, must be unique among all of the broker's clients. Two agents sharing one would keep disconnecting each other.
- **Persistence.** With the default id, a persistent session lasts across reconnects within one process, where it works as intended: the handlers are already registered when the queued messages arrive.
  - A new Agent instance (a restart) has a new id and therefore a new session.
  - A stable id such as `'{name}'` (with unique names) resumes the session after a restart. The broker delivers the queued messages right after the CONNACK, before the handlers exist; see "Early messages".
  - A broker keeps abandoned persistent sessions until its own expiry setting (for example mosquitto's `persistent_client_expiration`). So prefer the default `clean_session: true` unless you need queuing across reconnects.
- **Construction.** The paho Client is built once, in `start()`'s NEW → STARTING transition, with the client id, clean session, MQTT 3.1.1 and `reconnect_on_failure` (#2). Options are parsed before any state change, so a malformed one raises `ValueError` and leaves the broker NEW (RFC-011 amended).
- **Early messages.** While an agent's lifecycle hooks are still registering handlers, a resumed session's leftover is held rather than handed to the default `on_message`. A leftover is a message whose topic has no handler and matches no topic passed to `subscribe()` in this process (+ / # wildcards included). The hooks are `on_activate`, and `on_connected` after the first connect.
  - **Release.** Once both hooks have returned, even by raising, the held messages are replayed in arrival order on a background thread. A handler registered meanwhile receives them. A request among them is answered with its correlation id. Messages whose topic still has no handler reach `on_message` as before.
  - **Throttling.** The replay waits for room in the dispatcher queue, so a large backlog is not turned into "overloaded" drops.
  - **Deadline.** If a hook has not returned 10 s after the gate closed (`_EARLY_MESSAGE_MAX_HOLD_S`), for example a long-running `on_connected` loop, holding stops and everything held is released, with a warning.
  - **Not held.** Messages with a handler, or on a topic the agent subscribed in this process, are never held: holon topics, `publish_sync` replies, and the agent's own traffic. A hook can therefore wait for a message it asked for.
  - **Bound.** At most 4096 are held (`_EARLY_MESSAGE_LIMIT`); beyond that they are dropped with a rate-limited warning.
  - **Scope.** Handlers registered after these hooks, for example from a timer, can still miss queued messages. Only agents started through the normal lifecycle (`start()` / `start_thread()` → `_activate`) are gated.
- **Start timeouts.** A start that ends in `START_TIMEOUT` (connect still blocked) leaves a client behind. With a stable or shared id it would keep taking over its replacement's session. The startup helper therefore rolls its client back itself when connect finally returns.
- **Refusals.** A refused CONNACK raises `MqttConnectRefused`, a `ConnectionError` with the reason code. `start()` waits for a dedicated CONNACK event that `on_disconnect` does not clear. Before, a refusal followed at once by its disconnect could surface only as a timeout. It is not retryable when connecting again with the same configuration cannot succeed: unsupported protocol, client id rejected, bad user name or password, not authorised. That is MQTT 3.1.1 codes 1 / 2 / 4 / 5 and paho's MQTT 5 codes for them, plus 138 / 140. `Agent.__activating` then gives up at once instead of retrying three times, 5 s apart.

## 7. Outbound limit (issue #35)

paho queues QoS 0 messages without any limit (`max_queued_messages` applies only to QoS ≥ 1), so over a slow or stalled link every publish succeeded while memory grew.

**Option** (`broker` section, MQTT):

| Option | Default | Meaning |
|---|---|---|
| `max_pending_bytes` | `67108864` (64 MiB) | Payload bytes that may be pending: not yet written to the socket (QoS 0) or not yet acknowledged (QoS ≥ 1). A positive int; a malformed value raises at `start()` and leaves the broker NEW. |

- **Tracking.** A publish is pending from the paho call until paho's `on_publish` reports it. Its bytes are reserved before the call, so a report that arrives before `publish()` returns is handled. The broker's lock is never held across a paho call, because paho runs `on_publish` while holding its own message lock.
- **Refusal.** A publish that would take the pending bytes past the limit raises `MqttBackpressureError` before paho is called. It is a `MqttPublishError` (reason `backpressure`) and a transport-neutral `PublishBackpressureError` from `agentflow.broker`.
  - `Agent.publish` returns `False`, counts it in `publish_backpressure_count`, and logs it without a traceback (WARNING on the 1st, 2nd, 4th, ... refusal, DEBUG otherwise).
  - `publish_sync` raises it, so a requester fails fast.
- **Connection loss.** paho drops the QoS 0 messages it has not written, without reporting them, so they are forgotten. QoS ≥ 1 ones stay pending until paho resends them and they are acknowledged.
- **Metrics.** `recovery_metrics()` reports `pending_publish_count` and `pending_publish_bytes`.
