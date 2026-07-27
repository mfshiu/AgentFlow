# RFC-006 — publish_sync topic_wait collision

- **Status**: Draft
- **Author**: Audit follow-up
- **Depends on**: `docs/audit/05-risk-register.md` R.4 (documented in [RFC-001](RFC-001-publish-sync-subscription-lifecycle.md) §10.R.4 as a pre-existing race); consistent with [RFC-001](RFC-001-publish-sync-subscription-lifecycle.md), [RFC-002](RFC-002-publish-error-propagation.md), [RFC-003](RFC-003-auto-reply-contract.md), [RFC-004](RFC-004-bounded-message-dispatch.md), [RFC-005](RFC-005-mqtt-subscription-recovery.md)
- **Scope**: `Agent.publish_sync`'s handling of an explicit `topic_wait` that is already actively awaited by another `publish_sync` call; `__topic_handlers` registry synchronisation; a new `TopicWaitCollisionError`; register / cleanup linearisation
- **Explicitly out of scope**: Parcel metadata / correlation ID (deferred to a future R-20 RFC), multi-handler fan-out per topic, FIFO waiter queues, broker-level changes, ProcessWorker

---

## 1. Problem statement

`Agent.publish_sync(topic, data, topic_wait=X, timeout=T)` registers a per-call `handle_response` closure in `Agent.__topic_handlers[X]`. `Agent.subscribe` performs an **unconditional overwrite** when the same topic key is already registered:

```python
if topic in self.__topic_handlers:
    logger.warning(...)
self.__topic_handlers[topic] = topic_handler
```

Consequences when two concurrent `publish_sync` calls share the same explicit `topic_wait` (all runtime-confirmed — §2):

- Only the last-writer's closure remains registered. The first caller's `data_event` can never be set.
- A delivered response on that topic is routed to whichever closure happens to be registered — with no correlation between the response and the caller who "generated" it.
- The first caller always times out; the second caller may complete with a response semantically intended for the first.
- Under N callers sharing `topic_wait`, at most one completes; N-1 time out. Verified for N ∈ {3, 10, 50}.
- RFC-001's identity guard in the `finally` block prevents the first caller's cleanup from evicting the second caller's handler in the common ordering, but does not solve the routing ambiguity and has a narrow check-then-pop race window that could evict a foreign handler.

When `topic_wait` is omitted, `publish_sync` calls `__generate_return_topic` to produce a per-call unique return topic (`{tag}-{10 base-36 alnum}/{topic}`) with ~40 bits of entropy. The collision only fires when a caller **explicitly** reuses `topic_wait`.

This RFC proposes the smallest change that turns the collision from a silent overwrite into an immediate, observable failure, while keeping the omitted-`topic_wait` path (the recommended pattern) unchanged and preserving R-01 / R-02 / R-03 / R-04 / R-05 / R-13 contracts.

---

## 2. Runtime evidence

Baseline: `PYTHONPATH=src pytest tests/unit` → **230 passed, 3 xfailed in 10.94 s**.

Confirmed in `tests/unit/core/test_agent_publish_sync_concurrency.py`:

| Behaviour | Test | Result |
|---|---|---|
| Second `subscribe` overwrites first handler in registry | `test_second_subscribe_overwrites_first_handler_in_registry` | PASSED |
| Two `publish_sync` threads: only second handler registered | `test_publish_sync_two_threads_leave_only_second_handler_registered` | PASSED |
| Only second caller receives the delivered response | `test_only_second_caller_can_receive_delivered_response` | PASSED |
| Response semantically for caller A reaches caller B | `test_response_semantically_for_first_caller_reaches_second_caller` | PASSED |
| N callers same `topic_wait`: exactly one completes | `test_N_callers_same_topic_wait_at_most_one_completes[3/10/50]` | PASSED × 3 |
| Identity guard: A's cleanup does not evict B's handler | `test_first_caller_cleanup_does_not_evict_second_callers_handler` | PASSED |
| B's cleanup: no double-unsubscribe | `test_second_caller_cleanup_removes_handler_no_double_unsubscribe` | PASSED |
| Late response after B completes silently dropped | `test_late_response_after_second_caller_completes_is_silently_dropped` | PASSED |
| Distinct `topic_wait`: both callers complete | `test_distinct_topic_wait_both_callers_receive_own_response` | PASSED |
| Omitted `topic_wait`: unique per-call correlation | `test_omitted_topic_wait_auto_generates_unique_correlation` | PASSED |
| **Aspirational**: second call with same `topic_wait` should fail fast | `test_second_publish_sync_with_same_topic_wait_should_fail_fast` | **XFAIL (strict)** |
| **Aspirational**: both callers should receive own response | `test_both_callers_should_receive_own_response_with_shared_topic_wait` | XFAIL (strict) — deferred to future RFC |
| **Aspirational**: framework should support multiple handlers per topic | `test_framework_should_support_multiple_handlers_per_topic` | XFAIL (strict) — deferred to future RFC |

RFC-006's fix flips **one** of the three strict xfails to pass (`test_second_publish_sync_with_same_topic_wait_should_fail_fast`). The other two xfails remain, tracking the future correlation-ID and fan-out work.

Narrow-race caveat documented in the test file's module footer: `check-then-pop` inside `publish_sync`'s finally is not atomic — a foreign handler could be evicted if it overwrites between the identity check and the pop. Very narrow window; not deterministic under test but real. Addressed by decision 5 below.

---

## 3. Current state

Source: `src/agentflow/core/agent.py`.

```mermaid
sequenceDiagram
    autonumber
    participant Cs as Caller A + Caller B (threads)
    participant Ag as Agent
    participant H as __topic_handlers
    participant Br as Broker
    Cs->>Ag: A.publish_sync(topic, data_A, topic_wait='T')
    Ag->>H: register 'T' → A_closure   (silent, no lock)
    Ag->>Br: broker.subscribe('T'), broker.publish(topic, A_pcl)
    Ag->>Ag: A blocks on event_A.wait

    Cs->>Ag: B.publish_sync(topic, data_B, topic_wait='T')
    Ag->>H: register 'T' → B_closure   (OVERWRITE with WARNING)
    Ag->>Br: broker.subscribe('T'), broker.publish(topic, B_pcl)
    Ag->>Ag: B blocks on event_B.wait

    Br-->>Ag: deliver 'T', response
    Ag->>H: get('T') → B_closure  (A_closure is gone)
    Ag->>Ag: B_closure sets event_B
    Note over Cs: B returns response; A times out
```

Post-conditions:
- Exactly one caller wakes; the other times out.
- `broker.subscribe` was called twice for the same topic (broker-side R-03 concern, characterised separately).
- `Agent.subscribe` emits `logger.warning(...)` but returns normally.

---

## 4. Desired state

- On collision detection at `publish_sync` entry, immediately raise `TopicWaitCollisionError` (a new `RuntimeError` subclass). No subscribe, no publish, no wait.
- The check and the register are performed **atomically** under a per-Agent `_handlers_lock` so the collision decision cannot race with a concurrent register.
- The `finally` cleanup's identity check and pop are also performed atomically under the same lock, closing the narrow-race window documented in the R.4 characterisation.
- Omitted-`topic_wait` callers see no change: `__generate_return_topic` produces per-call unique topics, so no collision can arise.
- `Agent.subscribe` (the general-purpose subscribe API) retains its current warn-then-overwrite semantics. RFC-006 does **not** change that behaviour to avoid disturbing subscribers whose overwrite behaviour is intentional (e.g. rebinding a handler on reconfiguration).
- Wire format, Parcel schema, Broker API, and all public method signatures are unchanged.

---

## 5. Options considered

### Option A — Keep silent overwrite

Do nothing. Rely on the existing `logger.warning` inside `Agent.subscribe`.

| Aspect | Analysis |
|---|---|
| Solves R.4 | ✗ |
| Backward compat | ✓ |
| Framework contract clarity | ✗ (silent-overwrite is a footgun) |
| Verdict | rejected |

### Option B — Warning only (retain overwrite, log more loudly)

Upgrade the WARNING to include actionable context (topic name, both closure identities).

| Aspect | Analysis |
|---|---|
| Solves R.4 | ✗ still overwrites; first caller still times out |
| Backward compat | ✓ |
| Framework contract clarity | Marginal — log parsers can react, but code path is unchanged |
| Verdict | rejected |

### Option C — Collision fail-fast (**RECOMMENDED**)

At `publish_sync` entry, atomically test-and-register under a lock; on collision, raise `TopicWaitCollisionError`. Same lock protects the finally's identity-check-and-pop.

| Aspect | Analysis |
|---|---|
| Solves R.4 for the caller-explicit-topic_wait case | ✓ |
| Solves narrow check-then-pop race | ✓ (both operations under the same lock) |
| Backward compat | ✓ signature unchanged; caller behaviour changes only on the collision path (which previously produced silent failure) |
| Impact on `Agent.subscribe` | None — kept as warn+overwrite |
| Impact on omitted-`topic_wait` callers | None — no explicit topic to collide |
| Impact on multi-waiter or correlation-id scenarios | None — those are separate concerns |
| Complexity | Very low — one lock, one exception class, ~15 lines |
| Verdict | **Recommended.** Smallest change that turns the silent hazard into an observable, actionable error. |

### Option D — Per-topic waiter queue (FIFO)

Under a lock, allow multiple waiters per topic; deliver each incoming response to the head-of-queue.

| Aspect | Analysis |
|---|---|
| Solves R.4 for two callers | Partially — routing is FIFO, so caller A's response goes to whichever caller registered first, not to the caller whose request "matched" it. Still no correlation guarantee. |
| Requires framework-level fan-out changes | ✓ — `__topic_handlers` value becomes a queue / list |
| Interaction with duplicate / late responses | Complex: a late response could pop the head waiter (evict) or be silently dropped — semantic choice needed |
| Complexity | Medium |
| Verdict | rejected — solves the wrong problem; caller-intended routing is not by arrival order. Fan-out is deferred to a future RFC alongside correlation ID. |

### Option E — Correlation ID

Add a correlation-ID metadata field to `Parcel`; `publish_sync` embeds a per-call ID; the response echoes it; the framework matches ID → waiter.

| Aspect | Analysis |
|---|---|
| Solves R.4 correctly | ✓ (arbitrary many concurrent callers, no key collision at framework level) |
| Requires Parcel schema change | ✓ — new field, wire-visible, versioning concern |
| Requires broker + responder cooperation | ✓ (responder must echo the ID) |
| Out of RFC-006 scope | ✓ — falls under R-20 (message metadata) |
| Verdict | out of scope. Explicitly deferred to a future RFC that considers Parcel schema. |

### Option F — Force unique auto-generated topic even when caller supplies `topic_wait`

`publish_sync` ignores `topic_wait` and always generates its own return topic.

| Aspect | Analysis |
|---|---|
| Solves R.4 | ✓ (never any user-controlled topic collision) |
| Preserves `topic_wait` semantics | ✗ — caller specified a topic and expected it; framework silently uses a different one |
| Breaks Parcel `topic_return` pre-set case (RFC-001-tested scenario) | ✓ (regression) |
| Verdict | rejected — silently changes wire behaviour and breaks tests that pin the pre-set `topic_return` contract |

### Comparison summary

| Criterion | A | B | **C** | D | E | F |
|---|---|---|---|---|---|---|
| Solves R.4 collision | ✗ | ✗ | **✓** | Partial | ✓ | ✓ |
| Preserves omitted-`topic_wait` path | ✓ | ✓ | **✓** | ✓ | ✓ | ✓ |
| Preserves caller-supplied `topic_wait` semantics | ✓ | ✓ | **✓** | ✓ | ✓ | ✗ |
| No Parcel / schema change | ✓ | ✓ | **✓** | ✓ | ✗ | ✓ |
| No Broker ABC change | ✓ | ✓ | **✓** | ✓ | ✓ | ✓ |
| Closes narrow check-then-pop race | ✗ | ✗ | **✓** | ✓ | ✓ | n/a |
| Lines changed | 0 | ~2 | ~15 | ~60 | ~100+ | ~5 |
| Verdict | rejected | rejected | **chosen** | rejected | out of scope | rejected |

---

## 6. Recommended design (Option C)

Adopt fail-fast collision detection scoped to `Agent.publish_sync`.

### 6.1 New exception

Add to `src/agentflow/core/agent.py` (or a small `errors.py` if preferred):

```python
class TopicWaitCollisionError(RuntimeError):
    """Raised by Agent.publish_sync when the caller-supplied
    topic_wait is already actively awaited by another publish_sync
    on the same Agent."""
```

Chosen base: `RuntimeError`. Reasons:
- Not a general programmer error → not `Exception`-with-nothing-specific.
- Not I/O related → not `OSError`.
- Not caused by the broker → not `ConnectionError`.
- The condition is transient in principle (the collision could resolve when the other caller completes), so a retryable runtime error is the closest fit.

### 6.2 New lock

```python
# In Agent.__init__
self._handlers_lock = threading.RLock()
```

`RLock` chosen so that `Agent.unsubscribe`, which is also called from within the identity guard, can be safely invoked while the lock is held without deadlock in either direction (future-proofing).

The lock protects **only** `__topic_handlers` mutations relevant to `publish_sync`:
- Collision check-and-register at `publish_sync` entry.
- Identity-check-and-pop in `publish_sync`'s `finally`.

`Agent.subscribe` (the general API) is **NOT** put under the lock. Its existing warn-then-overwrite semantics remain, and its callers see no behavioural change. This is a deliberate scope limitation to avoid changing every subscribe call in existence.

### 6.3 publish_sync change (sketch)

```python
# Existing setup (parcel build, DataEvent, handle_response closure) …

# RFC-006: atomic collision check + register, under _handlers_lock.
with self._handlers_lock:
    if pcl.topic_return in self.__topic_handlers:
        raise TopicWaitCollisionError(
            f"topic_wait {pcl.topic_return!r} is already awaited by "
            f"another publish_sync on this Agent"
        )
    self.__topic_handlers[pcl.topic_return] = handle_response

# broker.subscribe is OUTSIDE the lock (never hold framework lock
# across a broker/network call — same principle as RFC-005 §6).
if self._broker:
    self._broker.subscribe(pcl.topic_return, "str")

try:
    self._publish_or_raise(topic, pcl)
    if data_event.event.wait(timeout):
        return data_event.data
    raise TimeoutError(f"No response received within timeout period for topic: {pcl.topic_return}.")
finally:
    # RFC-001 identity guard + RFC-006 atomicity: check-and-pop under
    # the same lock so a concurrent register cannot slip between.
    with self._handlers_lock:
        if self.__topic_handlers.get(pcl.topic_return) is handle_response:
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

Notes:
- The collision check inlines the registration (does not call `Agent.subscribe`) so the check and set are truly atomic. `Agent.subscribe`'s warn-then-overwrite is skipped for the publish_sync path; `broker.subscribe` is still invoked exactly once from publish_sync.
- The finally block splits the "decision under lock" from the "broker call outside lock" per the same principle used in RFC-005.
- `Agent.unsubscribe` is NOT called on the cleanup path (to avoid re-acquiring the lock and to keep the sequence explicit). The two-line inline (dict pop + broker.unsubscribe) preserves the exact same observable side effects.

### 6.4 Config

No new config keys. The behaviour is universal for `publish_sync`; opting out would defeat the purpose.

### 6.5 Observability

- The raised `TopicWaitCollisionError` message includes the offending `topic_return`.
- No new metrics attribute; collision events are intended to be exceptional and observable via the exception at the call site.

---

## 7. Concrete decisions (all 17)

### 7.1 When is collision detected
At `publish_sync` entry, immediately after determining the effective `pcl.topic_return` (whether from Parcel-preset, `topic_wait`, or `__generate_return_topic`). Detection is under `_handlers_lock` atomically with the register.

### 7.2 Exception type
`TopicWaitCollisionError(RuntimeError)`. Subclass of `RuntimeError` (not `Exception`, not `OSError`).

### 7.3 Exception message
`f"topic_wait {pcl.topic_return!r} is already awaited by another publish_sync on this Agent"`. Includes the exact offending topic. No traceback chaining (nothing to chain from).

### 7.4 Register + collision check atomicity
Both operations under a single `_handlers_lock.acquire()` call: `if topic in dict: raise; else: dict[topic] = handler`. Cannot be interrupted by a concurrent register.

### 7.5 Cleanup identity check + pop atomicity
`if dict.get(topic) is handle_response: dict.pop(topic, None)` under the same `_handlers_lock`. Closes the narrow-race window documented in the R.4 characterisation. The `broker.unsubscribe` call happens **outside** the lock, based on a boolean captured inside the lock.

### 7.6 `broker.subscribe` failure
If `self._broker.subscribe(...)` raises, the handler is already in the registry (registered under the lock, before the broker call). The exception propagates out of `publish_sync` (no `try/except` around the broker.subscribe call). The `finally` block runs; identity check succeeds; handler is popped; `broker.unsubscribe` is attempted (may itself raise, but that raise is caught and logged per RFC-001). Net effect: caller sees the original `broker.subscribe` exception; registry is left clean.

### 7.7 Publish failure cleanup
Unchanged from RFC-002 / RFC-001. `_publish_or_raise` raises → `finally` runs → identity check + pop (now atomic per §7.5) → `broker.unsubscribe`. Caller sees the original broker exception.

### 7.8 Timeout cleanup
Unchanged from RFC-001. `event.wait` returns False → `raise TimeoutError` → `finally` runs cleanup (atomic per §7.5).

### 7.9 Terminate
Unchanged. `publish_sync` runs on the caller's thread, not on the dispatcher. `Agent.terminate` does not interrupt in-flight `publish_sync` calls; each waits its configured `timeout`. The atomic identity guard means a `terminate → new publish_sync` sequence cannot corrupt registry state.

### 7.10 Duplicate / late response
Unchanged from RFC-001 / RFC-003. `handle_response` has an `is_set()` guard; duplicates are dropped. Late responses arriving after cleanup have no registered handler → RFC-003 R-fallback-silent → dropped.

### 7.11 `Agent.subscribe` collision policy
**Not changed by this RFC.** `Agent.subscribe` retains its warn-then-overwrite behaviour. Rationale: subscribers may legitimately rebind a handler (e.g. reconfigure at runtime); changing that semantic universally would be a larger, orthogonal decision. Callers who need collision detection for a specific topic can build it on top of `Agent.subscribe`.

### 7.12 Scope limitation
The fail-fast policy applies **only** to `Agent.publish_sync`. Direct callers of `Agent.subscribe`, and the internal subscribes done by `Agent._on_connect` for parent/child topics, are unaffected.

### 7.13 Logging
- On collision detected: no framework log at ERROR/WARNING (the raised exception carries the diagnostic; logging it inside `publish_sync` would duplicate the caller's own error handling). If a caller does not handle the exception, the framework's default exception handling (or the Python interpreter) will surface it.
- Existing `logger.warning` inside `Agent.subscribe` for duplicate topic registration is unchanged (still fires when a user calls `Agent.subscribe` directly with a duplicate topic).

### 7.14 Metrics
None. Collision events are intended to be exceptional and observable at the call site via the exception. If future observability needs arise, a metric can be added in a follow-up without changing the exception contract.

### 7.15 Backward compatibility
See §8.

### 7.16 Acceptance criteria
See §10.

### 7.17 Rollback
See §11.

---

## 8. Backward compatibility

### Source compatibility

| Symbol | Before | After | Compatibility |
|---|---|---|---|
| `Agent.publish_sync(topic, data, topic_wait, timeout)` | Signature same | Signature same | Full |
| `Agent.subscribe(topic, data_type, topic_handler)` | Warn + overwrite | **Unchanged** | Full |
| `Agent.unsubscribe(topic)` | Same | Same | Full |
| `Agent.publish` / `_publish_or_raise` / `_on_message` | Same | Same | Full |
| `Parcel` / `TextParcel` / `BinaryParcel` | — | — | Untouched |
| `MessageBroker` / `MqttBroker` | — | — | Untouched |
| Wire format | — | — | Untouched |
| `Agent._handlers_lock` | Not defined | New internal `threading.RLock` | Additive |
| `TopicWaitCollisionError` (in `agentflow.core.agent`, or a small `agentflow.core.errors`) | Not defined | New public exception class | Additive |

### Behavioural compatibility

One observable change:

- `publish_sync(topic_wait=X)` where X is already actively awaited by another `publish_sync` on the same Agent **now raises `TopicWaitCollisionError` immediately** instead of silently overwriting the other caller's handler and returning a response (or timing out).

Callers relying on the pre-RFC-006 silent-overwrite behaviour would observe the new exception. Grep-verification of the working tree (`src/`, `tests/`, `unit_test/`, `exe_test/`, `docs/`) finds no code path relying on this behaviour; the only tests that exercise it are the R.4 characterisation tests, which intentionally document the race (§10 lists the exact test updates).

Callers using `publish_sync` without an explicit `topic_wait` (the recommended pattern) see no change — the auto-generated return topic is per-call unique.

The very narrow check-then-pop race in RFC-001's identity guard is closed as a bonus. No caller could have relied on the race window's specific interleaving.

### Wire compatibility

No wire changes. Parcels sent on collision-abort now carry nothing (nothing is published), which is strictly less traffic than the pre-RFC-006 behaviour.

---

## 9. Interaction with prior RFCs

- **RFC-001 (R-02 publish_sync cleanup)**: RFC-006 tightens the identity guard by making it atomic with the pop. The four R-02 characterisation tests continue to pass unchanged — none of them exercise the collision path, and the finally's observable effect (pop + unsubscribe when the handler is still ours) is preserved.
- **RFC-002 (R-13 fast-fail)**: `_publish_or_raise` still propagates broker exceptions. RFC-006 does not alter the try/finally structure; publish-failure cleanup continues to run under §7.5's atomic identity guard. The 46 R-13 tests are unaffected.
- **RFC-003 (R-05 auto-reply)**: Auto-reply routing is in `Agent._on_message`, not `publish_sync`. Untouched by RFC-006. The 21 R-05 tests are unaffected.
- **RFC-004 (R-04 bounded dispatch)**: Dispatcher is per-Agent and dispatches inbound messages to consumers; `publish_sync` runs on the caller's thread. RFC-006's lock is per-Agent and never held across a dispatcher enqueue. The 33 R-04 tests are unaffected.
- **RFC-005 (R-03 subscription recovery)**: `MqttBroker._registry` is a broker-side concern; `Agent.__topic_handlers` is an Agent-side concern. RFC-006's lock protects only the Agent-side dict. The 48 R-03 tests are unaffected.

The 175 tests spanning R-02 / R-03 / R-04 / R-05 / R-13 form the regression floor for RFC-006.

---

## 10. Test migration plan

### The strict xfail that flips to PASS

In `tests/unit/core/test_agent_publish_sync_concurrency.py`:

- `test_second_publish_sync_with_same_topic_wait_should_fail_fast` — remove `@pytest.mark.xfail`. The test already checks that a second call with the same `topic_wait` raises within < 0.1 s. It will pass under RFC-006.

### Existing characterization tests that MUST be updated

Several R.4 characterisation tests currently document the silent-overwrite behaviour by spawning two callers with the same `topic_wait` and asserting that only one completes. Under RFC-006, the second call raises `TopicWaitCollisionError` immediately; the assertions need updating.

| Test | Current | Post-RFC-006 |
|---|---|---|
| `test_publish_sync_two_threads_leave_only_second_handler_registered` | Asserts B overwrote A | Rewrite: assert B raises `TopicWaitCollisionError`; A's handler stays registered until A completes/times out |
| `test_only_second_caller_can_receive_delivered_response` | Asserts B receives, A times out | Rewrite: assert B raises collision; A receives (or times out on its own) |
| `test_response_semantically_for_first_caller_reaches_second_caller` | Documents wrong-routing hazard | Rewrite: assert collision prevents the hazard; deliver response to A; A completes normally |
| `test_second_delivered_response_dropped_by_is_set_guard` | Assumes B is registered | Rewrite: assert B collides; only A is registered; is_set guard exercised via A only |
| `test_first_caller_cleanup_does_not_evict_second_callers_handler` | Documents identity guard under overwrite | Delete or repurpose: since collision now prevents overwrite, the identity-guard-under-overwrite scenario cannot occur via `publish_sync` |
| `test_second_caller_cleanup_removes_handler_no_double_unsubscribe` | Same | Same disposition |
| `test_late_response_after_second_caller_completes_is_silently_dropped` | Chains B completing then late deliver | Rewrite: A completes, late deliver, silent drop (same outcome, different setup) |
| `test_N_callers_same_topic_wait_at_most_one_completes[3/10/50]` | Asserts exactly 1 completes | Rewrite: assert exactly 1 completes and N-1 raise `TopicWaitCollisionError` |
| `test_second_subscribe_overwrites_first_handler_in_registry` | Tests `Agent.subscribe` directly | UNCHANGED — RFC-006 does not alter `Agent.subscribe` semantics |
| `test_distinct_topic_wait_both_callers_receive_own_response` | Positive control | UNCHANGED |
| `test_omitted_topic_wait_auto_generates_unique_correlation` | Positive control | UNCHANGED |
| `test_agent_terminate_during_concurrent_publish_sync_does_not_hang_waiters` | Uses distinct topics | UNCHANGED |

### New tests to add

| Test | Purpose |
|---|---|
| `test_collision_raises_TopicWaitCollisionError` | Direct positive assertion of the new exception type |
| `test_collision_error_message_contains_topic_wait` | Assert message format includes the offending topic |
| `test_collision_does_not_call_broker_subscribe` | The colliding call must not subscribe/publish at the broker |
| `test_collision_does_not_touch_registry_for_other_topics` | Isolation: only the colliding topic key is affected |
| `test_collision_after_first_caller_completes_is_not_raised` | Sequential (A finishes, B starts): B succeeds; no collision |
| `test_check_and_pop_atomicity_under_stress` | 25 trials × concurrent overwrite attempts on the same topic; assert final registry state matches the last successful register or is empty (no torn state) |
| `test_publish_sync_still_cleans_up_when_broker_subscribe_raises` | RFC-006 §7.6 path: broker.subscribe raises after registry write; cleanup pops handler + unsubscribes |
| `test_TopicWaitCollisionError_is_RuntimeError_subclass` | Trivial type assertion |
| `test_agent_subscribe_still_warn_and_overwrites_directly` | Assert `Agent.subscribe` semantics UNCHANGED (RFC-006 §7.11) |

### The other two strict xfails remain

- `test_both_callers_should_receive_own_response_with_shared_topic_wait` — remains XFAIL (needs correlation ID; future RFC).
- `test_framework_should_support_multiple_handlers_per_topic` — remains XFAIL (needs multi-handler fan-out; future RFC).

Their `reason` strings should be updated to note that RFC-006 has closed the fail-fast path and further improvement is deferred.

### Legacy suites

`unit_test/*` and `exe_test/*` remain quarantined via `pyproject.toml` `norecursedirs`. Not affected.

FakeBroker requires no changes.

---

## 11. Acceptance criteria

Before this RFC's implementation PR can merge:

1. `PYTHONPATH=src pytest tests/unit` reports **all-pass** with **exactly 2 strict xfailed** remaining (the two out-of-scope xfails documented above).
   - Baseline before implementation: 230 passed, 3 xfailed.
   - Target after implementation: ~239 passed, 2 xfailed (1 xfail flipped to pass; ~8 characterisation tests inverted / kept; ~9 new tests added).
2. `TopicWaitCollisionError` exists as a `RuntimeError` subclass, importable from `agentflow.core.agent` (or a small `agentflow.core.errors` module — implementer's choice).
3. `Agent.publish_sync` raises `TopicWaitCollisionError` when the effective `pcl.topic_return` is already a key in `__topic_handlers` at the moment of the atomic collision check.
4. On collision, `broker.subscribe` is NOT called and `broker.publish` is NOT called (asserted via a mocked broker's call counts).
5. On collision, no other topic's registry entry is touched.
6. `Agent._handlers_lock` is used for:
   - The collision-check-and-register in `publish_sync` entry.
   - The identity-check-and-pop in `publish_sync`'s `finally`.
   - Nothing else in `Agent` in this RFC.
7. `Agent.subscribe`, `Agent.unsubscribe`, `Agent.publish`, `Agent.on_message`, `Agent._on_message`, and all other public methods retain their pre-RFC signatures and behaviours.
8. RFC-001 (27), RFC-002 (46), RFC-003 (21), RFC-004 (33), RFC-005 (48) tests all pass **unchanged**.
9. No changes to:
   - `src/agentflow/core/parcel.py`
   - `src/agentflow/broker/*`
   - Wire format
   - `pyproject.toml`
10. This RFC file has status changed from `Draft` to `Implemented` in the same PR.
11. `docs/audit/05-risk-register.md` — add an "R-26 / R.4 (publish_sync topic_wait collision)" entry with status `Resolved` referencing RFC-006 (or amend the existing RFC-001 §10.R.4 note to point to RFC-006's resolution).

Out of scope (deferred to future RFCs):
- Multi-waiter support (Option D).
- Correlation-ID metadata (Option E; part of R-20).
- `Agent.subscribe` collision policy — its warn-then-overwrite semantics are intentional for the general subscribe API.
- Broker-level topic-collision detection (broker only sees SUBSCRIBE frames, not caller intent).

---

## 12. Rollback plan

Rollback trigger — any of:

- A caller depended on the pre-RFC-006 silent-overwrite behaviour (extremely unlikely; grep found none).
- A deadlock in the new `_handlers_lock` (would show as `publish_sync` hangs even without any concurrent caller).
- Any regression in R-01 / R-02 / R-03 / R-04 / R-05 / R-13 tests.
- The `TopicWaitCollisionError` exception fires in a production flow that was accidentally reusing `topic_wait` — this is arguably a correct signal, but if the caller cannot be updated quickly, the rollback lets them defer.

Rollback procedure — single `git revert` of the merge commit. Because:

- Additive symbols only: `Agent._handlers_lock`, `TopicWaitCollisionError`. Removing them is safe (no pre-RFC caller adopted them).
- `Agent.publish_sync` reverts to its RFC-001 form (with the narrow check-then-pop race re-introduced).
- No wire / schema / broker API changes to reconcile.
- Test updates revert alongside; the strict xfail re-marks.

Not rollback-safe: any change bundled in the same PR that modifies Parcel, Broker ABC, or Message Schema. This RFC forbids bundling.

Post-rollback state: R.4 returns to "Confirmed by runtime evidence, unresolved". The three original strict xfails re-appear. The narrow check-then-pop race is re-opened. `Agent.subscribe` is unaffected in either direction.

Interim mitigation (available without revert): a caller that hits the new collision exception can migrate to the auto-generated `topic_wait` pattern (drop the explicit `topic_wait` argument). This is the recommended usage anyway; the exception effectively documents that.

---

## Appendix A — Why not put `Agent.subscribe` under the same policy?

`Agent.subscribe(topic, topic_handler=fn)` has a warn-then-overwrite semantic that predates R.4 and serves legitimate use cases:

- **Handler rebinding at reconfiguration**: a caller may swap in a new handler for a topic they already subscribe to. The current warn-then-overwrite lets them do so without ceremony.
- **Idempotent subscribe under retry**: a subscriber that retries its own `subscribe` call after a broker reconnect could otherwise trip an error.

RFC-006 leaves `Agent.subscribe` alone. Callers who want strict collision semantics for a given topic can implement the check themselves on top of the public API. A future RFC may unify the policy once the general subscribe use cases are surveyed and a migration path is designed.

## Appendix B — Why `RLock` and not `Lock`?

The `publish_sync` finally block currently calls `self.unsubscribe(topic)`, which acquires no lock today but might be extended to acquire `_handlers_lock` in a future refactor. `RLock` ensures that if the finally holds the lock and calls `unsubscribe` which also tries to acquire it, no deadlock occurs. Under RFC-006's current design (finally inlines the pop + broker.unsubscribe rather than calling `Agent.unsubscribe`), a plain `Lock` would suffice; `RLock` is chosen as low-cost insurance against future refactors.

## Appendix C — Why not include a correlation ID here?

Correlation ID is the "correct" long-term solution: it decouples the framework's response-routing key from the topic namespace and enables truly concurrent multi-waiter semantics. However:

- It requires a Parcel schema change (new field, versioning concern) → falls under R-20.
- It requires the responder side to echo the ID → wire contract change.
- Users pinning to the current Parcel format cannot adopt it without cross-version negotiation.

RFC-006 delivers the smallest, non-schema-affecting improvement: turn the silent hazard into an observable, actionable exception. Once R-20 is scoped, a subsequent RFC can adopt correlation ID and lift the collision restriction — at which point the R-006 exception can be softened or removed. `TopicWaitCollisionError` is intentionally a `RuntimeError` (not a permanent contract exception) to leave that door open.
