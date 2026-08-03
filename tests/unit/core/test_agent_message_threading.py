"""Tests for RFC-004 bounded message dispatch (Risk R-04).

Post-RFC-004:

  - Agent._on_message enqueues a task onto a bounded queue.
  - A fixed pool of daemon consumer threads drains the queue.
  - Queue full → drop_newest with metric increment + rate-limited warn.
  - Agent.terminate() calls dispatcher.stop() which drains within a
    bounded shutdown_timeout_s deadline. Idempotent.
  - Legacy per_message_thread mode remains available (deprecated) for
    one release cycle; it emits a DeprecationWarning at Agent init.

RFC-003 auto-reply contract is preserved: the dispatcher runs the
same handle_message body that used to run on a per-message Thread.
"""

import threading as real_threading
import time
import types
import warnings
from typing import Any, Dict, List

import pytest

from agentflow.core.agent import Agent
from agentflow.core.dispatcher import MessageDispatcher, LegacyPerMessageDispatcher
from agentflow.core.parcel import Parcel, TextParcel

from tests.fakes.fake_broker import FakeBroker, FakeWorker


# --------------------------------------------------------------------------
# Fixtures + helpers
# --------------------------------------------------------------------------

def _make_agent(dispatch_cfg=None):
    cfg: Dict[str, Any] = {}
    if dispatch_cfg is not None:
        cfg['dispatch'] = dispatch_cfg
    a = Agent(name='test_r04', agent_config=cfg)
    b = FakeBroker(notifier=a)
    a._broker = b
    a._agent_worker = FakeWorker()
    return a, b


@pytest.fixture
def agent_with_fake_broker():
    a, b = _make_agent()
    try:
        yield a, b
    finally:
        a.terminate()


@pytest.fixture
def thread_recorder(monkeypatch):
    """Replace agentflow.core.agent.threading AND
    agentflow.core.dispatcher.threading with proxy modules whose only
    override is Thread → RecordingThread. All other threading
    attributes delegate to the real module."""
    from agentflow.core import agent as agent_mod
    from agentflow.core import dispatcher as dispatcher_mod

    recorded: List[real_threading.Thread] = []
    lock = real_threading.Lock()

    class RecordingThread(real_threading.Thread):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            with lock:
                recorded.append(self)

    def _install(module):
        proxy = types.ModuleType(f'threading_proxy_for_{module.__name__}')
        for name in dir(real_threading):
            setattr(proxy, name, getattr(real_threading, name))
        proxy.Thread = RecordingThread
        monkeypatch.setattr(module, 'threading', proxy)

    _install(agent_mod)
    _install(dispatcher_mod)

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
# Bounded dispatcher: fixed worker pool
# ==========================================================================

def test_dispatcher_is_created_lazily_on_first_message(agent_with_fake_broker):
    agent, broker = agent_with_fake_broker
    assert agent._dispatcher is None, 'dispatcher must not be built at __init__'
    broker.deliver('T', TextParcel('m').payload())
    assert agent._dispatcher is not None
    assert isinstance(agent._dispatcher, MessageDispatcher)


def test_1000_slow_messages_do_not_create_1000_threads(
    agent_with_fake_broker, thread_recorder,
):
    """RFC-004 core: N messages must NOT create N Threads."""
    agent, broker = agent_with_fake_broker
    release = real_threading.Event()

    def slow_handler(topic, pcl):
        release.wait(timeout=5.0)

    agent.subscribe('T', topic_handler=slow_handler)
    for i in range(1000):
        broker.deliver('T', TextParcel(str(i)).payload())

    # Only the dispatcher's fixed worker pool was created (default 8).
    # No per-message Thread objects.
    assert thread_recorder.count() == 8
    release.set()


def test_active_workers_never_exceeds_configured_max(thread_recorder):
    a, broker = _make_agent(dispatch_cfg={'workers': 4, 'queue_capacity': 32})
    try:
        release = real_threading.Event()

        def slow(topic, pcl):
            release.wait(timeout=5.0)

        a.subscribe('T', topic_handler=slow)
        for _ in range(20):
            broker.deliver('T', TextParcel('m').payload())

        ok = _wait_until(
            lambda: a._dispatcher.active_workers >= 4, timeout=2.0,
        )
        assert ok
        # Never exceeds the configured cap.
        assert a._dispatcher.active_workers == 4
        assert thread_recorder.count() == 4
        release.set()
    finally:
        a.terminate()


def test_workers_are_daemon_threads(agent_with_fake_broker, thread_recorder):
    """RFC-004 §7.7: workers are daemon as a process-exit safety net."""
    agent, broker = agent_with_fake_broker
    broker.deliver('T', TextParcel('m').payload())
    time.sleep(0.05)  # let dispatcher init complete
    assert thread_recorder.count() == 8
    assert all(t.daemon for t in thread_recorder.threads)


# ==========================================================================
# Bounded queue + drop_newest overflow policy
# ==========================================================================

def test_queue_depth_never_exceeds_capacity():
    a, broker = _make_agent(dispatch_cfg={'workers': 1, 'queue_capacity': 5})
    try:
        release = real_threading.Event()

        def slow(topic, pcl):
            release.wait(timeout=5.0)

        a.subscribe('T', topic_handler=slow)
        for _ in range(20):
            broker.deliver('T', TextParcel('m').payload())

        # 1 in flight + 5 in queue; the rest dropped.
        assert a._dispatcher.queue_depth <= 5
        release.set()
    finally:
        a.terminate()


def test_queue_full_drops_newest_and_increments_metric():
    a, broker = _make_agent(dispatch_cfg={'workers': 1, 'queue_capacity': 3})
    try:
        release = real_threading.Event()
        started = real_threading.Event()
        processed = []

        def slow(topic, pcl):
            started.set()
            release.wait(timeout=5.0)
            processed.append(pcl.content)

        a.subscribe('T', topic_handler=slow)
        # First delivery starts the handler and consumes the slot.
        broker.deliver('T', TextParcel('first').payload())
        assert started.wait(1.0)
        # Fill the queue (3 slots).
        broker.deliver('T', TextParcel('q1').payload())
        broker.deliver('T', TextParcel('q2').payload())
        broker.deliver('T', TextParcel('q3').payload())
        # These MUST be dropped (queue full).
        for i in range(5):
            broker.deliver('T', TextParcel(f'drop-{i}').payload())

        snap = a._dispatcher.metrics_snapshot()
        assert snap['queue_depth'] == 3
        assert snap['dropped_message_count'] == 5

        release.set()
        # Wait for consumer to drain the queue.
        ok = _wait_until(lambda: len(processed) == 4, timeout=2.0)
        assert ok
        # First 4 processed; the 5 dropped were never handled.
        assert 'first' in processed
        assert all(p.startswith('drop-') is False for p in processed)
    finally:
        a.terminate()


def test_dropped_count_matches_number_of_dropped_deliveries():
    a, broker = _make_agent(dispatch_cfg={'workers': 1, 'queue_capacity': 2})
    try:
        release = real_threading.Event()
        started = real_threading.Event()

        def slow(topic, pcl):
            started.set()
            release.wait(timeout=5.0)

        a.subscribe('T', topic_handler=slow)
        broker.deliver('T', TextParcel('go').payload())
        assert started.wait(1.0)
        for _ in range(2):  # fill capacity
            broker.deliver('T', TextParcel('q').payload())
        for _ in range(17):  # every one must be dropped
            broker.deliver('T', TextParcel('d').payload())

        assert a._dispatcher.dropped_message_count == 17
        release.set()
    finally:
        a.terminate()


def test_broker_callback_never_raises_on_queue_full():
    """RFC-004 §7.3 broker-callback safety invariant: queue.Full must
    NEVER escape to the paho loop thread."""
    a, broker = _make_agent(dispatch_cfg={'workers': 1, 'queue_capacity': 1})
    try:
        release = real_threading.Event()
        started = real_threading.Event()

        def slow(topic, pcl):
            started.set()
            release.wait(timeout=5.0)

        a.subscribe('T', topic_handler=slow)
        broker.deliver('T', TextParcel('go').payload())
        assert started.wait(1.0)
        # Fill queue.
        broker.deliver('T', TextParcel('q1').payload())
        # Now every further delivery would hit queue.Full inside enqueue.
        # broker.deliver invokes agent._on_message on the current thread
        # (simulating the paho loop). It MUST NOT raise.
        for i in range(10):
            broker.deliver('T', TextParcel(f'drop-{i}').payload())  # no raise

        assert a._dispatcher.dropped_message_count == 10
        release.set()
    finally:
        a.terminate()


# ==========================================================================
# Handler exception isolation (RFC-004 §7.10)
# ==========================================================================

def test_handler_exception_does_not_kill_consumer(agent_with_fake_broker):
    """Two-layer exception isolation:
      - handle_message (RFC-003) catches the handler's Exception first,
        so the task itself returns normally and the dispatcher counts
        it as processed.
      - Even if a rogue task raised out to the dispatcher, the
        consumer's try/except Exception (RFC-004 §7.10) would still
        keep it alive.

    This test verifies the consumer keeps processing after a raising
    handler. Dispatcher-level error_count is exercised separately in
    `test_dispatcher_counts_task_exceptions_from_direct_enqueue`."""
    agent, broker = agent_with_fake_broker
    processed = []

    def sometimes_raising(topic, pcl):
        if pcl.content == 'boom':
            raise RuntimeError('handler boom')
        processed.append(pcl.content)

    agent.subscribe('T', topic_handler=sometimes_raising)
    broker.deliver('T', TextParcel('boom').payload())
    broker.deliver('T', TextParcel('after-boom-1').payload())
    broker.deliver('T', TextParcel('after-boom-2').payload())

    ok = _wait_until(lambda: len(processed) == 2, timeout=1.0)
    assert ok
    assert processed == ['after-boom-1', 'after-boom-2']
    # All three tasks returned normally from the dispatcher's viewpoint
    # (handle_message caught the RuntimeError internally).
    snap = agent._dispatcher.metrics_snapshot()
    assert snap['processed_count'] == 3
    assert snap['error_count'] == 0


def test_dispatcher_counts_task_exceptions_from_direct_enqueue():
    """Dispatcher-level error_count fires when the enqueued task itself
    raises (i.e. bypasses handle_message's inner try/except)."""
    d = MessageDispatcher(workers=1, queue_capacity=4, shutdown_timeout_s=1.0,
                          name='err-count-test')
    try:
        done = real_threading.Event()

        def raising_task():
            raise RuntimeError('task-level boom')

        def marker_task():
            done.set()

        assert d.enqueue(raising_task, topic='X') is True
        assert d.enqueue(marker_task, topic='X') is True
        assert done.wait(1.0)  # consumer survived and ran the next task
        assert d.error_count == 1
        assert d.processed_count == 1  # marker_task
    finally:
        d.stop(timeout_s=1.0)


def test_dispatcher_metrics_snapshot_is_consistent():
    """metrics_snapshot returns a dict sampled under one lock."""
    d = MessageDispatcher(workers=2, queue_capacity=4, shutdown_timeout_s=1.0,
                          name='snap-test')
    try:
        snap = d.metrics_snapshot()
        assert set(snap.keys()) == {
            'active_workers', 'queue_depth', 'dropped_message_count',
            'rejected_after_stop_count', 'processed_count', 'error_count',
        }
        assert snap['active_workers'] == 0
        assert snap['queue_depth'] == 0
    finally:
        d.stop(timeout_s=1.0)


# ==========================================================================
# Graceful shutdown  (RFC-004 §7.5 – §7.7)
# ==========================================================================

def test_graceful_shutdown_drains_completable_tasks():
    a, broker = _make_agent(dispatch_cfg={
        'workers': 2, 'queue_capacity': 50, 'shutdown_timeout_s': 3.0,
    })
    try:
        processed = []
        lock = real_threading.Lock()

        def quick(topic, pcl):
            with lock:
                processed.append(pcl.content)

        a.subscribe('T', topic_handler=quick)
        for i in range(20):
            broker.deliver('T', TextParcel(f'm-{i}').payload())

        # Terminate — must drain all 20 within the 3-second budget.
        result = a._dispatcher.stop(timeout_s=3.0)
        assert result is True
        assert len(processed) == 20
        snap = a._dispatcher.metrics_snapshot()
        assert snap['processed_count'] == 20
        assert snap['queue_depth'] == 0
    finally:
        a.terminate()  # idempotent


def test_shutdown_timeout_is_bounded():
    a, broker = _make_agent(dispatch_cfg={
        'workers': 1, 'queue_capacity': 4, 'shutdown_timeout_s': 0.1,
    })
    try:
        release = real_threading.Event()

        def wedged(topic, pcl):
            release.wait(timeout=10.0)

        a.subscribe('T', topic_handler=wedged)
        broker.deliver('T', TextParcel('m').payload())
        # Give the consumer a moment to start the wedged handler.
        time.sleep(0.02)

        start = time.monotonic()
        result = a._dispatcher.stop(timeout_s=0.1)
        elapsed = time.monotonic() - start

        assert elapsed < 0.6, f'stop() blocked for {elapsed:.3f}s'
        assert result is False  # straggler
        release.set()
    finally:
        a.terminate()


def test_shutdown_is_idempotent():
    a, broker = _make_agent()
    try:
        broker.deliver('T', TextParcel('m').payload())
        time.sleep(0.05)

        first = a._dispatcher.stop(timeout_s=1.0)
        assert first is True

        # Second stop returns the cached first-call outcome quickly.
        start = time.monotonic()
        second = a._dispatcher.stop(timeout_s=5.0)
        elapsed = time.monotonic() - start
        assert second is True
        assert elapsed < 0.05

        # Third invocation via Agent.terminate() also safe.
        a.terminate()
    finally:
        pass


def test_agent_terminate_can_be_called_twice(agent_with_fake_broker):
    agent, broker = agent_with_fake_broker
    broker.deliver('T', TextParcel('m').payload())
    time.sleep(0.05)

    agent.terminate()
    agent.terminate()  # must not raise, must not regress metrics


def test_post_shutdown_enqueue_is_rejected():
    a, broker = _make_agent()
    try:
        processed = []

        def h(topic, pcl):
            processed.append(pcl.content)

        a.subscribe('T', topic_handler=h)
        broker.deliver('T', TextParcel('pre').payload())
        _wait_until(lambda: len(processed) == 1, timeout=1.0)

        a._dispatcher.stop(timeout_s=1.0)

        # Any delivery after stop is rejected — handler MUST NOT run.
        broker.deliver('T', TextParcel('post-1').payload())
        broker.deliver('T', TextParcel('post-2').payload())
        time.sleep(0.05)

        assert processed == ['pre']
        snap = a._dispatcher.metrics_snapshot()
        assert snap['rejected_after_stop_count'] == 2
    finally:
        a.terminate()


def test_post_shutdown_broker_delivery_does_not_raise(agent_with_fake_broker):
    """The paho loop calling _on_message after stop must not raise."""
    agent, broker = agent_with_fake_broker
    broker.deliver('T', TextParcel('m').payload())
    time.sleep(0.05)
    agent._dispatcher.stop(timeout_s=1.0)
    # No pytest.raises: any propagation is a failure.
    broker.deliver('T', TextParcel('post').payload())


# ==========================================================================
# Auto-reply preserved on consumer thread
# ==========================================================================

def test_auto_reply_still_runs_on_consumer_thread(agent_with_fake_broker):
    agent, broker = agent_with_fake_broker
    main_tid = real_threading.get_ident()
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
    assert handler_tids[0] == publish_tids[-1]
    assert publish_tids[-1] != main_tid


# ==========================================================================
# Legacy per_message_thread mode
# ==========================================================================

def test_legacy_mode_emits_deprecation_warning_at_agent_init():
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter('always')
        Agent(name='legacy', agent_config={
            'dispatch': {'mode': 'per_message_thread'},
        })
    dep = [w for w in caught if issubclass(w.category, DeprecationWarning)]
    assert dep, 'expected a DeprecationWarning'
    assert any('RFC-004' in str(w.message) for w in dep)


def test_legacy_mode_preserves_per_message_thread_behaviour(thread_recorder):
    with warnings.catch_warnings():
        warnings.simplefilter('ignore', DeprecationWarning)
        a, broker = _make_agent(
            dispatch_cfg={'mode': 'per_message_thread'},
        )
    try:
        counter_lock = real_threading.Lock()
        counter = {'v': 0}

        def h(topic, pcl):
            with counter_lock:
                counter['v'] += 1

        a.subscribe('T', topic_handler=h)
        for i in range(50):
            broker.deliver('T', TextParcel(str(i)).payload())

        ok = _wait_until(lambda: counter['v'] == 50, timeout=5.0)
        assert ok
        thread_recorder.wait_all(2.0)
        # One Thread per message under legacy mode.
        assert thread_recorder.count() == 50
        # Dispatcher metrics remain zero in legacy mode (RFC-004 §7.13).
        assert isinstance(a._dispatcher, LegacyPerMessageDispatcher)
        assert a._dispatcher.dropped_message_count == 0
    finally:
        a.terminate()


def test_legacy_mode_terminate_returns_immediately(agent_with_fake_broker):
    with warnings.catch_warnings():
        warnings.simplefilter('ignore', DeprecationWarning)
        a, broker = _make_agent(
            dispatch_cfg={'mode': 'per_message_thread'},
        )
    try:
        broker.deliver('T', TextParcel('m').payload())
        start = time.monotonic()
        a.terminate()
        elapsed = time.monotonic() - start
        # Byte-for-byte pre-RFC-004: terminate does not wait for
        # handler threads.
        assert elapsed < 0.2, f'legacy terminate blocked {elapsed:.3f}s'
    finally:
        pass


# ==========================================================================
# Ordering / concurrency semantics preserved from R-04 characterization
# ==========================================================================

def test_message_processing_order_across_consumers_not_guaranteed(
    agent_with_fake_broker,
):
    """Same-topic ordering is NOT guaranteed under Option D. Stage
    releases with wait-for-completion so the resulting order is
    deterministic in the test (= release order), and different from
    delivery order [0, 1, 2]."""
    agent, broker = agent_with_fake_broker
    completion_order: List[int] = []
    completion_lock = real_threading.Lock()
    releases = {i: real_threading.Event() for i in range(3)}

    def h(topic, pcl):
        msg_id = int(pcl.content)
        releases[msg_id].wait(timeout=5.0)
        with completion_lock:
            completion_order.append(msg_id)

    agent.subscribe('T', topic_handler=h)
    for i in range(3):
        broker.deliver('T', TextParcel(str(i)).payload())

    ok = _wait_until(
        lambda: agent._dispatcher.active_workers >= 3, timeout=2.0,
    )
    assert ok

    # Stage releases with wait-until between each so completion_order
    # reflects release order (not delivery order or GIL scheduling).
    releases[2].set()
    assert _wait_until(lambda: completion_order == [2], timeout=1.0)
    releases[0].set()
    assert _wait_until(lambda: completion_order == [2, 0], timeout=1.0)
    releases[1].set()
    assert _wait_until(lambda: completion_order == [2, 0, 1], timeout=1.0)

    # completion order differs from delivery order: framework did not
    # enforce same-topic FIFO across consumers.
    assert completion_order != [0, 1, 2]
    assert set(completion_order) == {0, 1, 2}


def test_same_topic_multiple_messages_execute_concurrently_up_to_workers():
    a, broker = _make_agent(dispatch_cfg={'workers': 4, 'queue_capacity': 32})
    try:
        N = 4  # equal to workers, so Barrier can release
        barrier = real_threading.Barrier(N + 1, timeout=3.0)

        def h(topic, pcl):
            barrier.wait()

        a.subscribe('T', topic_handler=h)
        for i in range(N):
            broker.deliver('T', TextParcel(str(i)).payload())

        barrier.wait()  # if concurrency < N, deadlock → BrokenBarrierError
    finally:
        a.terminate()


# ==========================================================================
# Direct MessageDispatcher unit tests
# ==========================================================================

def test_dispatcher_enqueue_returns_false_after_stop():
    d = MessageDispatcher(workers=1, queue_capacity=4, shutdown_timeout_s=1.0,
                          name='post-stop-test')
    d.stop(timeout_s=1.0)
    assert d.enqueue(lambda: None, topic='X') is False
    assert d.rejected_after_stop_count == 1


def test_dispatcher_enqueue_returns_false_when_full():
    started = real_threading.Event()
    release = real_threading.Event()

    def blocking():
        started.set()
        release.wait(timeout=5.0)

    d = MessageDispatcher(workers=1, queue_capacity=2, shutdown_timeout_s=1.0,
                          name='full-test')
    try:
        assert d.enqueue(blocking, topic='X') is True
        assert started.wait(1.0)
        assert d.enqueue(lambda: None, topic='X') is True
        assert d.enqueue(lambda: None, topic='X') is True
        # Queue capacity exhausted — next enqueue drops.
        assert d.enqueue(lambda: None, topic='X') is False
        assert d.dropped_message_count == 1
        release.set()
    finally:
        d.stop(timeout_s=2.0)


def test_dispatcher_processed_count_matches_completions():
    d = MessageDispatcher(workers=2, queue_capacity=64, shutdown_timeout_s=2.0,
                          name='count-test')
    try:
        counter_lock = real_threading.Lock()
        counter = {'v': 0}

        def h():
            with counter_lock:
                counter['v'] += 1

        for _ in range(30):
            assert d.enqueue(h, topic='T') is True
        ok = _wait_until(lambda: counter['v'] == 30, timeout=2.0)
        assert ok
    finally:
        d.stop(timeout_s=2.0)
    assert d.processed_count == 30
    assert d.error_count == 0


# ==========================================================================
# stop / enqueue linearization  (race between enqueue check and put,
# concurrent-stop safety)
# ==========================================================================

def test_all_accepted_tasks_processed_after_clean_shutdown():
    """Invariant: if enqueue returned True and stop() returned True,
    the task WAS executed by a consumer."""
    d = MessageDispatcher(workers=2, queue_capacity=200, shutdown_timeout_s=5.0,
                          name='invariant-1')
    try:
        processed = set()
        lock = real_threading.Lock()
        accepted = []
        acc_lock = real_threading.Lock()

        def make_task(i):
            def t():
                with lock:
                    processed.add(i)
            return t

        for i in range(100):
            if d.enqueue(make_task(i), topic='T'):
                with acc_lock:
                    accepted.append(i)

        result = d.stop(timeout_s=5.0)
        assert result is True
        assert processed == set(accepted)
    finally:
        d.stop(timeout_s=1.0)


def test_enqueue_returning_false_never_runs_task():
    """Invariant: enqueue returning False MUST NOT execute the task."""
    d = MessageDispatcher(workers=1, queue_capacity=2, shutdown_timeout_s=2.0,
                          name='invariant-2')
    try:
        started = real_threading.Event()
        release = real_threading.Event()
        rejected_tasks_ran = []
        rej_lock = real_threading.Lock()

        def slow(_id='slow'):
            started.set()
            release.wait(timeout=5.0)

        def make_reject_task(i):
            def t():
                with rej_lock:
                    rejected_tasks_ran.append(i)
            return t

        assert d.enqueue(slow, topic='T') is True
        assert started.wait(1.0)
        # Fill queue.
        assert d.enqueue(make_reject_task(-1), topic='T') is True
        assert d.enqueue(make_reject_task(-2), topic='T') is True
        # These MUST be rejected (drop_newest).
        rejected = []
        for i in range(10):
            if not d.enqueue(make_reject_task(i), topic='T'):
                rejected.append(i)

        assert len(rejected) == 10
        release.set()
        d.stop(timeout_s=3.0)
        # Every rejected task must NOT have executed.
        assert set(rejected_tasks_ran) & set(rejected) == set()
    finally:
        d.stop(timeout_s=1.0)


def test_sentinel_is_task_done_called_so_unfinished_tasks_reaches_zero():
    """Invariant: sentinels must go through task_done in the consumer's
    outer finally so that queue.unfinished_tasks reaches 0."""
    d = MessageDispatcher(workers=3, queue_capacity=10, shutdown_timeout_s=2.0,
                          name='invariant-3')
    # No user tasks; stop immediately. Sentinels are the only items enqueued.
    result = d.stop(timeout_s=2.0)
    assert result is True
    # If any sentinel was NOT task_done'd, unfinished_tasks > 0.
    assert d._queue.unfinished_tasks == 0


def test_queue_unfinished_tasks_is_zero_after_clean_shutdown_with_traffic():
    """Same invariant but with real traffic beforehand."""
    d = MessageDispatcher(workers=2, queue_capacity=50, shutdown_timeout_s=3.0,
                          name='invariant-4')
    try:
        counter_lock = real_threading.Lock()
        counter = {'v': 0}

        def h():
            with counter_lock:
                counter['v'] += 1

        for _ in range(40):
            assert d.enqueue(h, topic='T') is True
        result = d.stop(timeout_s=3.0)
        assert result is True
        assert counter['v'] == 40
        assert d._queue.unfinished_tasks == 0
    finally:
        d.stop(timeout_s=1.0)


def test_concurrent_stop_calls_execute_actual_shutdown_only_once():
    """Invariant: concurrent stop() calls result in a SINGLE actual
    shutdown — exactly `workers` sentinels are posted, and every
    concurrent caller observes the same True/False outcome."""
    d = MessageDispatcher(workers=3, queue_capacity=10, shutdown_timeout_s=2.0,
                          name='invariant-5')

    sentinel_puts = [0]
    sentinel_lock = real_threading.Lock()
    original_put_nowait = d._queue.put_nowait

    def counting_put(item):
        if item is MessageDispatcher._SHUTDOWN_SENTINEL:
            with sentinel_lock:
                sentinel_puts[0] += 1
        return original_put_nowait(item)

    d._queue.put_nowait = counting_put

    N_STOPPERS = 5
    barrier = real_threading.Barrier(N_STOPPERS)
    results = [None] * N_STOPPERS

    def do_stop(i):
        barrier.wait(timeout=3.0)  # start all at once
        results[i] = d.stop(timeout_s=2.0)

    threads = [
        real_threading.Thread(target=do_stop, args=(i,))
        for i in range(N_STOPPERS)
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join(3.0)

    # Only one stop() posted sentinels; count == workers.
    assert sentinel_puts[0] == 3, (
        f'expected exactly 3 sentinel puts (workers=3); got {sentinel_puts[0]}'
    )
    # All 5 callers observe the SAME outcome (True — clean shutdown).
    assert results == [True] * N_STOPPERS, f'inconsistent results: {results}'


def test_race_stop_wins_between_enqueue_check_and_put_deterministic():
    """DETERMINISTIC reproduction of the enqueue check-then-put race.

    Under a buggy implementation where enqueue reads `_accepting` and
    calls `_queue.put_nowait` outside a shared lock, a concurrent
    stop() can set _accepting=False AND post the shutdown sentinel
    between those two steps. The task then lands AFTER the sentinel;
    consumers see the sentinel first, exit, and never execute the
    task — but enqueue returned True.

    This test instruments _queue.put_nowait to pause when putting the
    target task, then triggers stop() in the pause window. Under a
    correct implementation, either the task is queued (and hence
    processed) or enqueue returns False."""
    d = MessageDispatcher(
        workers=1, queue_capacity=100, shutdown_timeout_s=5.0,
        name='race-det',
    )
    processed: List[str] = []
    proc_lock = real_threading.Lock()

    # Prime a slow task so the consumer is busy while we set up the race.
    consumer_started = real_threading.Event()
    consumer_release = real_threading.Event()

    def slow_prime():
        consumer_started.set()
        consumer_release.wait(timeout=5.0)

    assert d.enqueue(slow_prime, topic='prime') is True
    assert consumer_started.wait(1.0)

    def target():
        with proc_lock:
            processed.append('target')

    original_put_nowait = d._queue.put_nowait
    pause_at_put = real_threading.Event()
    resume_put = real_threading.Event()

    def instrumented_put_nowait(item):
        if item is target:
            pause_at_put.set()
            resume_put.wait(timeout=5.0)
        return original_put_nowait(item)

    d._queue.put_nowait = instrumented_put_nowait

    accepted = [None]

    def do_enqueue():
        accepted[0] = d.enqueue(target, topic='target')

    enq_thread = real_threading.Thread(target=do_enqueue)
    enq_thread.start()
    assert pause_at_put.wait(1.0), 'enqueue never reached the put step'

    stop_result = [None]

    def do_stop():
        stop_result[0] = d.stop(timeout_s=5.0)

    stop_thread = real_threading.Thread(target=do_stop)
    stop_thread.start()
    # Give stop a moment to try to run. Under buggy code, stop
    # completes its state flip + sentinel post here. Under a correct
    # (linearized) implementation, stop blocks on the same lock that
    # enqueue's put is inside.
    time.sleep(0.05)

    resume_put.set()
    enq_thread.join(1.0)
    consumer_release.set()
    stop_thread.join(5.0)

    if accepted[0] is True:
        assert 'target' in processed, (
            'RACE: enqueue returned True but target was queued after '
            'the shutdown sentinel and never executed. '
            'accepted=%r processed=%r' % (accepted[0], processed)
        )


def test_stop_and_enqueue_linearization_under_concurrency_stress():
    """LINEARIZATION invariant: for any interleaving of concurrent
    producer + stop, every enqueue returning True is executed before
    the (successful) shutdown completes.

    Runs multiple trials to widen the window in which the race between
    the `_accepting` check and the `put_nowait` inside enqueue can
    interleave with stop's `_accepting=False` + sentinel post."""
    trials = 25
    per_trial_msgs = 300
    for trial in range(trials):
        d = MessageDispatcher(
            workers=2, queue_capacity=per_trial_msgs * 2,
            shutdown_timeout_s=5.0, name=f'lin-stress-{trial}',
        )
        processed = set()
        proc_lock = real_threading.Lock()
        accepted = []
        acc_lock = real_threading.Lock()
        producer_done = real_threading.Event()

        def make_task(tid):
            def t():
                with proc_lock:
                    processed.add(tid)
            return t

        def producer():
            for i in range(per_trial_msgs):
                if d.enqueue(make_task(i), topic='T'):
                    with acc_lock:
                        accepted.append(i)
            producer_done.set()

        prod = real_threading.Thread(target=producer)
        prod.start()
        # Race window: producer is enqueueing while we call stop().
        result = d.stop(timeout_s=5.0)
        producer_done.wait(3.0)
        prod.join(1.0)

        assert result is True, f'trial {trial}: stop did not drain cleanly'
        with acc_lock:
            accepted_snapshot = set(accepted)
        missing = accepted_snapshot - processed
        assert not missing, (
            f'trial {trial}: {len(missing)} accepted tasks were queued '
            f'after sentinel and never ran; sample={sorted(missing)[:5]}, '
            f'accepted={len(accepted_snapshot)}, processed={len(processed)}'
        )
