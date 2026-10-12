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
  - start_process() with an attribute that cannot be pickled names it
    (spawns a child, which the failure cleans up).
"""

import threading
import warnings

import pytest

from agentflow.core.agent import Agent
from agentflow.core.agent_worker import ProcessWorker, WorkerState
from tests.fakes.fake_broker import FakeBroker, FakeWorker

xfail_9 = pytest.mark.xfail(strict=True, reason='issue #9: process-mode controller')

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


@xfail_9
def test_agent_documents_the_process_mode_contract():
    doc = Agent.__doc__ or ''
    assert 'process' in doc and 'start_thread' in doc


@xfail_9
@pytest.mark.parametrize('call', [
    lambda a: a.publish('t', 'x'),
    lambda a: a.subscribe('t', topic_handler=lambda topic, pcl: None),
])
def test_messaging_on_the_controlling_object_warns(controller, call):
    with pytest.warns(RuntimeWarning, match='start_thread'):
        call(controller)


@xfail_9
def test_publish_sync_on_the_controlling_object_warns(controller):
    with pytest.warns(RuntimeWarning, match='start_thread'):
        with pytest.raises(RuntimeError, match='no broker'):
            controller.publish_sync('t', 'x', timeout=0.1)


@xfail_9
@pytest.mark.parametrize('call', [
    lambda a: a.get_data('k'),
    lambda a: a.put_data('k', 1),
    lambda a: a.pop_data('k'),
])
def test_the_data_api_on_the_controlling_object_warns(controller, call):
    with pytest.warns(RuntimeWarning, match='start_thread'):
        with pytest.raises(AttributeError):
            call(controller)


@pytest.mark.parametrize('worker', [FakeWorker(), None])
def test_no_warning_in_thread_mode_before_start_or_in_the_child(worker):
    agent = Agent('local', {})
    agent._agent_worker = worker
    agent._broker = FakeBroker(notifier=agent) if worker is not None else None
    with warnings.catch_warnings():
        warnings.simplefilter('error', RuntimeWarning)
        agent.publish('t', 'x')
        agent.subscribe('t', topic_handler=lambda topic, pcl: None)


@xfail_9
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


class _WithLock(Agent):
    def __init__(self):
        super().__init__('locked', dict(EMPTY_BROKER))
        self.lock = threading.Lock()


@xfail_9
def test_an_attribute_that_cannot_be_pickled_is_named():
    agent = _WithLock()
    with pytest.raises(TypeError, match="'lock'") as raised:
        agent.start_process()
    assert 'on_activating' in str(raised.value)
    assert agent._agent_worker.state is WorkerState.START_FAILED
