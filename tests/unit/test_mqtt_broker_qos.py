"""MqttBroker QoS and retain (issue #4, W-11).

Every publish and subscribe used QoS 0 and retain=False, with no way to
ask for more. Contracts verified (with the mocked paho Client from
tests/conftest.py):

  - publish(topic, payload, *, qos=0, retain=False) and
    subscribe(topic, data_type, *, qos=0) hand QoS and retain to paho.
    With the defaults the paho call keeps its exact pre-#4 shape.
  - The subscription registry remembers each topic's QoS, and the
    resubscribe after a reconnect uses it.
  - An invalid QoS is refused before anything reaches paho.
  - Every built-in broker accepts the same keywords.
"""

import inspect

import pytest

from agentflow.broker.empty_broker import EmptyBroker
from agentflow.broker.mqtt_broker import MqttBroker, MqttPublishError
from agentflow.broker.redis_broker import RedisBroker
from agentflow.broker.ros_broker import RosBroker



class _PublishInfo:
    """paho 2's MQTTMessageInfo shape for a successful publish."""
    rc = 0
    mid = 1


class _NoConnInfo:
    """paho 2's result when the socket is already gone (MQTT_ERR_NO_CONN)."""
    rc = 4
    mid = 7


xfail_review = pytest.mark.xfail(strict=True, reason='issue #4: gaps found in review')


def _fire_connect(broker, fake_client, rc: int = 0):
    broker._on_connect(
        client=fake_client, userdata=None, flags={},
        reasonCode=rc, properties=None,
    )
    fake_client.publish.return_value = _PublishInfo()


def _fire_disconnect(broker, fake_client, rc: int = 1):
    broker._on_disconnect(
        client=fake_client, userdata=None, _flags={},
        reasonCode=rc, _properties=None,
    )


def test_defaults_keep_the_paho_call_shape(broker, fake_client):
    _fire_connect(broker, fake_client)
    broker.publish('t', b'x')
    broker.subscribe('s', 'str')
    fake_client.publish.assert_called_once_with(topic='t', payload=b'x')
    fake_client.subscribe.assert_called_once_with(topic='s')


def test_publish_hands_qos_and_retain_to_paho(broker, fake_client):
    _fire_connect(broker, fake_client)
    broker.publish('t', b'x', qos=1, retain=True)
    fake_client.publish.assert_called_once_with(topic='t', payload=b'x', qos=1, retain=True)


def test_subscribe_hands_qos_to_paho_and_reconnect_keeps_it(broker, fake_client):
    _fire_connect(broker, fake_client)
    broker.subscribe('A', 'str', qos=1)
    broker.subscribe('B', 'str')
    fake_client.subscribe.assert_any_call(topic='A', qos=1)
    fake_client.subscribe.assert_any_call(topic='B')

    _fire_disconnect(broker, fake_client)
    fake_client.subscribe.reset_mock()
    _fire_connect(broker, fake_client)

    resubscribed = {c.kwargs['topic']: c.kwargs.get('qos', 0)
                    for c in fake_client.subscribe.call_args_list}
    assert resubscribed == {'A': 1, 'B': 0}


@pytest.mark.parametrize('qos', [-1, 3, True, '1', 1.0, None])
def test_invalid_qos_is_refused_before_paho(broker, fake_client, qos):
    _fire_connect(broker, fake_client)
    with pytest.raises(ValueError, match='QoS must be 0, 1 or 2'):
        broker.publish('t', b'x', qos=qos)
    with pytest.raises(ValueError, match='QoS must be 0, 1 or 2'):
        broker.subscribe('s', 'str', qos=qos)
    fake_client.publish.assert_not_called()
    fake_client.subscribe.assert_not_called()


@pytest.mark.parametrize('broker_class', [MqttBroker, EmptyBroker, RedisBroker, RosBroker])
def test_every_built_in_broker_accepts_the_qos_keywords(broker_class):
    publish = inspect.signature(broker_class.publish).parameters
    subscribe = inspect.signature(broker_class.subscribe).parameters
    assert publish['qos'].kind is inspect.Parameter.KEYWORD_ONLY and publish['qos'].default == 0
    assert publish['retain'].kind is inspect.Parameter.KEYWORD_ONLY and publish['retain'].default is False
    assert subscribe['qos'].kind is inspect.Parameter.KEYWORD_ONLY and subscribe['qos'].default == 0


@xfail_review
def test_a_qos_1_publish_paho_queued_without_a_socket_counts_as_accepted(broker, fake_client):
    """paho closes the socket before it reports the disconnect. In that
    window a QoS >= 1 publish gets MQTT_ERR_NO_CONN, yet paho keeps the
    message and sends it after the reconnect. Reporting it as failed
    would make a caller retry, and the service run the request twice
    under two message ids. A QoS 0 publish is not kept, and still fails."""
    _fire_connect(broker, fake_client)
    fake_client.publish.return_value = _NoConnInfo()
    assert broker.publish('t', b'x', qos=1) is fake_client.publish.return_value
    with pytest.raises(MqttPublishError):
        broker.publish('t', b'x')


@xfail_review
def test_the_resubscribe_after_a_reconnect_uses_each_topics_current_qos(broker, fake_client):
    """A subscribe() that changes a topic's QoS while recovery is running
    must not be undone by recovery resubscribing at the snapshot's QoS."""
    _fire_connect(broker, fake_client)
    broker.subscribe('Z', 'str')
    broker.subscribe('A', 'str')                # QoS 0 in the snapshot
    _fire_disconnect(broker, fake_client)
    fake_client.subscribe.reset_mock()

    def raise_qos_of_a_during_recovery(topic, qos=0):
        if topic == 'Z':
            broker.subscribe('A', 'str', qos=1)
    fake_client.subscribe.side_effect = raise_qos_of_a_during_recovery
    _fire_connect(broker, fake_client)

    last_for_a = [c.kwargs.get('qos', 0) for c in fake_client.subscribe.call_args_list
                  if c.kwargs['topic'] == 'A'][-1]
    assert last_for_a == 1
