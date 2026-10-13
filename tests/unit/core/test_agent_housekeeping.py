"""Dead code and small inconsistencies in Agent and the legacy
dispatcher (issue #75, W-74).

`core/wrapper.py` was already removed with issue #1. What remained:

Contracts verified:
  - Agent's agent_config default is not a shared mutable dict.
  - The unused `_message_broker` attribute is gone.
  - `_dispatcher_init_lock` is the same kind of lock after an Agent is
    unpickled (process mode) as before.
  - The `_on_start` hook is documented.
  - The deprecated LegacyPerMessageDispatcher's stop() reports whether
    its handler threads have finished, instead of always True, and
    waits for them only when given a timeout; also when producers
    enqueue concurrently, and when called from a handler thread.
"""

import inspect
import pickle
import threading

import pytest

from agentflow.core.agent import Agent
from agentflow.core.dispatcher import LegacyPerMessageDispatcher
from tests.fakes.fake_bus import FakeBus


def test_agent_config_defaults_to_none_not_a_shared_dict():
    assert inspect.signature(Agent.__init__).parameters['agent_config'].default is None
    assert Agent('a').config == Agent('b').config


def test_the_unused_message_broker_attribute_is_gone():
    assert not hasattr(Agent('a', {}), '_message_broker')


def test_the_dispatcher_init_lock_keeps_its_type_across_pickling():
    agent = Agent('a', {})
    revived = pickle.loads(pickle.dumps(agent))
    assert type(revived._dispatcher_init_lock) is type(agent._dispatcher_init_lock)


def test_the_on_start_hook_is_documented():
    assert Agent._on_start.__doc__


def test_the_legacy_dispatcher_stop_reports_running_handlers():
    dispatcher = LegacyPerMessageDispatcher()
    release = threading.Event()
    dispatcher.enqueue(lambda: release.wait(5))
    assert dispatcher.stop() is False                       # a handler still runs
    assert dispatcher.stop(timeout_s=0.1) is False
    release.set()
    assert dispatcher.stop(timeout_s=2.0) is True
    assert FakeBus.wait_until(lambda: dispatcher.stop() is True, timeout=1)


class _DelayedFirstStart:
    """The dispatcher module's `threading`, except that the first Thread
    started waits for `gate` before it really starts: the window in
    which a concurrent enqueue used to drop it from the tracked
    handlers because it was not alive yet."""

    def __init__(self):
        self.gate = threading.Event()
        self.waiting = threading.Event()
        proxy = self

        class Thread(threading.Thread):
            first = True

            def start(self):
                if Thread.first:
                    Thread.first = False
                    proxy.waiting.set()
                    proxy.gate.wait(5)
                super().start()
        self.Thread = Thread

    def __getattr__(self, name):
        return getattr(threading, name)


def test_the_legacy_dispatcher_tracks_a_handler_enqueued_concurrently(monkeypatch):
    from agentflow.core import dispatcher as dispatcher_module
    delayed = _DelayedFirstStart()
    monkeypatch.setattr(dispatcher_module, 'threading', delayed)
    dispatcher = LegacyPerMessageDispatcher()
    release = threading.Event()
    producer = threading.Thread(target=dispatcher.enqueue, args=(lambda: release.wait(5),))
    try:
        producer.start()
        assert delayed.waiting.wait(2)                  # its thread is not started yet
        done = threading.Event()
        dispatcher.enqueue(done.set)                    # a concurrent enqueue meanwhile
        assert done.wait(2)
        delayed.gate.set()
        producer.join(2)
        assert dispatcher.stop() is False               # the first handler still waits
    finally:
        delayed.gate.set()
        release.set()
    assert dispatcher.stop(timeout_s=5.0) is True


def test_the_legacy_dispatcher_stop_from_a_handler_does_not_raise():
    dispatcher = LegacyPerMessageDispatcher()
    outcome = []

    def handler():
        try:
            outcome.append(dispatcher.stop(timeout_s=0.5))
        except Exception as ex:
            outcome.append(ex)
    dispatcher.enqueue(handler)
    assert FakeBus.wait_until(lambda: outcome, timeout=3)
    assert outcome == [True]                            # only this handler ran
