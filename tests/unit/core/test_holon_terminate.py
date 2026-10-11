"""A mid-tier agent must not turn its parent's terminate into an echo
storm (issue #5, W-03).

A mid-tier agent `b.a` listens on to_child.b.a (messages addressed to
agents named b.a) and also broadcasts to its own children on
to_child.b.a. MQTT 3.1.1 echoes a client's own publishes back to it, so
`b.a` received its own `terminate`, ran `_terminate` again, broadcast
again, and so on, starting one more sleeper thread each round.

Contracts verified (on a FakeBus capped at a fixed number of deliveries
so that a regression ends instead of spinning):
  - One terminate from the root runs each agent's `_terminate` body
    exactly once and makes the mid-tier broadcast terminate once.
  - The cascade still reaches every level (root -> mid -> leaf).
  - `_terminate` is idempotent when called several times directly
    (e.g. a worker stop and a parent's terminate both arrive).
  - An agent's own broadcast never reaches its own on_parents_message.
"""

import threading

import pytest

from agentflow.core.agent import Agent
from agentflow.core.parcel import Parcel
from tests.fakes.fake_bus import FakeBus

MAX_DELIVERIES = 300
SETTLE_S = 0.5

# Remove when issue #5 is fixed (strict: an unexpected pass fails).
_ISSUE5 = pytest.mark.xfail(strict=True, reason='issue #5: mid-tier terminate echo storm not fixed yet')


class TerminateAgent(Agent):
    def __init__(self, name):
        super().__init__(name=name, agent_config={})
        self.terminate_bodies = 0
        self.parents_messages = []      # (topic, subject, parent_id)
        # Normally created by Agent._activate; _terminate's stop thread
        # sets it.
        self._Agent__terminate_event = threading.Event()

    def _notify_children(self, subject, *args, **kwargs):
        if subject == 'terminate':
            self.terminate_bodies += 1
        return super()._notify_children(subject, *args, **kwargs)

    def on_parents_message(self, topic, parent):
        self.parents_messages.append(
            (topic, parent.get('subject'), parent.get('parent_id')))


@pytest.fixture
def bus():
    b = FakeBus(max_deliveries=MAX_DELIVERIES)
    b.agents = []
    yield b
    for a in b.agents:
        a.terminate()


def _agent(bus, name):
    a = TerminateAgent(name)
    bus.attach(a)
    bus.agents.append(a)
    return a


def _hierarchy(bus):
    root, mid, leaf = _agent(bus, 'a'), _agent(bus, 'b.a'), _agent(bus, 'c.b.a')
    for a in (root, mid, leaf):
        bus.connect(a)
    assert bus.wait_until(lambda: set(root._children) == {mid.agent_id}
                          and set(mid._children) == {leaf.agent_id}
                          and root.agent_id in mid._parents
                          and mid.agent_id in leaf._parents)
    return root, mid, leaf


def _terminates_published(agent, topic):
    return [1 for t, p in agent._broker.published
            if t == topic and Parcel.from_payload(p).content.get('subject') == 'terminate']


def _settle():
    FakeBus.wait_until(lambda: False, timeout=SETTLE_S)


@_ISSUE5
def test_root_terminate_runs_each_body_once_and_mid_broadcasts_once(bus):
    root, mid, leaf = _hierarchy(bus)
    threads_before = threading.active_count()
    root._terminate()
    assert bus.wait_until(lambda: leaf.terminate_bodies >= 1)
    _settle()
    assert (root.terminate_bodies, mid.terminate_bodies, leaf.terminate_bodies) == (1, 1, 1)
    assert len(_terminates_published(mid, 'to_child.b.a')) == 1
    assert bus.dropped == 0, f'message storm: {bus.dropped} deliveries over the cap'
    # One delayed-stop thread per agent at most, not one per echo.
    assert threading.active_count() - threads_before <= 3


def test_cascade_reaches_every_level(bus):
    root, mid, leaf = _hierarchy(bus)
    root._terminate()
    for a in (root, mid, leaf):
        assert a._Agent__terminate_event.wait(5), f'{a.name} did not stop'


@_ISSUE5
def test_terminate_is_idempotent(bus):
    mid = _agent(bus, 'b.a')
    bus.connect(mid)
    mid._children['leaf-id'] = {}
    threads_before = threading.active_count()
    for _ in range(3):              # worker stop + parent terminate + ...
        mid._terminate()
    assert mid.terminate_bodies == 1
    assert threading.active_count() - threads_before <= 1
    assert mid._Agent__terminate_event.wait(5)


@_ISSUE5
def test_own_broadcast_never_reaches_own_on_parents_message(bus):
    root, mid, leaf = _hierarchy(bus)
    mid._notify_children('hello')
    assert bus.wait_until(lambda: any(m[1] == 'hello' for m in leaf.parents_messages))
    _settle()
    assert not [m for m in mid.parents_messages if m[2] == mid.agent_id], \
        mid.parents_messages
