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
    waits for them only when given a timeout.
"""

import inspect
import pickle
import threading

import pytest

from agentflow.core.agent import Agent
from agentflow.core.dispatcher import LegacyPerMessageDispatcher
from tests.fakes.fake_bus import FakeBus

xfail_75 = pytest.mark.xfail(strict=True, reason='issue #75: dead code')


@xfail_75
def test_agent_config_defaults_to_none_not_a_shared_dict():
    assert inspect.signature(Agent.__init__).parameters['agent_config'].default is None
    assert Agent('a').config == Agent('b').config


@xfail_75
def test_the_unused_message_broker_attribute_is_gone():
    assert not hasattr(Agent('a', {}), '_message_broker')


@xfail_75
def test_the_dispatcher_init_lock_keeps_its_type_across_pickling():
    agent = Agent('a', {})
    revived = pickle.loads(pickle.dumps(agent))
    assert type(revived._dispatcher_init_lock) is type(agent._dispatcher_init_lock)


@xfail_75
def test_the_on_start_hook_is_documented():
    assert Agent._on_start.__doc__


@xfail_75
def test_the_legacy_dispatcher_stop_reports_running_handlers():
    dispatcher = LegacyPerMessageDispatcher()
    release = threading.Event()
    dispatcher.enqueue(lambda: release.wait(5))
    assert dispatcher.stop() is False                       # a handler still runs
    assert dispatcher.stop(timeout_s=0.1) is False
    release.set()
    assert dispatcher.stop(timeout_s=2.0) is True
    assert FakeBus.wait_until(lambda: dispatcher.stop() is True, timeout=1)
