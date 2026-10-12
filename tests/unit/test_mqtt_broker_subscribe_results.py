"""Subscription results are checked (issue #53, W-45).

MqttBroker never looked at the outcome of a subscription: it bound no
on_subscribe, so a broker refusing a topic with SUBACK 0x80 (an ACL,
a resource limit) went unnoticed; a resubscribe counted as a success
whenever paho did not raise, whatever its rc; and an invalid filter was
recorded in the registry before paho rejected it. The agent believed it
was subscribed, received nothing, and recovery_metrics() reported it
healthy.

Contracts verified (with the mocked paho Client from tests/conftest.py):
  - An invalid filter raises ValueError before the registry changes,
    and paho is not called; valid wildcards are accepted.
  - A SUBACK failure (reason code >= 0x80) is logged at WARNING and the
    topic is counted in failed_subscriptions, not active_subscriptions.
    It stays in the registry, so the next CONNACK retries it. A granted
    SUBACK keeps it active, as does a later successful one.
  - A SUBACK that arrives before subscribe() returns is handled.
  - A resubscribe whose paho rc is not success counts as a failure.
  - A paho error on subscribe() keeps the topic desired but not active.
  - unsubscribe() clears a failure; on_subscribe never raises into paho.
"""

import logging

import pytest
from paho.mqtt.packettypes import PacketTypes
from paho.mqtt.reasoncodes import ReasonCode

xfail_53 = pytest.mark.xfail(strict=True, reason='issue #53: subscription results not checked')

MQTT_ERR_NO_CONN = 4


def _fire_connect(broker, fake_client):
    broker._on_connect(client=fake_client, userdata=None, flags={},
                       reasonCode=0, properties=None)


def _fire_disconnect(broker, fake_client):
    broker._on_disconnect(client=fake_client, userdata=None, _flags={},
                          reasonCode=1, _properties=None)


def _suback(broker, fake_client, mid, *codes):
    broker._on_subscribe(fake_client, None, mid,
                         [ReasonCode(PacketTypes.SUBACK, identifier=c) for c in codes], None)


def _counts(broker):
    metrics = broker.recovery_metrics()
    return metrics['active_subscriptions'], metrics['failed_subscriptions']


@pytest.fixture
def connected(broker, fake_client):
    fake_client.subscribe.return_value = (0, 5)
    _fire_connect(broker, fake_client)
    return broker


@pytest.fixture
def warnings_logged(caplog):
    caplog.set_level(logging.WARNING)
    return lambda: [r for r in caplog.records if r.levelno == logging.WARNING]


@xfail_53
@pytest.mark.parametrize('topic', ['robot+1/cmd', 'a/#/b', 'a/b#', '#/x', ''])
def test_an_invalid_filter_is_refused_before_the_registry(connected, fake_client, topic):
    with pytest.raises(ValueError):
        connected.subscribe(topic, 'str')
    assert connected.recovery_metrics()['active_subscriptions'] == 0
    fake_client.subscribe.assert_not_called()


@pytest.mark.parametrize('topic', ['a/+/b', '+', '#', 'a/#', '$share/g/a/+', '/'])
def test_valid_filters_are_accepted(connected, fake_client, topic):
    connected.subscribe(topic, 'str')
    assert fake_client.subscribe.call_count == 1


@xfail_53
def test_a_rejected_suback_is_logged_and_not_counted_active(connected, fake_client, warnings_logged):
    connected.subscribe('secure/denied', 'str')
    _suback(connected, fake_client, 5, 0x80)
    assert _counts(connected) == (0, 1)
    assert any('secure/denied' in r.getMessage() for r in warnings_logged())
    assert 'secure/denied' in connected._registry          # retried on the next CONNACK


@xfail_53
def test_a_granted_suback_keeps_the_topic_active(connected, fake_client, warnings_logged):
    connected.subscribe('ok/topic', 'str', qos=1)
    _suback(connected, fake_client, 5, 1)
    assert _counts(connected) == (1, 0)
    assert warnings_logged() == []


@xfail_53
def test_a_later_successful_resubscribe_clears_the_failure(connected, fake_client):
    connected.subscribe('secure/denied', 'str')
    _suback(connected, fake_client, 5, 0x80)
    _fire_disconnect(connected, fake_client)
    fake_client.subscribe.return_value = (0, 6)
    _fire_connect(connected, fake_client)                  # the replay
    _suback(connected, fake_client, 6, 0)
    assert _counts(connected) == (1, 0)


@xfail_53
def test_a_suback_that_arrives_before_subscribe_returns_is_handled(connected, fake_client):
    def subscribe_and_refuse(*args, **kwargs):
        _suback(connected, fake_client, 9, 0x80)          # the network thread was faster
        return (0, 9)
    fake_client.subscribe.side_effect = subscribe_and_refuse
    connected.subscribe('secure/denied', 'str')
    assert _counts(connected) == (0, 1)


@xfail_53
def test_a_resubscribe_with_an_error_rc_counts_as_a_failure(connected, fake_client, warnings_logged):
    connected.subscribe('T', 'str')
    _fire_disconnect(connected, fake_client)
    fake_client.subscribe.return_value = (MQTT_ERR_NO_CONN, None)
    _fire_connect(connected, fake_client)
    metrics = connected.recovery_metrics()
    assert (metrics['resubscribe_success_count'], metrics['resubscribe_error_count']) == (0, 1)
    assert _counts(connected) == (0, 1)
    assert any("'T'" in r.getMessage() for r in warnings_logged())


@xfail_53
def test_a_paho_error_on_subscribe_keeps_the_topic_desired_but_not_active(connected, fake_client):
    fake_client.subscribe.side_effect = RuntimeError('paho failure')
    with pytest.raises(RuntimeError, match='paho failure'):
        connected.subscribe('T', 'str')
    assert 'T' in connected._registry                      # replayed on the next CONNACK
    assert _counts(connected) == (0, 1)


@xfail_53
def test_an_unsubscribe_clears_the_failure(connected, fake_client):
    connected.subscribe('secure/denied', 'str')
    _suback(connected, fake_client, 5, 0x80)
    connected.unsubscribe('secure/denied')
    assert _counts(connected) == (0, 0)


@xfail_53
@pytest.mark.parametrize('codes', [None, [object()], ['x']])
def test_on_subscribe_never_raises_into_paho(connected, fake_client, codes):
    connected.subscribe('T', 'str')
    connected._on_subscribe(fake_client, None, 5, codes, None)
