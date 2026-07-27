# RFC-007 — handler registry ownership

- **Status**: **Implemented (2026-07-27)**
- **Author**: Audit follow-up
- **Depends on**: [RFC-006](RFC-006-publish-sync-topic-collision.md) (§7.11 scope limitation and §Residual risks in `tests/unit/core/test_agent_publish_sync_cross_api.py`)
- **Scope**: Cross-API synchronisation and ownership tracking of `Agent.__topic_handlers`: how `Agent.subscribe`, `Agent.unsubscribe`, `Agent.publish_sync`, and `Agent._on_message` collectively manage the registry
- **Explicitly out of scope**: correlation ID, multi-handler fan-out per topic, Parcel schema, Message Schema, Broker API, ProcessWorker
- **Implementation summary** (2026-07-27):
  - Landed the recommended A + C + D combination in `src/agentflow/core/agent.py`. New internal types `_HandlerOwnerType(Enum)` (`NORMAL`, `PUBLISH_SYNC`) and `_HandlerRecord(frozen dataclass)` are added at module level with leading-underscore names to signal internal use. Both are importable via full path for tests but not re-exported by the package.
  - `Agent.__topic_handlers` value type changed from `Callable` to `_HandlerRecord`. All registry mutations and reads are now performed under the existing `_handlers_lock` (RFC-006's RLock); broker I/O (`broker.subscribe`, `broker.unsubscribe`, `dispatcher.enqueue`, handler invocation, `publish` / `event.wait`) always happens outside the lock.
  - `Agent.subscribe` and `Agent.unsubscribe` fail-fast with `TopicWaitCollisionError` when the target topic is currently owned by a `PUBLISH_SYNC` waiter. NORMAL rebind and NORMAL teardown are preserved unchanged.
  - `Agent.publish_sync` refuses to trample a NORMAL handler as well as a PUBLISH_SYNC waiter; both cases raise `TopicWaitCollisionError` with an owner-aware message.
  - `Agent._on_message` acquires the lock once, reads the record, then decides `is_specific_handler` + `topic_handler` from a single snapshot — closing the pre-RFC-007 TOCTOU.
  - Cleanup in `publish_sync.finally` runs a triple check under the lock: record exists AND `owner_type is PUBLISH_SYNC` AND `handler is handle_response`.
  - Test surface: `tests/unit/core/test_agent_publish_sync_cross_api.py` fully rewritten (19 tests, 4 categories); `tests/unit/core/test_agent_publish_sync.py` and `test_agent_publish_sync_concurrency.py` migrated for the new `HandlerRecord` shape (`.handler` accessor).
  - Full unit regression: `PYTHONPATH=src python -m pytest tests/unit` → **256 passed, 0 failed, 0 xfailed(strict-pass), 2 xfailed** in 12.03 s. The two remaining xfails are the RFC-006 out-of-scope items (`test_both_callers_should_receive_own_response_with_shared_topic_wait`, `test_framework_should_support_multiple_handlers_per_topic`) — deferred to a future RFC on correlation ID / multi-handler fan-out.
  - No regression: R-01 / R-02 (27) / R-03 (48) / R-04 (33) / R-05 (21) / R-13 (46) / RFC-006 same-API (21) — combined 196 tests pass unchanged.
  - 3 independent stability re-runs of the RFC-007 test file — all 19/19 pass, no flakes.
  - See [R-14 resolution block](../audit/05-risk-register.md#r-14--shared-dictionaries-without-locks) and the extended R-02 residual note for the cross-references.

---

## 1. Problem statement

RFC-006 introduced `Agent._handlers_lock` for atomic check-and-register and atomic identity-check-and-pop, but only inside `Agent.publish_sync`. Three residual risks (R.6-1, R.6-2, R.6-3) confirmed by `tests/unit/core/test_agent_publish_sync_cross_api.py`:

- **R.6-1** — `Agent.subscribe('T', new_handler)` while another thread is inside `publish_sync(topic_wait='T', ...)` silently overwrites the waiter's handler. Waiter times out; response is routed to the overwriting handler.
- **R.6-2** — `Agent.unsubscribe('T')` while a `publish_sync` waiter is active pops the waiter's handler and calls `broker.unsubscribe('T')`. Waiter times out.
- **R.6-3** — `Agent._on_message` reads `__topic_handlers` with two independent operations (`in` check, then `.get()`) — a TOCTOU window can produce inconsistent dispatch decisions when a concurrent mutation lands between them.

RFC-006 intentionally left these open (§7.11) to avoid disturbing the legitimate warn-then-overwrite pattern used by `Agent.subscribe` callers who rebind a handler. RFC-007 addresses all three by introducing explicit ownership tagging on registry entries and by extending `_handlers_lock` coverage to every registry operation.

---

## 2. Runtime evidence

Baseline: `PYTHONPATH=src pytest tests/unit` → **249 passed, 2 xfailed in 12.82 s**.

Cross-API characterisation (all currently PASSED, documenting the residual risks that RFC-007 will convert into fail-fast exceptions):

| Test | Documents |
|---|---|
| `test_direct_subscribe_can_overwrite_publish_sync_waiter` | R.6-1 overwrite |
| `test_response_after_direct_subscribe_overwrite_reaches_replacement` | R.6-1 wrong routing |
| `test_publish_sync_finally_does_not_evict_replacement_handler` | RFC-001 identity guard mitigation only |
| `test_direct_unsubscribe_removes_publish_sync_waiter_handler` | R.6-2 removal |
| `test_late_response_after_direct_unsubscribe_falls_through_silently` | R.6-2 consequence + RFC-003 fallback silent |
| `test_publish_sync_finally_no_double_unsubscribe_after_direct_unsubscribe` | RFC-001 identity guard mitigation only |
| `test_direct_subscribe_rebind_without_active_publish_sync_is_intentional` | RFC-006 §7.11 legitimate rebind |
| `test_direct_unsubscribe_of_own_handler_is_expected_use_case` | Legitimate teardown |
| `test_on_message_registry_read_is_atomic_under_gil` | GIL-atomic dict.get |
| `test_on_message_dispatch_survives_concurrent_direct_subscribe_stress` | R.6-3 no crash under stress |
| `test_on_message_dispatch_survives_concurrent_direct_unsubscribe_stress` | R.6-3 no crash |
| `test_on_message_dispatch_survives_publish_sync_and_concurrent_direct_ops` | R.6 combined no crash |

RFC-007's fix will convert the first six behavioural characterisations into positive assertions: direct subscribe/unsubscribe on a `publish_sync`-owned topic raises `TopicWaitCollisionError`. The three rebind / teardown tests (D category) remain unchanged.

---

## 3. Current state

Source: `src/agentflow/core/agent.py` (post-RFC-006).

| API | Lock use | Registry semantics |
|---|---|---|
| `Agent.subscribe(topic, ..., topic_handler)` | **None** | Warn-then-overwrite unconditional |
| `Agent.unsubscribe(topic)` | **None** | Pop unconditional |
| `Agent.publish_sync` register | `_handlers_lock` (RFC-006) | Raise `TopicWaitCollisionError` if `topic in dict`; else register |
| `Agent.publish_sync` cleanup | `_handlers_lock` (RFC-006) | Identity-check + pop |
| `Agent._on_message` | **None** | Two-step read: `topic in dict` then `dict.get(topic, on_message)` |

Registry value type: `Callable` (the handler function directly).

---

## 4. Desired state

- Registry value type: `HandlerRecord(owner_type, handler)`.
- Two owner types: `NORMAL` (from `Agent.subscribe`) and `PUBLISH_SYNC` (from `Agent.publish_sync`).
- All five operations above acquire the same `_handlers_lock` for the registry-mutation / registry-read portion. Broker I/O (`broker.subscribe`, `broker.unsubscribe`, `broker.publish`) always happens **outside** the lock.
- Direct `Agent.subscribe` on a `PUBLISH_SYNC`-owned topic: raise `TopicWaitCollisionError`.
- Direct `Agent.unsubscribe` on a `PUBLISH_SYNC`-owned topic: raise `TopicWaitCollisionError`.
- `Agent.publish_sync` on a topic with any pre-existing record (NORMAL or PUBLISH_SYNC): raise `TopicWaitCollisionError` (unchanged from RFC-006 semantically — the check `topic in dict` covers both).
- Direct `Agent.subscribe` on a NORMAL-owned topic: unchanged — warn and overwrite.
- Direct `Agent.subscribe` / `Agent.unsubscribe` on an empty topic slot: unchanged.
- `Agent._on_message` reads the record atomically under the lock; `should_auto_reply` is decided from a single snapshot; the dispatcher handles the dispatch outside the lock.

---

## 5. Options considered

### Option A — Extend `_handlers_lock` to subscribe / unsubscribe / _on_message (no ownership tagging)

Every registry mutation and read acquires `_handlers_lock`, but registry values remain plain `Callable`s.

| Aspect | Analysis |
|---|---|
| Closes R.6-3 TOCTOU | ✓ |
| Prevents R.6-1 overwrite | ✗ — subscribe still overwrites unconditionally (nothing distinguishes a publish_sync waiter) |
| Prevents R.6-2 cancellation | ✗ — same |
| No shape change to registry | ✓ |
| Test-helper migration | Minimal |
| Verdict | Insufficient alone; solves only the TOCTOU, not the semantic overwrite hazards |

### Option B — Add a separate `_reserved_topics: set[str]` for publish_sync ownership

Publish_sync register adds to both `__topic_handlers` and `_reserved_topics`; cleanup removes from both. `Agent.subscribe` / `Agent.unsubscribe` check `_reserved_topics` before proceeding.

| Aspect | Analysis |
|---|---|
| Closes R.6-1 / R.6-2 / R.6-3 | ✓ (paired with Option A's lock) |
| Shape change to registry values | ✗ (values stay `Callable`) |
| Two data structures to keep in sync | Slight risk of drift; requires discipline in every mutation site |
| Test-helper migration | Minimal — `_handlers(agent).get('T') is handle_response` still works |
| Verdict | Viable; smaller migration cost than C but two-source-of-truth is a design smell |

### Option C — Change registry value type to `HandlerRecord(owner_type, handler)` (**recommended core**)

Registry becomes `dict[str, HandlerRecord]`. Every mutation site now writes `HandlerRecord(NORMAL, handler)` or `HandlerRecord(PUBLISH_SYNC, handler)`. Every read unwraps to `.handler`.

| Aspect | Analysis |
|---|---|
| Closes R.6-1 / R.6-2 / R.6-3 | ✓ (paired with Option A's lock and Option D's fail-fast) |
| Single source of truth | ✓ |
| Shape change to registry values | Yes; test helpers that read the dict directly need one line of adaptation |
| Backward compat for `Agent.subscribe` legitimate rebind | ✓ — NORMAL over NORMAL still works |
| Verdict | Recommended core primitive |

### Option D — Direct subscribe / unsubscribe on a PUBLISH_SYNC-owned topic fails fast (**recommended policy**)

The concrete behavioural rule that Option C's owner tag enables.

| Aspect | Analysis |
|---|---|
| Reuses RFC-006 exception `TopicWaitCollisionError` | ✓ — consistent, no new class |
| Preserves NORMAL rebind (RFC-006 §7.11 spirit) | ✓ |
| Breaks callers that intentionally rebind a publish_sync topic | Yes — but no such caller exists in the codebase; the pattern is a bug in disguise |
| Verdict | Recommended policy on top of Options A + C |

### Option E — Docs-only: warn callers not to race publish_sync topics

No code change; add warnings to `Agent.publish_sync`, `Agent.subscribe`, `Agent.unsubscribe` docstrings.

| Aspect | Analysis |
|---|---|
| Closes R.6-1 / R.6-2 / R.6-3 | ✗ (relies entirely on caller discipline) |
| Framework contract clarity | Slight improvement |
| Verdict | Rejected — RFC-006 already established that framework-enforced protection is worth the small break, and the residual risks are exactly the same class as R.4 which we already resolved with fail-fast |

### Comparison summary

| Criterion | A | B | **A+B** | **A+C+D** | E |
|---|---|---|---|---|---|
| Closes TOCTOU (R.6-3) | ✓ | Partial (needs A) | ✓ | ✓ | ✗ |
| Closes R.6-1 / R.6-2 | ✗ | ✓ | ✓ | ✓ | ✗ |
| Preserves NORMAL rebind | ✓ | ✓ | ✓ | ✓ | ✓ |
| Single source of truth | ✓ | ✗ | ✗ | ✓ | ✓ |
| Test-helper migration cost | Zero | Zero | Zero | Small | Zero |
| No new exception type | ✓ | ✓ | ✓ | ✓ (reuses `TopicWaitCollisionError`) | ✓ |
| Verdict | insufficient | viable | viable-lite | **chosen** | rejected |

---

## 6. Recommended design (Options A + C + D combined)

Adopt three changes together:

1. **Extend `_handlers_lock` coverage** to `Agent.subscribe`, `Agent.unsubscribe`, and `Agent._on_message` (Option A). Broker I/O stays outside the lock.
2. **Introduce `HandlerRecord`** as the registry value type (Option C).
3. **Fail-fast on cross-API collision** with `PUBLISH_SYNC`-owned topics (Option D).

### 6.1 New types

Added to `src/agentflow/core/agent.py` (or `agentflow.core.errors` if a dedicated errors module is preferred):

```python
from enum import Enum
from dataclasses import dataclass
from typing import Callable


class HandlerOwnerType(Enum):
    NORMAL = 'normal'
    PUBLISH_SYNC = 'publish_sync'


@dataclass(frozen=True)
class HandlerRecord:
    owner_type: HandlerOwnerType
    handler: Callable
```

`TopicWaitCollisionError` (RFC-006) is **reused unchanged**.

### 6.2 Illustrative `Agent.subscribe`

```python
@final
def subscribe(self, topic, data_type: str = "str", topic_handler=None):
    if not isinstance(data_type, str):
        raise TypeError(...)   # unchanged
    should_forward = True
    if topic_handler:
        with self._handlers_lock:
            existing = self.__topic_handlers.get(topic)
            if existing is not None and existing.owner_type is HandlerOwnerType.PUBLISH_SYNC:
                raise TopicWaitCollisionError(
                    f"topic {topic!r} is currently reserved by an active "
                    f"publish_sync caller; direct subscribe is refused"
                )
            if existing is not None:
                logger.warning(self.M(f"Exist the handler for topic: {topic}"))
            self.__topic_handlers[topic] = HandlerRecord(
                HandlerOwnerType.NORMAL, topic_handler,
            )
    # broker.subscribe outside the lock
    return self._broker.subscribe(topic, data_type) if self._broker else None
```

### 6.3 Illustrative `Agent.unsubscribe`

```python
@final
def unsubscribe(self, topic: str) -> None:
    with self._handlers_lock:
        existing = self.__topic_handlers.get(topic)
        if existing is not None and existing.owner_type is HandlerOwnerType.PUBLISH_SYNC:
            raise TopicWaitCollisionError(
                f"topic {topic!r} is currently reserved by an active "
                f"publish_sync caller; direct unsubscribe is refused"
            )
        self.__topic_handlers.pop(topic, None)
    if self._broker:
        self._broker.unsubscribe(topic)
```

### 6.4 `Agent.publish_sync` — updated register + cleanup

```python
# register (RFC-006 + RFC-007)
with self._handlers_lock:
    if pcl.topic_return in self.__topic_handlers:
        raise TopicWaitCollisionError(
            f"topic_wait {pcl.topic_return!r} is already registered "
            f"(publish_sync collision or normal subscribe conflict)"
        )
    self.__topic_handlers[pcl.topic_return] = HandlerRecord(
        HandlerOwnerType.PUBLISH_SYNC, handle_response,
    )

# ... try: broker.subscribe / publish / wait ...

# cleanup (RFC-001 identity + RFC-006 atomic + RFC-007 owner check)
with self._handlers_lock:
    record = self.__topic_handlers.get(pcl.topic_return)
    if (record is not None
            and record.owner_type is HandlerOwnerType.PUBLISH_SYNC
            and record.handler is handle_response):
        self.__topic_handlers.pop(pcl.topic_return, None)
        need_broker_unsubscribe = True
    else:
        need_broker_unsubscribe = False
if need_broker_unsubscribe:
    try:
        if self._broker:
            self._broker.unsubscribe(pcl.topic_return)
    except Exception as cleanup_ex:
        logger.exception(cleanup_ex)
```

The `record.owner_type is PUBLISH_SYNC` check is defence-in-depth: identity match `record.handler is handle_response` already implies PUBLISH_SYNC ownership because that closure was only ever wrapped as PUBLISH_SYNC.

### 6.5 `Agent._on_message` — atomic snapshot

```python
@final
def _on_message(self, topic: str, data):
    pcl = Parcel.from_payload(data)

    # RFC-007: atomic single-lock snapshot of the registry.
    with self._handlers_lock:
        record = self.__topic_handlers.get(topic)
        if record is not None:
            is_specific_handler = True
            topic_handler = record.handler
        else:
            is_specific_handler = False
            topic_handler = self.on_message
    should_auto_reply = bool(pcl.topic_return) and is_specific_handler

    def handle_message():
        # RFC-003 auto-reply body unchanged
        ...

    self._get_dispatcher().enqueue(handle_message, topic=topic)
```

The dispatch itself (RFC-004) still runs outside the lock; the lock only protects the two-step read.

### 6.6 Broker I/O outside the lock

Consistent with RFC-005 §6 and RFC-006 §7.10. `_handlers_lock` is **never** held while calling `broker.subscribe`, `broker.unsubscribe`, `broker.publish`, `broker.disconnect`, `broker.loop_stop`, or the dispatcher's `enqueue`. All broker calls happen either before or after the `with self._handlers_lock:` block.

---

## 7. Concrete decisions (all 14)

### 7.1 Registry value change to HandlerRecord
**Yes.** `__topic_handlers: dict[str, HandlerRecord]`. Adds ownership tag; single source of truth. Small test-helper migration cost is worth the clarity.

### 7.2 Owner types
Two values in an `Enum`: `NORMAL` (from `Agent.subscribe`) and `PUBLISH_SYNC` (from `Agent.publish_sync`). Future RFCs may add more (e.g., `TRANSIENT`, `SYSTEM`) without breaking the shape.

### 7.3 `Agent.subscribe` on a PUBLISH_SYNC-owned topic
**Raise `TopicWaitCollisionError`.** Reuses RFC-006 exception. Message identifies that the topic is reserved by an active `publish_sync` waiter.

### 7.4 `Agent.unsubscribe` on a PUBLISH_SYNC-owned topic
**Raise `TopicWaitCollisionError`.** Symmetric with §7.3. Cannot silently cancel a waiter.

### 7.5 NORMAL handler rebind
**Allowed and unchanged.** `Agent.subscribe` on a NORMAL-owned topic emits the existing WARNING log and overwrites. RFC-006 §7.11 spirit preserved.

### 7.6 `Agent.publish_sync` on a topic already registered by NORMAL
**Raise `TopicWaitCollisionError`.** Symmetric with §7.3: a `publish_sync` waiter must not silently trample a normal subscriber. Semantically consistent with RFC-006's "never clobber someone else's handler". Callers who need to reuse a topic name must unsubscribe first (which they own and can safely do).

**Note**: this is already the behaviour under RFC-006 as a side effect of the `topic in dict` check — RFC-007 keeps the same effect but the error message can be more informative (mentions NORMAL vs PUBLISH_SYNC owner).

### 7.7 `_on_message` atomic lookup
Single `with self._handlers_lock:` block wraps both the presence check and the value read; `topic_handler` and `is_specific_handler` are decided from a single snapshot. Dispatch runs outside the lock via the RFC-004 dispatcher.

### 7.8 Cleanup ownership verification
`publish_sync`'s finally checks all three under the lock: `record is not None`, `record.owner_type is PUBLISH_SYNC`, `record.handler is handle_response`. Only then does it pop and set `need_broker_unsubscribe = True`. Any of the three failing → no pop, no `broker.unsubscribe`.

### 7.9 Exception type
**Reuse `TopicWaitCollisionError`** (introduced in RFC-006). Do not add a new subclass. The message field distinguishes context (waiter collision, direct-subscribe collision, direct-unsubscribe collision).

### 7.10 Broker I/O timing
Never inside `with self._handlers_lock:`. Every `broker.subscribe` / `unsubscribe` call is made either before acquiring the lock (never done) or after releasing it. Guarantees no deadlock with paho's own internal locks.

### 7.11 Backward compatibility
See §8.

### 7.12 Logging
- NORMAL rebind: **existing** `logger.warning(self.M(f"Exist the handler for topic: {topic}"))` in `Agent.subscribe`, unchanged.
- Collision: **no** framework log (per RFC-006 §7.13); the raised `TopicWaitCollisionError` is the diagnostic. The message text distinguishes context.

### 7.13 Acceptance criteria
See §10.

### 7.14 Rollback plan
See §11.

---

## 8. Backward compatibility

### Source compatibility

| Symbol | Before RFC-007 | After RFC-007 | Compatibility |
|---|---|---|---|
| `Agent.publish` / `Agent.publish_sync` / `Agent._publish_or_raise` signatures | Same | Same | Full |
| `Agent.subscribe(topic, data_type, topic_handler)` signature | Same | Same | Full |
| `Agent.unsubscribe(topic)` signature | Same | Same | Full |
| `Agent._on_message` signature | Same | Same | Full |
| `TopicWaitCollisionError` | New from RFC-006 | Reused, message shapes extended | Additive |
| `HandlerRecord` | Not defined | New public dataclass in `agentflow.core.agent` | Additive |
| `HandlerOwnerType` | Not defined | New public enum | Additive |
| `Agent.__topic_handlers` value type | `dict[str, Callable]` | `dict[str, HandlerRecord]` | **Internal shape change**; only test helpers that inspect it directly must adapt |
| `Parcel` / `TextParcel` / `BinaryParcel` | — | — | Untouched |
| `MessageBroker` / `MqttBroker` | — | — | Untouched |
| Wire format | — | — | Untouched |

### Behavioural compatibility

New raise paths under `Agent.subscribe` and `Agent.unsubscribe` when the target topic is reserved by an active `publish_sync`:

- **Callers who intentionally rebind a normal handler**: **unchanged** — NORMAL over NORMAL is allowed and still emits the WARNING log.
- **Callers who race a direct `subscribe` against an active `publish_sync` waiter**: **raise** `TopicWaitCollisionError`. This was previously a silent overwrite (R.6-1). No codebase caller does this intentionally.
- **Callers who race a direct `unsubscribe` against an active `publish_sync` waiter**: **raise** `TopicWaitCollisionError`. Previously it silently cancelled the waiter (R.6-2). No codebase caller does this intentionally.
- **`publish_sync` callers**: unchanged behaviour (collision still raises `TopicWaitCollisionError`; error message may be more specific about the collision cause).
- **`Agent._on_message` observers**: dispatch decisions now come from a consistent snapshot; the TOCTOU window (R.6-3) is closed.

### Wire compatibility

None. No parcel or broker changes. Broker.subscribe / broker.unsubscribe calls are unchanged in shape and ordering (still outside the framework lock, following RFC-005 §6).

---

## 9. Interaction with prior RFCs

- **RFC-001 (R-02 cleanup)**: The identity guard in `publish_sync`'s finally continues to work; the check is extended to include `record.owner_type is PUBLISH_SYNC` for defence-in-depth. All 27 R-02 tests should continue to pass with test-helper adaptations only where they inspect `_handlers(agent)` directly.
- **RFC-002 (R-13 fast-fail)**: Publish failure cleanup still runs the same finally block, which now uses the RFC-007 record-aware identity check. 46 R-13 tests unaffected in semantics.
- **RFC-003 (R-05 auto-reply)**: `Agent._on_message`'s auto-reply decision (`should_auto_reply = bool(pcl.topic_return) and is_specific_handler`) now reads from a single snapshot; the R-fallback-silent rule is preserved. 21 R-05 tests unaffected in semantics.
- **RFC-004 (R-04 bounded dispatch)**: The dispatcher is not in the lock's critical section. `_on_message` reads the snapshot under the lock and enqueues outside. 33 R-04 tests unaffected.
- **RFC-005 (R-03 subscription recovery)**: `MqttBroker._registry` is a broker-side concern; `Agent.__topic_handlers` is an Agent-side concern. RFC-007's Agent-side lock does not affect broker-side recovery. 48 R-03 tests unaffected.
- **RFC-006 (R.4 publish_sync collision)**: RFC-007 is the direct successor. The same `_handlers_lock` and the same `TopicWaitCollisionError` are used. The 21 pass + 2 xfail R.6 tests should continue to pass; the 12 residual-risk cross-API tests convert into positive assertions (§10).

The 231 tests spanning R-02 / R-03 / R-04 / R-05 / R-13 / RFC-006 (same-API) form the regression floor for RFC-007.

---

## 10. Test migration plan

### Cross-API characterisation tests that FLIP under RFC-007

In `tests/unit/core/test_agent_publish_sync_cross_api.py`:

| Test (current) | Post-RFC-007 |
|---|---|
| `test_direct_subscribe_can_overwrite_publish_sync_waiter` | **Invert**: assert `TopicWaitCollisionError` is raised; waiter's handler is preserved |
| `test_response_after_direct_subscribe_overwrite_reaches_replacement` | **Rewrite**: response goes to the waiter (no replacement possible); waiter completes |
| `test_publish_sync_finally_does_not_evict_replacement_handler` | **Delete or repurpose**: replacement cannot be installed |
| `test_direct_unsubscribe_removes_publish_sync_waiter_handler` | **Invert**: `TopicWaitCollisionError`; waiter's handler survives |
| `test_late_response_after_direct_unsubscribe_falls_through_silently` | **Rewrite**: no direct unsubscribe possible; keep the "late deliver after cleanup silent-drop" flavor if desired |
| `test_publish_sync_finally_no_double_unsubscribe_after_direct_unsubscribe` | **Delete**: scenario unreachable |
| `test_direct_subscribe_rebind_without_active_publish_sync_is_intentional` | **UNCHANGED** — NORMAL rebind still allowed |
| `test_direct_unsubscribe_of_own_handler_is_expected_use_case` | **UNCHANGED** — teardown on NORMAL topic still allowed |
| `test_on_message_registry_read_is_atomic_under_gil` | UNCHANGED — still true, now additionally protected by lock |
| `test_on_message_dispatch_survives_concurrent_direct_subscribe_stress` | **Adapt**: the concurrent direct subscribe on a not-owned-by-publish_sync topic still succeeds; if any subscribe would collide (unlikely without a publish_sync in the mix), it must raise. Test can keep its "no crash" spirit. |
| `test_on_message_dispatch_survives_concurrent_direct_unsubscribe_stress` | **Adapt** similarly |
| `test_on_message_dispatch_survives_publish_sync_and_concurrent_direct_ops` | **Adapt**: the churner's subscribe/unsubscribe on the topic owned by publish_sync will raise; count collisions bounded; asserted framework does not crash |

### New tests to add

| Test | Purpose |
|---|---|
| `test_direct_subscribe_raises_on_publish_sync_reserved_topic` | Positive assertion of §7.3 |
| `test_direct_unsubscribe_raises_on_publish_sync_reserved_topic` | §7.4 |
| `test_publish_sync_raises_on_normal_subscribed_topic` | §7.6 — publish_sync cannot trample a NORMAL handler |
| `test_normal_rebind_over_normal_is_allowed_and_warns` | §7.5 preserved (parallel to existing RFC-006 §7.11 test) |
| `test_subscribe_collision_error_message_identifies_publish_sync_owner` | Message shape distinguishes vs a publish_sync-vs-publish_sync collision |
| `test_HandlerRecord_owner_type_is_PUBLISH_SYNC_after_publish_sync_register` | Direct inspection of registry value |
| `test_HandlerRecord_owner_type_is_NORMAL_after_agent_subscribe` | Direct inspection |
| `test_on_message_atomic_snapshot_under_concurrent_mutation` | No inconsistent dispatch decisions under stress (paired with the existing R.6-3 stress test's "no crash" — now upgraded to "consistent snapshot") |
| `test_handlers_lock_is_never_held_across_broker_call` | Spy on broker.subscribe / broker.unsubscribe verifies lock is not held (RFC-005 lock-hygiene analogue) |

### Existing R-02 / R-04 / R-05 / R-13 test helpers

Any test that does `_handlers(agent).get('T') is handle_response` needs to become `_handlers(agent).get('T').handler is handle_response` (one word inserted per assertion). Recommended: add a small helper `_handler_of(agent, topic)` that returns `.handler` (or `None`), and migrate call sites in a single sweep.

Grep count in the current tree:
- `_handlers(agent)` reads in `test_agent_publish_sync.py`, `test_agent_publish_sync_concurrency.py`, `test_agent_publish_sync_cross_api.py`, `test_agent_publish_errors.py`, `test_agent_reply_behavior.py`: ~35 lines total. All are one-line adaptations; no assertion logic changes.

### Legacy suites

`unit_test/*` and `exe_test/*` remain quarantined via `pyproject.toml` `norecursedirs`. Not affected.

### FakeBroker

No changes required.

---

## 11. Acceptance criteria

Before this RFC's implementation PR can merge:

1. `PYTHONPATH=src pytest tests/unit` reports **all-pass** with **exactly 2 strict xfailed** remaining (the same two RFC-006 out-of-scope xfails — `test_both_callers_should_receive_own_response_with_shared_topic_wait`, `test_framework_should_support_multiple_handlers_per_topic`).
   - Baseline before implementation: 249 passed, 2 xfailed.
   - Target after implementation: ~255 passed, 2 xfailed (6 residual-risk tests inverted or rewritten; ~9 new tests added; 3 existing kept unchanged; small helper migration in ~35 lines across R-02/R-04/R-05/R-13 tests).
2. `HandlerRecord` and `HandlerOwnerType` exist in `agentflow.core.agent` (or `agentflow.core.errors`).
3. `Agent.__topic_handlers` values are exclusively `HandlerRecord` instances after any registration path (verified by a direct-inspection test).
4. `Agent.subscribe` on a `PUBLISH_SYNC`-owned topic raises `TopicWaitCollisionError`.
5. `Agent.unsubscribe` on a `PUBLISH_SYNC`-owned topic raises `TopicWaitCollisionError`.
6. `Agent.publish_sync` on a topic already registered (either NORMAL or PUBLISH_SYNC owner) raises `TopicWaitCollisionError`.
7. `Agent.subscribe` on a NORMAL-owned topic emits the existing WARNING and overwrites (RFC-006 §7.11 preserved).
8. `Agent._on_message` reads `__topic_handlers` under `_handlers_lock`; `is_specific_handler` and `topic_handler` are decided from a single snapshot (verified via a stress test).
9. `_handlers_lock` is never held during a `broker.subscribe` / `broker.unsubscribe` / `broker.publish` call (verified via spy).
10. R-02 (27), R-04 (33), R-05 (21), R-13 (46), R-03 (48), RFC-006 same-API (21+2 xfail) tests continue to pass; xfail counts unchanged.
11. No changes to:
    - `src/agentflow/core/parcel.py`
    - `src/agentflow/broker/*`
    - `pyproject.toml`
    - Wire format
12. This RFC file has status changed from `Draft` to `Implemented` in the same PR.
13. `docs/audit/05-risk-register.md` — add or amend the R.6 residual-risk entry, marking R.6-1 / R.6-2 / R.6-3 as `Resolved` via RFC-007.

Out of scope (deferred to future RFCs):
- Correlation ID / multi-caller shared-topic support (R-20).
- Multi-handler fan-out per topic.
- ProcessWorker.

---

## 12. Rollback plan

Rollback trigger — any of:

- A caller that intentionally raced `Agent.subscribe` or `Agent.unsubscribe` against `publish_sync` (extremely unlikely; nothing in the codebase does this).
- Contention on `_handlers_lock` measurably degrades `_on_message` throughput below acceptable levels (would require > tens of thousands of messages per second per Agent; not expected in current usage).
- Deadlock in `_handlers_lock` interactions with the dispatcher or broker (would be a bug in the implementation; the lock-hygiene test in §11 item 9 is designed to catch this).
- Regression in R-01 / R-02 / R-03 / R-04 / R-05 / R-13 / RFC-006 tests.

Rollback procedure — single `git revert` of the merge commit. Because:

- `HandlerRecord` and `HandlerOwnerType` are additive; removing them is safe.
- `Agent.subscribe` / `Agent.unsubscribe` / `Agent.publish_sync` / `Agent._on_message` revert to their post-RFC-006 forms.
- No wire / Parcel / broker API changes to reconcile.
- Test-helper migration reverts alongside.

Not rollback-safe: any change bundled in the same PR that modifies Parcel, Broker ABC, or Message Schema. This RFC forbids bundling.

Post-rollback state: R.6-1 / R.6-2 / R.6-3 return to their currently-characterised behaviours. The 12 cross-API tests re-document the residual risks (their positive assertions revert to the pre-RFC-007 documented-hazard form).

Interim mitigation (available without revert): a specific deployment that hits the new `TopicWaitCollisionError` can adjust its usage — either avoid direct subscribe/unsubscribe on topics used as `publish_sync`'s `topic_wait`, or migrate away from explicit `topic_wait` altogether (recommended: use the auto-generated per-call return topic).

---

## Appendix A — Why not adopt Option B (`_reserved_topics` set) instead?

Option B adds a second data structure (`set[str]`) alongside `__topic_handlers` to track publish_sync ownership. It preserves the current dict shape and requires no test-helper migration for reading handlers. The trade-off:

- **Pro (Option B)**: smaller test-helper impact; ~zero migration for R-02/R-04/R-05/R-13 tests that read `_handlers(agent).get('T')`.
- **Con (Option B)**: two data structures, two places to update on every mutation, drift risk. Also, extending to future owner types (SYSTEM, TRANSIENT) requires either more sets or a mapping-of-topic-to-owner-type — approaching the shape of Option C anyway.

Option C's `HandlerRecord` is the cleaner long-term shape. The one-line test-helper adaptation is a fair price. If future implementation experience proves this wrong, Option B remains available as an alternative — the RFC's decisions (§7.3, §7.4, §7.6) apply equally under either data-structure choice.

## Appendix B — Why not lock `_on_connect`'s built-in subscribes?

`Agent._on_connect` (`agent.py`) does several `self.subscribe(...)` calls for parent/child topics. Under RFC-007, each acquires `_handlers_lock`. The `_connected_once` guard prevents second-connect re-subscription (RFC-005 handles reconnect at the broker level), so contention on `_handlers_lock` during connect is bounded to the initial handshake. No special-casing needed.

## Appendix C — Why reuse `TopicWaitCollisionError` rather than introduce new exception classes?

- RFC-006 established `TopicWaitCollisionError(RuntimeError)` for the publish_sync-vs-publish_sync case. The cross-API cases (direct subscribe/unsubscribe on a reserved topic) share the same semantic: the operation was refused because the topic key is already owned by an in-flight `publish_sync`.
- Reusing the exception means callers writing `except TopicWaitCollisionError:` catch all four collision types (waiter-vs-waiter, waiter-vs-direct-subscribe, waiter-vs-direct-unsubscribe, direct-subscribe-vs-waiter) uniformly.
- The exception message field distinguishes the four cases at read time. Tests can pattern-match the message if they want to assert a specific case.
- If future work reveals a need for finer discrimination in catch handlers, subclasses can be introduced additively without breaking existing catchers.

## Appendix D — Interaction with RFC-005's MqttBroker._registry

MqttBroker maintains its own `_registry: dict[topic, data_type]` for reconnect recovery. This is a broker-side registry, entirely distinct from Agent's `__topic_handlers`. RFC-007's `HandlerRecord` type does not appear at the broker layer. On reconnect, MqttBroker re-issues `client.subscribe(topic)` for each of its `_registry` entries; Agent's `__topic_handlers` is unaffected and continues to route delivered messages via `_on_message`'s snapshot lookup.

No cross-registry consistency concern arises because the two registries answer different questions:
- MqttBroker._registry: "which topics should the wire subscription include?"
- Agent.__topic_handlers: "for topic X, what handler (and which owner) should receive it?"
