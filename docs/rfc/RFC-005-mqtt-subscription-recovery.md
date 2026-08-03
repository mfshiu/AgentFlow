# RFC-005 — MQTT subscription recovery

- **Status**: **Implemented (2026-07-27)**
- **Author**: Audit follow-up
- **Depends on**: `docs/audit/05-risk-register.md` R-03; consistent with [RFC-001](RFC-001-publish-sync-subscription-lifecycle.md), [RFC-002](RFC-002-publish-error-propagation.md), [RFC-003](RFC-003-auto-reply-contract.md), [RFC-004](RFC-004-bounded-message-dispatch.md)
- **Scope**: MqttBroker subscription registry, on_connect / on_disconnect state machine, resubscribe on reconnect, stop-vs-reconnect race resolution
- **Explicitly out of scope**: real-network retry policy, exponential-backoff algorithms, offline publish queue, broker-cluster failover, Parcel schema changes, ProcessWorker dispatcher (RFC-004 Appendix C), distributed subscription registry
- **Implementation summary** (2026-07-27):
  - Landed the recommended Option C in `src/agentflow/broker/mqtt_broker.py`. All state is contained within `MqttBroker`; `MessageBroker` ABC, `BrokerNotifier` ABC, `Agent.*` public API, `Parcel` and wire format are all **untouched**.
  - Added attributes protected by `_state_lock`: `_registry: dict[str, Any]`, `_connected`, `_ever_connected`, `_stopping`, `_last_disconnect_was_planned`, three recovery counters (`_resubscribe_success_count`, `_resubscribe_error_count`, `_recovery_run_count`). Public observability surface added: `last_disconnect_was_planned` property, `recovery_metrics()` snapshot method.
  - `subscribe`/`unsubscribe` update the registry under lock and forward to `client.subscribe`/`client.unsubscribe` only when currently connected; disconnected calls only update the registry and return `None`. Post-stop calls short-circuit and return `None` without touching the registry.
  - `_on_connect(rc=0)` distinguishes first-connect (empty snapshot, skip recovery) from reconnect (snapshot the registry, invoke `_recover_subscriptions`) via `_ever_connected`. Recovery iterates outside the lock with a per-iteration re-check of `_stopping` and `topic in _registry`; per-topic failures are counted and logged but do not abort the loop.
  - `_on_disconnect` classifies planned vs unexpected (`_stopping` or `reasonCode == 0`), clears `_connect_ok` and `_connected_evt`, preserves the registry.
  - `stop()` flips `_stopping = True` **before** `client.disconnect()`; any inline `_on_disconnect` or late `_on_connect` observes the flag.
  - Test surface: `tests/unit/test_mqtt_broker_reconnect.py` rewritten (48 tests, 10 categories) — the 6 aspirational strict xfails from R-03 characterization all converted to passing positive assertions; new tests cover per-topic failure isolation, stop-during-recovery, unsubscribe-during-recovery, subscribe-during-recovery, concurrent producer thread-safety, and three `_state_lock`-not-held-across-client-call assertions.
  - `tests/unit/test_mqtt_broker_lifecycle.py` — 5 subscribe/unsubscribe tests updated to `_prime_connected(broker, fake_client)` first because those calls now require `_connected=True` to forward.
  - Full unit regression: `PYTHONPATH=src python -m pytest tests/unit` → **216 passed, 0 failed, 0 xfailed, 0 xpassed** in 3.34 s. R-02 (27), R-04 (33), R-05 (21), R-13 (46) all pass unchanged.
  - See [R-03 resolution block](../audit/05-risk-register.md#r-03--mqtt-reconnect-and-subscription-recovery-are-not-implemented) for the full behavioural-change table and known-remaining gaps.

---

## 1. Problem statement

`MqttBroker` (`src/agentflow/broker/mqtt_broker.py`) is a thin pass-through over `paho.mqtt.Client`. It holds no subscription state, does not distinguish planned disconnect from unexpected drop, and has no state observable from `_on_connect` that a late callback could use to detect that `stop()` was called. `paho.Client` itself is constructed with `reconnect_on_failure=False`, so paho does not auto-reconnect either.

Consequences (all runtime-confirmed — §2):

- A MQTT session that survives a broker restart or a transient network drop returns to work with **zero prior subscriptions**. The Agent's Python-side `__topic_handlers` still holds handlers, but no messages arrive for them.
- `Agent._on_connect` has a `_connected_once` guard that ignores every callback after the first, so it cannot itself drive resubscription on reconnect.
- A `stop()` followed by a late `_on_connect` callback (rare but observable in tests) still delegates to the notifier as if the broker were freshly connected.
- `broker._connect_ok` reports the state at first-successful-connect time forever after; it is never cleared on disconnect.
- `_on_disconnect` treats reason-code 0 (planned) and non-zero (unexpected) identically — one `logger.warning`.

This RFC proposes a self-contained recovery mechanism inside `MqttBroker` that preserves the Agent-facing surface unchanged.

---

## 2. Runtime evidence

Baseline: `PYTHONPATH=src pytest tests/unit` → **194 passed, 6 xfailed in 3.36 s**.

Six R-03 aspirational tests (all strict xfail) form the acceptance-check set for §11:

| Test | Current outcome |
|---|---|
| `test_reconnect_should_resubscribe_previously_subscribed_topics` | XFAIL — broker has no registry to resubscribe from |
| `test_reconnect_should_not_resurrect_unsubscribed_topic` | XFAIL — no recovery means the assertion form is untestable today; the ideal must both preserve `KEEP` and skip `DROP` |
| `test_on_connect_after_stop_should_not_notify_notifier` | XFAIL — `stop()` sets no observable flag |
| `test_on_disconnect_should_distinguish_planned_from_unexpected` | XFAIL — planned and unexpected disconnects hit the same log line |
| `test_connect_ok_should_be_cleared_on_disconnect` | XFAIL — `_connect_ok` is never cleared |
| `test_broker_should_expose_active_subscriptions_after_subscribe` | XFAIL — no accessor / no registry |

Confirmed characterizations (26 passing tests in `tests/unit/test_mqtt_broker_reconnect.py`) document the current behaviour that this RFC changes.

---

## 3. Current state

Source: `src/agentflow/broker/mqtt_broker.py`.

```mermaid
sequenceDiagram
    autonumber
    participant Ag as Agent
    participant Br as MqttBroker
    participant Cl as paho.Client (mocked in tests)
    Ag->>Br: subscribe(topic, data_type)
    Br->>Cl: client.subscribe(topic=topic)
    Note over Br: NO registry write
    Cl-->>Ag: (later) on_message(topic, payload)
    Note over Br,Cl: network drop
    Cl->>Br: _on_disconnect(rc=1)
    Br->>Br: logger.warning; NO state change
    Note over Br: _connect_ok, _connected_evt, notifier are unchanged
    Cl->>Br: _on_connect(rc=0)  [external reconnect or session revival]
    Br->>Ag: notifier._on_connect()
    Note over Ag: Agent._connected_once guard → early return
    Note over Br,Cl: NO resubscribe. All prior topics silently dropped.
```

---

## 4. Desired state

- `MqttBroker` owns a `dict[topic, data_type]` **subscription registry**, updated on every `subscribe` / `unsubscribe` under a single lock.
- On the **first** successful `_on_connect`, notify the notifier normally; the notifier drives initial subscribes as today (populating the registry).
- On a **subsequent** successful `_on_connect` (== reconnect), the broker snapshots the registry, iterates it under best-effort ordering, and re-issues `client.subscribe(topic)` for each entry before notifying the notifier. Per-topic failures are counted and logged; recovery continues.
- `stop()` sets a `_stopped` flag under the same lock. Any subsequent `subscribe` / `unsubscribe` / `_on_connect` short-circuits: subscribe/unsubscribe log-and-return without touching either the registry or the client; `_on_connect` skips both recovery and notifier delegation.
- `_on_disconnect` records whether the disconnect was **planned** (reason code 0 or during `stop()`) or **unexpected**, exposed as `last_disconnect_was_planned` for observability; clears `_connect_ok` and `_connected_evt`.
- Public API surface — `MessageBroker` ABC, `Agent.publish` / `publish_sync` / `subscribe` / `unsubscribe` / `_publish_or_raise`, `Parcel`, wire format — **unchanged**.

---

## 5. Options considered

### Option A — Fully rely on paho auto-reconnect (no framework registry)

Set `reconnect_on_failure=True`, `clean_start=False`, choose a stable `client_id`; delegate resubscribe entirely to paho + MQTT session persistence at the broker.

| Aspect | Analysis |
|---|---|
| Covers all reconnect paths | ✓ while the session is alive on the MQTT broker |
| Depends on external broker configuration | **Yes** — MQTT broker must persist sessions and the deployment must not clear them |
| Depends on stable `client_id` | **Yes** — currently `MqttBroker` does not set one; auto-generated IDs differ across processes |
| Handles unsubscribe correctly | ✓ paho tracks it in the client-side session state |
| Handles session expiry | ✗ once the session expires, everything is lost |
| Recovers if the broker drops the session | ✗ |
| Portability across brokers | Some brokers do not persist sessions the same way |
| Complexity added to `MqttBroker` | Very low |

**Verdict**: rejected as a standalone solution. Deployment risk is high because it forces a specific broker configuration outside of AgentFlow's control. May be composed with the recommended design in the future as a "fast path" (paho's session survives → we skip recovery), but that is out of RFC-005 scope.

---

### Option B — Agent-side subscription registry + re-emit on reconnect

Add a broker → notifier `on_reconnect()` callback; Agent maintains its own registry alongside `__topic_handlers` and re-emits subscribes on that callback.

| Aspect | Analysis |
|---|---|
| Registry lives where handlers live | ✓ symmetric |
| Requires `BrokerNotifier` ABC extension | **Yes** — a new abstract or non-abstract method |
| Mixes concerns | Agent's `__topic_handlers` is populated by both permanent `subscribe(topic, handler)` calls AND ephemeral `publish_sync` handlers; distinguishing "permanent" from "sync request" from Agent state alone is fragile |
| Ordering with concurrent subscribe/unsubscribe | Same race concerns as Option C |
| Agent behaviour test surface | Larger — Agent gets new callback + recovery loop |
| MessageBroker API | Untouched (only Notifier extended) |

**Verdict**: viable but muddles Agent responsibilities. Also requires distinguishing publish_sync's temporary handlers from permanent subscriptions, which is not currently exposed in the API.

---

### Option C — MqttBroker owns the desired-state registry (**RECOMMENDED**)

The broker maintains an authoritative `dict[topic, data_type]` populated by every successful `subscribe` and drained by every successful `unsubscribe`. On reconnect, the broker iterates the registry and re-emits `client.subscribe(topic)` for each entry.

| Aspect | Analysis |
|---|---|
| Registry lives where paho state lives | ✓ same object owns both |
| MessageBroker ABC change | None required — the registry and recovery are internal to `MqttBroker` |
| Agent API change | None |
| Registry as source of truth | ✓ unsubscribe deletes from registry; recovery cannot resurrect |
| First-connect vs reconnect distinction | Via a single `_ever_connected` bool; first connect skips recovery loop |
| Stop guard | Same `_state_lock` protects registry, `_stopped`, and `_ever_connected` |
| Per-topic failure isolation | Straightforward: try/except per `client.subscribe` inside the recovery loop |
| Race between concurrent subscribe/unsubscribe and recovery | Present but rare; MQTT idempotency limits damage. Documented in §7 |

**Verdict**: **Recommended.** Fits the priority list — no Agent API change, no MessageBroker ABC change, no wire change; recovery isolated to a single class.

---

### Option D — Rebuild subscription from Agent handlers on each reconnect

On reconnect, the broker asks Agent (via a new callback) for its current handler set and re-subscribes each topic.

| Aspect | Analysis |
|---|---|
| Requires broker → Agent introspection | ✓ new abstract method on `BrokerNotifier` |
| Mixes publish_sync ephemeral handlers | ✗ same problem as Option B, worse — recovery would recreate subscriptions for topic_return topics that publish_sync already unsubscribed |
| Agent must expose an "authoritative subscriptions" API | Adds public surface |
| Verdict | Rejected: entangles subscription state with request-response state |

---

### Option E — Connection generation / epoch counter

Increment an `_epoch` counter on each successful `_on_connect`. Tag every `subscribe`/`unsubscribe` operation with the current epoch. Recovery only re-emits topics that were subscribed under a **prior** epoch.

| Aspect | Analysis |
|---|---|
| Standalone? | No — needs a store (i.e. a registry) that this tags. Complements C/B, doesn't replace them. |
| Solves the concurrent subscribe/recovery race | Partially — allows dropping stale ops; requires more state |
| Complexity | Meaningful — every subscribe call needs epoch tracking |

**Verdict**: nice-to-have addition on top of Option C. Deferred: Option C's simple boolean flag suffices for the MVP; epoch counter can be added in a follow-up RFC if concurrent races become a measured problem.

---

### Comparison summary

| Criterion | A | B | **C** | D | E (alone) |
|---|---|---|---|---|---|
| Recovers all valid subscriptions | Depends on session | ✓ | ✓ | ✓ | n/a — needs storage |
| Does not resurrect unsubscribed | ✓ (session-based) | ✓ | ✓ | ✓ | n/a |
| No Agent API change | ✓ | ✗ | **✓** | ✗ | n/a |
| No MessageBroker ABC change | ✓ | ✗ | **✓** | ✗ | n/a |
| No wire schema change | ✓ | ✓ | ✓ | ✓ | ✓ |
| Portable across MQTT brokers | ✗ | ✓ | **✓** | ✓ | n/a |
| Per-topic failure isolation | (paho's problem) | ✓ | **✓** | ✓ | n/a |
| Handles stop / late on_connect race | ✗ | Partial | **✓** | Partial | n/a |
| Lines changed | ~5 | ~40+ | ~40 | ~50+ | additive |
| Verdict | rejected | viable | **chosen** | rejected | deferred |

---

## 6. Recommended design

Adopt **Option C**. All state lives in `MqttBroker`; `MessageBroker` ABC and `Agent` are untouched.

### 6.1 New state on `MqttBroker`

```python
# Added in __init__
self._state_lock = threading.Lock()
self._registry: dict[str, str] = {}         # topic → data_type
self._ever_connected: bool = False
self._stopped: bool = False
self._last_disconnect_was_planned: Optional[bool] = None

# Metrics (mutations under _state_lock; snapshot API for coherence)
self._resubscribe_success_count: int = 0
self._resubscribe_error_count: int = 0
self._recovery_run_count: int = 0
```

### 6.2 subscribe / unsubscribe

```python
def subscribe(self, topic: str, data_type):
    with self._state_lock:
        if self._stopped:
            logger.info("subscribe after stop; dropping topic %r", topic)
            return None
        self._registry[topic] = data_type
    return self._client.subscribe(topic=topic)

def unsubscribe(self, topic: str):
    with self._state_lock:
        if self._stopped:
            logger.info("unsubscribe after stop; dropping topic %r", topic)
            return None
        self._registry.pop(topic, None)
    return self._client.unsubscribe(topic)
```

`client.subscribe` / `client.unsubscribe` are called **outside** the lock so that paho's own network I/O never happens while we hold `_state_lock`.

### 6.3 _on_connect

```python
def _on_connect(self, client, userdata, flags, reasonCode, properties):
    if reasonCode != 0:
        # existing error path (unchanged)
        self._connect_ok = False
        name = getattr(reasonCode, "getName", lambda: str(reasonCode))()
        self._connect_err = f"{reasonCode} ({name})"
        logger.error(f"MQTT connection failed: {self._connect_err}")
        self._connected_evt.set()
        return

    with self._state_lock:
        if self._stopped:
            logger.info("on_connect after stop; skipping notifier + recovery")
            return
        is_first = not self._ever_connected
        self._ever_connected = True
        snapshot = dict(self._registry) if not is_first else {}

    self._connect_ok = True
    self._connect_err = None

    if snapshot:
        self._recover_subscriptions(snapshot)

    try:
        self._notifier._on_connect()
    finally:
        self._connected_evt.set()

def _recover_subscriptions(self, snapshot: dict[str, str]):
    with self._state_lock:
        self._recovery_run_count += 1
    successes = failures = 0
    for topic, data_type in snapshot.items():
        # Bail early if stop() was called mid-recovery.
        with self._state_lock:
            if self._stopped:
                logger.warning(
                    "recovery aborted by stop() after %d/%d topics",
                    successes + failures, len(snapshot),
                )
                return
        try:
            self._client.subscribe(topic=topic)
            successes += 1
        except Exception as ex:
            failures += 1
            logger.exception(
                "recovery: resubscribe failed for topic %r: %s", topic, ex,
            )
    with self._state_lock:
        self._resubscribe_success_count += successes
        self._resubscribe_error_count += failures
    logger.info(
        "recovery complete: %d topics succeeded, %d failed",
        successes, failures,
    )
```

### 6.4 _on_disconnect

```python
def _on_disconnect(self, client, userdata, _flags, reasonCode, _properties):
    with self._state_lock:
        # Planned iff stop() has already flipped _stopped, OR the
        # reason code is 0 (paho's "normal disconnection").
        planned = self._stopped or (
            reasonCode == 0
            or getattr(reasonCode, "value", None) == 0
        )
        self._last_disconnect_was_planned = planned
    self._connect_ok = False
    self._connected_evt.clear()
    if planned:
        logger.info(f"MQTT disconnected (planned): {reasonCode}")
    else:
        logger.warning(f"MQTT disconnected (unexpected): {reasonCode}")
```

### 6.5 stop

```python
def stop(self):
    with self._state_lock:
        self._stopped = True
    logger.warning("MQTT broker is stopping...")
    self._client.disconnect()
    self._client.loop_stop()
```

`_stopped` is set BEFORE `client.disconnect()` so that any callback fired synchronously from within `disconnect()` sees the flag.

### 6.6 Observability

New read-only surface on `MqttBroker`:

```python
@property
def last_disconnect_was_planned(self) -> Optional[bool]: ...

def recovery_metrics(self) -> dict:
    """Snapshot of resubscribe metrics; taken under _state_lock."""
    with self._state_lock:
        return {
            "resubscribe_success_count": self._resubscribe_success_count,
            "resubscribe_error_count": self._resubscribe_error_count,
            "recovery_run_count": self._recovery_run_count,
            "active_subscriptions": len(self._registry),
            "stopped": self._stopped,
            "ever_connected": self._ever_connected,
        }
```

---

## 7. Concrete decisions (all 16)

### 7.1 Subscription source of truth
**`MqttBroker._registry`**, a `dict[str, str]` mapping topic → data_type. Written by `subscribe`, deleted by `unsubscribe`. Agent's `__topic_handlers` is **not** the source of truth for wire-level subscriptions — the two live at different layers and may diverge (Agent may register a handler without a broker being ready; `publish_sync` uses `__topic_handlers` for temporary sync-request handlers that RFC-001 already cleans up).

### 7.2 Registry fields
`{topic: data_type}`. `data_type` is kept even though `MqttBroker.subscribe` currently ignores it — future `RosBroker`-style subclasses need it. Additional metadata (subscription time, epoch, options) explicitly deferred.

### 7.3 Subscribe when disconnected
`broker.subscribe(topic, data_type)` **still updates the registry and calls `client.subscribe`**. paho decides what to do while disconnected (typically queues or fails silently). The registry write is the durable contract: the topic will be re-subscribed on the next successful reconnect regardless of what paho did with the immediate call.

### 7.4 Unsubscribe when disconnected
Symmetric: `broker.unsubscribe(topic)` **removes from registry and calls `client.unsubscribe`**. Registry deletion is the durable contract; the topic will not be re-subscribed on reconnect.

### 7.5 on_connect success recovery flow
Under `_state_lock`: check `_stopped` (bail if True); determine `is_first = not _ever_connected`; set `_ever_connected = True`; snapshot the registry if not first. Release lock. If snapshot non-empty, run `_recover_subscriptions`. Then call `notifier._on_connect()`. Finally set `_connected_evt`.

Recovery iterates the snapshot outside the lock, checking `_stopped` between topics to allow prompt shutdown. Per-topic failures are counted (`_resubscribe_error_count`) and logged; recovery continues.

### 7.6 on_connect failure (rc != 0)
Unchanged from today: log the error, set `_connect_ok=False`, set `_connect_err`, set `_connected_evt`, return. **No recovery attempt on a failed connect** — recovery is triggered by a successful connect.

### 7.7 stop vs reconnect callback race
`stop()` sets `_stopped=True` under `_state_lock` before calling `client.disconnect()`. All entry points (`subscribe`, `unsubscribe`, `_on_connect`, `_recover_subscriptions`'s inner loop) check `_stopped` under the same lock and short-circuit. A late `_on_connect` callback firing after `stop()` observes `_stopped=True` and does not call the notifier or recover subscriptions.

### 7.8 Registry lock strategy
Single `_state_lock` protects `_registry`, `_stopped`, `_ever_connected`, `_last_disconnect_was_planned`, and all recovery-metric counters. The lock is **never** held across a `client.subscribe`, `client.publish`, `client.unsubscribe`, `client.disconnect`, or `client.loop_stop` call — no network I/O under lock. Critical sections are three or four dict / int operations; contention is negligible.

### 7.9 Snapshot then subscribe
Yes. Recovery snapshots the registry under the lock, then iterates the snapshot outside the lock. Any concurrent `subscribe` / `unsubscribe` that arrives between the snapshot and the last iteration is applied to the live registry (which the next reconnect will observe) but not to the currently-running recovery.

### 7.10 Linearization semantics
- Registry writes (`subscribe`'s dict-set, `unsubscribe`'s dict-pop, `stop`'s flag flip) are strictly linearizable via `_state_lock`.
- `client.subscribe` / `client.unsubscribe` calls are best-effort ordered relative to each other: the calling thread issues them after releasing the lock, so if two threads concurrently call `broker.subscribe(A)` and `broker.unsubscribe(A)`, the client-side ordering is not deterministic. The registry-side ordering IS deterministic. MQTT itself is idempotent for repeated subscribes/unsubscribes, which bounds the damage.
- Recovery vs concurrent `subscribe`/`unsubscribe` race: acknowledged and documented. A `subscribe(X)` arriving between snapshot and the recovery loop's last iteration may result in two client-side subscribes for X — harmless. An `unsubscribe(X)` in the same window may result in a client.subscribe(X) followed by a client.unsubscribe(X) — final state matches the caller's intent.

### 7.11 Connection generation
**Not introduced in this RFC.** A single `_ever_connected` boolean suffices to distinguish first-connect (no recovery) from reconnect (recovery). A full epoch counter is a valid future addition (Option E) to enable stricter concurrent-race detection; deferring keeps the initial change small.

### 7.12 Individual resubscribe failure handling
`_recover_subscriptions` wraps each `client.subscribe` call in `try/except Exception`. Failures increment `_resubscribe_error_count` and log at ERROR with topic name and exception detail. **The loop continues with the next topic**; a single failing topic does not abort the recovery of the remaining topics. `BaseException` subclasses propagate (KeyboardInterrupt / SystemExit), consistent with RFC-004 §7.10.

### 7.13 Logging + metrics
- INFO on `subscribe` / `unsubscribe` after stop ("dropping topic X")
- INFO on `on_connect` after stop ("skipping notifier + recovery")
- INFO on recovery start / complete with success + failure counts
- ERROR per failed resubscribe (not rate-limited — resubscribe count is bounded by registry size)
- INFO on planned disconnect; WARNING on unexpected disconnect (existing WARNING preserved as WARNING for unexpected)
- Metrics attributes: `resubscribe_success_count`, `resubscribe_error_count`, `recovery_run_count`, `active_subscriptions`, `stopped`, `ever_connected`, plus the existing `_connect_ok`, `_connect_err`
- `recovery_metrics()` returns a coherent snapshot under `_state_lock`

### 7.14 Backward compatibility
- `MessageBroker` ABC: **unchanged**. `EmptyBroker`, `RedisBroker`, `RosBroker`, `LegacyPerMessageDispatcher`-adjacent stubs: **unchanged**.
- `Agent.publish` / `publish_sync` / `subscribe` / `unsubscribe` / `_publish_or_raise` public signatures: **unchanged**.
- `Parcel` / `TextParcel` / `BinaryParcel` / wire format: **unchanged**.
- `MqttBroker.subscribe` / `unsubscribe` return values: **unchanged** for the connected happy path (paho MessageInfo). Post-stop return value changes from paho MessageInfo to `None` — see §8.
- `BrokerNotifier`: **unchanged**. Agent gets no new callback.
- Observable behavioural changes at the wire level: on reconnect, previously-subscribed topics now receive a SUBSCRIBE frame from the client. Brokers that already had the session persisted see a duplicate — MQTT subscribe is idempotent.

### 7.15 Acceptance criteria
See §11.

### 7.16 Rollback plan
See §12.

---

## 8. Backward compatibility

### Source compatibility

| Symbol | Before | After | Compatibility |
|---|---|---|---|
| `MessageBroker` ABC | Same | Same | Full |
| `EmptyBroker`, `RedisBroker`, `RosBroker`, `DdsBroker`, `RosNoeticBroker` | Same | Same | Full |
| `MqttBroker.subscribe(topic, data_type)` | Returns paho MessageInfo | Returns paho MessageInfo when accepting, `None` after `stop()` | **Semi-compatible**: existing callers already accept `None` return (documented in `test_subscribe_after_stop_still_forwards_to_client` xfail direction) |
| `MqttBroker.unsubscribe(topic)` | Returns paho MessageInfo | Same rule as subscribe | Semi-compatible |
| `MqttBroker.stop` | `client.disconnect + loop_stop` | Set `_stopped=True` first, then same | Full — externally identical |
| `MqttBroker._on_connect` (`@final` in behaviour) | Notify unconditionally on rc=0 | Recover (if reconnect) + notify (if not stopped) | Behavioural change |
| `MqttBroker._on_disconnect` | Log only | Log + set `_connect_ok=False` + set `_last_disconnect_was_planned` | Behavioural change |
| `MqttBroker.recovery_metrics()` | Not defined | New read-only method | Additive |
| `MqttBroker.last_disconnect_was_planned` | Not defined | New read-only property | Additive |
| `Agent.*` | — | — | Untouched |
| `Parcel` / wire | — | — | Untouched |

### Behavioural compatibility

- **Callers of `broker.subscribe` / `broker.unsubscribe` after `stop()`**: previously the call went through to paho (with undefined behaviour); now it returns `None` and logs at INFO. No caller in the codebase invokes `broker.subscribe` after `stop()` intentionally; the change protects against a bug rather than exercising a real use case.
- **Callers reading `broker._connect_ok`**: previously stuck at `True` forever after first connect; now clears on disconnect. Only `MqttBroker.start` reads this field, and only during the initial `_connected_evt.wait` window — behaviour unchanged for `start`. External readers (rare) see more accurate state.
- **Callers relying on `_on_connect` firing the notifier after a reconnect**: Agent's `_connected_once` guard means the notifier is already a no-op on reconnect; the RFC-005 change (recover-then-notify) preserves this and additionally restores the wire subscriptions.
- **`on_disconnect` log level**: unchanged for unexpected disconnects (WARNING); INFO for planned. Log parsers keyed on WARNING for disconnect will now miss the planned-stop case — arguably an improvement (planned stop is not noise-worthy).

### Wire compatibility

- On reconnect, the client sends SUBSCRIBE frames for every registry entry. The MQTT protocol treats a duplicate subscribe as idempotent (last-QoS-wins), so brokers with persisted sessions accept the frames without error.
- No new fields on any wire message.

---

## 9. Interaction with prior RFCs

- **RFC-001 (R-02 publish_sync cleanup)**: Unaffected. `publish_sync` still calls `agent.subscribe(return_topic, handler)` on entry and `agent.unsubscribe(return_topic)` in the finally. Both paths go through `MqttBroker.subscribe` / `MqttBroker.unsubscribe`, which now maintain the registry as a side effect. The registry state after a successful `publish_sync` returns to the pre-call state — exactly the R-02 invariant.
- **RFC-002 (R-13 fast-fail)**: Unaffected. `_publish_or_raise` calls `broker.publish(topic, payload)`, which does not touch the subscription registry or state lock.
- **RFC-003 (R-05 auto-reply)**: Unaffected. Auto-reply logic runs inside `Agent._on_message` (dispatcher-driven per RFC-004); the broker layer's recovery path does not observe or alter parcel routing.
- **RFC-004 (R-04 bounded dispatch)**: Independent axis. Dispatcher lifecycle (`Agent._dispatcher`) does not interact with broker connection lifecycle. During recovery, the notifier is only called AFTER recovery completes; dispatcher tasks continue to run on their own consumer threads throughout.

The R-02 / R-04 / R-05 / R-13 combined 127 tests form the regression floor for RFC-005.

---

## 10. Test migration plan

### Six R-03 xfails that flip to PASS on implementation

Existing tests in `tests/unit/test_mqtt_broker_reconnect.py`:

| Test | Post-RFC-005 |
|---|---|
| `test_reconnect_should_resubscribe_previously_subscribed_topics` | PASS — remove `@pytest.mark.xfail` |
| `test_reconnect_should_not_resurrect_unsubscribed_topic` | PASS — remove xfail |
| `test_on_connect_after_stop_should_not_notify_notifier` | PASS — remove xfail |
| `test_on_disconnect_should_distinguish_planned_from_unexpected` | PASS — remove xfail (test may need to read `broker.last_disconnect_was_planned` after adjusting the attribute name) |
| `test_connect_ok_should_be_cleared_on_disconnect` | PASS — remove xfail |
| `test_broker_should_expose_active_subscriptions_after_subscribe` | PASS — remove xfail (test reads `broker._registry` or `recovery_metrics()['active_subscriptions']`) |

### Existing characterization tests that may need touching

- `test_subscribe_pass_through_to_client` — still passes (subscribe still calls client.subscribe).
- `test_on_disconnect_does_not_reset_connect_ok_flag` — will FAIL after RFC-005 because `_connect_ok` now clears. **Invert** to `test_on_disconnect_clears_connect_ok_flag`.
- `test_on_disconnect_does_not_clear_connected_event` — will FAIL. **Invert** to `test_on_disconnect_clears_connected_event`.
- `test_on_disconnect_planned_and_unexpected_treated_the_same` — will FAIL because log level and `last_disconnect_was_planned` now differ. **Invert** or **delete** in favour of the un-xfailed `_should_distinguish_planned_from_unexpected` test.
- `test_on_connect_after_stop_still_notifies_notifier` — will FAIL under the fix. **Invert** to `test_on_connect_after_stop_skips_notifier`.
- `test_subscribe_after_stop_still_forwards_to_client` — will FAIL. **Invert** to `test_subscribe_after_stop_returns_None_and_does_not_forward`.
- `test_broker_holds_no_subscription_registry_attribute` — will FAIL because `_registry` now exists. **Delete** in favour of the un-xfailed `_should_expose_active_subscriptions`.
- `test_stop_does_not_set_any_stopped_flag` — will FAIL because `_stopped` now exists. **Invert** to `test_stop_sets_stopped_flag`.

### New tests to add

| Test | Purpose |
|---|---|
| `test_recovery_resubscribes_all_registered_topics_on_reconnect` | Positive assertion: after disconnect + reconnect, all registry topics see a fresh `client.subscribe` call |
| `test_recovery_does_not_resubscribe_after_unsubscribe` | Positive: `subscribe A, subscribe B, unsubscribe A → reconnect → only B resubscribed` |
| `test_recovery_per_topic_failure_does_not_abort_others` | `client.subscribe.side_effect = [Ok, RuntimeError, Ok]` on 3 topics — assert `_resubscribe_error_count == 1` and remaining topics were still attempted |
| `test_stop_during_recovery_aborts_remaining_topics` | Long registry; `stop()` mid-recovery; assert recovery bailed early |
| `test_concurrent_subscribe_during_recovery_does_not_hang` | Producer thread calls `broker.subscribe(X)` while recovery is iterating; recovery completes; final `_registry` contains X |
| `test_concurrent_unsubscribe_during_recovery_final_state_matches_intent` | Same shape as above but with unsubscribe |
| `test_recovery_metrics_snapshot_is_consistent` | Read multiple counters via `recovery_metrics()` and via individual attributes; snapshot values are coherent |
| `test_multiple_reconnect_cycles_are_idempotent` | disconnect → reconnect → disconnect → reconnect; every cycle re-subscribes the registry once |
| `test_broker_lock_is_never_held_across_client_call` | Use a spy on `client.subscribe` that acquires `_state_lock`; assert no deadlock |

### Legacy suites

`unit_test/*` and `exe_test/*` remain quarantined via `pyproject.toml` `norecursedirs`. Not affected.

---

## 11. Acceptance criteria

Before this RFC's implementation PR can merge:

1. `PYTHONPATH=src pytest tests/unit` reports **all-pass** with **0 xfailed, 0 xpassed, 0 failed**.
   - Baseline before implementation: 194 passed, 6 xfailed.
   - Target after implementation: ~200 passed, 0 xfailed (6 xfails flip to pass; ~8 characterization tests inverted; ~9 new tests added).
2. `MqttBroker.subscribe` and `MqttBroker.unsubscribe` maintain `_registry` correctly (asserted by direct inspection).
3. On reconnect (second and subsequent successful `_on_connect`), every entry in `_registry` sees a fresh `client.subscribe(topic)` call BEFORE `notifier._on_connect()` is called.
4. `stop()` sets `_stopped=True` under `_state_lock`. Subsequent `subscribe` / `unsubscribe` return `None` without forwarding; subsequent `_on_connect(rc=0)` does not recover and does not notify.
5. `_on_disconnect` sets `_last_disconnect_was_planned`, clears `_connect_ok`, and clears `_connected_evt`.
6. `recovery_metrics()` returns a coherent snapshot: `active_subscriptions`, `resubscribe_success_count`, `resubscribe_error_count`, `recovery_run_count`, `stopped`, `ever_connected`.
7. A test verifies that `_state_lock` is never held during a `client.subscribe` / `client.publish` / `client.unsubscribe` / `client.disconnect` / `client.loop_stop` call (via a spy that would deadlock if the lock were held).
8. Per-topic resubscribe failure is isolated: `_resubscribe_error_count` reflects the count; other topics are attempted.
9. RFC-001 (27), RFC-002 (46), RFC-003 (21), RFC-004 (33) tests pass **unchanged**.
10. No changes to:
    - `src/agentflow/core/parcel.py`
    - `src/agentflow/core/agent.py`
    - `src/agentflow/broker/message_broker.py` (MessageBroker ABC)
    - `src/agentflow/broker/notifier.py`
    - Any test outside `tests/unit/test_mqtt_broker_reconnect.py` and `tests/unit/test_mqtt_broker_callbacks.py`
    - `pyproject.toml`
11. This RFC file has status changed from `Draft` to `Accepted` in the same PR.
12. `docs/audit/05-risk-register.md` R-03 status changed to `Resolved` in the same PR.

Out of scope (deferred to future RFCs):
- Connection generation / epoch counter (Option E).
- paho `reconnect_on_failure=True` fast-path (Option A hybrid).
- `BrokerNotifier.on_reconnect` callback (Option B / D).
- Offline publish queue.
- Real-network retry policy / exponential backoff.
- Broker-cluster failover.

---

## 12. Rollback plan

Rollback trigger — any of:

- A caller that read `broker._connect_ok` as "was ever connected" and relied on the stuck-True value.
- A user that relied on subscribe-after-stop silently going through to paho (extremely unlikely but possible in test harnesses).
- Deadlock or livelock in the recovery path.
- Any regression in R-01 / R-02 / R-03 / R-04 / R-05 / R-13 tests.
- Per-topic recovery failures becoming so frequent that logs are overwhelmed — indicates a paho compatibility issue.

Rollback procedure — single `git revert` of the merge commit. Because:

- All changes are internal to `MqttBroker`; no ABC / API / wire changes to reconcile.
- New attributes (`_registry`, `_stopped`, `_ever_connected`, `_last_disconnect_was_planned`, recovery counters, `_state_lock`) are all internal.
- `recovery_metrics()` and `last_disconnect_was_planned` are additive; removing them cannot break any caller that did not adopt them.
- Test changes revert alongside; the 6 xfails re-mark.

Not rollback-safe: any additional change bundled into the same PR that modifies Parcel, MessageBroker ABC, BrokerNotifier ABC, or Agent public API. This RFC forbids bundling.

Post-rollback state: R-03 returns to "Confirmed by runtime evidence, unresolved". The 6 aspirational xfails re-appear. The 26 characterization tests (which pin current behaviour) continue to pass.

Interim mitigation (available without revert): if a specific deployment scenario reveals a regression, the recovery loop can be disabled via a config-level opt-out — for example an internal `_recovery_enabled` flag defaulting to `True` and settable to `False` for the affected deployment. This is not part of the RFC's default configuration but is a low-risk hedge that can be added if needed.

---

## Appendix A — Why not extend `MessageBroker` ABC with a `recover_subscriptions` method?

Two reasons:

1. **Only `MqttBroker` has a session that persists across reconnect.** `EmptyBroker` / `RedisBroker` / `RosBroker` / `DdsBroker` do not have equivalent semantics. Extending the ABC would force meaningless overrides.
2. **The recovery is entirely an implementation detail of the broker's own lifecycle.** Callers (Agent, tests) do not need to know about it. Keeping the API surface unchanged means every existing caller works without adaptation.

If a future broker (e.g. a Redis one with pub/sub survival semantics) benefits from a shared recovery abstraction, that RFC can factor `MqttBroker`'s implementation into a mixin — additively, without disturbing the ABC.

## Appendix B — Interaction with paho session persistence

`paho.mqtt.Client` supports MQTT session persistence via `clean_start=False` and a stable `client_id`. If both are set AND the MQTT broker keeps the session, paho AND the broker together will resurrect subscriptions on reconnect without any client-side registry.

RFC-005 does NOT set `clean_start=False`. Reasons:

- Requires callers to provide a stable `client_id`, which `MqttBroker` does not currently manage.
- Requires the deployed MQTT broker to persist sessions, which is deployment-dependent.
- Even with session persistence, the client-side registry remains valuable for observability, testing, and for the (common) case where the session has expired.

A future RFC may add opt-in session persistence as a fast-path optimisation on top of the RFC-005 registry — if paho reports the session was resurrected on reconnect, the registry-based recovery can be skipped.
