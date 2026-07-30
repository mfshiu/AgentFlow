"""RFC-009 tests: ThreadWorker bounded cooperative lifecycle.

Post-RFC-009 contract:

  - ThreadWorker has an explicit `WorkerState` state machine
    (NEW → STARTING → RUNNING → STOPPING → STOPPED / STOP_TIMEOUT /
    FAILED / START_FAILED).
  - `stop(graceful_timeout_s=5.0) -> bool`. True = thread exited or
    never started; False = deadline expired, thread still alive.
  - stop-before-start is a no-op returning True; state stays NEW.
  - Repeated start() from any non-NEW state raises RuntimeError.
  - STOP_TIMEOUT is retriable — a subsequent stop() runs a fresh
    escalation with a fresh budget.
  - Concurrent stop() callers coordinate via `_stop_complete_event`
    with a BOUNDED wait (never `.wait()` without a timeout).
  - `_run_target` catches Exception only (not BaseException);
    captured into `last_exception`; state → FAILED.
  - `work_thread.daemon = False` — bounded stop only guarantees
    Agent.terminate returns, NOT that interpreter shutdown will
    succeed. Documented limitation.

The hang-observation pattern is unchanged from the RFC-008
characterisation: daemon controller thread + release Event +
short bounded wait. No test may block the pytest main thread on a
wedged target.
"""

import inspect
import queue
import re
import threading
import time
from typing import Any, Dict, List, Optional

import pytest

from agentflow.broker.broker_maker import BrokerMaker
from agentflow.core.agent import Agent
from agentflow.core.agent_worker import (
    ProcessWorker,
    ThreadWorker,
    Worker,
    WorkerState,
)


# ---------------------------------------------------------------------------
# Helper: minimal duck-typed initiator agent
# ---------------------------------------------------------------------------


class _FakeInitiator:
    """Minimal object shaped like what ThreadWorker reads from
    `initiator_agent`. `_activate` is a plain attribute so tests can
    swap it for a lambda / closure without wrestling with method
    binding."""

    def __init__(self, *, activate=None):
        self.config: Dict[str, Any] = {}
        self._activate = activate if activate is not None else self._default_activate
        self.name_tag = 'fake_initiator'

    def _default_activate(self, cfg):
        q = cfg['work_queue']
        while True:
            try:
                item = q.get(timeout=0.05)
            except queue.Empty:
                continue
            if item == 'terminate':
                return

    def M(self, message=None):
        return f'{self.name_tag} {message}' if message else self.name_tag


def _make_thread_worker(activate=None) -> ThreadWorker:
    return ThreadWorker(_FakeInitiator(activate=activate))


class _StopProbe:
    """Runs `worker.stop(...)` from a daemon controller thread and
    reports the outcome back via an Event, so the main thread can
    make bounded observations about a hanging stop() without ever
    calling join() on a wedged worker itself."""

    def __init__(self, worker: ThreadWorker, **stop_kwargs):
        self.worker = worker
        self.stop_kwargs = stop_kwargs
        self.returned = threading.Event()
        self.result: Optional[bool] = None
        self.raised: Optional[BaseException] = None

    def start(self) -> threading.Thread:
        def _run():
            try:
                self.result = self.worker.stop(**self.stop_kwargs)
            except BaseException as ex:
                self.raised = ex
            finally:
                self.returned.set()
        t = threading.Thread(target=_run, daemon=True, name='StopProbe')
        t.start()
        return t


class _TerminateProbe:
    def __init__(self, agent: Agent):
        self.agent = agent
        self.returned = threading.Event()
        self.raised: Optional[BaseException] = None

    def start(self) -> threading.Thread:
        def _run():
            try:
                self.agent.terminate()
            except BaseException as ex:
                self.raised = ex
            finally:
                self.returned.set()
        t = threading.Thread(target=_run, daemon=True, name='TerminateProbe')
        t.start()
        return t


def _wait_thread_alive(t: Optional[threading.Thread], timeout: float = 1.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if t is not None and t.is_alive():
            return True
        time.sleep(0.005)
    return t is not None and t.is_alive()


# ===========================================================================
# A. Startup / thread properties (items 1, 25)
# ===========================================================================


def test_A1_start_creates_non_daemon_worker_thread_and_returns_it():
    release = threading.Event()
    worker = _make_thread_worker(activate=lambda cfg: release.wait(timeout=5.0))
    try:
        returned = worker.start()
        assert returned is worker.work_thread
        assert isinstance(worker.work_thread, threading.Thread)
        assert _wait_thread_alive(worker.work_thread, timeout=1.0)
        # RFC-009 §7.13: daemon flag stays False.
        assert worker.work_thread.daemon is False
        # State transitioned NEW → STARTING → RUNNING.
        assert worker.state is WorkerState.RUNNING
    finally:
        release.set()
        assert worker.stop(graceful_timeout_s=2.0) is True


def test_A2_start_uses_original_agent_instance_by_identity():
    release = threading.Event()
    initiator = _FakeInitiator(activate=lambda cfg: release.wait(timeout=5.0))
    worker = ThreadWorker(initiator)
    try:
        worker.start()
        assert worker.initiator_agent is initiator
    finally:
        release.set()
        worker.stop(graceful_timeout_s=2.0)


def test_A3_start_mutates_agent_config_in_place_with_work_queue():
    """RFC-009 §7.15 preserves the shared-instance model of thread
    mode. Contrast RFC-008 §7.6 for ProcessWorker (copy)."""
    release = threading.Event()
    initiator = _FakeInitiator(activate=lambda cfg: release.wait(timeout=5.0))
    assert 'work_queue' not in initiator.config
    worker = ThreadWorker(initiator)
    try:
        worker.start()
        assert 'work_queue' in initiator.config
        assert initiator.config['work_queue'] is worker.work_queue
    finally:
        release.set()
        worker.stop(graceful_timeout_s=2.0)


def test_A4_broker_dispatcher_and_handler_registry_are_shared_by_reference():
    """Shared-instance model is essential to parent-side publish /
    subscribe / publish_sync working in thread mode."""
    release = threading.Event()
    marker: Dict[str, Any] = {}

    def _activate(cfg):
        initiator._broker = 'child-installed'
        initiator._dispatcher = 'child-installed'
        initiator._handler_registry = {'topic/x': lambda: None}
        marker['child_ran'] = True
        release.wait(timeout=5.0)

    initiator = _FakeInitiator(activate=_activate)
    initiator._broker = None
    initiator._dispatcher = None
    initiator._handler_registry: Dict[str, Any] = {}

    worker = ThreadWorker(initiator)
    try:
        worker.start()
        deadline = time.monotonic() + 1.0
        while time.monotonic() < deadline and not marker.get('child_ran'):
            time.sleep(0.005)
        assert marker.get('child_ran') is True
        assert initiator._broker == 'child-installed'
        assert initiator._dispatcher == 'child-installed'
        assert 'topic/x' in initiator._handler_registry
    finally:
        release.set()
        worker.stop(graceful_timeout_s=2.0)


# ===========================================================================
# B. State machine: NEW / stop-before-start / restart guards
#    (items 2, 11, 12)
# ===========================================================================


def test_B1_stop_before_start_is_noop_returning_True_state_stays_NEW():
    """RFC-009 §7.6 modification: no more AttributeError; state
    remains NEW so subsequent start() is allowed."""
    worker = _make_thread_worker()
    assert worker.state is WorkerState.NEW
    result = worker.stop()
    assert result is True
    assert worker.state is WorkerState.NEW


def test_B2_stop_before_start_then_start_is_allowed():
    worker = _make_thread_worker()
    worker.stop()   # no-op
    assert worker.state is WorkerState.NEW
    try:
        worker.start()
        assert worker.state is WorkerState.RUNNING
    finally:
        assert worker.stop(graceful_timeout_s=2.0) is True


def test_B3_start_twice_while_running_raises_RuntimeError():
    """RFC-009 §7.9 / §7.10: restart is not supported."""
    release = threading.Event()
    worker = _make_thread_worker(activate=lambda cfg: release.wait(timeout=5.0))
    try:
        worker.start()
        assert worker.state is WorkerState.RUNNING
        with pytest.raises(RuntimeError):
            worker.start()
    finally:
        release.set()
        worker.stop(graceful_timeout_s=2.0)


def test_B4_start_after_STOPPED_raises_RuntimeError():
    worker = _make_thread_worker()
    worker.start()
    assert worker.stop(graceful_timeout_s=2.0) is True
    assert worker.state is WorkerState.STOPPED
    with pytest.raises(RuntimeError):
        worker.start()


def test_B5_start_after_START_FAILED_raises_RuntimeError(monkeypatch):
    """RFC-009 §7.10 restart guard on START_FAILED terminal state."""
    real_thread_cls = threading.Thread

    class ExplodingThread(real_thread_cls):
        def start(self):
            raise OSError("simulated Thread.start failure")

    monkeypatch.setattr('agentflow.core.agent_worker.threading.Thread', ExplodingThread)
    worker = _make_thread_worker()
    with pytest.raises(OSError):
        worker.start()
    assert worker.state is WorkerState.START_FAILED
    assert worker.work_thread is None
    # Restore for the restart-guard check.
    monkeypatch.setattr('agentflow.core.agent_worker.threading.Thread', real_thread_cls)
    with pytest.raises(RuntimeError):
        worker.start()


# ===========================================================================
# C. stop() cooperative return / bounded join / STOP_TIMEOUT
#    (items 3, 4, 5, 6, 7)
# ===========================================================================


def test_C1_stop_source_uses_bounded_join_with_graceful_timeout_s():
    """Static evidence: the unbounded `work_thread.join()` from the
    pre-RFC-009 code is gone; `join(graceful_timeout_s)` is used."""
    src = inspect.getsource(ThreadWorker.stop)
    # Must not contain unbounded join.
    assert '.work_thread.join()' not in src, (
        "unbounded work_thread.join() should have been replaced"
    )
    # Must contain a bounded join.
    assert 'join(graceful_timeout_s)' in src, (
        "expected a bounded join(graceful_timeout_s) call in stop()"
    )
    # signature carries the timeout with the expected default.
    sig = inspect.signature(ThreadWorker.stop)
    assert 'graceful_timeout_s' in sig.parameters
    assert sig.parameters['graceful_timeout_s'].default == 5.0


def test_C2_cooperative_stop_returns_True_fast():
    worker = _make_thread_worker()
    worker.start()
    t0 = time.monotonic()
    ok = worker.stop(graceful_timeout_s=2.0)
    elapsed = time.monotonic() - t0
    assert ok is True
    assert elapsed < 1.0, f"cooperative stop took {elapsed:.3f}s"
    assert worker.state is WorkerState.STOPPED
    assert not worker.work_thread.is_alive()


def test_C3_wedged_thread_returns_False_bounded_and_state_STOP_TIMEOUT():
    """Runtime confirmation of the bounded contract: `_activate` is
    wedged, stop() returns False in ~graceful_timeout_s, state ends
    at STOP_TIMEOUT, thread reference is retained, is_working=True."""
    release = threading.Event()
    worker = _make_thread_worker(
        activate=lambda cfg: release.wait(timeout=30.0)
    )
    try:
        worker.start()
        t0 = time.monotonic()
        result = worker.stop(graceful_timeout_s=0.3)
        elapsed = time.monotonic() - t0
        assert result is False
        assert elapsed < 0.7, f"bounded stop took {elapsed:.3f}s"
        assert worker.state is WorkerState.STOP_TIMEOUT
        # Thread reference retained (RFC-009 §7.4).
        assert worker.work_thread is not None
        assert worker.work_thread.is_alive()
        assert worker.is_working() is True
    finally:
        release.set()
        # Cleanup retry so the test doesn't leak the wedged thread.
        assert worker.stop(graceful_timeout_s=2.0) is True


def test_C4_STOP_TIMEOUT_thread_reference_retained():
    """Explicit assertion for item #6."""
    release = threading.Event()
    worker = _make_thread_worker(
        activate=lambda cfg: release.wait(timeout=30.0)
    )
    try:
        worker.start()
        original_thread = worker.work_thread
        worker.stop(graceful_timeout_s=0.2)
        assert worker.state is WorkerState.STOP_TIMEOUT
        assert worker.work_thread is original_thread
    finally:
        release.set()
        worker.stop(graceful_timeout_s=2.0)


def test_C5_is_working_returns_True_during_STOP_TIMEOUT():
    """Explicit assertion for item #7. `is_working` reflects the
    real thread liveness, not the abstract state."""
    release = threading.Event()
    worker = _make_thread_worker(
        activate=lambda cfg: release.wait(timeout=30.0)
    )
    try:
        worker.start()
        worker.stop(graceful_timeout_s=0.2)
        assert worker.state is WorkerState.STOP_TIMEOUT
        assert worker.is_working() is True
    finally:
        release.set()
        worker.stop(graceful_timeout_s=2.0)


# ===========================================================================
# D. STOP_TIMEOUT retry (items 8, 9, D+K in RFC-009)
# ===========================================================================


def test_D1_STOP_TIMEOUT_retry_reaches_STOPPED_when_blocker_released():
    """After release, a retry-stop drives the state to STOPPED."""
    release = threading.Event()
    worker = _make_thread_worker(
        activate=lambda cfg: release.wait(timeout=30.0)
    )
    worker.start()
    assert worker.stop(graceful_timeout_s=0.2) is False
    assert worker.state is WorkerState.STOP_TIMEOUT
    release.set()
    result = worker.stop(graceful_timeout_s=2.0)
    assert result is True
    assert worker.state is WorkerState.STOPPED
    assert not worker.work_thread.is_alive()


def test_D2_STOP_TIMEOUT_retry_still_wedged_stays_STOP_TIMEOUT():
    """A retry that finds the thread still wedged stays at
    STOP_TIMEOUT and returns False."""
    release = threading.Event()
    worker = _make_thread_worker(
        activate=lambda cfg: release.wait(timeout=30.0)
    )
    try:
        worker.start()
        assert worker.stop(graceful_timeout_s=0.2) is False
        assert worker.state is WorkerState.STOP_TIMEOUT
        # Retry while still wedged.
        assert worker.stop(graceful_timeout_s=0.2) is False
        assert worker.state is WorkerState.STOP_TIMEOUT
        assert worker.work_thread.is_alive()
    finally:
        release.set()
        worker.stop(graceful_timeout_s=2.0)


def test_D3_repeated_stop_after_STOPPED_returns_True_idempotent():
    """Item #10: idempotent replay. No new sentinel is enqueued."""
    worker = _make_thread_worker()
    worker.start()
    assert worker.stop(graceful_timeout_s=2.0) is True
    assert worker.state is WorkerState.STOPPED
    # Snapshot queue size after the cooperative shutdown consumed
    # its 'terminate' sentinel.
    qsize_after_first = worker.work_queue.qsize()
    # Second and third stop() calls must not push new sentinels.
    assert worker.stop() is True
    assert worker.stop() is True
    assert worker.work_queue.qsize() == qsize_after_first
    assert worker.state is WorkerState.STOPPED


# ===========================================================================
# E. Concurrent stop semantics (items 17, 18, 19)
# ===========================================================================


def test_E1_concurrent_stop_sends_terminate_exactly_once_via_state_lock():
    """Only the first caller enqueues 'terminate'; waiters do not.
    Verified against a wedged _activate so the sentinel accumulates
    in the queue rather than being drained."""
    release = threading.Event()
    worker = _make_thread_worker(
        activate=lambda cfg: release.wait(timeout=30.0)
    )
    try:
        worker.start()
        assert worker.work_queue.qsize() == 0
        N = 5
        barrier = threading.Barrier(N, timeout=2.0)
        probes = [_StopProbe(worker, graceful_timeout_s=0.2) for _ in range(N)]

        def caller(p):
            barrier.wait()
            p.start()

        launchers = [
            threading.Thread(target=caller, args=(p,), daemon=True)
            for p in probes
        ]
        for t in launchers:
            t.start()
        for t in launchers:
            t.join(3.0)
        for p in probes:
            assert p.returned.wait(3.0), "a concurrent stop() did not return"

        # All callers observe the same cached bool (False, because the
        # thread is wedged).
        results = [p.result for p in probes]
        assert results == [False] * N
        # Exactly one 'terminate' reached the queue.
        assert worker.work_queue.qsize() == 1
        assert worker.state is WorkerState.STOP_TIMEOUT
    finally:
        release.set()
        worker.stop(graceful_timeout_s=2.0)


def test_E2_concurrent_waiter_uses_bounded_event_wait_by_source_inspection():
    """RFC-009 §E: waiter path must NOT use `.wait()` without a
    timeout. The bounded margin is graceful_timeout_s + a small
    coordination margin."""
    src = inspect.getsource(ThreadWorker.stop)
    # No bare `_stop_complete_event.wait()`.
    assert '_stop_complete_event.wait()' not in src, (
        "waiter must not perform an unbounded event.wait()"
    )
    # Some form of `_stop_complete_event.wait(...)` must exist.
    assert re.search(r'_stop_complete_event\.wait\(\s*\S', src), (
        "expected a bounded _stop_complete_event.wait(<timeout>) call"
    )


def test_E3_waiter_returns_bounded_when_completion_event_never_fires():
    """Directly exercise the waiter path with a state that will not
    self-complete: we place the worker into STOPPING (bypassing the
    first-caller flow) so a subsequent stop() call takes the waiter
    branch and must observe the bounded timeout."""
    release = threading.Event()
    worker = _make_thread_worker(
        activate=lambda cfg: release.wait(timeout=30.0)
    )
    try:
        worker.start()
        # Simulate: another caller entered STOPPING but never set the
        # completion event (crash / lost). The next stop() call is a
        # waiter and must NOT block forever.
        with worker._state_lock:
            worker._state = WorkerState.STOPPING
            worker._stop_complete_event.clear()

        t0 = time.monotonic()
        result = worker.stop(graceful_timeout_s=0.2)
        elapsed = time.monotonic() - t0
        # Bounded by graceful_timeout_s + coordination margin (0.1s).
        assert elapsed < 0.7, f"waiter path exceeded bounded window: {elapsed:.3f}s"
        # Thread is alive → waiter reports False.
        assert result is False
    finally:
        # Restore to a state that allows retry cleanup.
        with worker._state_lock:
            worker._state = WorkerState.STOP_TIMEOUT
        release.set()
        worker.stop(graceful_timeout_s=2.0)


# ===========================================================================
# F. Agent.terminate behaviour (items 20, 21, 22, 23)
# ===========================================================================


class _HangingBroker:
    def __init__(self, release: threading.Event):
        self._release = release
        self.stop_started = threading.Event()

    def start(self, options: dict):
        return None

    def stop(self):
        self.stop_started.set()
        self._release.wait(timeout=30.0)

    def publish(self, topic, payload): pass
    def subscribe(self, topic, data_type): return None
    def unsubscribe(self, topic): pass


class _FastBroker:
    def __init__(self):
        self.stop_calls = 0

    def start(self, options: dict): return None
    def stop(self): self.stop_calls += 1
    def publish(self, topic, payload): pass
    def subscribe(self, topic, data_type): return None
    def unsubscribe(self, topic): pass


def _install_stub_broker(monkeypatch, stub):
    monkeypatch.setattr(
        BrokerMaker, 'create_broker',
        lambda self, broker_type, notifier: stub,
    )


def _minimal_agent_config():
    return {
        'CONCURRENCY_TYPE': 'thread',
        'broker': {
            'broker_name': 'my_broker',
            'my_broker': {'broker_type': 'empty'},
        },
    }


def test_F1_agent_terminate_returns_bounded_when_broker_stop_wedges_and_logs_WARNING(
    monkeypatch, caplog,
):
    """RFC-009 primary outcome: broker.stop wedging inside the worker
    thread no longer hangs Agent.terminate. The worker.stop returns
    False after the deadline, Agent.terminate logs a WARNING and
    returns."""
    import logging as py_logging
    release = threading.Event()
    broker = _HangingBroker(release)
    _install_stub_broker(monkeypatch, broker)

    agent = Agent(name='broker_hang', agent_config={
        **_minimal_agent_config(),
    })
    agent.start()
    deadline = time.monotonic() + 1.0
    while time.monotonic() < deadline and agent._broker is not broker:
        time.sleep(0.005)
    assert agent._broker is broker

    # Wait for the worker to reach the queue loop before we terminate.
    time.sleep(0.05)

    # Use a small worker timeout so the test is fast; explicitly go
    # through Agent.terminate rather than worker.stop directly so we
    # exercise the wrapper logging.
    # Temporarily reduce ThreadWorker.stop default via monkeypatched
    # config-independent path: call worker.stop with a small timeout.
    # But Agent.terminate uses default (5.0). To keep the test fast,
    # monkeypatch stop's default via functools.partial.
    original_stop = agent._agent_worker.stop
    monkeypatch.setattr(
        agent._agent_worker, 'stop',
        lambda graceful_timeout_s=0.3: original_stop(graceful_timeout_s=graceful_timeout_s),
    )

    try:
        with caplog.at_level(py_logging.WARNING):
            probe = _TerminateProbe(agent)
            probe.start()
            # Worker path: `_terminate` schedules a `sleep(1) →
            # set(terminate_event)` helper; then `queue.get(timeout=1)`
            # may need one full timeout after the event fires to
            # observe it, giving a ~2s minimum before __deactivating
            # runs and calls broker.stop. Allow 4s headroom for slower
            # Python 3.9 sleep scheduling.
            assert broker.stop_started.wait(4.0), (
                "broker.stop was never invoked from the worker thread"
            )
            # Agent.terminate must return within: dispatcher.stop
            # (bounded RFC-004) + worker.stop (bounded RFC-009).
            assert probe.returned.wait(3.0), (
                "Agent.terminate did not return within bounded window"
            )
            assert probe.raised is None
        # WARNING should mention state and worker.stop not-stopping.
        combined = ' '.join(rec.getMessage() for rec in caplog.records)
        assert (
            'stop_timeout' in combined.lower()
            or 'did not stop' in combined.lower()
            or 'worker did not stop' in combined.lower()
        ), f"expected WARNING about worker not stopping; got: {combined!r}"
    finally:
        release.set()
        # Retry a bounded stop to reclaim the thread.
        for _ in range(3):
            if not agent._agent_worker.work_thread.is_alive():
                break
            agent._agent_worker.stop(graceful_timeout_s=1.0)


def test_F2_agent_terminate_returns_bounded_when_handler_wedges(monkeypatch):
    """Handler wedging alone does NOT hang Agent.terminate — the
    dispatcher's daemon consumers straggle but do not block the
    worker.stop path. RFC-009 preserves this."""
    broker = _FastBroker()
    _install_stub_broker(monkeypatch, broker)

    agent = Agent(name='handler_wedge', agent_config={
        **_minimal_agent_config(),
        'dispatch': {'workers': 2, 'shutdown_timeout_s': 0.3},
    })
    agent.start()
    time.sleep(0.1)

    handler_release = threading.Event()

    def wedged_handler(topic, pcl):
        handler_release.wait(timeout=10.0)

    from agentflow.core.parcel import TextParcel
    agent.subscribe('topic/wedge', topic_handler=wedged_handler)
    agent._on_message('topic/wedge', TextParcel('x').payload())

    probe = _TerminateProbe(agent)
    probe.start()
    try:
        assert probe.returned.wait(6.0), "Agent.terminate did not return"
        assert probe.raised is None
    finally:
        handler_release.set()


def test_F3_agent_terminate_dispatcher_stop_still_precedes_worker_stop_by_source_inspection():
    """RFC-009 §7.16: ordering preserved."""
    src = inspect.getsource(Agent.terminate)
    idx_dispatcher = src.find('self._dispatcher.stop')
    idx_worker = src.find('self._agent_worker.stop')
    assert idx_dispatcher != -1
    assert idx_worker != -1
    assert idx_dispatcher < idx_worker


def test_F4_agent_terminate_never_raises_when_worker_stop_returns_False(monkeypatch):
    """RFC-009 §E: terminate returns normally on False; does not raise."""
    release = threading.Event()
    broker = _HangingBroker(release)
    _install_stub_broker(monkeypatch, broker)
    agent = Agent(name='no_raise', agent_config=_minimal_agent_config())
    agent.start()
    deadline = time.monotonic() + 1.0
    while time.monotonic() < deadline and agent._broker is not broker:
        time.sleep(0.005)

    original_stop = agent._agent_worker.stop
    monkeypatch.setattr(
        agent._agent_worker, 'stop',
        lambda graceful_timeout_s=0.2: original_stop(graceful_timeout_s=graceful_timeout_s),
    )

    try:
        agent.terminate()   # must not raise
    finally:
        release.set()
        for _ in range(3):
            if not agent._agent_worker.work_thread.is_alive():
                break
            agent._agent_worker.stop(graceful_timeout_s=1.0)


# ===========================================================================
# G. Post-exit stop / self-exit (item 15)
# ===========================================================================


def test_G1_stop_after_target_already_exited_reaches_STOPPED_bounded():
    """When _activate self-exits, `_run_target` marks STOPPED. A later
    stop() returns True immediately from the STOPPED shortcut."""
    def _self_exiting(cfg):
        return

    worker = _make_thread_worker(activate=_self_exiting)
    worker.start()
    worker.work_thread.join(2.0)
    assert not worker.work_thread.is_alive()
    # Wait for _run_target's finally to update state.
    deadline = time.monotonic() + 1.0
    while time.monotonic() < deadline and worker.state != WorkerState.STOPPED:
        time.sleep(0.005)
    assert worker.state is WorkerState.STOPPED

    t0 = time.monotonic()
    assert worker.stop() is True
    elapsed = time.monotonic() - t0
    assert elapsed < 0.05, f"STOPPED shortcut took {elapsed:.3f}s"


# ===========================================================================
# H. Exception observability + FAILED state
#    (items 13, 14, 15 [source-inspection], 16)
# ===========================================================================


def test_H1_thread_start_failure_transitions_to_START_FAILED(monkeypatch):
    """Item #13: Thread.start() raises → state=START_FAILED, thread
    reference cleared, original exception re-raised."""
    class ExplodingThread(threading.Thread):
        def start(self):
            raise OSError("simulated Thread.start failure")

    monkeypatch.setattr('agentflow.core.agent_worker.threading.Thread', ExplodingThread)
    worker = _make_thread_worker()
    with pytest.raises(OSError):
        worker.start()
    assert worker.state is WorkerState.START_FAILED
    assert worker.work_thread is None


def test_H2_activate_Exception_captured_into_last_exception_state_FAILED():
    """Items #14, #16: uncaught Exception in _activate → captured
    into last_exception, state → FAILED."""
    def _bad(cfg):
        raise ValueError("boom")

    worker = _make_thread_worker(activate=_bad)
    worker.start()
    worker.work_thread.join(2.0)
    # Wait for _run_target's except handler to update state.
    deadline = time.monotonic() + 1.0
    while time.monotonic() < deadline and worker.state != WorkerState.FAILED:
        time.sleep(0.005)
    assert worker.state is WorkerState.FAILED
    assert isinstance(worker.last_exception, ValueError)
    assert str(worker.last_exception) == "boom"


def test_H3_run_target_catches_Exception_only_not_BaseException_by_source_inspection():
    """Item #15: safe source inspection. The wrapper must catch
    Exception explicitly; it must not use bare `except:` and must
    not catch BaseException."""
    src = inspect.getsource(ThreadWorker._run_target)
    assert 'except Exception' in src
    assert 'except BaseException' not in src
    assert not re.search(r'except\s*:\s*$', src, flags=re.MULTILINE), (
        "bare 'except:' would sweep BaseException too"
    )


def test_H4_stop_from_FAILED_state_returns_True_no_join(monkeypatch):
    """FAILED state means the thread has already ended (`_run_target`
    finished its except branch). stop() returns True immediately
    without another cooperative attempt."""
    def _bad(cfg):
        raise RuntimeError("nope")

    worker = _make_thread_worker(activate=_bad)
    worker.start()
    worker.work_thread.join(2.0)
    deadline = time.monotonic() + 1.0
    while time.monotonic() < deadline and worker.state != WorkerState.FAILED:
        time.sleep(0.005)
    assert worker.state is WorkerState.FAILED

    t0 = time.monotonic()
    assert worker.stop() is True
    elapsed = time.monotonic() - t0
    assert elapsed < 0.05


def test_H5_last_exception_is_None_after_clean_run():
    worker = _make_thread_worker()
    worker.start()
    assert worker.stop(graceful_timeout_s=2.0) is True
    assert worker.state is WorkerState.STOPPED
    assert worker.last_exception is None


def test_H6_state_STOP_TIMEOUT_is_not_STOPPED_when_thread_survives():
    """Item #24: guard against silently marking STOPPED when the
    thread is actually still alive."""
    release = threading.Event()
    worker = _make_thread_worker(
        activate=lambda cfg: release.wait(timeout=30.0)
    )
    try:
        worker.start()
        assert worker.stop(graceful_timeout_s=0.2) is False
        assert worker.state is not WorkerState.STOPPED
        assert worker.state is WorkerState.STOP_TIMEOUT
    finally:
        release.set()
        worker.stop(graceful_timeout_s=2.0)


# ===========================================================================
# I. Observability API surface (contrast ProcessWorker)
# ===========================================================================


def test_I1_thread_worker_exposes_state_and_last_exception_properties():
    """RFC-009 adds `state` + `last_exception` properties to
    ThreadWorker for parity with RFC-008 ProcessWorker."""
    worker = _make_thread_worker()
    assert hasattr(worker, 'state')
    assert hasattr(worker, 'last_exception')
    # ProcessWorker still has state + exitcode.
    pw = ProcessWorker(_FakeInitiator())
    assert hasattr(pw, 'state')
    assert hasattr(pw, 'exitcode')


def test_I2_daemon_flag_stays_False_baseline_preserved():
    """Item #25 explicit assertion for the interpreter-exit caveat.
    If this ever becomes True, the RFC-009 §H limitation changes and
    the RFC needs an update."""
    release = threading.Event()
    worker = _make_thread_worker(
        activate=lambda cfg: release.wait(timeout=5.0)
    )
    try:
        worker.start()
        assert worker.work_thread.daemon is False
    finally:
        release.set()
        worker.stop(graceful_timeout_s=2.0)


# ===========================================================================
# J. Baseline preservation
# ===========================================================================


def test_J1_worker_init_still_forces_spawn_start_method():
    import multiprocessing as mp
    _ = ThreadWorker(_FakeInitiator())
    assert mp.get_start_method() == 'spawn'


def test_J2_thread_worker_create_event_returns_threading_event():
    tw = ThreadWorker(_FakeInitiator())
    evt = tw.create_event()
    assert isinstance(evt, threading.Event)
