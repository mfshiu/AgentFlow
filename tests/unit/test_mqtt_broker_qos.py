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
from agentflow.broker.mqtt_broker import MqttBroker
from agentflow.broker.redis_broker import RedisBroker
from agentflow.broker.ros_broker import RosBroker



class _PublishInfo:
    """paho 2's MQTTMessageInfo shape for a successful publish."""
    rc = 0
    mid = 1


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
