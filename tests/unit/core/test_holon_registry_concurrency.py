"""Concurrent holon registrations (issue #57, W-66).

Registration handlers run on up to 8 dispatcher threads at once. Writes
to `_children` / `_parents` are made under `_holon_lock` (since #6 /
#13), but the user hooks run outside it, so a hook iterating
`self._children` while other children register hit "dictionary changed
size during iteration". And a hook that raised made the framework skip
the `register_parent` answer, so that child never learned its parent.

Contracts verified (with FakeBroker):
  - 64 children registering at once: a hook iterating `self._children`
    never sees it change size (the registries are replaced, not
    mutated, on each registration), and every child is answered.
  - A raising on_register_child is logged and the child still gets
    register_parent; a raising on_register_parent is logged and does
    not escape the handler.
"""

import logging
import threading
import time

import pytest

from agentflow.core.agent import Agent
from agentflow.core.parcel import Parcel
from tests.fakes.fake_broker import FakeBroker, FakeWorker
from tests.fakes.fake_bus import FakeBus

xfail_57 = pytest.mark.xfail(strict=True, reason='issue #57: holon registry concurrency')


class Parent(Agent):
    def __init__(self):
        super().__init__(name='boss', agent_config={'dispatch': {'shutdown_timeout_s': 2.0}})
        self.iteration_errors = 0
        self.hook_raises = False
        self._count_lock = threading.Lock()

    def on_register_child(self, child_id, info):
        try:
            for _ in self._children:            # e.g. building a broadcast list
                time.sleep(0.0005)
        except RuntimeError:
            with self._count_lock:
                self.iteration_errors += 1
            raise
        if self.hook_raises:
            raise ValueError('hook failed')


def _attach(agent):
    broker = FakeBroker(notifier=agent)
    agent._broker = broker
    agent._agent_worker = FakeWorker()
    return broker


def _register_child(broker, child_id):
    broker.deliver('to_parent.boss', Parcel.from_content(
        {'child_id': child_id, 'subject': 'register_child'}).payload())


def _answers(broker):
    return [t for t, _ in broker.publish_calls if '.to_child.boss' in t]


@xfail_57
def test_a_hook_iterating_children_survives_concurrent_registrations():
    parent = Parent()
    broker = _attach(parent)
    parent.subscribe('to_parent.boss', topic_handler=parent._handle_children)
    try:
        for i in range(64):
            _register_child(broker, f'c{i}')
        assert FakeBus.wait_until(lambda: len(_answers(broker)) == 64, timeout=10), len(_answers(broker))
        assert parent.iteration_errors == 0
        assert len(parent._children) == 64
    finally:
        parent.terminate()


@xfail_57
def test_a_raising_child_hook_still_answers_register_parent(caplog):
    caplog.set_level(logging.ERROR)
    parent = Parent()
    parent.hook_raises = True
    broker = _attach(parent)
    parent.subscribe('to_parent.boss', topic_handler=parent._handle_children)
    try:
        _register_child(broker, 'c1')
        assert FakeBus.wait_until(lambda: _answers(broker) == ['c1.to_child.boss'], timeout=3)
        assert any('on_register_child' in r.getMessage() for r in caplog.records)
    finally:
        parent.terminate()


class Child(Agent):
    def __init__(self):
        super().__init__(name='kid.boss', agent_config={})

    def on_register_parent(self, parent_id, info):
        raise ValueError('hook failed')


@xfail_57
def test_a_raising_parent_hook_is_logged_and_contained(caplog):
    caplog.set_level(logging.ERROR)
    child = Child()
    _attach(child)
    topic = f'{child.agent_id}.to_child.boss'
    message = Parcel.from_content({'parent_id': 'p1', 'parent_name': 'boss',
                                   'subject': 'register_parent'})
    try:
        child._handle_parents(topic, message)       # must not raise
        assert 'p1' in child._parents
        assert any('on_register_parent' in r.getMessage() for r in caplog.records)
    finally:
        child.terminate()


@xfail_57
def test_registries_are_replaced_not_mutated():
    parent = Parent()
    broker = _attach(parent)
    parent.subscribe('to_parent.boss', topic_handler=parent._handle_children)
    try:
        before = parent._children
        _register_child(broker, 'c1')
        assert FakeBus.wait_until(lambda: 'c1' in parent._children, timeout=3)
        assert before == {}                         # a reader's snapshot never changes
    finally:
        parent.terminate()
