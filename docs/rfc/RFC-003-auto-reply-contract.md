# RFC-003 — auto-reply contract

- **Status**: **Implemented (2026-07-26)**
- **Author**: Audit follow-up
- **Depends on**: `docs/audit/05-risk-register.md` R-05; consistent with [RFC-001](RFC-001-publish-sync-subscription-lifecycle.md) and [RFC-002](RFC-002-publish-error-propagation.md)
- **Scope**: `Agent._on_message` auto-reply semantics: specific-handler vs fall-through-on_message dispatch, return-type rules, whether reply parcels preserve `topic_return`, and how to prevent self-loop / multi-agent loop
- **Explicitly out of scope**: hop count, message metadata, Parcel schema version, rate limiting, broker-level duplicate suppression, MQTT reconnect (R-03), ProcessWorker (R-06/R-08), thread pool (R-04)
- **Implementation summary** (2026-07-26):
  - Adopted the recommended Option D (three rules together) in `src/agentflow/core/agent.py`, `Agent._on_message` only. Public signature unchanged; no changes to `Parcel`, `MessageBroker`, `MqttBroker`, `Agent.publish` / `subscribe` / `unsubscribe` / `publish_sync` / `_publish_or_raise`, wire format, or `pyproject.toml`.
  - Test surface: `tests/unit/core/test_agent_reply_behavior.py` — 5 loop-forcing tests rewritten to assert termination (≤ 3 publishes each), 2 strict xfails removed (now pass), 6 R-strip / R-exception-fresh tests added, `publish_sync` late-reply drop test added, `publish_sync` happy-path regression check added.
  - Full unit regression: `PYTHONPATH=src python -m pytest tests/unit` → **135 passed, 0 failed, 0 xfailed, 0 xpassed** in 2.73 s.
  - No regression to R-02 (RFC-001) or R-13 (RFC-002); their combined 73 tests pass unchanged.
  - See [R-05 resolution block](../audit/05-risk-register.md#r-05--suspected-reply-loop-on-error-paths-and-on-default-handlers).

---

## 1. Problem statement

`Agent._on_message` (`src/agentflow/core/agent.py:536-562`) unconditionally publishes to `pcl.topic_return` whenever it is truthy. The reply payload is derived from either the handler's return value or, on exception, the incoming parcel itself. Two combined properties make this dangerous:

1. **On exception**, `data_resp = p` — the incoming parcel is mutated (`p.error = str(ex)`, `p.content = None`) and reused. Because `p.topic_return` is preserved, the echoed error carries the same `topic_return` back to the wire.
2. **On any handler return value that is already a `Parcel`**, `Agent.publish` forwards it verbatim; `topic_return` set by the handler is preserved on the wire.

If either the publisher's client self-echoes (paho does when subscribed to the topic it publishes to) or a peer agent's handler also produces a `Parcel`-with-`topic_return`, the auto-reply forms an infinite loop.

The concrete confirmed patterns are documented in `tests/unit/core/test_agent_reply_behavior.py` — see §2.

---

## 2. Runtime evidence

Baseline: `PYTHONPATH=src pytest tests/unit` → **125 passed, 2 strict xfailed in 2.45 s**.

| Behaviour | Test | Result |
|---|---|---|
| No `topic_return` → no auto-reply | `test_scenario_1_no_topic_return_no_auto_reply` | PASSED |
| Handler returns `None` → auto-reply wraps `None`, reply parcel has `topic_return=None` | `test_scenario_2_handler_returns_None_auto_reply_wraps_None` | PASSED |
| Handler returns scalar → auto-reply wraps the scalar | `test_scenario_3_handler_returns_string_auto_reply_wraps_string` | PASSED |
| Handler returns `Parcel` → auto-reply publishes that parcel | `test_scenario_4_handler_returns_Parcel_auto_reply_uses_that_Parcel` | PASSED |
| Handler-returned `Parcel` with `topic_return` → wire preserves it | `test_scenario_5_reply_Parcel_with_topic_return_preserves_it_on_wire` | PASSED |
| Duplicate delivery no `topic_return` after publish_sync cleanup → silent | `test_scenario_7_duplicate_delivery_no_topic_return_after_publish_sync` | PASSED |
| Duplicate delivery **with** `topic_return` → fall-through auto-replies | `test_scenario_7b_duplicate_delivery_with_topic_return_triggers_auto_reply` | PASSED |
| Handler exception + self-echo → reply loop (bounded to 20 dispatches) | `test_scenario_9_handler_exception_creates_reply_loop_bounded_by_broker` | PASSED |
| Handler returns loopy Parcel + self-echo → reply loop (bounded to 20) | `test_scenario_4_5_handler_returns_topic_return_parcel_creates_loop` | PASSED |
| Two-agent mutual reply → cross-agent loop (bounded to 30) | `test_scenario_6_two_agent_reply_loop_via_hub_broker` | PASSED |
| Default on_message + self-echo → self-terminates in ≤ 4 publishes | `test_scenario_10_default_on_message_auto_reply_wraps_None_and_terminates` | PASSED |
| **Aspirational**: fallback on_message should not auto-reply | `test_default_on_message_should_not_auto_reply_when_no_specific_handler` | **XFAIL (strict)** |
| **Aspirational**: exception echo should not carry topic_return | `test_handler_exception_error_echo_should_not_carry_topic_return` | **XFAIL (strict)** |

The two strict xfails form part of the acceptance-check set for §11.

---

## 3. Current behavior (all 10 scenarios)

Source: `src/agentflow/core/agent.py:536-562`.

```python
def _on_message(self, topic, data):
    pcl = Parcel.from_payload(data)
    topic_handler = self.__topic_handlers.get(topic, self.on_message)

    def handle_message(topic_handler, topic, p):
        if p.topic_return:
            try:
                data_resp = topic_handler(topic, p)
            except Exception as ex:
                logger.exception(ex)
                p.error = str(ex)
                p.content = None
                data_resp = p            # mutates & reuses incoming parcel
            finally:
                self.publish(pcl.topic_return, data_resp)
        else:
            try:
                topic_handler(topic, p)
            except Exception as ex:
                logger.exception(ex)

    threading.Thread(target=handle_message, args=(topic_handler, topic, pcl)).start()
```

Per scenario, the observable outcome:

| # | Scenario | Current behaviour |
|---|---|---|
| 1 | No `topic_return` | Handler runs; no publish |
| 2 | Specific handler returns `None` | Publishes to `topic_return`; reply parcel wraps `None`, `reply.topic_return = None` |
| 3 | Specific handler returns scalar | Publishes to `topic_return`; reply parcel wraps scalar, `reply.topic_return = None` |
| 4 | Specific handler returns `Parcel` | Publishes that parcel to `topic_return`; parcel forwarded verbatim |
| 5 | Specific handler returns `Parcel` with `topic_return` | Same as #4 — `topic_return` **preserved on wire** |
| 6 | Specific handler raises | Mutates `p` (`p.error`, `p.content=None`) and publishes `p` to `topic_return`; `p.topic_return` **preserved** |
| 7 | Fall-through on_message returns `None` | Default `on_message` returns `None`; publishes to `topic_return`; reply parcel wraps `None` |
| 8 | Fall-through on_message returns scalar | Only possible if user overrode `on_message` to return; publishes to `topic_return`; reply parcel wraps scalar |
| 9 | Duplicate / late delivery (with `topic_return`) | If a specific handler is still registered, its return value drives the reply (see #2-#6); if not, on_message fallback drives it (see #7-#8) |
| 10 | Two-agent mutual reply | Each side's handler feeds into the other's `topic_return`; if both return `Parcel`-with-`topic_return`, ping-pong loop (§2 evidence) |

Loop-triggering combinations (§5 evidence):
- Scenario 6 with self-echo.
- Scenario 5 with self-echo.
- Scenario 10 with two agents whose handlers both return `Parcel`-with-`topic_return`.

Self-terminating patterns:
- Scenario 2/3/7 (reply parcel loses `topic_return` because `Parcel.from_content(non_parcel)` doesn't set it).

---

## 4. Desired behavior (post-RFC-003)

The following rules must hold together:

- **R-fallback-silent**: If the dispatch selected `self.on_message` because the topic has no specific handler in `__topic_handlers`, no auto-reply is emitted regardless of `pcl.topic_return`. "No handler" means "no reply".
- **R-strip-topic_return**: For every auto-reply, the outgoing parcel's `topic_return` is `None`. Specifically, if `data_resp` is a `Parcel` whose `topic_return` is truthy, the wire payload is reconstructed to carry the same content but `topic_return = None`. If `data_resp` is not a `Parcel`, no change — `Parcel.from_content(...)` already leaves `topic_return = None`.
- **R-exception-fresh**: On handler exception, the auto-reply does not reuse the incoming `p`. A new parcel carrying the error information is constructed with `topic_return = None`. The incoming parcel `p` is not mutated.

Post-RFC-003 behaviour per scenario:

| # | Scenario | Post-RFC-003 behaviour |
|---|---|---|
| 1 | No `topic_return` | Unchanged — handler runs; no publish |
| 2 | Specific handler returns `None` | Unchanged externally — auto-reply wraps `None`, `topic_return=None` |
| 3 | Specific handler returns scalar | Unchanged externally — auto-reply wraps scalar, `topic_return=None` |
| 4 | Specific handler returns `Parcel` (no `topic_return`) | Unchanged — parcel forwarded verbatim |
| 5 | Specific handler returns `Parcel` **with** `topic_return` | **Changed**: auto-reply carries the same content but `topic_return` stripped |
| 6 | Specific handler raises | **Changed**: incoming `p` is not mutated; a fresh error parcel is emitted with `topic_return=None` |
| 7 | Fall-through on_message | **Changed**: no auto-reply is emitted, even when `pcl.topic_return` is set |
| 8 | Fall-through on_message returns scalar (user override) | **Changed**: no auto-reply (return value is discarded because the dispatch was a fall-through) |
| 9 | Duplicate / late delivery | After RFC-001 cleanup, the topic has no specific handler → R-fallback-silent applies → no auto-reply. If a specific handler is (still) registered, R-strip-topic_return applies to whatever it returns |
| 10 | Two-agent mutual reply | Each side's reply carries `topic_return=None` (R-strip-topic_return) → the receiving agent's dispatch sees `topic_return=None` → §3 row 1 applies → no auto-reply. Loop broken in one hop |

`publish_sync` is unaffected: its `handle_response` returns `None` (already loses `topic_return`), and its specific handler on `return_topic` prevents R-fallback-silent from applying.

---

## 5. Options considered

### Option A — Only exception branch strips topic_return

Sketch: replace `data_resp = p` in the exception branch with `data_resp = TextParcel(None); data_resp.error = str(ex)`. Every other branch unchanged.

| Aspect | Analysis |
|---|---|
| Blocks exception loop | ✓ |
| Blocks handler-returns-loopy-Parcel loop | ✗ |
| Blocks two-agent loop (via handler return value) | ✗ |
| Blocks fall-through-triggered chains | ✗ |
| Changes `publish_sync` observable behaviour | No |
| Behaviour change surface | Only exception path |
| Handler contract clarity | Partial — only tells users what happens on error |

**Verdict**: rejected. Insufficient — leaves the handler-return-Parcel loop and two-agent loop unresolved.

---

### Option B — All auto-replies strip topic_return

Sketch: after `data_resp` is decided, if it is a `Parcel` with `topic_return`, reconstruct with `topic_return=None` before `publish`. Applied to every path.

| Aspect | Analysis |
|---|---|
| Blocks exception loop | ✓ (indirectly: exception path echoes p with topic_return stripped) |
| Blocks handler-returns-loopy-Parcel loop | ✓ |
| Blocks two-agent loop | ✓ (each hop strips) |
| Blocks fall-through-triggered chains | Partially — fall-through still emits one auto-reply, which self-terminates because reply has `topic_return=None` |
| Changes `publish_sync` observable behaviour | No — `handle_response` returns `None` |
| Behaviour change surface | Auto-reply always strips |
| Handler contract clarity | Clear: "your returned `Parcel`'s `topic_return` is ignored on the reply path" |

**Verdict**: minimal single-rule fix; blocks all three confirmed loops. However, still emits noise on fall-through paths (an unsolicited `None` reply for every topic no one handles specifically), and leaves the exception branch's `p` mutation in place.

---

### Option C — Fall-through on_message does not auto-reply

Sketch: detect "no specific handler" (`topic not in self.__topic_handlers`) and skip the reply branch entirely for that dispatch.

| Aspect | Analysis |
|---|---|
| Blocks exception loop | ✗ (specific handler that raises still loops) |
| Blocks handler-returns-loopy-Parcel loop | ✗ |
| Blocks two-agent loop | ✗ (the two-agent case uses explicit handlers) |
| Blocks fall-through noise | ✓ |
| Changes `publish_sync` observable behaviour | No |
| Behaviour change surface | Only fall-through path |
| Handler contract clarity | Clear: "no handler → no reply" |

**Verdict**: rejected on its own — the loop patterns that led to R-05's runtime confirmation all involve explicit handlers, which Option C leaves untouched.

---

### Option D — B + C combined (**RECOMMENDED**)

Sketch: adopt both rules together, plus fix the exception path to emit a fresh parcel (R-exception-fresh).

| Aspect | Analysis |
|---|---|
| Blocks exception loop | ✓ |
| Blocks handler-returns-loopy-Parcel loop | ✓ |
| Blocks two-agent loop | ✓ |
| Blocks fall-through noise | ✓ |
| Changes `publish_sync` observable behaviour | No |
| Behaviour change surface | Fall-through + exception + Parcel-with-topic_return |
| Handler contract clarity | Clearest — three explicit rules, one per concern |
| Message Schema | Unchanged |

**Verdict**: **Recommended.** Blocks every confirmed loop pattern, preserves `publish_sync`, no wire-format changes.

---

### Option E — Hop count / message id

Sketch: add a `hops` counter or unique `message_id` set to `Parcel`; drop or refuse to publish when threshold crossed / id already seen.

| Aspect | Analysis |
|---|---|
| Blocks all loops | Depends on threshold; hop threshold caps depth, id set stops exact repeats |
| Message Schema | **Yes — requires a new Parcel field** |
| Scope | Overlaps R-20 (message metadata) which this RFC explicitly excludes |
| Migration | Needs cross-agent version negotiation |
| Verdict | Out of scope by RFC-003 §Scope |

---

### Comparison summary

| Criterion | A | B | C | **D** | E |
|---|---|---|---|---|---|
| Blocks exception loop | ✓ | ✓ | ✗ | ✓ | ✓ |
| Blocks handler-loopy-Parcel loop | ✗ | ✓ | ✗ | ✓ | ✓ |
| Blocks two-agent loop | ✗ | ✓ | ✗ | ✓ | ✓ |
| Blocks fall-through noise | ✗ | ✗ | ✓ | ✓ | ✓ |
| No Parcel / schema change | ✓ | ✓ | ✓ | ✓ | **✗** |
| Preserves `publish_sync` | ✓ | ✓ | ✓ | ✓ | ✓ |
| Handler contract clarity | Low | Medium | Medium | **High** | Medium |
| Lines changed | ~4 | ~6 | ~4 | ~10 | ~50+ |
| Verdict | rejected | insufficient alone | insufficient alone | **chosen** | out of scope |

---

## 6. Recommended design

Adopt **Option D** — the union of R-fallback-silent, R-strip-topic_return, and R-exception-fresh.

### 6.1 Illustrative diff sketch

```diff
--- a/src/agentflow/core/agent.py
+++ b/src/agentflow/core/agent.py
@@ Agent._on_message
     def _on_message(self, topic, data):
         pcl = Parcel.from_payload(data)
-        topic_handler = self.__topic_handlers.get(topic, self.on_message)
+        is_fallback = topic not in self.__topic_handlers
+        topic_handler = self.__topic_handlers.get(topic, self.on_message)
+        # R-fallback-silent: a topic that has no specific handler
+        # must never generate an auto-reply, regardless of topic_return.
+        should_auto_reply = bool(pcl.topic_return) and not is_fallback

         def handle_message(topic_handler, topic, p):
-            if p.topic_return:
+            if should_auto_reply:
                 try:
                     data_resp = topic_handler(topic, p)
                 except Exception as ex:
                     logger.exception(ex)
-                    p.error = str(ex)
-                    p.content = None
-                    data_resp = p
+                    # R-exception-fresh: emit a new parcel; do NOT
+                    # mutate the incoming p or reuse it as reply.
+                    err_pcl = TextParcel(None)
+                    err_pcl.error = str(ex)
+                    data_resp = err_pcl
                 finally:
-                    self.publish(pcl.topic_return, data_resp)
+                    # R-strip-topic_return: the reply parcel must
+                    # never carry topic_return; strip it to break
+                    # any potential loop chain at the next hop.
+                    if isinstance(data_resp, Parcel) and data_resp.topic_return:
+                        # Reconstruct instead of mutating the handler's
+                        # returned object (which might be user-owned).
+                        stripped = type(data_resp)(data_resp.content)
+                        stripped.error = data_resp.error
+                        data_resp = stripped
+                    self.publish(pcl.topic_return, data_resp)
             else:
                 try:
                     topic_handler(topic, p)
                 except Exception as ex:
                     logger.exception(ex)

         threading.Thread(target=handle_message, args=(topic_handler, topic, pcl)).start()
```

Net addition ≈ 15 lines in a single file.

### 6.2 Reconstruction detail

Reconstructing rather than mutating the handler's returned parcel is intentional. Handler code may keep a reference to the returned parcel (e.g., stored in a cache); silently zeroing its `topic_return` would violate the "don't touch user objects" principle. Reconstruction preserves that principle at the cost of one allocation per auto-reply.

`TextParcel` and `BinaryParcel` both accept `content` as a single positional argument (`parcel.py:135, 159`); `type(data_resp)(content)` picks the correct subclass. The `error` field is preserved. `version` is auto-set by `Parcel.__init__`.

### 6.3 Detection of "no specific handler"

`is_fallback = topic not in self.__topic_handlers` is a single dict membership check, evaluated once per dispatch. Cheaper and clearer than `topic_handler is self.on_message` (which is fragile if a user subclass has bound `on_message` differently).

---

## 7. Handler contract (formal)

After RFC-003, the contract that `Agent._on_message` implements is:

1. **Dispatch selection**:
   - If `topic in __topic_handlers`, the registered specific handler is invoked (`specific-handler dispatch`).
   - Otherwise, `self.on_message` is invoked (`fall-through dispatch`).
2. **Auto-reply eligibility**: an auto-reply is emitted **iff** the incoming parcel's `topic_return` is truthy **and** the dispatch was specific-handler.
3. **Reply payload**:
   - If the specific handler returned a value, that value becomes the reply body (`Parcel.from_content` wraps non-Parcels; `Parcel` values pass through).
   - If the specific handler raised, the reply body is a fresh `TextParcel(None)` with `.error` set to `str(ex)`.
4. **Reply `topic_return`**: always `None`. The framework strips any `topic_return` from the handler's returned parcel before publishing.
5. **Non-mutation guarantee**: the incoming parcel `p` is never mutated by `_on_message`. Handlers may safely read `p` (e.g., cache it) without observing later mutations.

This contract eliminates all three loop patterns confirmed under R-05:

- **Exception loop**: broken by rule 3 (fresh parcel) + rule 4 (strip).
- **Handler-returns-loopy-Parcel loop**: broken by rule 4.
- **Two-agent loop**: broken by rule 4 on both sides.
- **Fall-through noise**: eliminated by rule 2 (no auto-reply on fall-through).

---

## 8. Backward compatibility

### Source compatibility

| Symbol | Signature change | Semantic change |
|---|---|---|
| `Agent._on_message` | None (`@final`) | See below |
| `Agent.publish` / `publish_sync` / `subscribe` / `unsubscribe` / `_publish_or_raise` | None | None |
| `Parcel` / `TextParcel` / `BinaryParcel` | **None** | **None** — no schema field added |
| `MessageBroker` / `MqttBroker` | None | None |
| Wire format / topic naming | None | None |

### Behavioural compatibility

Two observable changes:

1. **A published parcel that arrives at a topic with no specific handler no longer produces an auto-reply**, even if its `topic_return` is set. Any caller that relied on the current "fall-through auto-reply with `None` content" pattern will time out or hang. Grep of the working tree (`src/`, `tests/`, `unit_test/`, `exe_test/`) finds no code path relying on this; the current `test_scenario_7b_duplicate_delivery_with_topic_return_triggers_auto_reply` and `test_scenario_10_default_on_message_auto_reply_wraps_None_and_terminates` both explicitly document that they characterize an unwanted behaviour.
2. **Auto-reply parcels never carry `topic_return`**. Any handler that returned a `Parcel` with `topic_return` set intending to redirect the reply chain will find that redirection dropped. No such handler exists in the codebase; the scenario is exercised only by the loop-forcing tests introduced for R-05 characterization.

### Wire compatibility

No changes. `Parcel` payloads on the wire remain byte-compatible with older agents. Older agents talking to a newer agent will observe the same reply payloads with `topic_return=None`, which is already the majority case (all non-Parcel returns already produce `topic_return=None` today).

### Non-mutation change

Handlers previously observed `p.error` and `p.content` being mutated after their exception was caught by the framework (e.g. `p.error = str(ex)`). Any handler that inspected `p` after the fact — via a stored reference or by re-raising — would have seen the mutated values. Under RFC-003, `p` is untouched. Handlers must obtain error information from their own `except` block (which is their normal pattern) or by comparing against a captured pre-image (unusual).

---

## 9. Interaction with R-02 cleanup (RFC-001)

RFC-001 established that `publish_sync` cleans up its handler and broker subscription on every exit path. RFC-003 does not touch `publish_sync`'s `try/finally` structure and does not alter when handlers are created or destroyed.

Concrete interaction:
- When a late/duplicate reply arrives at a `topic_return` whose handler has already been cleaned up by RFC-001, the dispatch falls through to `on_message`. Under RFC-003 R-fallback-silent, no auto-reply is emitted. The late reply is silently dropped — the desired outcome.
- The four R-02 characterization tests (`test_publish_sync_cleans_up_handler_when_broker_publish_raises`, `test_publish_sync_calls_broker_unsubscribe_when_broker_publish_raises`, `test_publish_sync_cleans_up_handler_when_broker_is_none`, `test_publish_sync_with_none_broker_does_not_crash_on_cleanup`) continue to pass unchanged.

RFC-002 (fast-fail on publish exception) is orthogonal: it changes the error path of `publish_sync` from `TimeoutError` to propagating the broker exception. RFC-003 changes the responder-side error echo behaviour. These do not conflict.

---

## 10. Test migration plan

### Existing tests that will FLIP under the fix

In `tests/unit/core/test_agent_reply_behavior.py`:

| Test | Current | Post-RFC-003 |
|---|---|---|
| `test_scenario_5_reply_Parcel_with_topic_return_preserves_it_on_wire` | Asserts `reply_pcl.topic_return == 'keep_me'` | **Must fail** — R-strip-topic_return sets it to `None`. Rewrite to assert `reply_pcl.topic_return is None` and rename to `test_scenario_5_reply_Parcel_with_topic_return_is_stripped_on_wire`. |
| `test_scenario_7b_duplicate_delivery_with_topic_return_triggers_auto_reply` | Asserts fall-through emits a reply to `UNRELATED_REPLY_TOPIC` | **Must fail** — R-fallback-silent suppresses it. Rewrite to `test_scenario_7b_duplicate_delivery_with_topic_return_does_not_trigger_auto_reply` and invert the assertion. |
| `test_scenario_9_handler_exception_creates_reply_loop_bounded_by_broker` | Asserts loop hits `max_dispatches=20` | **Must fail** — chain terminates in 2 publishes (initial + one error echo with `topic_return=None`). Rewrite to `test_scenario_9_handler_exception_does_not_create_reply_loop`. |
| `test_scenario_4_5_handler_returns_topic_return_parcel_creates_loop` | Asserts loop hits `max_dispatches=20` | **Must fail** — same reason. Rewrite to `test_scenario_4_5_reply_Parcel_with_topic_return_does_not_create_loop`. |
| `test_scenario_6_two_agent_reply_loop_via_hub_broker` | Asserts hub hits `MAX=30` | **Must fail** — chain terminates in 2 publishes. Rewrite to `test_scenario_6_two_agent_reply_does_not_loop`. |
| `test_scenario_10_default_on_message_auto_reply_wraps_None_and_terminates` | Asserts `< 5` publishes because reply loses topic_return | Still passes but for a different reason (R-fallback-silent, not the `None` wrapping). Optionally update the docstring; assertion remains valid. |

Existing xfails that will flip to XPASS (remove `@pytest.mark.xfail`):

- `test_default_on_message_should_not_auto_reply_when_no_specific_handler` → passes under R-fallback-silent.
- `test_handler_exception_error_echo_should_not_carry_topic_return` → passes under R-strip-topic_return + R-exception-fresh.

### New tests to add

| Test | Purpose |
|---|---|
| `test_exception_path_does_not_mutate_incoming_parcel` | Verify R-exception-fresh — capture `p.content` and `p.error` before/after, assert unchanged |
| `test_exception_path_publishes_fresh_parcel_with_error_field` | Verify the emitted error carries `.error = str(ex)` and `content=None`, `topic_return=None` |
| `test_handler_returned_Parcel_content_survives_strip` | Verify R-strip-topic_return preserves content while zeroing topic_return |
| `test_handler_returned_Parcel_error_survives_strip` | Verify `.error` field survives reconstruction |
| `test_handler_returned_BinaryParcel_stays_BinaryParcel_after_strip` | Verify `type(data_resp)(content)` preserves subclass |
| `test_publish_sync_reply_still_reaches_caller_after_RFC_003` | Positive check that RFC-003 does not regress publish_sync's happy path |
| `test_publish_sync_late_reply_after_cleanup_is_silently_dropped` | Verify R-fallback-silent + RFC-001 combined behaviour |
| `test_specific_handler_returning_scalar_still_produces_reply` | Confirm scenario 3 unchanged (positive) |

### Legacy tests

`unit_test/*` and `exe_test/*` remain quarantined via `pyproject.toml` `norecursedirs`; not affected.

FakeBroker's `enable_self_echo` and `subscribed_topics` remain useful for the negative tests (assert bounds are NOT hit).

---

## 11. Acceptance criteria

Before this RFC's implementation PR can merge:

1. `PYTHONPATH=src pytest tests/unit` reports **all-pass** with **0 xfailed, 0 xpassed, 0 failed**.
   - Baseline before implementation: 125 passed, 2 xfailed.
   - Target after implementation: ≥ 128 passed, 0 xfailed (the 2 xfails flip to pass, ~5 loop tests are rewritten to assert termination, ~8 new tests added).
2. The two-agent loop test's outcome inverts: `hub_publish_calls` count is now **≤ 3** (initial + at most one auto-reply per side stripped of `topic_return`).
3. RFC-001's 4 R-02 cleanup tests continue to pass unchanged.
4. RFC-002's 46 R-13 tests continue to pass unchanged.
5. `publish_sync` timing under happy path stays under existing budgets (no new latency added by the reconstruction step; a single `Parcel` allocation is O(1)).
6. No changes to:
   - `src/agentflow/core/parcel.py`
   - Parcel wire format (`TextParcel.HEAD`, `BinaryParcel.HEAD`, envelope fields)
   - `MessageBroker` / `MqttBroker` API surface
   - `pyproject.toml`
7. No new dependencies. No new exception classes. No new Parcel fields.
8. Handler contract from §7 is added as a docstring on `Agent._on_message` in the same PR.
9. This RFC file has status changed from `Draft` to `Accepted` in the same PR.

Out of scope (deferred):
- Hop counter or message id (would require R-20 metadata work).
- Rate limiting / duplicate suppression at broker level (R-04-adjacent).
- Refactoring the per-message thread spawn (R-04).
- Any change to `Agent.subscribe`'s silent-overwrite behaviour on same-topic concurrent registrations.

---

## 12. Rollback plan

Rollback trigger — any of:

- A downstream caller that relied on fall-through auto-reply (rare) reports missing replies.
- A downstream handler that intentionally set `topic_return` on its returned parcel for redirection (unknown existence) reports broken routing.
- Any regression in `publish_sync` observable behaviour that was not caught by the acceptance-check tests.
- Any measurable latency regression from the reconstruction step under production load.

Rollback procedure — single `git revert` of the merge commit. Because:

- No wire schema change to reconcile.
- No public API change: `Agent.publish` / `subscribe` / `unsubscribe` / `publish_sync` / `_publish_or_raise` all unchanged.
- No new symbol to remove.
- The rewritten and added tests revert alongside; the two xfails re-mark.
- FakeBroker's `enable_self_echo` remains (added in R-05 characterization, orthogonal to this fix).

Not rollback-safe: any additional change bundled into the same PR that modifies Parcel, Broker ABC, or Message Schema. This RFC forbids bundling such changes.

Post-rollback state: R-05 returns to "Confirmed by runtime evidence, unresolved". The three loop patterns re-open. The two aspirational xfails re-appear as XFAIL.

---

## Appendix A — Why R-fallback-silent is safe for `publish_sync`

`publish_sync` (`agent.py:321-353`) always installs a specific handler via `self.subscribe(pcl.topic_return, topic_handler=handle_response)` before publishing. Therefore, for the duration of a `publish_sync` call, `pcl.topic_return in __topic_handlers` is `True`, so `is_fallback` is `False`, so R-fallback-silent does not apply.

After RFC-001 cleanup (success, timeout, publish exception, missing broker), the handler is popped. A late reply arriving after that point sees `is_fallback = True` and is silently dropped — the desired behaviour that R-05's `test_scenario_7_duplicate_delivery_no_topic_return_after_publish_sync` and `test_scenario_10_default_on_message_auto_reply_wraps_None_and_terminates` already document as safe.

## Appendix B — Why R-strip-topic_return is safe for `publish_sync`

`publish_sync`'s `handle_response` closure (`agent.py`) returns `None`. `Parcel.from_content(None)` produces a `TextParcel(None)` whose `topic_return` is already `None`. R-strip-topic_return is therefore a no-op for the `publish_sync` reply path; the reconstruction branch is not entered because `data_resp` is not a `Parcel`.

## Appendix C — Why not just document "handlers must return `None`"?

An advisory-only rule ("please don't return `Parcel`-with-`topic_return`") relies on caller discipline. It cannot prevent the exception loop (which is framework-generated) and cannot prevent an accidentally-loopy handler in production. RFC-003's approach makes the framework enforce the contract structurally, which is strictly stronger.
