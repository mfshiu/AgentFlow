"""publish_sync called from a handler (issue #7, W-09).

publish_sync waits for its reply on the calling thread, but the reply
was handled like any other message: queued for the same bounded
dispatcher (8 consumers by default). Called from a handler, which runs
on a consumer, the waiting handler held the consumer the reply needed:
with dispatch.workers=1 every nested call timed out, with 8 workers
8 concurrent nested calls stalled until their timeouts, and a full
queue dropped the reply.

Replies to publish_sync now have their own lane, a one-consumer
dispatcher that only decodes and routes them to the waiting call
(RFC-004 §9 amended): it never runs user handlers, so they cannot
starve it.

Contracts verified (with FakeBroker answering every request at once):
  - A handler's publish_sync returns at once with a single worker, and
    8 concurrent nested calls all return with 8 workers.
  - A reply is delivered while the dispatcher queue is full.
  - terminate() stops the reply lane; a pickled Agent carries none.
"""

import pickle
import threading
import time

import pytest

from agentflow.core.agent import Agent
from agentflow.core.parcel import TextParcel
from tests.fakes.fake_broker import FakeBroker, FakeWorker
from tests.fakes.fake_bus import FakeBus

xfail_7 = pytest.mark.xfail(strict=True, reason='issue #7: replies share the dispatcher')


def _agent(**dispatch):
    agent = Agent('coordinator', {'dispatch': {'shutdown_timeout_s': 1.0, **dispatch}})
    broker = FakeBroker(notifier=agent)
    agent._broker = broker
    agent._agent_worker = FakeWorker()
    broker.auto_respond_with('pong')        # every request is answered at once
    return agent, broker


def _forwarding_handler(agent, results, timeout=3.0):
    """A Loading Coordinator: forwards each request and records the
    answer, or the exception, and how long it took."""
    def handle(topic, pcl):
        started = time.monotonic()
        try:
            outcome = agent.publish_sync('svc', pcl.content, timeout=timeout).content
        except Exception as ex:
            outcome = ex
        results.append((outcome, time.monotonic() - started))
    return handle


@xfail_7
def test_a_handler_can_publish_sync_with_a_single_worker():
    agent, broker = _agent(workers=1)
    results = []
    agent.subscribe('trigger', topic_handler=_forwarding_handler(agent, results))
    try:
        broker.deliver('trigger', TextParcel('q').payload())
        assert FakeBus.wait_until(lambda: results, timeout=5)
        [(outcome, elapsed)] = results
        assert outcome == 'pong' and elapsed < 1.0
    finally:
        agent.terminate()


@xfail_7
def test_eight_concurrent_nested_calls_all_return_with_eight_workers():
    agent, broker = _agent(workers=8)
    results = []
    entered = threading.Barrier(8, timeout=5)

    def handle(topic, pcl):
        entered.wait()                      # all 8 consumers are busy
        _forwarding_handler(agent, results)(topic, pcl)
    agent.subscribe('trigger', topic_handler=handle)
    try:
        for i in range(8):
            broker.deliver('trigger', TextParcel(i).payload())
        assert FakeBus.wait_until(lambda: len(results) == 8, timeout=5)
        assert all(outcome == 'pong' and elapsed < 1.0 for outcome, elapsed in results), results
    finally:
        agent.terminate()


@xfail_7
def test_a_reply_is_delivered_while_the_dispatcher_queue_is_full():
    agent, broker = _agent(workers=1, queue_capacity=1)
    gate, busy = threading.Event(), threading.Event()

    def block(topic, pcl):
        busy.set()
        gate.wait(5)
    agent.subscribe('busy', topic_handler=block)
    try:
        broker.deliver('busy', TextParcel('b').payload())
        assert busy.wait(2)
        broker.deliver('busy', TextParcel('queued').payload())     # the queue is full
        assert agent.publish_sync('svc', 'q', timeout=2).content == 'pong'
    finally:
        gate.set()
        agent.terminate()


@xfail_7
def test_terminate_stops_the_reply_lane():
    agent, broker = _agent()
    assert agent.publish_sync('svc', 'q', timeout=2).content == 'pong'
    lane = agent._reply_dispatcher
    agent.terminate()
    assert lane.is_stopped


@xfail_7
def test_a_pickled_agent_carries_no_reply_lane():
    agent, broker = _agent()
    try:
        assert agent.publish_sync('svc', 'q', timeout=2).content == 'pong'
        clone = pickle.loads(pickle.dumps(agent))
        assert clone._reply_dispatcher is None
    finally:
        agent.terminate()
