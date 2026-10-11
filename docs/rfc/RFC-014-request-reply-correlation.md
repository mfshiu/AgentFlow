# RFC-014 — Request/reply correlation id

- **Status**: **Implemented** (2026-10-12, issue #36)
- **Author**: Audit follow-up
- **Depends on**: `docs/audit/05-risk-register.md` R-20 (no message metadata). Consistent with [RFC-001](RFC-001-publish-sync-subscription-lifecycle.md) (reply-topic lifecycle), [RFC-003](RFC-003-auto-reply-contract.md) (auto-reply contract) and [RFC-006](RFC-006-publish-sync-topic-collision.md) (topic_wait collision; this is its Option E).
- **Scope**:
  - An optional `correlation_id` field in the Parcel envelope.
  - `publish_sync` stamps it on each request and matches it on the reply.
  - Auto-replies echo it.
- **Explicitly out of scope**:
  - A new envelope version or prefix (roadmap P2-1).
  - A per-agent reply inbox (P2-2).
  - Request deadlines.
  - Message ids and deduplication (#4).
  - Trace context (#65).
  - Structured errors (#68).
  - Lifting RFC-006's fail-fast for concurrent callers that share a `topic_wait` (§6).

---

## 1. Problem

The envelope had only `version`, `content`, `topic_return` and `error`. `publish_sync` therefore matched a reply by topic alone and returned the first parcel to arrive on its reply topic. With an explicit `topic_wait` reused across calls, a late reply to an earlier, timed-out call became the answer to the next call. So did any other parcel published on that topic.

## 2. Design

### 2.1 Envelope

`Parcel.correlation_id: Optional[str]`, default `None`. The default is declared on the class too, so a Parcel built without `Parcel.__init__` still encodes. That covers a subclass that skips `super().__init__`, and a Parcel pickled by older code.

- **Encoding**: written to the envelope (JSON object or pickled dict) under the key `correlation_id` **only when it is not `None`**. An uncorrelated parcel therefore encodes byte for byte as before; `tests/unit/core/test_reply_correlation.py` pins golden bytes.
- **Decoding**: read when present. A value that is not a `str` decodes as `None`.
- **Version**: `VERSION` stays 3. The field is an additive, optional extension of envelope v3. Decoders older than RFC-014 read only the four original keys, so they ignore it. Decoders do not validate the version anyway (#15).

### 2.2 Requester: `publish_sync`

- Each call draws a fresh id: `secrets.token_hex(16)`, 32 lowercase hex characters, unpredictable.
- The id is set on a **copy** of the outgoing Parcel, so the caller's Parcel is not modified. The existing `topic_return` handling is unchanged (#21).
- The reply handler applies the rules in §2.4.
- The Parcel returned to the caller has its `correlation_id` cleared. The id served only to match the reply. Without clearing, a gateway that answers in `on_message` (no auto-reply there, RFC-003) by forwarding that Parcel by hand would send its own requester a foreign id, and the requester would ignore it.

### 2.3 Responder: auto-reply (RFC-003)

- The auto-reply carries the request's `correlation_id`. This covers a content reply, a Parcel reply and the error reply sent when the handler raises.
- A Parcel returned by the handler is not modified. When its `topic_return` is set (RFC-003) or its correlation id differs, the reply is a shallow copy (`copy.copy`) with `topic_return` cleared and the request's id set. A copy keeps a subclass's own state and never calls its constructor.
- A request without a correlation id gets a reply without one, encoded exactly as before.

### 2.4 Matching rules (requester side)

| Reply `correlation_id` | Outcome |
|---|---|
| equal to the request's | accepted (first one wins, as before) |
| `None` (responder older than RFC-014) | accepted, as before |
| any other value | ignored, logged at DEBUG |

## 3. Compatibility

| Requester \ Responder | Pre-RFC-014 | RFC-014 |
|---|---|---|
| **Pre-RFC-014** | unchanged | Request has no id, so the reply has none: unchanged bytes. |
| **RFC-014** | The responder ignores the id and replies without one; the reply is accepted as before. | Correlated. |

No deployment order is required. The protection against late replies needs the responder to be upgraded. Replies forwarded by hand keep working (§2.2).

## 4. Security notes

- A correlation id is **not authentication**. Anyone subscribed to the request topic sees the id together with `topic_return`, and can forge a matching reply.
- A forged reply can also simply omit the id: uncorrelated replies are accepted for compatibility (§2.4). Message-level authentication and TLS / ACL (#3, #13) remain the controls for that.
- The id comes from `secrets`, so a sender that sees no traffic cannot guess it.

## 5. Alternatives considered

- **New envelope version or prefix (P2-1)**: required a three-release migration for one optional field. Deferred to the full envelope redesign.
- **Reject uncorrelated replies**: would break every responder older than RFC-014. A strict opt-in mode can be added once fleets are upgraded.
- **FIFO waiters per topic (RFC-006 Option D)**: still rejected; arrival order is not the caller's intent.
- **Per-agent inbox with a single wildcard subscription (P2-2)**: removes the per-call SUBSCRIBE / UNSUBSCRIBE. It needs wildcard routing in `Agent._on_message` and in every broker backend, so it is deferred. The correlation id is its prerequisite.

## 6. Follow-ups enabled

- **Shared `topic_wait`**: RFC-006 made a second concurrent `publish_sync` on the same `topic_wait` fail fast because replies could not be told apart. With correlation ids they can, so the waiters on one topic could share it, each receiving its own reply. Doing so changes RFC-006's tested contract (8 tests) and needs a rule for uncorrelated replies when several callers wait. Tracked separately.
- **Request deadlines**: let a responder skip requests whose caller has given up (needs a clock-skew policy).
- **Message ids, deduplication and retries** (#4), **trace context** (#65), **structured errors** (#68).

## 7. Verification

- `tests/unit/core/test_reply_correlation.py` covers:
  - envelope golden bytes and the round trip in both encodings;
  - malformed values;
  - auto-reply echo (content, Parcel, error) with no mutation;
  - fresh ids per request, with the caller's Parcel untouched;
  - a late reply to an earlier call, and a reply to another request, ignored;
  - a reply forwarded by hand through a gateway;
  - a handler's Parcel subclass kept intact;
  - a Parcel built without `__init__`;
  - an uncorrelated reply accepted;
  - a two-agent round trip on a FakeBus.
- `tests/wire/test_reply_correlation_wire.py` checks a two-agent round trip over real MqttBroker clients: the request carries an id and the reply echoes it.
- The existing RFC-001 / RFC-003 / RFC-006 / RFC-007 suites pass unchanged.
