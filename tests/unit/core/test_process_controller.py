"""The controlling object of a process-mode Agent (issue #9, W-08).

CONCURRENCY_TYPE defaults to 'process'. After start() the agent runs in
a spawned child; the caller's Agent object has no broker and no data
store, yet nothing said so: publish() only logged an exception and the
message was lost, subscribe() registered a handler that never ran, the
data API raised AttributeError, and Agent had no docstring although
RFC-008 said the contract was documented there. An attribute that
cannot be pickled made start() fail without naming it.

This change only adds warnings and messages (roadmap P0-5); refusing
these calls is left to P1-5.

Contracts verified (no child process is spawned except where noted):
  - Agent documents the process-mode contract.
  - On the controlling object (a ProcessWorker, no broker), publish,
    publish_sync, subscribe, get_data, put_data and pop_data warn
    (RuntimeWarning) and point to start_thread(). Nothing warns in
    thread mode, before start(), or in the child.
  - start() without an explicit CONCURRENCY_TYPE warns (FutureWarning)
    that the default will become 'thread'; start_thread(),
    start_process() and an explicit setting do not.
  - start_process() with an attribute that cannot be pickled names it,
    keeping the exception's type, and names nothing else (a
    multiprocessing primitive, an attribute referring to the agent);
    start_thread() then works on the same object. These spawn a child,
    which the failure cleans up.
  - A mode set after construction (set_config) counts as chosen.
  - unsubscribe and start_interval_loop warn on the controller too, and
    every warning points at the caller's line.
"""

import multiprocessing
import pickle
import threading
import warnings

import pytest

from agentflow.core.agent import Agent
from agentflow.core.agent_worker import ProcessWorker, ThreadWorker, WorkerState
from tests.fakes.fake_broker import FakeBroker, FakeWorker

EMPTY_BROKER = {'broker': {'broker_name': 'b', 'b': {'broker_type': 'empty'}}}


class _StartableWorker(FakeWorker):
    def start(self):
        return None


@pytest.fixture
def controller():
    """An Agent as its caller holds it after start_process(): the agent
    itself runs in a child process."""
    agent = Agent('ctl', {})
    agent._agent_worker = ProcessWorker(agent)
    yield agent
    agent._agent_worker = None


def test_agent_documents_the_process_mode_contract():
    doc = Agent.__doc__ or ''
    assert 'process' in doc and 'start_thread' in doc


@pytest.mark.parametrize('call', [
    lambda a: a.publish('t', 'x'),
    lambda a: a.subscribe('t', topic_handler=lambda topic, pcl: None),
])
def test_messaging_on_the_controlling_object_warns(controller, call):
    with pytest.warns(RuntimeWarning, match='start_thread'):
        call(controller)


def test_publish_sync_on_the_controlling_object_warns(controller):
    with pytest.warns(RuntimeWarning, match='start_thread'):
        with pytest.raises(RuntimeError, match='no broker'):
            controller.publish_sync('t', 'x', timeout=0.1)


@pytest.mark.parametrize('call', [
    lambda a: a.get_data('k'),
    lambda a: a.put_data('k', 1),
    lambda a: a.pop_data('k'),
])
def test_the_data_api_on_the_controlling_object_warns(controller, call):
    with pytest.warns(RuntimeWarning, match='start_thread'):
        with pytest.raises(AttributeError):
            call(controller)


@pytest.mark.parametrize('worker', ['thread-before-broker', 'thread', None])
def test_no_warning_in_thread_mode_before_start_or_in_the_child(worker):
    agent = Agent('local', {})
    if worker == 'thread-before-broker':          # e.g. during on_activating
        agent._agent_worker = ThreadWorker(agent)
    elif worker == 'thread':
        agent._agent_worker = FakeWorker()
        agent._broker = FakeBroker(notifier=agent)
    with warnings.catch_warnings():
        warnings.simplefilter('error', RuntimeWarning)
        agent.publish('t', 'x')
        agent.subscribe('t', topic_handler=lambda topic, pcl: None)


def test_start_with_the_default_mode_warns_of_the_coming_change():
    agent = Agent('default', {})
    agent._agent_worker = _StartableWorker()
    with pytest.warns(FutureWarning, match="'thread'"):
        agent.start()


@pytest.mark.parametrize('start', ['start_thread', 'start_process', 'explicit'])
def test_an_explicit_mode_does_not_warn(start):
    agent = Agent('chosen', {'CONCURRENCY_TYPE': 'process'} if start == 'explicit' else {})
    agent._agent_worker = _StartableWorker()
    with warnings.catch_warnings():
        warnings.simplefilter('error', FutureWarning)
        getattr(agent, 'start' if start == 'explicit' else start)()


@pytest.mark.parametrize('call', [
    lambda a: a.unsubscribe('t'),
    lambda a: a.start_interval_loop(0),
])
def test_other_controller_calls_warn_too(controller, call):
    with pytest.warns(RuntimeWarning, match='start_thread'):
        call(controller)


def test_the_controller_warning_points_at_the_caller(controller):
    with pytest.warns(RuntimeWarning) as caught:
        controller.publish('t', 'x')
    assert caught[0].filename == __file__


def test_the_default_mode_warning_points_at_the_caller():
    agent = Agent('default', {})
    agent._agent_worker = _StartableWorker()
    with pytest.warns(FutureWarning) as caught:
        agent.start()
    assert caught[0].filename == __file__


def test_a_mode_set_after_construction_counts_as_chosen():
    agent = Agent('sub', {})
    agent.set_config('CONCURRENCY_TYPE', 'process')
    agent._agent_worker = _StartableWorker()
    with warnings.catch_warnings():
        warnings.simplefilter('error', FutureWarning)
        agent.start()


class _WithLock(Agent):
    def __init__(self):
        super().__init__('locked', dict(EMPTY_BROKER))
        self.lock = threading.Lock()


def test_an_attribute_that_cannot_be_pickled_is_named():
    agent = _WithLock()
    with pytest.raises(TypeError, match="'lock'") as raised:
        agent.start_process()
    assert 'on_activating' in str(raised.value) + ''.join(getattr(raised.value, '__notes__', []))
    assert agent._agent_worker.state is WorkerState.START_FAILED


class _Helper:
    def __init__(self, agent):
        self.agent = agent


class _WithSharedEventAndLambda(Agent):
    def __init__(self):
        super().__init__('mixed', dict(EMPTY_BROKER))
        self.ready = multiprocessing.Event()          # shared with the child: fine
        self.helper = _Helper(self)                   # refers to the agent: fine
        self.callback = lambda: None                  # cannot be pickled


def test_only_the_attributes_at_fault_are_named_and_the_type_is_kept():
    agent = _WithSharedEventAndLambda()
    with pytest.raises((AttributeError, pickle.PicklingError)) as raised:
        agent.start_process()
    notes = ''.join(getattr(raised.value, '__notes__', []))
    assert "'callback'" in notes
    assert "'ready'" not in notes and "'helper'" not in notes


def test_start_thread_works_after_a_failed_process_start():
    agent = _WithLock()
    with pytest.raises(TypeError):
        agent.start_process()
    try:
        agent.start_thread()
        assert isinstance(agent._agent_worker, ThreadWorker)
    finally:
        agent.terminate()
