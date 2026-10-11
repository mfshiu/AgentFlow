"""Messages that arrive before the agent's handlers are registered
(follow-up of issue #17).

A resumed persistent session (RFC-015 §6) makes the broker deliver its
queued messages right after the CONNACK, before on_activate /
on_connected have registered the handlers. Such a message reached the
default on_message, was acknowledged, and was lost.

While an agent's lifecycle hooks are still running, a message with no
handler for its topic is now held, and released through normal routing
once every awaited hook has returned. These tests drive the gate
through Agent's private hooks (`__await_hook` / `__hook_done`), which
`_activate` and `_on_connect` call.

Contracts verified:
  - A held message reaches a handler registered meanwhile; one whose
    topic still has no handler reaches on_message as before.
  - Messages with a handler are never held.
  - The gate opens only when every awaited hook has returned.
  - The hold is bounded; overflow is dropped and counted.
  - Held messages are replayed despite their message id having been
    seen, and a held request is answered (RFC-003 / RFC-014).
  - Agents outside the lifecycle (no _activate) are never gated.
"""

import pytest

import agentflow.core.agent as agent_module
from agentflow.core.agent import Agent
from agentflow.core.parcel import Parcel, TextParcel
from tests.fakes.fake_broker import FakeBroker, FakeWorker
from tests.fakes.fake_bus import FakeBus

xfail_held = pytest.mark.xfail(strict=True, reason='messages before handler registration are lost')

SETTLE_S = 0.3


def _wait_until(predicate, timeout=2.0):
    return FakeBus.wait_until(predicate, timeout=timeout)


def _settle():
    FakeBus.wait_until(lambda: False, timeout=SETTLE_S)


class Recorder(Agent):
    def __init__(self, config=None):
        super().__init__('worker', config or {'dispatch': {'shutdown_timeout_s': 1.0}})
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


def _await(agent, *hooks):
    for hook in hooks:
        agent._Agent__await_hook(hook)


def _done(agent, hook):
    agent._Agent__hook_done(hook)


@xfail_held
def test_a_held_message_reaches_the_handler_registered_meanwhile(agent_and_broker):
    agent, broker = agent_and_broker
    _await(agent, 'on_activate')
    broker.deliver('jobs', TextParcel('j1').payload())
    broker.deliver('stray', TextParcel('s1').payload())
    _settle()
    assert agent.unhandled == []

    jobs = []
    agent.subscribe('jobs', topic_handler=lambda topic, pcl: jobs.append(pcl.content))
    _done(agent, 'on_activate')

    assert _wait_until(lambda: jobs == ['j1'] and agent.unhandled == [('stray', 's1')])


@xfail_held
def test_messages_with_a_handler_are_never_held(agent_and_broker):
    agent, broker = agent_and_broker
    jobs = []
    agent.subscribe('jobs', topic_handler=lambda topic, pcl: jobs.append(pcl.content))
    _await(agent, 'on_activate')
    broker.deliver('jobs', TextParcel('j1').payload())
    assert _wait_until(lambda: jobs == ['j1'])
    _done(agent, 'on_activate')


@xfail_held
def test_the_gate_opens_only_when_every_awaited_hook_has_returned(agent_and_broker):
    agent, broker = agent_and_broker
    _await(agent, 'on_activate', 'on_connected')
    broker.deliver('stray', TextParcel('s1').payload())
    _done(agent, 'on_activate')
    _settle()
    assert agent.unhandled == []
    _done(agent, 'on_connected')
    assert _wait_until(lambda: agent.unhandled == [('stray', 's1')])


@xfail_held
def test_the_hold_is_bounded():
    limit = agent_module._EARLY_MESSAGE_LIMIT
    agent = Recorder({'dispatch': {'queue_capacity': 2 * limit, 'shutdown_timeout_s': 1.0}})
    broker = FakeBroker(notifier=agent)
    agent._broker = broker
    agent._agent_worker = FakeWorker()
    try:
        _await(agent, 'on_activate')
        for i in range(limit + 10):
            broker.deliver('stray', TextParcel(i).payload())
        assert len(agent._early_messages) == limit
        _done(agent, 'on_activate')
        assert _wait_until(lambda: len(agent.unhandled) == limit, timeout=5)
        assert [content for _, content in agent.unhandled[:3]] == [0, 1, 2]
    finally:
        agent.terminate()


@xfail_held
def test_a_held_message_is_replayed_although_its_id_was_seen(agent_and_broker):
    agent, broker = agent_and_broker
    _await(agent, 'on_activate')
    parcel = TextParcel('j1')
    parcel.message_id = 'm-1'
    broker.deliver('jobs', parcel.payload())
    jobs = []
    agent.subscribe('jobs', topic_handler=lambda topic, pcl: jobs.append(pcl.content))
    _done(agent, 'on_activate')
    assert _wait_until(lambda: jobs == ['j1'])


@xfail_held
def test_a_held_request_is_answered_once_its_handler_exists(agent_and_broker):
    agent, broker = agent_and_broker
    _await(agent, 'on_activate')
    request = TextParcel('ping', topic_return='reply/1')
    request.correlation_id = 'c-1'
    broker.deliver('svc', request.payload())
    agent.subscribe('svc', topic_handler=lambda topic, pcl: 'pong')
    _done(agent, 'on_activate')

    def replies():
        return [Parcel.from_payload(p) for t, p in broker.publish_calls if t == 'reply/1']
    assert _wait_until(lambda: replies())
    assert (replies()[0].content, replies()[0].correlation_id) == ('pong', 'c-1')


def test_agents_outside_the_lifecycle_are_never_gated():
    bus = FakeBus()
    agent = Recorder()
    bus.attach(agent)
    bus.connect(agent)          # first connect, but _activate never ran
    try:
        agent.subscribe('stray', 'str')      # no handler: on_message
        bus._route('stray', TextParcel('s1').payload())
        assert _wait_until(lambda: ('stray', 's1') in agent.unhandled, timeout=0.5)
    finally:
        agent.terminate()
