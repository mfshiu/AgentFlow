"""Holon membership must converge regardless of start order (issue #6).

`register_child` used to be sent once, on the first connection only, and
a parent never asked for registrations. So a parent knew its children
only if it happened to be online before them, a parent restart or a
child reconnect lost the membership, and the parent's `terminate` never
reached an unregistered subtree.

Contracts verified (on a FakeBus shared by several Agents):
  - Every (re)connect re-sends `register_child`, and every (re)connect
    of a potential parent broadcasts `parent_online` on
    `to_child.{name}`; a child receiving it on its parent channel
    re-registers with that parent.
  - Membership converges to exactly one entry per side for every start
    order: parent first, child first, child reconnect after the parent
    came up, and parent restart (new agent_id).
  - Registration is idempotent: repeated registrations refresh the entry
    but on_register_child / on_register_parent fire once.
  - An agent ignores `parent_online` arriving on any channel other than
    its parent channel (its own broadcast echoed back, a same-name
    replica's broadcast).
  - A parent's `terminate` reaches a child that started first.
  - The membership lock is runtime-only (process mode pickles Agents).
"""

import pickle
import threading
import time

import pytest

from agentflow.core.agent import Agent
from agentflow.core.parcel import Parcel
from tests.fakes.fake_bus import FakeBus

SETTLE_S = 0.3


class HolonAgent(Agent):
    def __init__(self, name):
        super().__init__(name=name, agent_config={})
        self.registered_children = []
        self.registered_parents = []
        self.parents_messages = []      # (topic, subject, parent_id)
        self.terminate_calls = 0
        self.child_hook_delay = 0.0
        self.child_hook_done_at = None
        self.parent_registered_at = None

    def on_register_child(self, child_id, child_info):
        time.sleep(self.child_hook_delay)
        self.registered_children.append(child_id)
        self.child_hook_done_at = time.monotonic()

    def on_register_parent(self, parent_id, parent_info):
        self.registered_parents.append(parent_id)
        self.parent_registered_at = time.monotonic()

    def on_parents_message(self, topic, parent):
        self.parents_messages.append(
            (topic, parent.get('subject'), parent.get('parent_id')))

    def _terminate(self):
        # Record instead of stopping: these agents have no worker loop.
        self.terminate_calls += 1


@pytest.fixture
def bus():
    b = FakeBus()
    b.agents = []
    yield b
    for a in b.agents:
        a.terminate()


def _agent(bus, name):
    a = HolonAgent(name)
    bus.attach(a)
    bus.agents.append(a)
    return a


def _membership(parent, child):
    return (set(parent._children) == {child.agent_id}
            and parent.agent_id in child._parents)


def _membership_all(parent, children):
    return (set(parent._children) == {c.agent_id for c in children}
            and all(parent.agent_id in c._parents for c in children))


def _settle():
    FakeBus.wait_until(lambda: False, timeout=SETTLE_S)


def test_parent_first_then_child_registers_both_sides(bus):
    parent, child = _agent(bus, 'p'), _agent(bus, 'k.p')
    bus.connect(parent)
    bus.connect(child)
    assert bus.wait_until(lambda: _membership(parent, child))
    assert set(child._parents) == {parent.agent_id}


def test_child_first_then_parent_converges(bus):
    parent, child = _agent(bus, 'p'), _agent(bus, 'k.p')
    bus.connect(child)          # its register_child reaches nobody
    bus.connect(parent)
    assert bus.wait_until(lambda: _membership(parent, child)), \
        'parent never learned about a child that started first'
    assert set(child._parents) == {parent.agent_id}


def test_child_reconnect_reregisters_when_it_missed_the_parent(bus):
    parent, child = _agent(bus, 'p'), _agent(bus, 'k.p')
    bus.connect(child)
    bus.disconnect(child)
    bus.connect(parent)         # its parent_online is lost: child offline
    bus.connect(child)          # reconnect
    assert bus.wait_until(lambda: _membership(parent, child)), \
        'a reconnecting child did not re-register'


def test_parent_restart_child_registers_with_new_parent(bus):
    old, child = _agent(bus, 'p'), _agent(bus, 'k.p')
    bus.connect(old)
    bus.connect(child)
    assert bus.wait_until(lambda: _membership(old, child))
    bus.remove(old)             # parent crashed

    new = _agent(bus, 'p')      # restarted parent: new agent_id
    bus.connect(new)
    assert bus.wait_until(lambda: set(new._children) == {child.agent_id}), \
        'restarted parent never learned about the existing child'
    assert bus.wait_until(lambda: new.agent_id in child._parents)


def test_repeated_registration_fires_hooks_once(bus):
    parent, child = _agent(bus, 'p'), _agent(bus, 'k.p')
    bus.connect(parent)
    bus.connect(child)
    assert bus.wait_until(lambda: _membership(parent, child))
    for _ in range(2):
        bus.disconnect(parent)
        bus.connect(parent)
        bus.disconnect(child)
        bus.connect(child)
    _settle()
    assert set(parent._children) == {child.agent_id}
    assert set(child._parents) == {parent.agent_id}
    assert parent.registered_children == [child.agent_id]
    assert child.registered_parents == [parent.agent_id]


def test_terminate_reaches_child_that_started_first(bus):
    parent, child = _agent(bus, 'p'), _agent(bus, 'k.p')
    bus.connect(child)
    bus.connect(parent)
    assert bus.wait_until(lambda: _membership(parent, child))
    parent._notify_children('terminate')
    assert bus.wait_until(lambda: child.terminate_calls >= 1)


def test_parent_reconnect_picks_up_child_started_while_it_was_offline(bus):
    parent, child = _agent(bus, 'p'), _agent(bus, 'k.p')
    bus.connect(parent)
    bus.disconnect(parent)
    bus.connect(child)          # its register_child is lost: parent offline
    bus.connect(parent)         # reconnect must broadcast parent_online
    assert bus.wait_until(lambda: _membership(parent, child)),         'parent reconnect did not pick up a child that started while it was offline'
    parent._notify_children('terminate')
    assert bus.wait_until(lambda: child.terminate_calls >= 1)


def test_parent_online_is_broadcast_on_reconnect(bus):
    parent = _agent(bus, 'p')
    bus.connect(parent)
    bus.disconnect(parent)
    n0 = len(parent._broker.published)
    bus.connect(parent)
    sent = [Parcel.from_payload(p).content for t, p in parent._broker.published[n0:]
            if t == 'to_child.p']
    assert {'parent_id': parent.agent_id, 'subject': 'parent_online',
            'data': None} in sent


def test_parent_online_never_reaches_on_parents_message(bus):
    # parent_online is internal: neither our own echo, nor a same-name
    # replica's broadcast, nor a real parent's announcement reaches the
    # user hook.
    parent = _agent(bus, 'p')
    first, second = _agent(bus, 'k.p'), _agent(bus, 'k.p')
    bus.connect(first)
    bus.connect(second)
    bus.connect(parent)
    bus.disconnect(first)
    bus.connect(first)
    assert bus.wait_until(lambda: _membership_all(parent, [first, second]))
    _settle()
    for a in (first, second):
        assert not [m for m in a.parents_messages if m[1] == 'parent_online'],             a.parents_messages


def test_reregistration_is_sent_only_to_the_announcing_parent(bus):
    # Two replicas of parent 'p': a child's answer to one replica's
    # parent_online must not be delivered to the other replica.
    p1, p2 = _agent(bus, 'p'), _agent(bus, 'p')
    child = _agent(bus, 'k.p')
    bus.connect(child)
    bus.connect(p1)
    bus.connect(p2)
    assert bus.wait_until(lambda: child.agent_id in p1._children
                          and child.agent_id in p2._children)
    _settle()
    for receiver, other in ((p1, p2), (p2, p1)):
        misdelivered = [
            1 for b, t, payload in bus.delivered
            if b is receiver._broker
            and Parcel.from_payload(payload).content.get('target_parents') == [other.agent_id]
        ]
        assert not misdelivered, f'{len(misdelivered)} answers meant for the other replica'


def test_register_parent_is_not_sent_before_the_first_hook_returns(bus):
    # A duplicate register_child (here: the child's answer to the
    # parent's reconnect announcement) arrives while the first
    # registration's on_register_child is still running. The child
    # must learn its parent only after that hook has returned.
    parent, child = _agent(bus, 'p'), _agent(bus, 'k.p')
    parent.child_hook_delay = 0.5
    bus.connect(parent)
    bus.connect(child)              # first register_child: hook starts
    bus.disconnect(parent)
    bus.connect(parent)             # parent_online -> duplicate register_child
    assert bus.wait_until(lambda: child.parent_registered_at is not None)
    assert parent.child_hook_done_at is not None
    assert child.parent_registered_at >= parent.child_hook_done_at,         'register_parent overtook the first on_register_child'


def test_parent_online_is_broadcast_on_connect(bus):
    parent = _agent(bus, 'p')
    bus.connect(parent)
    sent = [Parcel.from_payload(p).content for t, p in parent._broker.published
            if t == 'to_child.p']
    assert {'parent_id': parent.agent_id, 'subject': 'parent_online',
            'data': None} in sent


def test_own_parent_online_echo_does_not_trigger_reregistration(bus):
    # 'm.root' broadcasts parent_online on to_child.m.root, a channel it
    # also listens on (children named m.root); the echo must be ignored.
    mid = _agent(bus, 'm.root')
    bus.connect(mid)
    _settle()
    assert mid._broker.topics_published().count('to_parent.root') == 1


def test_same_name_replica_parent_online_is_ignored(bus):
    first, second = _agent(bus, 'k.p'), _agent(bus, 'k.p')
    bus.connect(first)
    bus.connect(second)         # its parent_online reaches `first`
    _settle()
    assert first._broker.topics_published().count('to_parent.p') == 1
    assert second._broker.topics_published().count('to_parent.p') == 1


def test_membership_lock_is_runtime_only_for_process_mode():
    a = HolonAgent('k.p')
    clone = pickle.loads(pickle.dumps(a))
    # The unpickled Agent (as in a spawned child) can still register.
    clone._Agent__register_parent('pid-1', {'parent_id': 'pid-1'})
    assert set(clone._parents) == {'pid-1'}
