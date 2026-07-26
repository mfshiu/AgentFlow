"""Characterization tests for Agent._on_message per-message thread
creation (Risk R-04 in docs/audit/05-risk-register.md).

Agent._on_message (agent.py) spawns one fresh threading.Thread per
received message via:

    threading.Thread(target=handle_message, args=(...)).start()

This test file documents:

  - Every message spawns a NEW Thread (no pool, no reuse).
  - Bounded stress runs of 100/500/1000 messages each spawn N threads.
  - Slow handlers cause active-thread count to grow with N (unbounded).
  - The framework holds no reference to spawned threads; Agent.terminate
    does not wait for handler threads to complete.
  - Handler threads inherit non-daemon status (block interpreter exit).
  - Handler exceptions terminate the spawned thread cleanly.
  - Delivery order is not enforced across threads.
  - Multiple messages on the same topic execute concurrently.
  - Shared mutable state accessed inside handlers is exposed to the
    Python GIL without additional framework locking.
  - Auto-reply publish runs on the same handler thread.

Constraints:
  - No real MQTT / socket / ProcessWorker.
  - All stress tests have bounded upper limits.
  - No modification of Agent.
  - No introduction of a ThreadPoolExecutor.
"""

import threading as real_threading
import time
import types
from typing import List

import pytest

from agentflow.core.agent import Agent
from agentflow.core.parcel import Parcel, TextParcel

from tests.fakes.fake_broker import FakeBroker, FakeWorker


# --------------------------------------------------------------------------
# Fixtures + helpers
# --------------------------------------------------------------------------

@pytest.fixture
def agent_with_fake_broker():
    a = Agent(name='test_r04', agent_config={})
    b = FakeBroker(notifier=a)
    a._broker = b
    a._agent_worker = FakeWorker()
    return a, b


@pytest.fixture
def thread_recorder(monkeypatch):
    """Replace agentflow.core.agent.threading with a proxy module whose
    only override is Thread → RecordingThread. All other threading
    attributes (Event, Lock, Barrier, ...) delegate to the real module,
    and no test outside agent.py is affected."""
    from agentflow.core import agent as agent_mod

    recorded: List[real_threading.Thread] = []
    lock = real_threading.Lock()

    class RecordingThread(real_threading.Thread):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            with lock:
                recorded.append(self)

    proxy = types.ModuleType('threading_proxy_for_agent_r04')
    for name in dir(real_threading):
        setattr(proxy, name, getattr(real_threading, name))
    proxy.Thread = RecordingThread

    monkeypatch.setattr(agent_mod, 'threading', proxy)

    class Recorder:
        threads = recorded

        @staticmethod
        def count() -> int:
            with lock:
                return len(recorded)

        @staticmethod
        def alive_count() -> int:
            with lock:
                return sum(1 for t in recorded if t.is_alive())

        @staticmethod
        def wait_all(timeout: float = 10.0) -> bool:
            deadline = time.monotonic() + timeout
            for t in list(recorded):
                remaining = max(0.0, deadline - time.monotonic())
                t.join(remaining)
            return not any(t.is_alive() for t in recorded)

    return Recorder


def _wait_until(predicate, timeout: float = 1.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.002)
    return False


# ==========================================================================
# 1. Every message spawns a new thread
# ==========================================================================

def test_every_message_spawns_one_new_thread(
    agent_with_fake_broker, thread_recorder,
):
    """Each broker delivery spawns a distinct threading.Thread OBJECT.
    (OS-level thread IDs can be recycled when threads exit quickly, so
    we assert on the identity of the Thread instances rather than on
    threading.get_ident() values.)"""
    agent, broker = agent_with_fake_broker
    counter = {'v': 0}
    counter_lock = real_threading.Lock()
    main_tid = real_threading.get_ident()
    handler_tids: List[int] = []
    handler_tids_lock = real_threading.Lock()

    def h(topic, pcl):
        with handler_tids_lock:
            handler_tids.append(real_threading.get_ident())
        with counter_lock:
            counter['v'] += 1

    agent.subscribe('T', topic_handler=h)
    for i in range(5):
        broker.deliver('T', TextParcel(f'msg-{i}').payload())

    ok = _wait_until(lambda: counter['v'] == 5, timeout=1.0)
    assert ok
    thread_recorder.wait_all(1.0)

    # Five Thread OBJECTS were created — one per delivery.
    assert thread_recorder.count() == 5
    assert len({id(t) for t in thread_recorder.threads}) == 5
    # No handler ran on the main test thread.
    assert main_tid not in handler_tids


# ==========================================================================
# 2. 100 / 500 / 1000 messages → N threads
# ==========================================================================

@pytest.mark.parametrize('n', [100, 500, 1000])
def test_N_messages_create_N_threads(
    n, agent_with_fake_broker, thread_recorder,
):
    agent, broker = agent_with_fake_broker
    counter_lock = real_threading.Lock()
    counter = {'v': 0}

    def h(topic, pcl):
        with counter_lock:
            counter['v'] += 1

    agent.subscribe('T', topic_handler=h)
    for i in range(n):
        broker.deliver('T', TextParcel(f'{i}').payload())

    ok = _wait_until(lambda: counter['v'] == n, timeout=15.0)
    assert ok, f'only {counter["v"]}/{n} handlers completed'
    thread_recorder.wait_all(5.0)
    assert thread_recorder.count() == n, (
        f'expected {n} threads created; got {thread_recorder.count()}'
    )


# ==========================================================================
# 3. Slow handler → active-thread count grows with N
# ==========================================================================

@pytest.mark.parametrize('n', [10, 50, 100])
def test_slow_handler_active_thread_count_scales_with_n(
    n, agent_with_fake_broker, thread_recorder,
):
    agent, broker = agent_with_fake_broker
    release = real_threading.Event()

    def slow_handler(topic, pcl):
        release.wait(timeout=5.0)

    agent.subscribe('T', topic_handler=slow_handler)
    for i in range(n):
        broker.deliver('T', TextParcel(f'{i}').payload())

    # Wait for all handler threads to start blocking.
    ok = _wait_until(lambda: thread_recorder.alive_count() >= n, timeout=3.0)
    assert ok, (
        f'expected >= {n} alive threads; got {thread_recorder.alive_count()}'
    )
    # Framework has no upper bound: alive threads == N (all in flight).
    assert thread_recorder.alive_count() == n
    assert thread_recorder.count() == n

    # Release; every thread should die.
    release.set()
    ok = _wait_until(lambda: thread_recorder.alive_count() == 0, timeout=3.0)
    assert ok


# ==========================================================================
# 4. Agent.terminate does NOT wait for handler threads
# ==========================================================================

def test_agent_terminate_does_not_join_handler_threads(
    agent_with_fake_broker, thread_recorder,
):
    agent, broker = agent_with_fake_broker
    started = real_threading.Event()
    release = real_threading.Event()

    def slow_handler(topic, pcl):
        started.set()
        release.wait(timeout=5.0)

    agent.subscribe('T', topic_handler=slow_handler)
    broker.deliver('T', TextParcel('msg').payload())
    assert started.wait(1.0)

    handler_thread = thread_recorder.threads[-1]
    assert handler_thread.is_alive()

    t0 = time.monotonic()
    agent.terminate()  # calls FakeWorker.stop() — no-op
    elapsed = time.monotonic() - t0

    # terminate did NOT wait for the handler thread.
    assert elapsed < 0.2, f'terminate blocked for {elapsed:.3f}s'
    assert handler_thread.is_alive(), (
        'agent.terminate() must not have joined handler threads'
    )

    release.set()
    handler_thread.join(1.0)
    assert not handler_thread.is_alive()


# ==========================================================================
# 5. Handler thread is not daemon
# ==========================================================================

def test_handler_thread_is_not_daemon(agent_with_fake_broker, thread_recorder):
    agent, broker = agent_with_fake_broker
    started = real_threading.Event()
    release = real_threading.Event()

    def h(topic, pcl):
        started.set()
        release.wait(timeout=5.0)

    agent.subscribe('T', topic_handler=h)
    broker.deliver('T', TextParcel('msg').payload())
    assert started.wait(1.0)

    handler_thread = thread_recorder.threads[-1]
    # Non-daemon threads block interpreter exit until they finish;
    # daemon threads do not. Agent does not set daemon=True.
    assert handler_thread.daemon is False, (
        'handler thread is non-daemon (blocks process exit)'
    )
    release.set()


# ==========================================================================
# 6. Handler exception terminates the thread cleanly
# ==========================================================================

def test_handler_exception_terminates_thread_cleanly(
    agent_with_fake_broker, thread_recorder,
):
    agent, broker = agent_with_fake_broker
    entered = real_threading.Event()

    def raising_handler(topic, pcl):
        entered.set()
        raise RuntimeError('handler exploded')

    agent.subscribe('T', topic_handler=raising_handler)
    broker.deliver('T', TextParcel('req').payload())
    assert entered.wait(1.0)

    handler_thread = thread_recorder.threads[-1]
    handler_thread.join(1.0)
    assert not handler_thread.is_alive(), (
        'handler thread should have terminated after exception'
    )


def test_handler_exception_does_not_leak_thread_when_auto_reply_active(
    agent_with_fake_broker, thread_recorder,
):
    """RFC-002 / RFC-003: auto-reply path with a raising handler still
    lets the thread complete after the error echo is published."""
    agent, broker = agent_with_fake_broker

    def raising(topic, pcl):
        raise RuntimeError('boom')

    agent.subscribe('T', topic_handler=raising)
    broker.deliver('T', TextParcel('req', topic_return='R').payload())

    ok = _wait_until(
        lambda: any(t == 'R' for (t, _p) in broker.publish_calls),
        timeout=1.0,
    )
    assert ok
    handler_thread = thread_recorder.threads[-1]
    handler_thread.join(1.0)
    assert not handler_thread.is_alive()


# ==========================================================================
# 7. Message processing order is NOT guaranteed
# ==========================================================================

def test_message_processing_order_across_threads_is_not_guaranteed(
    agent_with_fake_broker, thread_recorder,
):
    agent, broker = agent_with_fake_broker
    completion_order: List[int] = []
    completion_lock = real_threading.Lock()
    # Per-message events so the test can control completion order.
    releases = {i: real_threading.Event() for i in range(3)}

    def h(topic, pcl):
        msg_id = int(pcl.content)
        releases[msg_id].wait(timeout=5.0)
        with completion_lock:
            completion_order.append(msg_id)

    agent.subscribe('T', topic_handler=h)
    for i in range(3):
        broker.deliver('T', TextParcel(str(i)).payload())

    # Wait until all 3 handlers have spawned and are blocking.
    ok = _wait_until(lambda: thread_recorder.alive_count() >= 3, timeout=2.0)
    assert ok

    # Release in reverse order of delivery.
    releases[2].set()
    releases[0].set()
    releases[1].set()

    ok = _wait_until(lambda: len(completion_order) == 3, timeout=2.0)
    assert ok
    # Framework provides no ordering guarantee; the completion order
    # matches the release order, NOT the delivery order [0, 1, 2].
    assert completion_order != [0, 1, 2]
    assert set(completion_order) == {0, 1, 2}


# ==========================================================================
# 8. Same topic, multiple messages → concurrent execution
# ==========================================================================

def test_same_topic_multiple_messages_execute_concurrently(
    agent_with_fake_broker, thread_recorder,
):
    agent, broker = agent_with_fake_broker
    N = 8
    # Barrier requires all N handlers PLUS the main thread; if any
    # handler is serialized instead of concurrent, the barrier deadlocks
    # and BrokenBarrierError is raised on timeout.
    barrier = real_threading.Barrier(N + 1, timeout=3.0)

    def h(topic, pcl):
        barrier.wait()

    agent.subscribe('T', topic_handler=h)
    for i in range(N):
        broker.deliver('T', TextParcel(str(i)).payload())

    # Main thread joins the barrier; success means all N handlers were
    # concurrently in flight.
    barrier.wait()
    thread_recorder.wait_all(2.0)


# ==========================================================================
# 9. Shared mutable state is exposed under GIL without framework locking
# ==========================================================================

def test_framework_provides_no_lock_around_handler_dispatch(
    agent_with_fake_broker, thread_recorder,
):
    """Positive proof that handlers run concurrently without any
    framework-provided synchronization: N threads simultaneously
    write into a shared list via the GIL-atomic list.append."""
    agent, broker = agent_with_fake_broker
    N = 10
    shared: List[int] = []
    barrier = real_threading.Barrier(N + 1, timeout=3.0)

    def h(topic, pcl):
        barrier.wait()  # force all N to run in parallel
        # list.append is atomic under GIL; count remains correct.
        shared.append(int(pcl.content))

    agent.subscribe('T', topic_handler=h)
    for i in range(N):
        broker.deliver('T', TextParcel(str(i)).payload())

    barrier.wait()
    ok = _wait_until(lambda: len(shared) == N, timeout=2.0)
    assert ok
    # Atomic op → all N recorded. Non-atomic ops (e.g. `+= 1` on int)
    # would race; the framework provides no protection either way.
    assert sorted(shared) == list(range(N))


# ==========================================================================
# 10. Auto-reply publish runs on the handler thread
# ==========================================================================

def test_auto_reply_publish_runs_on_the_handler_thread(
    agent_with_fake_broker, thread_recorder,
):
    agent, broker = agent_with_fake_broker
    handler_tids: List[int] = []
    publish_tids: List[int] = []

    def h(topic, pcl):
        handler_tids.append(real_threading.get_ident())
        return 'reply'

    original_publish = broker.publish

    def spy_publish(topic, payload):
        publish_tids.append(real_threading.get_ident())
        return original_publish(topic, payload)

    broker.publish = spy_publish

    agent.subscribe('T', topic_handler=h)
    broker.deliver('T', TextParcel('req', topic_return='R').payload())

    ok = _wait_until(lambda: publish_tids, timeout=1.0)
    assert ok
    # The auto-reply's broker.publish call ran on the SAME thread as
    # the handler — i.e. handle_message inlines the publish, meaning
    # a slow broker.publish (in real MQTT) would block the handler
    # thread.
    assert handler_tids[0] == publish_tids[-1]
    # And that thread is NOT the test's main thread.
    assert real_threading.get_ident() != handler_tids[0]


# ==========================================================================
# Supplementary: thread count == number of _on_message dispatches
# ==========================================================================

def test_each_broker_deliver_causes_exactly_one_handler_thread(
    agent_with_fake_broker, thread_recorder,
):
    """One broker.deliver() call ↔ one Agent._on_message ↔ one
    Thread. No pooling, no batching, no coalescing."""
    agent, broker = agent_with_fake_broker
    counter = {'v': 0}
    counter_lock = real_threading.Lock()

    def h(topic, pcl):
        with counter_lock:
            counter['v'] += 1

    agent.subscribe('T', topic_handler=h)
    for i in range(7):
        broker.deliver('T', TextParcel(f'{i}').payload())
        # Between deliveries, publish_calls does not change (deliver
        # goes through notifier, not through broker.publish).
    ok = _wait_until(lambda: counter['v'] == 7, timeout=1.0)
    assert ok
    thread_recorder.wait_all(1.0)
    assert thread_recorder.count() == 7
