"""Authentication of holon control messages (issue #13).

When the Agent config sets `holon.control_secret`, every holon message
whose subject is a control subject (see CONTROL_SUBJECTS) carries

    'auth': {'v': 1, 'ts': <milliseconds since the epoch>, 'mac': <hex>}

where `mac` is HMAC-SHA256 under the shared secret over a canonical
JSON encoding of the topic the message is published to, `ts`, and the
message without its `auth` field. A receiver with the same secret
drops a control message whose MAC does not match, or whose `ts` is
more than `holon.control_max_age` seconds away from its own clock.

Binding the topic means a captured message cannot be replayed onto
another topic, e.g. a unicast `register_parent` onto another child's
channel. Replays on the same topic within the age window are not
detected; the control operations are idempotent (#5, #6).

Only the four control subjects are authenticated. Application subjects
sent through the holon helpers are not, like any other topic.
"""

import hashlib
import hmac
import json
import re
import time
from typing import Optional

CONTROL_SUBJECTS = frozenset({
    'terminate', 'register_child', 'register_parent', 'parent_online',
})
AUTH_FIELD = 'auth'

_VERSION = 1
_DOMAIN = 'agentflow.holon.control'
_MAC_RE = re.compile(r'[0-9a-f]{64}')
_MAX_TS = 2 ** 53       # milliseconds; far beyond any real clock


def is_control(message: dict) -> bool:
    subject = message.get('subject')
    return isinstance(subject, str) and subject in CONTROL_SUBJECTS


def _canonical(topic: str, ts: int, message: dict) -> bytes:
    body = {k: v for k, v in message.items() if k != AUTH_FIELD}
    # Sign what the receiver sees: a TextParcel round trip turns tuples
    # into lists and non-str keys into str. Normalising on both sides
    # also covers BinaryParcel payloads, which keep the original types.
    body = json.loads(json.dumps(body, allow_nan=False))
    return json.dumps(
        {'domain': _DOMAIN, 'v': _VERSION, 'topic': topic, 'ts': ts, 'message': body},
        sort_keys=True, separators=(',', ':'), ensure_ascii=True, allow_nan=False,
    ).encode('ascii')


def _mac(key: bytes, topic: str, ts: int, message: dict) -> str:
    return hmac.new(key, _canonical(topic, ts, message), hashlib.sha256).hexdigest()


def sign(key: bytes, topic: str, message: dict, now: Optional[float] = None) -> dict:
    """Return a copy of `message` carrying an `auth` field for `topic`.
    Raises TypeError / ValueError / RecursionError if the message cannot
    be encoded as JSON (e.g. bytes or NaN in its data)."""
    ts = int((time.time() if now is None else now) * 1000)
    signed = dict(message)
    signed[AUTH_FIELD] = {'v': _VERSION, 'ts': ts, 'mac': _mac(key, topic, ts, message)}
    return signed


def verify(key: bytes, topic: str, message: dict, max_age: float,
           now: Optional[float] = None) -> Optional[str]:
    """Return None if `message` was signed with `key` for `topic` within
    `max_age` seconds of `now`; otherwise the reason it was not. Never
    raises on malformed input."""
    auth = message.get(AUTH_FIELD)
    if not isinstance(auth, dict):
        return 'not signed'
    if auth.get('v') != _VERSION:
        return 'unsupported auth version'
    ts, mac = auth.get('ts'), auth.get('mac')
    if (type(ts) is not int or not 0 <= ts <= _MAX_TS
            or not isinstance(mac, str) or not _MAC_RE.fullmatch(mac)):
        return 'malformed auth'
    now_ms = (time.time() if now is None else now) * 1000
    if abs(now_ms - ts) > max_age * 1000:
        return 'timestamp outside the allowed age'
    try:
        expected = _mac(key, topic, ts, message)
    except (TypeError, ValueError, RecursionError):
        return 'content cannot be encoded canonically'
    if not hmac.compare_digest(expected, mac):
        return 'bad MAC'
    return None
