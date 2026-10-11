"""Parent-to-child messages must reach only the addressed agents
(issue #38, W-30).

`to_child.{X}` means two things: a parent named X broadcasting to its
own children, and a message addressed by name to the agents named X.
Every agent named X listens on it for the second meaning, and every
child of an X listens on it for the first. So:

  - a mid-tier replica `b.a` received its same-name sibling's broadcast
    to that sibling's children and took it for a message from its own
    parent: one replica's terminate stopped the other replicas;
  - a message addressed by name to `b.a` (from its parent `a`) also
    reached `b.a`'s children, `c.b.a`, as if `b.a` had sent it.

Every parent-to-child message now carries the sender's name
(`parent_name`), and an agent accepts it only from an agent named like
its parent. Messages from older senders, which lack the field, are
still accepted.
"""

import threading

import pytest

from agentflow.core.agent import Agent
from agentflow.core.parcel import Parcel
from tests.fakes.fake_bus import FakeBus

SETTLE_S = 0.5

# Remove when issue #38 is fixed (strict: an unexpected pass fails).
_ISSUE38 = pytest.mark.xfail(strict=True, reason='issue #38: to_child.{name} still reaches same-name replicas and grandchildren')


class HolonAgent(Agent):
    def __init__(self, name):
        super().__init__(name=name, agent_config={})
        self.parents_messages = []      # (topic, subject, parent_id)
        self._Agent__terminate_event = threading.Event()

    def on_parents_message(self, topic, parent):
        self.parents_messages.append(
            (topic, parent.get('subject'), parent.get('parent_id')))

    def terminated(self, timeout=0.0):
        return self._Agent__terminate_event.wait(timeout)


@pytest.fixture
def bus():
    b = FakeBus(max_deliveries=500)
    b.agents = []
    yield b
    for a in b.agents:
        a.terminate()


def _agent(bus, name):
    a = HolonAgent(name)
    bus.attach(a)
    bus.agents.append(a)
    bus.connect(a)
    return a


def _settle():
    FakeBus.wait_until(lambda: False, timeout=SETTLE_S)


@_ISSUE38
def test_replica_terminate_does_not_stop_its_same_name_sibling(bus):
    root = _agent(bus, 'a')
    m1, m2 = _agent(bus, 'b.a'), _agent(bus, 'b.a')
    leaf = _agent(bus, 'c.b.a')     # so that m1 has children to notify
    assert bus.wait_until(lambda: set(root._children) == {m1.agent_id, m2.agent_id}
                          and leaf.agent_id in m1._children)

    m1._terminate()                 # broadcasts terminate on to_child.b.a

    assert m1.terminated(5)
    _settle()
    assert not m2.terminated(), "a same-name replica's terminate stopped its sibling"


def test_parent_terminate_still_reaches_every_replica(bus):
    root = _agent(bus, 'a')
    m1, m2 = _agent(bus, 'b.a'), _agent(bus, 'b.a')
    assert bus.wait_until(lambda: set(root._children) == {m1.agent_id, m2.agent_id})

    root._terminate()

    assert m1.terminated(5) and m2.terminated(5)


@_ISSUE38
def test_replica_broadcast_never_reaches_sibling_on_parents_message(bus):
    _agent(bus, 'a')
    m1, m2 = _agent(bus, 'b.a'), _agent(bus, 'b.a')
    leaf = _agent(bus, 'c.b.a')
    assert bus.wait_until(lambda: leaf.agent_id in m1._children)

    m1._notify_children('hello')

    assert bus.wait_until(lambda: any(m[1] == 'hello' for m in leaf.parents_messages))
    _settle()
    assert not [m for m in m2.parents_messages if m[1] == 'hello'], m2.parents_messages


@_ISSUE38
def test_message_addressed_by_name_does_not_reach_grandchildren(bus):
    root = _agent(bus, 'a')
    mid = _agent(bus, 'b.a')
    leaf = _agent(bus, 'c.b.a')
    assert bus.wait_until(lambda: mid.agent_id in root._children
                          and leaf.agent_id in mid._children)

    root._notify_children('hello', target_child_name='b.a')

    assert bus.wait_until(lambda: any(m[1] == 'hello' for m in mid.parents_messages))
    _settle()
    assert not [m for m in leaf.parents_messages if m[1] == 'hello'], leaf.parents_messages


@_ISSUE38
def test_parent_messages_carry_the_sender_name(bus):
    root = _agent(bus, 'a')
    mid = _agent(bus, 'b.a')
    assert bus.wait_until(lambda: mid.agent_id in root._children)
    root._notify_children('hello')
    sent = [Parcel.from_payload(p).content for t, p in root._broker.published
            if t.endswith('to_child.a')]
    assert sent and all(m.get('parent_name') == 'a' for m in sent), sent


def test_messages_from_older_senders_without_sender_name_are_still_accepted(bus):
    child = _agent(bus, 'k.p')
    legacy = Parcel.from_content({'parent_id': 'old-parent', 'subject': 'hello', 'data': None})
    bus._route('to_child.p', legacy.payload())
    assert bus.wait_until(lambda: ('to_child.p', 'hello', 'old-parent') in child.parents_messages)
