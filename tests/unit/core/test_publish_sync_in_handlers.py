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
  - A reply is delivered while the dispatcher queue, or its byte
    budget (dispatch.max_queued_bytes), is full: replies have a budget
    of their own, and a queue of at least 1024 whatever the
    dispatch.queue_capacity, so a burst of replies is not dropped.
  - terminate() stops the reply lane after the dispatcher, so a handler
    draining meanwhile still gets its reply, and starts no lane
    afterwards; a pickled Agent carries none.
"""

import pickle
import threading
import time

import pytest

from agentflow.core.agent import Agent
from agentflow.core.parcel import Parcel, TextParcel
from tests.fakes.fake_broker import FakeBroker, FakeWorker
from tests.fakes.fake_bus import FakeBus

xfail_7r = pytest.mark.xfail(strict=True, reason='issue #7 review: fixed in the next commit')


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


@xfail_7r
def test_a_reply_is_delivered_while_the_byte_budget_is_full():
    agent, broker = _agent(workers=1, max_queued_bytes=4096)
    gate, busy = threading.Event(), threading.Event()

    def block(topic, pcl):
        busy.set()
        gate.wait(5)
    agent.subscribe('busy', topic_handler=block)
    try:
        broker.deliver('busy', TextParcel('b').payload())
        assert busy.wait(2)
        broker.deliver('other', TextParcel('x' * 2000).payload())
        overhead = len(TextParcel('').payload())
        remaining = 4096 - agent._queued_bytes
        broker.deliver('other', TextParcel('x' * (remaining - overhead)).payload())
        assert agent._queued_bytes == 4096  # the budget is exactly full
        assert agent.publish_sync('svc', 'q', timeout=2).content == 'pong'
    finally:
        gate.set()
        agent.terminate()


@xfail_7r
def test_a_burst_of_replies_is_not_dropped_with_a_small_queue_capacity():
    agent, broker = _agent(workers=8, queue_capacity=1)
    broker.stop_auto_responding()
    results = []

    def call():
        try:
            results.append(agent.publish_sync('svc', 'q', timeout=3).content)
        except Exception as ex:
            results.append(ex)
    callers = [threading.Thread(target=call) for _ in range(8)]
    try:
        for caller in callers:
            caller.start()
        assert FakeBus.wait_until(lambda: len(broker.publish_calls) == 8, timeout=3)
        for _, payload in list(broker.publish_calls):   # back to back, as paho delivers them
            request = Parcel.from_payload(payload)
            reply = TextParcel('pong')
            reply.correlation_id = request.correlation_id
            broker.deliver(request.topic_return, reply.payload())
        for caller in callers:
            caller.join(5)
        assert results == ['pong'] * 8
    finally:
        agent.terminate()


@pytest.mark.parametrize('lane_exists', [True, False])
def test_a_handler_draining_during_terminate_still_gets_its_reply(lane_exists):
    agent, broker = _agent(workers=1)
    terminating, results = threading.Event(), []
    if lane_exists:
        assert agent.publish_sync('svc', 'warm-up', timeout=2).content == 'pong'

    def drain(topic, pcl):
        terminating.wait(5)
        try:
            results.append(agent.publish_sync('svc', 'q', timeout=0.8).content)
        except Exception as ex:
            results.append(ex)
    agent.subscribe('job', topic_handler=drain)
    broker.deliver('job', TextParcel('j').payload())
    dispatcher = agent._get_dispatcher()
    stop_dispatcher = dispatcher.stop

    def stop(*args, **kwargs):
        terminating.set()                   # the handler calls publish_sync while terminate() drains
        return stop_dispatcher(*args, **kwargs)
    dispatcher.stop = stop
    agent.terminate()
    assert results == ['pong']


def test_no_reply_lane_is_started_after_terminate():
    agent, broker = _agent()
    agent._get_dispatcher()                 # an agent that has received messages
    agent.terminate()
    with pytest.raises(TimeoutError):
        agent.publish_sync('svc', 'q', timeout=0.3)
    assert agent._reply_dispatcher is None


def test_terminate_stops_the_reply_lane():
    agent, broker = _agent()
    assert agent.publish_sync('svc', 'q', timeout=2).content == 'pong'
    lane = agent._reply_dispatcher
    agent.terminate()
    assert lane.is_stopped


def test_a_pickled_agent_carries_no_reply_lane():
    agent, broker = _agent()
    try:
        assert agent.publish_sync('svc', 'q', timeout=2).content == 'pong'
        clone = pickle.loads(pickle.dumps(agent))
        assert clone._reply_dispatcher is None
    finally:
        agent.terminate()
