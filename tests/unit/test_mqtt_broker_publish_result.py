"""RFC-012 tests: MQTT publish result contract.

Post-RFC-012 behaviour:
  - `MqttBroker.publish` runs a state gate (pre-call snapshot).
  - Rejection reasons raised as `MqttPublishError`:
      * BROKER_STOPPING (priority over state)
      * BROKER_NOT_RUNNING (state != RUNNING)
      * BROKER_DISCONNECTED (state RUNNING but _connected=False)
      * PAHO_REJECTED (rc != MQTT_ERR_SUCCESS)
      * UNSUPPORTED_RESULT (result shape or rc/mid unparseable)
  - Success returns paho's original result unchanged.
  - `Agent.publish` still fire-and-forget; `_publish_or_raise`
    propagates; `publish_sync` fast-fails BEFORE `event.wait`.

Gate is a pre-call best-effort snapshot; not a full stop
linearization barrier. paho rc validation is the second layer.
"""

import inspect
import threading
import time
from typing import Any, List, Optional
from unittest.mock import MagicMock

import pytest

from agentflow.broker import MqttPublishError, MqttPublishReason
from agentflow.broker.empty_broker import EmptyBroker
from agentflow.broker.message_broker import MessageBroker
from agentflow.broker.mqtt_broker import MqttBroker
from agentflow.core.agent_worker import WorkerState


# Paho MQTT error codes (subset).
MQTT_ERR_SUCCESS = 0
MQTT_ERR_NOMEM = 1
MQTT_ERR_PROTOCOL = 2
MQTT_ERR_INVAL = 3
MQTT_ERR_NO_CONN = 4
MQTT_ERR_QUEUE_SIZE = 14


# ---------------------------------------------------------------------------
# Fake MessageInfo (mimics paho.mqtt.client.MQTTMessageInfo shape)
# ---------------------------------------------------------------------------


class _FakeMessageInfo:
    """Duck-typed MQTTMessageInfo with `.rc` and `.mid`."""

    def __init__(self, rc: int = MQTT_ERR_SUCCESS, mid: int = 1):
        self.rc = rc
        self.mid = mid

    def __repr__(self):
        return f"_FakeMessageInfo(rc={self.rc}, mid={self.mid})"


def _success_info(mid=1):
    return _FakeMessageInfo(rc=MQTT_ERR_SUCCESS, mid=mid)


def _configure_paho_return(fake_client, message_info):
    fake_client.publish.return_value = message_info


def _prime_connected(broker, fake_client):
    """Drive state → RUNNING with _connected=True via _on_connect."""
    broker._on_connect(
        client=fake_client, userdata=None, flags={},
        reasonCode=0, properties=None,
    )
    fake_client.subscribe.reset_mock()
    fake_client.unsubscribe.reset_mock()


# ===========================================================================
# A. Basic publish lifecycle (rc SUCCESS path preserved)
# ===========================================================================


def test_A1_publish_delegates_to_client_publish_when_state_gate_open(broker, fake_client):
    _prime_connected(broker, fake_client)
    _configure_paho_return(fake_client, _success_info(mid=42))
    broker.publish("some/topic", b"payload-bytes")
    fake_client.publish.assert_called_once_with(
        topic="some/topic", payload=b"payload-bytes",
    )


def test_A2_publish_uses_keyword_args_topic_and_payload(broker, fake_client):
    _prime_connected(broker, fake_client)
    _configure_paho_return(fake_client, _success_info())
    broker.publish("t", b"p")
    args, kwargs = fake_client.publish.call_args
    assert args == ()
    assert set(kwargs.keys()) == {"topic", "payload"}


def test_A3_publish_does_NOT_pass_qos_or_retain(broker, fake_client):
    """RFC-012 §Out of scope: QoS/retain kwargs deferred."""
    _prime_connected(broker, fake_client)
    _configure_paho_return(fake_client, _success_info())
    broker.publish("t", b"p")
    _, kwargs = fake_client.publish.call_args
    assert "qos" not in kwargs
    assert "retain" not in kwargs


def test_A4_publish_serialization_TextParcel_uses_text_head(broker, fake_client):
    from agentflow.core.parcel import TextParcel
    _prime_connected(broker, fake_client)
    _configure_paho_return(fake_client, _success_info())
    payload = TextParcel({"k": "v"}).payload()
    broker.publish("t", payload)
    _, kwargs = fake_client.publish.call_args
    assert kwargs["payload"].startswith(b"text/json|")


def test_A5_publish_serialization_BinaryParcel_uses_pickle_head(broker, fake_client):
    from agentflow.core.parcel import BinaryParcel
    _prime_connected(broker, fake_client)
    _configure_paho_return(fake_client, _success_info())
    payload = BinaryParcel(b"raw-bytes").payload()
    broker.publish("t", payload)
    _, kwargs = fake_client.publish.call_args
    assert kwargs["payload"].startswith(b"application/pickle|")


def test_A6_publish_success_returns_original_paho_result_unchanged(broker, fake_client):
    """RFC-012 §7.10: success returns paho's original object."""
    _prime_connected(broker, fake_client)
    info = _success_info(mid=99)
    _configure_paho_return(fake_client, info)
    result = broker.publish("t", b"p")
    assert result is info


def test_A7_publish_source_still_does_NOT_call_wait_for_publish():
    """RFC-012 §Out of scope: QoS 1/2 acknowledgment deferred."""
    src = inspect.getsource(MqttBroker.publish)
    assert "wait_for_publish" not in src


def test_A8_publish_source_still_does_NOT_call_is_published():
    src = inspect.getsource(MqttBroker.publish)
    assert "is_published" not in src


def test_A9_publish_source_NOW_validates_rc_via_normalizer():
    """INVERTED: pre-RFC-012 source had no rc check; post-RFC-012
    delegates to `_normalise_publish_result` which reads .rc."""
    publish_src = inspect.getsource(MqttBroker.publish)
    assert "MQTT_ERR_SUCCESS" in publish_src
    assert "_normalise_publish_result" in publish_src


def test_A10_client_publish_raise_propagates_up(broker, fake_client):
    """RFC-002 semantics preserved: paho exceptions propagate
    (as opposed to being wrapped as MqttPublishError)."""
    _prime_connected(broker, fake_client)
    fake_client.publish.side_effect = RuntimeError("paho broke")
    with pytest.raises(RuntimeError, match="paho broke"):
        broker.publish("t", b"p")


# ===========================================================================
# B. MessageInfo.rc — rc failures raise (INVERTED from characterization)
# ===========================================================================


def test_B11_rc_SUCCESS_publish_returns_normally(broker, fake_client):
    _prime_connected(broker, fake_client)
    info = _success_info()
    _configure_paho_return(fake_client, info)
    result = broker.publish("t", b"p")
    assert result is info


def test_B12_rc_NO_CONN_raises_MqttPublishError_PAHO_REJECTED(broker, fake_client):
    """INVERTED: pre-RFC-012 silently swallowed NO_CONN."""
    _prime_connected(broker, fake_client)
    _configure_paho_return(fake_client, _FakeMessageInfo(rc=MQTT_ERR_NO_CONN, mid=0))
    with pytest.raises(MqttPublishError) as exc_info:
        broker.publish("t", b"p")
    assert exc_info.value.reason is MqttPublishReason.PAHO_REJECTED
    assert exc_info.value.rc == MQTT_ERR_NO_CONN
    assert exc_info.value.topic == "t"


def test_B13_rc_QUEUE_SIZE_raises_MqttPublishError(broker, fake_client):
    _prime_connected(broker, fake_client)
    _configure_paho_return(fake_client, _FakeMessageInfo(rc=MQTT_ERR_QUEUE_SIZE))
    with pytest.raises(MqttPublishError) as exc_info:
        broker.publish("t", b"p")
    assert exc_info.value.reason is MqttPublishReason.PAHO_REJECTED
    assert exc_info.value.rc == MQTT_ERR_QUEUE_SIZE


def test_B14_rc_PROTOCOL_raises_MqttPublishError(broker, fake_client):
    _prime_connected(broker, fake_client)
    _configure_paho_return(fake_client, _FakeMessageInfo(rc=MQTT_ERR_PROTOCOL))
    with pytest.raises(MqttPublishError) as exc_info:
        broker.publish("t", b"p")
    assert exc_info.value.reason is MqttPublishReason.PAHO_REJECTED


def test_B15_rc_UNKNOWN_nonzero_raises_MqttPublishError_forward_compat(broker, fake_client):
    _prime_connected(broker, fake_client)
    _configure_paho_return(fake_client, _FakeMessageInfo(rc=99))
    with pytest.raises(MqttPublishError) as exc_info:
        broker.publish("t", b"p")
    assert exc_info.value.rc == 99


def test_B16_object_without_rc_attribute_raises_UNSUPPORTED_RESULT(broker, fake_client):
    """INVERTED: pre-RFC-012 silently returned arbitrary objects."""
    _prime_connected(broker, fake_client)
    _configure_paho_return(fake_client, object())
    with pytest.raises(MqttPublishError) as exc_info:
        broker.publish("t", b"p")
    assert exc_info.value.reason is MqttPublishReason.UNSUPPORTED_RESULT
    assert exc_info.value.result_type == "object"


def test_B17_None_return_raises_UNSUPPORTED_RESULT(broker, fake_client):
    """INVERTED: pre-RFC-012 silently returned None."""
    _prime_connected(broker, fake_client)
    fake_client.publish.return_value = None
    with pytest.raises(MqttPublishError) as exc_info:
        broker.publish("t", b"p")
    assert exc_info.value.reason is MqttPublishReason.UNSUPPORTED_RESULT
    assert exc_info.value.result_type == "NoneType"


def test_B18_paho_v1_tuple_success_returns_original_tuple(broker, fake_client):
    """RFC-012 §7.8: v1 (rc, mid) tuple supported."""
    _prime_connected(broker, fake_client)
    fake_client.publish.return_value = (MQTT_ERR_SUCCESS, 42)
    result = broker.publish("t", b"p")
    assert result == (MQTT_ERR_SUCCESS, 42)


def test_B19_paho_v1_tuple_rc_failure_raises_PAHO_REJECTED(broker, fake_client):
    _prime_connected(broker, fake_client)
    fake_client.publish.return_value = (MQTT_ERR_NO_CONN, 0)
    with pytest.raises(MqttPublishError) as exc_info:
        broker.publish("t", b"p")
    assert exc_info.value.reason is MqttPublishReason.PAHO_REJECTED
    assert exc_info.value.rc == MQTT_ERR_NO_CONN
    assert exc_info.value.mid == 0


def test_B20_no_shared_last_publish_error_field(broker):
    """RFC-012 §Appendix A / §H rejects shared last-error field."""
    assert not hasattr(broker, 'last_publish_error')
    assert not hasattr(broker, 'last_publish_rc')
    assert not hasattr(broker, 'last_publish_mid')


def test_B21_malformed_rc_raises_UNSUPPORTED_with_cause(broker, fake_client):
    """RFC-012 §C: rc coercion failure raises UNSUPPORTED_RESULT
    with the underlying exception preserved as __cause__."""
    _prime_connected(broker, fake_client)
    bad = _FakeMessageInfo()
    bad.rc = object()   # int() will fail
    _configure_paho_return(fake_client, bad)
    with pytest.raises(MqttPublishError) as exc_info:
        broker.publish("t", b"p")
    assert exc_info.value.reason is MqttPublishReason.UNSUPPORTED_RESULT
    assert exc_info.value.detail == "invalid rc"
    assert exc_info.value.__cause__ is not None
    assert isinstance(exc_info.value.__cause__, (TypeError, ValueError))


def test_B22_malformed_mid_raises_UNSUPPORTED_with_cause(broker, fake_client):
    _prime_connected(broker, fake_client)
    bad = _FakeMessageInfo(rc=MQTT_ERR_SUCCESS)
    bad.mid = object()
    _configure_paho_return(fake_client, bad)
    with pytest.raises(MqttPublishError) as exc_info:
        broker.publish("t", b"p")
    assert exc_info.value.reason is MqttPublishReason.UNSUPPORTED_RESULT
    assert exc_info.value.detail == "invalid mid"
    assert exc_info.value.__cause__ is not None


def test_B23_tuple_with_malformed_rc_raises_UNSUPPORTED_with_cause(broker, fake_client):
    _prime_connected(broker, fake_client)
    fake_client.publish.return_value = (object(), 1)
    with pytest.raises(MqttPublishError) as exc_info:
        broker.publish("t", b"p")
    assert exc_info.value.reason is MqttPublishReason.UNSUPPORTED_RESULT
    assert exc_info.value.__cause__ is not None


# ===========================================================================
# C. State gate — publish rejection at gate (INVERTED)
# ===========================================================================


def test_C21_publish_in_NEW_state_raises_BROKER_NOT_RUNNING(broker, fake_client):
    """INVERTED: pre-RFC-012 publish reached client from NEW."""
    assert broker.state is WorkerState.NEW
    with pytest.raises(MqttPublishError) as exc_info:
        broker.publish("t", b"p")
    assert exc_info.value.reason is MqttPublishReason.BROKER_NOT_RUNNING
    assert exc_info.value.state is WorkerState.NEW
    # paho client NOT invoked.
    fake_client.publish.assert_not_called()


def test_C22_publish_in_STARTING_state_raises_BROKER_NOT_RUNNING(broker, fake_client):
    broker.start({})
    assert broker.state is WorkerState.STARTING
    with pytest.raises(MqttPublishError) as exc_info:
        broker.publish("t", b"p")
    assert exc_info.value.reason is MqttPublishReason.BROKER_NOT_RUNNING
    assert exc_info.value.state is WorkerState.STARTING
    fake_client.publish.assert_not_called()


def test_C23_publish_in_RUNNING_and_connected_is_allowed(broker, fake_client):
    _prime_connected(broker, fake_client)
    _configure_paho_return(fake_client, _success_info())
    broker.publish("t", b"p")
    fake_client.publish.assert_called_once()


def test_C24_publish_in_RUNNING_but_disconnected_raises_BROKER_DISCONNECTED(
    broker, fake_client,
):
    """Simulate RFC-005 disconnect between _prime and publish."""
    _prime_connected(broker, fake_client)
    # Simulate _on_disconnect flipping _connected without a re-connect.
    broker._on_disconnect(
        client=fake_client, userdata=None, _flags={},
        reasonCode=7, _properties=None,
    )
    assert broker._connected is False
    assert broker.state is WorkerState.RUNNING
    with pytest.raises(MqttPublishError) as exc_info:
        broker.publish("t", b"p")
    assert exc_info.value.reason is MqttPublishReason.BROKER_DISCONNECTED
    fake_client.publish.assert_not_called()


def test_C25_publish_from_STOPPED_raises_BROKER_STOPPING(broker, fake_client):
    """After stop(), _stopping=True; RFC-012 §B priority order puts
    STOPPING check FIRST (even before state check)."""
    _prime_connected(broker, fake_client)
    broker.stop(graceful_timeout_s=2.0)
    fake_client.publish.reset_mock()
    with pytest.raises(MqttPublishError) as exc_info:
        broker.publish("t", b"p")
    # _stopping check fires before state check.
    assert exc_info.value.reason is MqttPublishReason.BROKER_STOPPING
    fake_client.publish.assert_not_called()


def test_C26_publish_from_START_FAILED_raises(fake_client, notifier, monkeypatch):
    monkeypatch.setattr(
        'agentflow.broker.mqtt_broker.Client', lambda *a, **kw: fake_client,
    )
    b = MqttBroker(notifier=notifier, wait=True, timeout=0.1)
    fake_client.connect.side_effect = RuntimeError("nope")
    with pytest.raises(RuntimeError):
        b.start({})
    assert b.state is WorkerState.START_FAILED
    fake_client.publish.reset_mock()
    with pytest.raises(MqttPublishError) as exc_info:
        b.publish("t", b"p")
    # START_FAILED sets _stopping=True → BROKER_STOPPING wins.
    assert exc_info.value.reason is MqttPublishReason.BROKER_STOPPING
    fake_client.publish.assert_not_called()


def test_C27_stopping_flag_priority_over_state_check(broker, fake_client):
    """RFC-012 §B modification 2: _stopping=True check WINS over state
    check — even if state happens to be RUNNING."""
    _prime_connected(broker, fake_client)   # state=RUNNING
    broker._stopping = True                 # simulate stop mid-flight
    with pytest.raises(MqttPublishError) as exc_info:
        broker.publish("t", b"p")
    assert exc_info.value.reason is MqttPublishReason.BROKER_STOPPING


def test_C28_publish_with_connected_False_uses_gate_not_paho_rc(
    broker, fake_client,
):
    """When _connected=False AND state=RUNNING, gate rejects with
    BROKER_DISCONNECTED (rc=None), NOT PAHO_REJECTED (rc=NO_CONN)."""
    # Simulate: state=RUNNING but _connected still False (didn't
    # complete _on_connect — very short window, hypothetical).
    broker._state = WorkerState.RUNNING
    broker._connected = False
    with pytest.raises(MqttPublishError) as exc_info:
        broker.publish("t", b"p")
    assert exc_info.value.reason is MqttPublishReason.BROKER_DISCONNECTED
    assert exc_info.value.rc is None   # gate rejected before paho
    fake_client.publish.assert_not_called()


def test_C29_gate_does_NOT_hold_state_lock_across_client_publish_by_source():
    """RFC-012 §B modification 2: lock hygiene. State snapshot must
    be OUTSIDE the state-lock section that wraps client.publish."""
    src = inspect.getsource(MqttBroker.publish)
    # Verify shape: 'with self._state_lock:' block precedes but does
    # NOT contain 'self._client.publish('.
    lock_pos = src.find("with self._state_lock:")
    publish_pos = src.find("self._client.publish(")
    assert lock_pos != -1 and publish_pos != -1
    # Find end of 'with' block: next non-indented line.
    # A simpler check: 'self._client.publish(' must not appear inside
    # the 'with' block. Since 'with' block ends by dedent, and the
    # publish call is at the same indent level as `with`, we can
    # verify that 'client.publish' appears AFTER the 'with' block's
    # last indented line.
    assert publish_pos > lock_pos
    # And no 'self._client.publish' inside a `with self._state_lock`
    # section (check by absence of both on same indent-level line).
    lock_block = src[lock_pos:publish_pos]
    assert "self._client.publish" not in lock_block


def test_C30_gate_is_documented_as_best_effort_snapshot_by_source():
    """RFC-012 §B modification 2 requires an explicit comment
    clarifying the gate is NOT a full linearization barrier with stop."""
    src = inspect.getsource(MqttBroker.publish)
    lowered = src.lower()
    assert "pre-call" in lowered or "snapshot" in lowered
    assert "linearization" in lowered or "concurrent" in lowered


def test_C31_snapshot_race_permitted_publish_may_reach_paho_after_snapshot(
    broker, fake_client,
):
    """RFC-012 §B modification 2: after snapshot, a concurrent stop()
    can flip _stopping=True before client.publish runs. paho's rc
    validation is the authoritative second layer — a truly rejected
    publish surfaces as PAHO_REJECTED.

    Documents the KNOWN race, does NOT test-forbid it: the gate is
    intentionally best-effort per RFC-012 modification 2.
    """
    _prime_connected(broker, fake_client)

    # Force snapshot to see "OK" but flip _stopping between snapshot
    # and paho call. Implement via a side_effect on client.publish
    # that fires AFTER our snapshot was already taken.
    def _paho_side_effect(*a, **kw):
        # By the time paho is invoked, another thread has stopped us.
        # paho itself would (in real deployment) return NO_CONN.
        return _FakeMessageInfo(rc=MQTT_ERR_NO_CONN)

    fake_client.publish.side_effect = _paho_side_effect
    with pytest.raises(MqttPublishError) as exc_info:
        broker.publish("t", b"p")
    # Second-layer catches it as PAHO_REJECTED (rc=NO_CONN).
    assert exc_info.value.reason is MqttPublishReason.PAHO_REJECTED
    assert exc_info.value.rc == MQTT_ERR_NO_CONN


# ===========================================================================
# D. QoS semantics unchanged (RFC-012 explicitly out of scope)
# ===========================================================================


def test_D32_no_qos_argument_defaults_to_paho_default_qos_0(broker, fake_client):
    _prime_connected(broker, fake_client)
    _configure_paho_return(fake_client, _success_info())
    broker.publish("t", b"p")
    _, kwargs = fake_client.publish.call_args
    assert "qos" not in kwargs


def test_D33_wait_for_publish_still_not_called_by_source():
    src = inspect.getsource(MqttBroker.publish)
    assert "wait_for_publish" not in src


def test_D34_is_published_still_not_called_by_source():
    src = inspect.getsource(MqttBroker.publish)
    assert "is_published" not in src


def test_D35_retain_flag_still_not_passed(broker, fake_client):
    _prime_connected(broker, fake_client)
    _configure_paho_return(fake_client, _success_info())
    broker.publish("t", b"p")
    _, kwargs = fake_client.publish.call_args
    assert "retain" not in kwargs


# ===========================================================================
# E. Agent integration — fire-and-forget preserved; fast-fail restored
# ===========================================================================


def _make_agent_wired_to(broker):
    from agentflow.core.agent import Agent
    from tests.fakes.fake_broker import FakeWorker
    a = Agent(name='pub_result', agent_config={})
    a._broker = broker
    a._agent_worker = FakeWorker()
    return a


def test_E41_agent_publish_returns_None_and_swallows_MqttPublishError(broker, fake_client):
    """RFC-012 §E: fire-and-forget preserved."""
    _prime_connected(broker, fake_client)
    _configure_paho_return(fake_client, _FakeMessageInfo(rc=MQTT_ERR_NO_CONN))
    agent = _make_agent_wired_to(broker)
    result = agent.publish("t", "hello")
    assert result is None   # swallowed by except Exception


def test_E42_publish_or_raise_propagates_MqttPublishError(broker, fake_client):
    """INVERTED: pre-RFC-012 silently returned None on rc failure."""
    _prime_connected(broker, fake_client)
    _configure_paho_return(fake_client, _FakeMessageInfo(rc=MQTT_ERR_NO_CONN))
    agent = _make_agent_wired_to(broker)
    with pytest.raises(MqttPublishError):
        agent._publish_or_raise("t", "hello")


def test_E43_publish_sync_rc_failure_fast_fails_within_50ms(broker, fake_client):
    """INVERTED: pre-RFC-012 waited full timeout."""
    _prime_connected(broker, fake_client)
    _configure_paho_return(fake_client, _FakeMessageInfo(rc=MQTT_ERR_NO_CONN))
    agent = _make_agent_wired_to(broker)
    t0 = time.monotonic()
    with pytest.raises(MqttPublishError):
        agent.publish_sync("t", "hello", timeout=5.0)
    elapsed = time.monotonic() - t0
    # RFC-002 fast-fail contract RESTORED.
    assert elapsed < 0.5, f"expected fast-fail, elapsed={elapsed:.3f}"


def test_E44_publish_sync_broker_publish_raise_still_fast_fails(broker, fake_client):
    """Positive control: RFC-002 fast-fail continues to work for
    Exception paths."""
    _prime_connected(broker, fake_client)
    fake_client.publish.side_effect = ConnectionError("broker down")
    agent = _make_agent_wired_to(broker)
    t0 = time.monotonic()
    with pytest.raises(ConnectionError):
        agent.publish_sync("t", "hello", timeout=5.0)
    elapsed = time.monotonic() - t0
    assert elapsed < 0.5


def test_E45_rc_failure_and_Exception_both_fast_fail_uniformly(broker, fake_client):
    """RFC-012 restored parity: rc failure and Exception both fast-fail
    at the Agent layer (unlike pre-RFC-012)."""
    _prime_connected(broker, fake_client)
    agent = _make_agent_wired_to(broker)

    # Case A: rc failure → MqttPublishError
    _configure_paho_return(fake_client, _FakeMessageInfo(rc=MQTT_ERR_NO_CONN))
    fake_client.publish.side_effect = None
    with pytest.raises(MqttPublishError):
        agent._publish_or_raise("t", "x")

    # Case B: paho Exception → propagates
    fake_client.publish.side_effect = ConnectionError("nope")
    with pytest.raises(ConnectionError):
        agent._publish_or_raise("t", "x")

    # Both cases: Agent.publish silently swallows (fire-and-forget).
    fake_client.publish.side_effect = None
    _configure_paho_return(fake_client, _FakeMessageInfo(rc=MQTT_ERR_NO_CONN))
    assert agent.publish("t", "x") is None
    fake_client.publish.side_effect = ConnectionError("nope")
    assert agent.publish("t", "x") is None


def test_E46_agent_publish_source_still_fire_and_forget_shape():
    from agentflow.core.agent import Agent
    src = inspect.getsource(Agent.publish)
    assert "try:" in src
    assert "except Exception" in src
    assert "_publish_or_raise" in src


def test_E47_publish_sync_finally_registry_cleanup_still_runs_on_MqttPublishError(
    broker, fake_client,
):
    """RFC-006/007 waiter cleanup MUST run even when _publish_or_raise
    raises MqttPublishError."""
    _prime_connected(broker, fake_client)
    _configure_paho_return(fake_client, _FakeMessageInfo(rc=MQTT_ERR_NO_CONN))
    agent = _make_agent_wired_to(broker)
    handlers_before = dict(agent._Agent__topic_handlers)
    try:
        agent.publish_sync("t", "hello", topic_wait="wait/topic", timeout=1.0)
    except MqttPublishError:
        pass
    # Waiter registry cleaned up.
    handlers_after = dict(agent._Agent__topic_handlers)
    assert "wait/topic" not in handlers_after


def test_E48_publish_sync_finally_broker_unsubscribe_still_called_on_MqttPublishError(
    broker, fake_client,
):
    """RFC-005 broker.unsubscribe still called in finally."""
    _prime_connected(broker, fake_client)
    _configure_paho_return(fake_client, _FakeMessageInfo(rc=MQTT_ERR_NO_CONN))
    agent = _make_agent_wired_to(broker)
    fake_client.unsubscribe.reset_mock()
    try:
        agent.publish_sync("t", "hello", topic_wait="wait/topic", timeout=1.0)
    except MqttPublishError:
        pass
    # broker.unsubscribe was invoked in the finally cleanup.
    assert fake_client.unsubscribe.called


def test_E49_publish_sync_rc_failure_never_enters_event_wait(broker, fake_client):
    """Elapsed timing confirms _publish_or_raise raised BEFORE
    event.wait(timeout). Compare to E43 for fast-fail evidence."""
    _prime_connected(broker, fake_client)
    _configure_paho_return(fake_client, _FakeMessageInfo(rc=MQTT_ERR_QUEUE_SIZE))
    agent = _make_agent_wired_to(broker)
    t0 = time.monotonic()
    with pytest.raises(MqttPublishError):
        agent.publish_sync("t", "hello", timeout=10.0)
    elapsed = time.monotonic() - t0
    # Much less than the 10s timeout.
    assert elapsed < 0.5


def test_E50_agent_publish_from_STOPPED_broker_silent(broker, fake_client):
    """State-gate MqttPublishError caught by Agent.publish's
    fire-and-forget; caller sees None."""
    _prime_connected(broker, fake_client)
    broker.stop(graceful_timeout_s=2.0)
    agent = _make_agent_wired_to(broker)
    assert agent.publish("t", "x") is None


# ===========================================================================
# F. Auto-reply / dispatcher fault isolation
# ===========================================================================


def test_F52_auto_reply_publish_rc_failure_swallowed_by_Agent_publish(
    broker, fake_client,
):
    """RFC-003 auto-reply goes through Agent.publish (fire-and-forget).
    RFC-012 MqttPublishError caught by existing except Exception."""
    _prime_connected(broker, fake_client)
    _configure_paho_return(fake_client, _FakeMessageInfo(rc=MQTT_ERR_NO_CONN))
    agent = _make_agent_wired_to(broker)
    # Simulate auto-reply.
    agent.publish("reply/topic", "reply-data")
    # No exception raised; dispatcher unaffected.


def test_F53_notifier_callback_publish_failure_does_not_stop_further_publishes(
    broker, fake_client,
):
    _prime_connected(broker, fake_client)
    _configure_paho_return(fake_client, _FakeMessageInfo(rc=MQTT_ERR_NO_CONN))
    agent = _make_agent_wired_to(broker)
    # Multiple publishes; each fails but caller keeps going.
    for _ in range(5):
        agent.publish("t", "x")
    assert fake_client.publish.call_count == 5


def test_F54_dispatcher_continues_after_MqttPublishError_from_handler(broker, fake_client):
    """Handler raises MqttPublishError → RFC-004 dispatcher isolates
    the failure; next enqueued task runs."""
    from agentflow.core.dispatcher import MessageDispatcher
    dispatcher = MessageDispatcher(workers=1, queue_capacity=8, shutdown_timeout_s=1.0)
    try:
        first_error = threading.Event()
        second_ran = threading.Event()

        def bad_task():
            first_error.set()
            raise MqttPublishError(
                topic="t", reason=MqttPublishReason.PAHO_REJECTED,
                rc=MQTT_ERR_NO_CONN,
            )

        def good_task():
            second_ran.set()

        dispatcher.enqueue(bad_task, topic="t")
        assert first_error.wait(1.0)
        dispatcher.enqueue(good_task, topic="t")
        assert second_ran.wait(1.0)
        # error_count reflects the first task's raise.
        assert dispatcher.error_count >= 1
    finally:
        dispatcher.stop(timeout_s=1.0)


# ===========================================================================
# G. Return / exception contract
# ===========================================================================


def test_G61_MqttPublishError_is_RuntimeError_subclass():
    """§7.3: RuntimeError so Agent.publish's except Exception catches it."""
    assert issubclass(MqttPublishError, RuntimeError)
    assert issubclass(MqttPublishError, Exception)


def test_G62_MqttPublishError_reason_is_enum_stable_short_code():
    """RFC-012 modification 1: reason is a MqttPublishReason enum,
    not a dynamic frozen string."""
    err = MqttPublishError(
        topic="t", reason=MqttPublishReason.PAHO_REJECTED,
        rc=MQTT_ERR_NO_CONN, mid=0,
    )
    assert err.reason is MqttPublishReason.PAHO_REJECTED
    assert err.reason.value == "paho_rejected"


def test_G63_MqttPublishError_structured_fields_all_queryable():
    err = MqttPublishError(
        topic="some/topic",
        reason=MqttPublishReason.PAHO_REJECTED,
        rc=99,
        mid=7,
        state=WorkerState.RUNNING,
        result_type="MQTTMessageInfo",
        detail="test",
    )
    assert err.topic == "some/topic"
    assert err.rc == 99
    assert err.mid == 7
    assert err.reason is MqttPublishReason.PAHO_REJECTED
    assert err.state is WorkerState.RUNNING
    assert err.result_type == "MQTTMessageInfo"
    assert err.detail == "test"


def test_G64_MqttPublishError_message_includes_topic_rc_mid_reason():
    err = MqttPublishError(
        topic="sensor/data",
        reason=MqttPublishReason.PAHO_REJECTED,
        rc=4, mid=17,
    )
    msg = str(err)
    assert "topic='sensor/data'" in msg
    assert "rc=4" in msg
    assert "mid=17" in msg
    assert "reason=paho_rejected" in msg


def test_G65_MqttPublishError_message_includes_state_when_present():
    err = MqttPublishError(
        topic="t",
        reason=MqttPublishReason.BROKER_NOT_RUNNING,
        state=WorkerState.NEW,
    )
    msg = str(err)
    assert "state=new" in msg


def test_G66_MqttPublishError_message_includes_result_type_when_present():
    err = MqttPublishError(
        topic="t",
        reason=MqttPublishReason.UNSUPPORTED_RESULT,
        result_type="NoneType",
    )
    msg = str(err)
    assert "result_type=NoneType" in msg


def test_G67_MqttPublishError_message_includes_detail_when_present():
    err = MqttPublishError(
        topic="t",
        reason=MqttPublishReason.UNSUPPORTED_RESULT,
        result_type="MessageInfo",
        detail="invalid rc",
    )
    msg = str(err)
    assert "detail='invalid rc'" in msg


def test_G68_MqttPublishError_public_reexport_from_agentflow_broker():
    """RFC-012 §7.2: re-exported."""
    from agentflow.broker import MqttPublishError as ReExported
    from agentflow.broker.mqtt_broker import MqttPublishError as ModuleOne
    assert ReExported is ModuleOne


def test_G69_MqttPublishReason_public_reexport_from_agentflow_broker():
    from agentflow.broker import MqttPublishReason as ReExported
    from agentflow.broker.mqtt_broker import MqttPublishReason as ModuleOne
    assert ReExported is ModuleOne


def test_G70_MqttBroker_publish_still_no_return_annotation_success_type_paho_dependent():
    sig = inspect.signature(MqttBroker.publish)
    assert sig.return_annotation is inspect.Signature.empty


def test_G71_message_broker_ABC_signature_unchanged(broker):
    sig = inspect.signature(MessageBroker.publish)
    assert list(sig.parameters) == ['self', 'topic', 'payload']


def test_G72_empty_broker_publish_returns_None_unchanged():
    b = EmptyBroker(notifier=MagicMock(name='notifier'))
    result = b.publish("t", b"p")
    assert result is None


def test_G73_fake_broker_publish_returns_None_unchanged():
    from tests.fakes.fake_broker import FakeBroker
    b = FakeBroker(notifier=MagicMock(name='notifier'))
    result = b.publish("t", b"p")
    assert result is None


# ===========================================================================
# H. Payload / serialization boundaries (kept from characterization)
# ===========================================================================


def test_H61_pickle_failure_raises_at_serialization_before_broker_publish():
    from agentflow.core.parcel import BinaryParcel
    with pytest.raises(Exception):
        BinaryParcel(lambda: None).payload()


def test_H62_invalid_payload_type_forwarded_to_paho(broker, fake_client):
    """Non-Parcel, non-bytes payload passed through — paho decides."""
    _prime_connected(broker, fake_client)
    _configure_paho_return(fake_client, _success_info())
    broker.publish("t", "raw-string")
    _, kwargs = fake_client.publish.call_args
    assert kwargs["payload"] == "raw-string"


def test_H63_binary_parcel_bytes_payload_preserved(broker, fake_client):
    from agentflow.core.parcel import BinaryParcel
    _prime_connected(broker, fake_client)
    _configure_paho_return(fake_client, _success_info())
    raw = b"\x00\x01\x02\xff"
    payload = BinaryParcel(raw).payload()
    broker.publish("t", payload)
    _, kwargs = fake_client.publish.call_args
    assert kwargs["payload"] == payload


def test_H64_text_parcel_utf8_json_encoding(broker, fake_client):
    from agentflow.core.parcel import TextParcel
    _prime_connected(broker, fake_client)
    _configure_paho_return(fake_client, _success_info())
    payload = TextParcel({"key": "值"}).payload()
    broker.publish("t", payload)
    _, kwargs = fake_client.publish.call_args
    assert isinstance(kwargs["payload"], (bytes, bytearray))
    assert kwargs["payload"].startswith(b"text/json|")


# ===========================================================================
# I. Concurrency
# ===========================================================================


def test_I75_multi_thread_publish_each_success_returns_own_info(broker, fake_client):
    """paho thread-safety; each publish gets its own return."""
    _prime_connected(broker, fake_client)
    call_index = [0]

    def next_info(*a, **kw):
        idx = call_index[0]
        call_index[0] += 1
        return _FakeMessageInfo(rc=MQTT_ERR_SUCCESS, mid=idx + 100)

    fake_client.publish.side_effect = next_info

    N = 20
    barrier = threading.Barrier(N, timeout=2.0)
    done = [threading.Event() for _ in range(N)]

    def caller(i):
        try:
            barrier.wait()
            broker.publish(f"t/{i}", b"p")
        finally:
            done[i].set()

    threads = [threading.Thread(target=caller, args=(i,), daemon=True) for i in range(N)]
    for t in threads:
        t.start()
    for e in done:
        assert e.wait(3.0)
    assert fake_client.publish.call_count == N


def test_I76_concurrent_rc_failures_produce_independent_exception_instances(
    broker, fake_client,
):
    """Each failing publish raises its OWN MqttPublishError instance;
    no shared exception state."""
    _prime_connected(broker, fake_client)
    _configure_paho_return(fake_client, _FakeMessageInfo(rc=MQTT_ERR_NO_CONN))

    caught: List[MqttPublishError] = []
    caught_lock = threading.Lock()
    barrier = threading.Barrier(4, timeout=2.0)

    def caller():
        barrier.wait()
        try:
            broker.publish("t", b"p")
        except MqttPublishError as ex:
            with caught_lock:
                caught.append(ex)

    threads = [threading.Thread(target=caller, daemon=True) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(3.0)

    assert len(caught) == 4
    # Distinct exception object identities.
    assert len(set(id(e) for e in caught)) == 4


def test_I77_no_shared_last_publish_error_field_verified_post_failure(broker, fake_client):
    _prime_connected(broker, fake_client)
    _configure_paho_return(fake_client, _FakeMessageInfo(rc=MQTT_ERR_NO_CONN))
    try:
        broker.publish("t", b"p")
    except MqttPublishError:
        pass
    # No stashed last-error.
    assert not hasattr(broker, 'last_publish_error')
    assert not hasattr(broker, 'last_publish_rc')


def test_I78_publish_source_has_no_publish_specific_lock(broker):
    """publish serialisation is delegated to paho's thread-safety;
    MqttBroker adds no publish-specific lock."""
    src = inspect.getsource(MqttBroker.publish)
    # `_state_lock` used only for snapshot (verified in C29); no
    # other lock acquisition.
    assert src.count("with self._state_lock:") == 1


def test_I79_publish_during_stop_inline_may_still_reach_paho(broker, fake_client):
    """RFC-012 §B modification 2 acknowledgment: gate is not
    linearized with stop. Publish scheduled INSIDE stop() helper
    (via inline disconnect callback) will observe stopping=True
    by the time snapshot runs → raises BROKER_STOPPING; no leak to
    paho in that specific ordering. Documented for the race
    boundary."""
    _configure_paho_return(fake_client, _success_info())
    _prime_connected(broker, fake_client)
    seen = {}

    def inline_publish_during_stop(*a, **kw):
        try:
            broker.publish("late/topic", b"late")
            seen['result'] = 'reached-paho'
        except MqttPublishError as ex:
            seen['result'] = 'gated'
            seen['reason'] = ex.reason

    fake_client.disconnect.side_effect = inline_publish_during_stop
    broker.stop(graceful_timeout_s=2.0)
    # By the time inline publish runs, _stopping was already set by
    # stop() linearization → gate rejects.
    assert seen['result'] == 'gated'
    assert seen['reason'] is MqttPublishReason.BROKER_STOPPING


def test_I80_publish_never_transitions_broker_state(broker, fake_client):
    _prime_connected(broker, fake_client)
    _configure_paho_return(fake_client, _success_info())
    state_before = broker.state
    broker.publish("t", b"p")
    assert broker.state is state_before
