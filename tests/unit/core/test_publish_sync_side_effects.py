"""publish_sync side effects (issue #21, W-65).

publish_sync wrote its reply topic into the caller's Parcel, so the
same Parcel published later still carried a stale topic_return (a
receiver auto-replied to a topic nobody listened on), and a retry reused
the topic. It asked the worker for an event on every call, which could
create a ProcessWorker and set the global multiprocessing start method.
timeout=None waited forever without a word, and a reply arriving after
the call had given up reached the user's on_message.

The retry itself no longer returns the earlier call's late reply since
RFC-014 (#36): replies are matched by correlation id.

Contracts verified (with FakeBroker):
  - The caller's Parcel is not modified; a retry with it gets a new
    reply topic. A topic_wait, or a topic_return already on the Parcel,
    is still used.
  - publish_sync does not touch the worker.
  - timeout=None is deprecated (DeprecationWarning; it still waits).
  - A late reply on a reply topic publish_sync generated is dropped and
    counted (late_reply_count), not handed to on_message; one on a
    caller's own topic_wait still reaches on_message (RFC-001 test 6).
"""

import warnings

import pytest

from agentflow.core.agent import Agent
from agentflow.core.parcel import Parcel, TextParcel
from tests.fakes.fake_broker import FakeBroker, FakeWorker
from tests.fakes.fake_bus import FakeBus


class Recorder(Agent):
    def __init__(self):
        super().__init__('requester', {'dispatch': {'shutdown_timeout_s': 1.0}})
        self.unhandled = []

    def on_message(self, topic, pcl):
        self.unhandled.append((topic, pcl.content))


@pytest.fixture
def agent_and_broker():
    agent = Recorder()
    broker = FakeBroker(notifier=agent)
    agent._broker = broker
    agent._agent_worker = FakeWorker()
    yield agent, broker
    agent.terminate()


def _requests(broker, topic='svc'):
    return [Parcel.from_payload(p) for t, p in broker.publish_calls if t == topic]


def test_the_callers_parcel_is_not_modified(agent_and_broker):
    agent, broker = agent_and_broker
    broker.auto_respond_with('pong')
    parcel = Parcel.from_content('q')
    assert agent.publish_sync('svc', parcel, timeout=2).content == 'pong'
    assert parcel.topic_return is None
    agent.publish('notify', parcel)
    assert _requests(broker, 'notify')[0].topic_return is None


def test_a_retry_with_the_same_parcel_gets_a_new_reply_topic(agent_and_broker):
    agent, broker = agent_and_broker
    parcel = Parcel.from_content('q')
    for _ in range(2):
        with pytest.raises(TimeoutError):
            agent.publish_sync('svc', parcel, timeout=0.05)
    first, second = _requests(broker)
    assert first.topic_return != second.topic_return


def test_a_topic_wait_or_a_preset_topic_return_is_still_used(agent_and_broker):
    agent, broker = agent_and_broker
    broker.auto_respond_with('pong')
    agent.publish_sync('svc', 'q', topic_wait='my/replies', timeout=2)
    preset = Parcel.from_content('q')
    preset.topic_return = 'preset/replies'
    agent.publish_sync('svc', preset, timeout=2)
    assert [r.topic_return for r in _requests(broker)] == ['my/replies', 'preset/replies']
    assert preset.topic_return == 'preset/replies'


def test_publish_sync_does_not_touch_the_worker(agent_and_broker):
    agent, broker = agent_and_broker
    agent._agent_worker = None
    broker.auto_respond_with('pong')
    assert agent.publish_sync('svc', 'q', timeout=2).content == 'pong'
    assert agent._agent_worker is None


def test_timeout_none_is_deprecated(agent_and_broker):
    agent, broker = agent_and_broker
    broker.auto_respond_with('pong')
    with pytest.warns(DeprecationWarning, match='timeout=None'):
        assert agent.publish_sync('svc', 'q', timeout=None).content == 'pong'


def test_a_positive_timeout_does_not_warn(agent_and_broker):
    agent, broker = agent_and_broker
    broker.auto_respond_with('pong')
    with warnings.catch_warnings():
        warnings.simplefilter('error', DeprecationWarning)
        agent.publish_sync('svc', 'q', timeout=2)


def test_a_late_reply_on_a_generated_topic_is_dropped_and_counted(agent_and_broker):
    agent, broker = agent_and_broker
    with pytest.raises(TimeoutError):
        agent.publish_sync('svc', 'q', timeout=0.05)
    [request] = _requests(broker)
    late = TextParcel('too late')
    late.correlation_id = request.correlation_id
    broker.deliver(request.topic_return, late.payload())
    assert FakeBus.wait_until(lambda: agent.late_reply_count == 1, timeout=2)
    FakeBus.wait_until(lambda: False, timeout=0.2)
    assert agent.unhandled == []


def test_a_late_reply_on_a_callers_topic_wait_still_reaches_on_message(agent_and_broker):
    agent, broker = agent_and_broker
    with pytest.raises(TimeoutError):
        agent.publish_sync('svc', 'q', topic_wait='my/replies', timeout=0.05)
    broker.deliver('my/replies', TextParcel('too late').payload())
    assert FakeBus.wait_until(lambda: agent.unhandled == [('my/replies', 'too late')], timeout=2)

