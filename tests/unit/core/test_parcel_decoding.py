"""Parcel decoding validates the envelope, and decode failures are
counted instead of flooding the log (issue #15, W-37).

Parcel wrote a `version` but never checked it: 999, "3", true or a
missing version were all accepted. A payload without an AgentFlow
prefix (plain JSON from another MQTT client) raised a bare TypeError
that escaped Agent._on_message, so MqttBroker._on_message logged a full
traceback for every such message, with no count and no rate limit.

Contracts verified:
  - The envelope's `version` must be an int (not a bool) from 1 to
    VERSION; otherwise, or when it is missing, decoding raises
    ParcelDecodeError (a TypeError) in both encodings.
  - A payload without an AgentFlow prefix raises ParcelDecodeError.
  - Unknown envelope keys are ignored, so that optional fields added
    later (as RFC-014 / RFC-015 did) do not break older receivers.
  - Agent._on_message counts decode failures (decode_failure_count)
    and logs them at WARNING on the 1st, 2nd, 4th, ... occurrence,
    without a traceback; nothing escapes to the broker callback.
"""

import json
import logging
import pickle
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from agentflow.broker.mqtt_broker import MqttBroker
from agentflow.core.agent import Agent
from agentflow.core.parcel import (
    VERSION,
    BinaryParcel,
    Parcel,
    ParcelDecodeError,
    TextParcel,
)
from tests.fakes.fake_broker import FakeBroker, FakeWorker
from tests.fakes.fake_bus import FakeBus



def _text(body) -> bytes:
    return TextParcel.HEAD + json.dumps(body).encode()


def _binary(body) -> bytes:
    return BinaryParcel.HEAD + pickle.dumps(body)


# ==========================================================================
# A. Envelope validation
# ==========================================================================

@pytest.mark.parametrize('encode', [_text, _binary], ids=['text', 'binary'])
@pytest.mark.parametrize('body', [
    {'version': VERSION + 1, 'content': 'x'},
    {'version': 999, 'content': 'x'},
    {'version': 0, 'content': 'x'},
    {'version': -1, 'content': 'x'},
    {'version': '3', 'content': 'x'},
    {'version': True, 'content': 'x'},
    {'version': 3.0, 'content': 'x'},
    {'version': None, 'content': 'x'},
    {'content': 'x'},
    {},
], ids=lambda b: repr(b)[:40])
def test_an_unsupported_or_missing_version_is_refused(encode, body):
    with pytest.raises(ParcelDecodeError, match='version'):
        Parcel.from_payload(encode(body))


@pytest.mark.parametrize('payload', [b'{"temp": 21.5}', b'plain text', b'', b'\x00\x01'])
def test_a_payload_without_an_agentflow_prefix_is_refused(payload):
    with pytest.raises(ParcelDecodeError, match='AgentFlow'):
        Parcel.from_payload(payload)


@pytest.mark.parametrize('encode', [_text, _binary], ids=['text', 'binary'])
@pytest.mark.parametrize('version', list(range(1, VERSION + 1)))
def test_supported_versions_are_accepted(encode, version):
    parcel = Parcel.from_payload(encode({'version': version, 'content': 'x'}))
    assert (parcel.version, parcel.content) == (version, 'x')


@pytest.mark.parametrize('encode', [_text, _binary], ids=['text', 'binary'])
def test_unknown_envelope_keys_are_ignored(encode):
    parcel = Parcel.from_payload(encode({'version': VERSION, 'content': 'x', 'added_later': 1}))
    assert parcel.content == 'x'


def test_parcel_decode_error_is_still_a_type_error():
    assert issubclass(ParcelDecodeError, TypeError)


# ==========================================================================
# B. Decode failures in Agent._on_message
# ==========================================================================

@pytest.fixture
def records():
    collected = []
    handler = logging.Handler()
    handler.emit = collected.append
    root = logging.getLogger()
    level = root.level
    root.addHandler(handler)
    root.setLevel(logging.DEBUG)
    yield collected
    root.removeHandler(handler)
    root.setLevel(level)


def test_decode_failures_are_counted_not_logged_with_tracebacks(records):
    agent = Agent('sensor_reader', {})
    broker = MqttBroker(agent, wait=False)
    for _ in range(5):
        broker._on_message(MagicMock(), None,
                           SimpleNamespace(topic='sensor/temp', payload=b'{"temp": 21.5}'))

    # Since issue #34 decoding runs on a dispatcher thread.
    assert FakeBus.wait_until(lambda: agent.decode_failure_count == 5, timeout=3)
    assert [r for r in records if r.exc_info] == []
    assert [r for r in records if r.levelno >= logging.ERROR] == []
    warnings = [r for r in records if r.levelno == logging.WARNING]
    assert len(warnings) == 3                       # the 1st, 2nd and 4th
    assert 'sensor/temp' in warnings[0].getMessage()


def test_a_valid_message_after_failures_is_still_handled():
    agent = Agent('sensor_reader', {'dispatch': {'shutdown_timeout_s': 1.0}})
    broker = FakeBroker(notifier=agent)
    agent._broker = broker
    agent._agent_worker = FakeWorker()
    seen = []
    agent.subscribe('t', topic_handler=lambda topic, pcl: seen.append(pcl.content))
    try:
        for payload in (b'{"temp": 21.5}', TextParcel.HEAD + b'[1]'):
            try:
                broker.deliver('t', payload)
            except Exception:
                pass                                # escaped before #15
        broker.deliver('t', TextParcel('ok').payload())
        assert FakeBus.wait_until(lambda: seen == ['ok'], timeout=2)
        assert FakeBus.wait_until(lambda: agent.decode_failure_count == 2, timeout=2)
    finally:
        agent.terminate()


# ==========================================================================
# C. Hostile envelopes (gaps found in review)
# ==========================================================================

def _global_ref(module: str, name: str) -> bytes:
    """A protocol-4 pickle that only references module.name (refused by
    the restricted unpickler before anything is called)."""
    def short_or_long(text):
        raw = text.encode('utf-8')
        if len(raw) < 256:
            return b'\x8c' + bytes([len(raw)]) + raw
        return b'\x8d' + len(raw).to_bytes(8, 'little') + raw
    return b'\x80\x04' + short_or_long(module) + short_or_long(name) + b'\x93.'


@pytest.mark.parametrize('payload', [
    _binary({'version': 1 << 20000, 'content': 'x'}),       # int too large to print
    TextParcel.HEAD + b'[' * 200000,                          # nesting too deep for json
    TextParcel.HEAD + b'{"version": ' + b'[' * 200000 + b']' * 200000 + b'}',
], ids=['huge-int-version', 'deep-json', 'deep-json-version'])
def test_hostile_envelopes_raise_parcel_decode_error(payload):
    with pytest.raises(ParcelDecodeError):
        Parcel.from_payload(payload)


def test_the_decode_failure_warning_is_escaped_and_bounded(records):
    agent = Agent('sensor_reader', {})
    broker = MqttBroker(agent, wait=False)
    forged = 'x\n2026-10-12 00:00:00,000 CRITICAL agentflow forged line' + 'y' * 100000
    payload = BinaryParcel.HEAD + _global_ref(forged, 'name')
    broker._on_message(MagicMock(), None, SimpleNamespace(topic='t', payload=payload))

    assert FakeBus.wait_until(lambda: agent.decode_failure_count == 1, timeout=3)
    [warning] = [r for r in records if r.levelno == logging.WARNING]
    text = warning.getMessage()
    assert '\n' not in text
    assert len(text) < 1000
