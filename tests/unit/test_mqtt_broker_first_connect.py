"""Subscriptions made before the first CONNACK (issue #54, W-46).

MqttBroker.subscribe only records a topic in the registry while it is
not connected, relying on the next _on_connect to send it. But
_on_connect replayed the registry only on a REconnect, so a topic
subscribed while the broker was STARTING (start() with wait=False, or
another thread while start() waits for the CONNACK) never reached the
broker, while recovery_metrics() counted it as active.

Contracts verified (with the mocked paho Client from tests/conftest.py):
  - The first CONNACK sends every topic subscribed before it, at its
    QoS, and counts the replay as a recovery run.
  - A topic unsubscribed before the first CONNACK is not sent.
  - A first CONNACK with an empty registry runs no recovery.
"""

import pytest


def _fire_connect(broker, fake_client):
    broker._on_connect(client=fake_client, userdata=None, flags={},
                       reasonCode=0, properties=None)


def _subscribed_topics(fake_client):
    return [c.kwargs['topic'] for c in fake_client.subscribe.call_args_list]


def test_a_subscribe_before_the_first_connack_is_sent_on_it(broker, fake_client):
    assert broker.start({}) is True                 # wait=False: still STARTING
    broker.subscribe('early/topic', 'str')
    broker.subscribe('early/control', 'str', qos=1)
    assert _subscribed_topics(fake_client) == []    # not connected yet

    _fire_connect(broker, fake_client)
    assert sorted(_subscribed_topics(fake_client)) == ['early/control', 'early/topic']
    [control] = [c for c in fake_client.subscribe.call_args_list
                 if c.kwargs['topic'] == 'early/control']
    assert control.kwargs['qos'] == 1
    metrics = broker.recovery_metrics()
    assert (metrics['recovery_run_count'], metrics['resubscribe_success_count']) == (1, 2)


def test_a_topic_unsubscribed_before_the_first_connack_is_not_sent(broker, fake_client):
    assert broker.start({}) is True
    broker.subscribe('early/topic', 'str')
    broker.unsubscribe('early/topic')
    _fire_connect(broker, fake_client)
    assert _subscribed_topics(fake_client) == []


def test_a_first_connack_with_no_subscriptions_runs_no_recovery(broker, fake_client):
    assert broker.start({}) is True
    _fire_connect(broker, fake_client)
    assert broker.recovery_metrics()['recovery_run_count'] == 0
