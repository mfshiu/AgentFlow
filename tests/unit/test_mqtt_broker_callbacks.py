"""Verify MqttBroker._on_connect / _on_message / _on_disconnect callbacks
and their notifier delegation, using a mocked paho Client and notifier.

Target module: agentflow.broker.mqtt_broker (real code).
"""

import types

import pytest


# --------------------------------------------------------------------------
# _on_connect
# --------------------------------------------------------------------------

def test_on_connect_notifies_notifier_when_reason_code_is_zero(
    broker, fake_client, notifier
):
    broker._on_connect(
        client=fake_client,
        userdata=None,
        flags={},
        reasonCode=0,
        properties=None,
    )
    notifier._on_connect.assert_called_once_with()
    assert broker._connect_ok is True
    assert broker._connect_err is None
    assert broker._connected_evt.is_set()


def test_on_connect_does_not_notify_when_reason_code_is_nonzero(
    broker, fake_client, notifier
):
    broker._on_connect(
        client=fake_client,
        userdata=None,
        flags={},
        reasonCode=5,
        properties=None,
    )
    notifier._on_connect.assert_not_called()
    assert broker._connect_ok is False
    assert broker._connect_err is not None
    assert broker._connected_evt.is_set()


def test_on_connect_sets_event_even_if_notifier_raises(
    broker, fake_client, notifier
):
    notifier._on_connect.side_effect = RuntimeError("notifier explode")
    with pytest.raises(RuntimeError):
        broker._on_connect(
            client=fake_client,
            userdata=None,
            flags={},
            reasonCode=0,
            properties=None,
        )
    # The `finally:` clause at mqtt_broker.py:42-43 must still fire.
    assert broker._connected_evt.is_set()
    # _connect_ok is set BEFORE the notifier call and must remain True.
    assert broker._connect_ok is True


# --------------------------------------------------------------------------
# _on_message
# --------------------------------------------------------------------------

def test_on_message_forwards_topic_and_payload_to_notifier(
    broker, fake_client, notifier
):
    msg = types.SimpleNamespace(topic="the/topic", payload=b"the-payload")
    broker._on_message(client=fake_client, db=None, message=msg)
    notifier._on_message.assert_called_once_with("the/topic", b"the-payload")


def test_on_message_isolates_notifier_exception(broker, fake_client, notifier):
    """paho loop-thread isolation contract: a raising notifier must not
    propagate out of the broker's _on_message wrapper."""
    notifier._on_message.side_effect = RuntimeError("notifier explode")
    msg = types.SimpleNamespace(topic="err/topic", payload=b"boom")
    # Must not raise.
    broker._on_message(client=fake_client, db=None, message=msg)
    notifier._on_message.assert_called_once_with("err/topic", b"boom")


def test_on_message_forwards_each_call_independently(broker, fake_client, notifier):
    msg1 = types.SimpleNamespace(topic="t1", payload=b"p1")
    msg2 = types.SimpleNamespace(topic="t2", payload=b"p2")
    broker._on_message(client=fake_client, db=None, message=msg1)
    broker._on_message(client=fake_client, db=None, message=msg2)
    assert notifier._on_message.call_args_list == [
        (("t1", b"p1"), {}),
        (("t2", b"p2"), {}),
    ]


# --------------------------------------------------------------------------
# _on_disconnect
# --------------------------------------------------------------------------

def test_on_disconnect_does_not_raise(broker, fake_client):
    # mqtt_broker.py:52-53 only logs; this pins that contract.
    broker._on_disconnect(
        client=fake_client,
        userdata=None,
        _flags={},
        reasonCode=0,
        _properties=None,
    )


def test_on_disconnect_does_not_notify_notifier(broker, fake_client, notifier):
    broker._on_disconnect(
        client=fake_client,
        userdata=None,
        _flags={},
        reasonCode=0,
        _properties=None,
    )
    notifier._on_connect.assert_not_called()
    notifier._on_message.assert_not_called()
