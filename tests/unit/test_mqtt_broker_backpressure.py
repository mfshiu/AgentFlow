"""Outbound backpressure for MqttBroker (issue #35, W-70).

paho does not apply max_queued_messages to QoS 0: such messages go
straight into its _out_packet deque. MqttBroker set no limit and
exposed no pending count, so over a slow or stalled link every publish
"succeeded" while memory grew without bound (CLAUDE.md invariant 5).

Contracts verified (with the mocked paho Client from tests/conftest.py):
  - Every publish is tracked by mid, with its size and QoS, until paho
    reports it sent (on_publish: QoS 0 written, QoS 1 acknowledged),
    including a report that arrives before publish() returns.
  - A disconnect forgets unsent QoS 0 messages (paho drops them) but
    keeps QoS 1 ones (paho resends them).
  - recovery_metrics() exposes pending_publish_count and
    pending_publish_bytes.
  - A publish that would take the pending bytes past max_pending_bytes
    (default 64 MiB) raises MqttBackpressureError, a MqttPublishError
    with reason BACKPRESSURE and a transport-neutral
    PublishBackpressureError, before paho is called.
  - A malformed max_pending_bytes leaves the broker NEW.
  - Agent.publish reports backpressure as False, counted and logged
    at a rate-limited level, without a traceback.
"""

import itertools
import logging

import pytest

from agentflow.broker import message_broker, mqtt_broker
from agentflow.broker.mqtt_broker import MqttPublishError
from agentflow.core.agent import Agent
from agentflow.core.agent_worker import WorkerState
from tests.fakes.fake_broker import FakeBroker, FakeWorker

xfail_35 = pytest.mark.xfail(strict=True, reason='issue #35: no outbound limit')

MiB = 1024 * 1024


class _Info:
    def __init__(self, mid):
        self.rc = 0
        self.mid = mid


@pytest.fixture
def mids(fake_client):
    """fake_client.publish returns paho-like results with mids 1, 2, ..."""
    counter = itertools.count(1)
    fake_client.publish.side_effect = lambda *args, **kwargs: _Info(next(counter))
    return counter


def _running(broker, fake_client, options=None):
    assert broker.start(options or {}) is True
    broker._on_connect(client=fake_client, userdata=None, flags={},
                       reasonCode=0, properties=None)
    return broker


def _sent(broker, fake_client, mid):
    broker._on_publish(fake_client, None, mid, 0, None)


def _pending(broker):
    metrics = broker.recovery_metrics()
    return metrics['pending_publish_count'], metrics['pending_publish_bytes']


@xfail_35
def test_publishes_are_pending_until_paho_reports_them_sent(broker, fake_client, mids):
    _running(broker, fake_client)
    for _ in range(3):
        broker.publish('t', b'x' * 1000)
    assert _pending(broker) == (3, 3000)
    _sent(broker, fake_client, 1)
    assert _pending(broker) == (2, 2000)


@xfail_35
def test_a_publish_beyond_max_pending_bytes_is_refused_before_paho(broker, fake_client, mids):
    _running(broker, fake_client, {'max_pending_bytes': 2500})
    broker.publish('t', b'x' * 1000)
    broker.publish('t', b'x' * 1000)
    with pytest.raises(MqttPublishError) as refused:
        broker.publish('t', b'x' * 1000)
    assert refused.value.reason is mqtt_broker.MqttPublishReason.BACKPRESSURE
    assert isinstance(refused.value, message_broker.PublishBackpressureError)
    assert fake_client.publish.call_count == 2
    _sent(broker, fake_client, 1)
    broker.publish('t', b'x' * 1000)             # room again


@xfail_35
def test_the_default_limit_is_64_mib(broker, fake_client, mids):
    _running(broker, fake_client)
    broker.publish('t', b'x' * (64 * MiB))      # exactly the limit
    with pytest.raises(message_broker.PublishBackpressureError):
        broker.publish('t', b'x')


@xfail_35
def test_a_report_that_arrives_before_publish_returns_is_not_counted(broker, fake_client):
    counter = itertools.count(1)

    def publish_and_report(*args, **kwargs):
        mid = next(counter)
        _sent(broker, fake_client, mid)          # paho wrote it at once
        return _Info(mid)
    fake_client.publish.side_effect = publish_and_report
    _running(broker, fake_client)
    broker.publish('t', b'x' * 1000)
    assert _pending(broker) == (0, 0)


@xfail_35
def test_a_disconnect_forgets_unsent_qos0_but_keeps_qos1(broker, fake_client, mids):
    _running(broker, fake_client)
    broker.publish('t', b'x' * 1000)             # mid 1, QoS 0
    broker.publish('t', b'x' * 500, qos=1)       # mid 2, QoS 1
    broker._on_disconnect(client=fake_client, userdata=None, _flags={},
                          reasonCode=1, _properties=None)
    assert _pending(broker) == (1, 500)
    _sent(broker, fake_client, 2)                # acknowledged after the reconnect
    assert _pending(broker) == (0, 0)


@xfail_35
@pytest.mark.parametrize('value, error', [(0, ValueError), (-1, ValueError), (True, TypeError), ('1', TypeError)])
def test_a_malformed_max_pending_bytes_leaves_the_broker_new(broker, value, error):
    with pytest.raises(error, match='max_pending_bytes'):
        broker.start({'max_pending_bytes': value})
    assert broker._state is WorkerState.NEW


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


@xfail_35
def test_agent_publish_reports_backpressure_quietly(records):
    agent = Agent('sender', {'dispatch': {'shutdown_timeout_s': 1.0}})
    broker = FakeBroker(notifier=agent)
    agent._broker = broker
    agent._agent_worker = FakeWorker()
    broker.publish_exception = message_broker.PublishBackpressureError('pending bytes over the limit')
    try:
        assert [agent.publish('t', 'x') for _ in range(4)] == [False] * 4
        assert agent.publish_backpressure_count == 4
        assert [r for r in records if r.exc_info] == []
        assert len([r for r in records if r.levelno == logging.WARNING]) == 3   # 1st, 2nd, 4th
    finally:
        agent.terminate()
