"""Verify MqttBroker.stop / publish / subscribe with a mocked paho Client.

Target module: agentflow.broker.mqtt_broker (real code).
"""


def _prime_connected(broker, fake_client):
    """Post-RFC-005: subscribe/unsubscribe only forward to the paho
    client when the broker is currently connected. Fire _on_connect
    once so the broker enters the connected state, then reset the
    fake client's mock counters so tests can assert on the calls that
    matter to them."""
    broker._on_connect(
        client=fake_client, userdata=None, flags={},
        reasonCode=0, properties=None,
    )
    fake_client.subscribe.reset_mock()
    fake_client.unsubscribe.reset_mock()


# --------------------------------------------------------------------------
# stop()
# --------------------------------------------------------------------------

def test_stop_calls_disconnect_and_loop_stop(broker, fake_client):
    broker.stop()
    fake_client.disconnect.assert_called_once_with()
    fake_client.loop_stop.assert_called_once_with()


def test_stop_calls_disconnect_before_loop_stop(broker, fake_client):
    call_order = []
    fake_client.disconnect.side_effect = lambda *a, **kw: call_order.append("disconnect")
    fake_client.loop_stop.side_effect = lambda *a, **kw: call_order.append("loop_stop")
    broker.stop()
    assert call_order == ["disconnect", "loop_stop"]


def test_stop_can_be_called_without_prior_start(broker, fake_client):
    # stop() reads only self._client, which exists after __init__.
    broker.stop()  # must not raise
    fake_client.disconnect.assert_called_once_with()


# --------------------------------------------------------------------------
# publish()
# --------------------------------------------------------------------------

def test_publish_delegates_topic_and_payload_by_keyword(broker, fake_client):
    broker.publish("some/topic", b"payload-bytes")
    fake_client.publish.assert_called_once_with(
        topic="some/topic", payload=b"payload-bytes"
    )


def test_publish_returns_underlying_client_result(broker, fake_client):
    sentinel = object()
    fake_client.publish.return_value = sentinel
    result = broker.publish("t", b"p")
    assert result is sentinel


def test_publish_forwards_non_bytes_payload_unchanged(broker, fake_client):
    # MqttBroker.publish does not serialise; that is the caller's job.
    payload_obj = {"a": 1}
    broker.publish("t", payload_obj)
    fake_client.publish.assert_called_once_with(topic="t", payload=payload_obj)


# --------------------------------------------------------------------------
# subscribe()
# --------------------------------------------------------------------------

def test_subscribe_delegates_topic_by_keyword(broker, fake_client):
    _prime_connected(broker, fake_client)
    broker.subscribe("some/topic", data_type="str")
    fake_client.subscribe.assert_called_once_with(topic="some/topic")


def test_subscribe_returns_underlying_client_result(broker, fake_client):
    _prime_connected(broker, fake_client)
    sentinel = object()
    fake_client.subscribe.return_value = sentinel
    assert broker.subscribe("t", "str") is sentinel


def test_subscribe_does_not_forward_data_type_to_paho(broker, fake_client):
    # MqttBroker.subscribe drops data_type. Pin the current contract.
    _prime_connected(broker, fake_client)
    broker.subscribe("t", data_type="bytes")
    args, kwargs = fake_client.subscribe.call_args
    assert "data_type" not in kwargs
    assert args == ()


# --------------------------------------------------------------------------
# unsubscribe() — added for R-02 (RFC-001)
# --------------------------------------------------------------------------

def test_unsubscribe_delegates_topic_to_client(broker, fake_client):
    _prime_connected(broker, fake_client)
    # Register the topic first so unsubscribe has something to remove.
    broker.subscribe("some/topic", "str")
    fake_client.unsubscribe.reset_mock()
    broker.unsubscribe("some/topic")
    fake_client.unsubscribe.assert_called_once_with("some/topic")


def test_unsubscribe_returns_underlying_client_result(broker, fake_client):
    _prime_connected(broker, fake_client)
    broker.subscribe("t", "str")
    sentinel = object()
    fake_client.unsubscribe.return_value = sentinel
    assert broker.unsubscribe("t") is sentinel
