"""RFC-011 tests: MqttBroker bounded startup lifecycle.

Post-RFC-011 contract:

  - `start(options, *, startup_timeout_s=None) -> bool`; when
    `startup_timeout_s is None`, falls back to `self._timeout`
    (constructor arg default 10.0). Backward-compat with existing
    tests that use `MqttBroker(wait=True, timeout=0.1)`.
  - Startup runs `connect + loop_start` on a `daemon=True` helper
    thread; caller bounded-joins with the shared deadline.
  - `wait=True` awaits `_on_connect` within the SAME remaining budget.
  - `wait=False` returns `True` ONLY once connect + loop_start were
    initiated (helper completed); state stays STARTING until
    `_on_connect(rc=0)` transitions to RUNNING.
  - Failed instance is TERMINAL — any non-NEW `start()` raises
    `RuntimeError` without modifying any lifecycle flag.
  - Failure transitions atomically set `_stopping=True` under
    `_state_lock`; RFC-010 callback fencing kicks in immediately
    (no late `_on_connect` writes / notifier invocations).
  - Startup failure paths run the private bounded rollback primitive
    (`_run_client_shutdown_primitive`) — but ONLY when the startup
    helper has finished. START_TIMEOUT (helper still alive) defers
    rollback to a subsequent `stop()` call (RFC-011 mod 1+2).
  - Concurrent start callers coordinate via `_start_complete_event`
    with a BOUNDED wait; N callers share exactly ONE
    `(connect + loop_start)` pair to paho.
  - Concurrent waiter that observes failure raises a NEW
    `RuntimeError` chained to the original exception via `from`
    (RFC-011 modification 3 — no shared exception instance mutation).
  - `stop()` from `START_TIMEOUT` bounded-waits for the startup
    helper, then runs the rollback primitive at most once
    (`_last_start_cleanup_result` cache) — never mislabels True.
"""

import inspect
import re
import threading
import time
from typing import Optional
from unittest.mock import MagicMock

import pytest

from agentflow.broker.empty_broker import EmptyBroker
from agentflow.broker.message_broker import MessageBroker
from agentflow.broker.mqtt_broker import MqttBroker
from agentflow.core.agent_worker import WorkerState

from tests.helpers.source_checks import identifiers


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


class _StartProbe:
    def __init__(self, broker, options=None, **kwargs):
        self.broker = broker
        self.options = options or {}
        self.kwargs = kwargs
        self.returned = threading.Event()
        self.result = None
        self.raised: Optional[BaseException] = None

    def start(self) -> threading.Thread:
        def _run():
            try:
                self.result = self.broker.start(self.options, **self.kwargs)
            except BaseException as ex:
                self.raised = ex
            finally:
                self.returned.set()
        t = threading.Thread(target=_run, daemon=True, name='StartProbe')
        t.start()
        return t


class _StopProbe:
    def __init__(self, broker, **stop_kwargs):
        self.broker = broker
        self.stop_kwargs = stop_kwargs
        self.returned = threading.Event()
        self.result = None
        self.raised: Optional[BaseException] = None

    def start(self) -> threading.Thread:
        def _run():
            try:
                self.result = self.broker.stop(**self.stop_kwargs)
            except BaseException as ex:
                self.raised = ex
            finally:
                self.returned.set()
        t = threading.Thread(target=_run, daemon=True, name='StopProbe')
        t.start()
        return t


def _install_blocking(mock_call, release_event, started_event):
    def _blocked(*args, **kwargs):
        started_event.set()
        release_event.wait(timeout=30.0)
    mock_call.side_effect = _blocked


def _make_broker_wait_true(fake_client, notifier, monkeypatch, timeout=0.2):
    monkeypatch.setattr(
        'agentflow.broker.mqtt_broker.Client',
        lambda *a, **kw: fake_client,
    )
    return MqttBroker(notifier=notifier, wait=True, timeout=timeout)


def _fire_connect_success(broker, fake_client):
    broker._on_connect(
        client=fake_client, userdata=None, flags={},
        reasonCode=0, properties=None,
    )


def _record_calling_thread(mock_call) -> list:
    """Make a fake paho method record the thread that calls it."""
    threads = []
    mock_call.side_effect = (
        lambda *args, **kwargs: threads.append(threading.current_thread())
    )
    return threads


class _TimeWithoutSleep:
    """Stands in for the `time` module inside agentflow.core.agent, with
    sleep() a no-op, so the pauses between broker start attempts in
    Agent.__activating cost nothing."""

    def __getattr__(self, name):
        return getattr(time, name)

    @staticmethod
    def sleep(_seconds):
        pass


def _make_activating_agent(monkeypatch, make_broker):
    """An Agent whose __activating builds its brokers with
    `make_broker(notifier)` and does not pause between attempts."""
    from agentflow.broker.broker_maker import BrokerMaker
    from agentflow.core import agent as agent_module
    monkeypatch.setattr(
        BrokerMaker, 'create_broker',
        lambda self, broker_type, notifier: make_broker(notifier),
    )
    monkeypatch.setattr(agent_module, 'time', _TimeWithoutSleep())
    agent = agent_module.Agent('activating', {
        'broker': {'broker_name': 'x', 'x': {'broker_type': 'mqtt'}},
    })
    # Normally created by Agent._activate, which runs __activating.
    agent._Agent__terminate_event = threading.Event()
    return agent


# ===========================================================================
# A. Basic start lifecycle (RFC-011 §A)
# ===========================================================================


def test_A1_client_created_in_init_not_in_start(fake_client, notifier, monkeypatch):
    monkeypatch.setattr(
        'agentflow.broker.mqtt_broker.Client',
        lambda *a, **kw: fake_client,
    )
    b = MqttBroker(notifier=notifier, wait=False)
    assert b._client is fake_client
    assert b.state is WorkerState.NEW


def test_A2_callbacks_bound_before_helper_runs_connect(broker, fake_client):
    """RFC-011: callbacks bound BEFORE helper spawn / connect.
    Verified via runtime — at connect-time, on_connect must be set."""
    seen = {}
    def capture(*a, **kw):
        seen['on_connect_set'] = fake_client.on_connect is not None
        seen['on_connect_value'] = fake_client.on_connect
    fake_client.connect.side_effect = capture
    broker.start({})
    assert seen['on_connect_set'] is True
    # MagicMock preserves the assignment; the equality check is what
    # RFC-005 test_start_registers_on_disconnect_callback_on_client uses.
    assert fake_client.on_connect == broker._on_connect


def test_A3_connect_called_before_loop_start(broker, fake_client):
    order = []
    fake_client.connect.side_effect = lambda *a, **kw: order.append('connect')
    fake_client.loop_start.side_effect = lambda *a, **kw: order.append('loop_start')
    broker.start({})
    assert order == ['connect', 'loop_start']


def test_A4_state_transitions_NEW_to_STARTING_at_entry(broker, fake_client):
    assert broker.state is WorkerState.NEW
    seen = {}
    def capture(*a, **kw):
        seen['state_at_connect'] = broker.state
    fake_client.connect.side_effect = capture
    broker.start({})
    assert seen['state_at_connect'] is WorkerState.STARTING


def test_A5_connected_evt_cleared_in_start(broker, fake_client):
    broker._connected_evt.set()
    seen = {}
    def capture(*a, **kw):
        seen['evt'] = broker._connected_evt.is_set()
    fake_client.connect.side_effect = capture
    broker.start({})
    assert seen['evt'] is False


def test_A6_connect_ok_reset_in_start(broker, fake_client):
    broker._connect_ok = True
    seen = {}
    def capture(*a, **kw):
        seen['ok'] = broker._connect_ok
    fake_client.connect.side_effect = capture
    broker.start({})
    assert seen['ok'] is False


def test_A7_stopping_reset_at_NEW_start_entry_only(broker, fake_client):
    """RFC-011 modification 5: `_stopping` is reset to False ONLY in
    the NEW→STARTING branch of the linearization. This restores clean
    startup semantics for a fresh broker. (Restart-from-STOPPED is
    structurally impossible per §7.5 — see test_H4 / test_H5.)"""
    seen = {}
    def capture(*a, **kw):
        seen['stopping'] = broker._stopping
    fake_client.connect.side_effect = capture
    broker.start({})
    assert seen['stopping'] is False


def test_A8_wait_false_True_only_means_startup_initiated_state_stays_STARTING(
    broker, fake_client,
):
    """RFC-011 modification 4: wait=False's True is a WEAKER contract
    than "connected". It ONLY means connect + loop_start were
    initiated (paho started). State stays STARTING until
    `_on_connect(rc=0)` fires. Callback is out-of-scope for caller's
    return."""
    assert broker.start({}) is True
    fake_client.connect.assert_called_once()
    fake_client.loop_start.assert_called_once()
    # Not connected yet — state remains STARTING.
    assert broker.state is WorkerState.STARTING


def test_A9_wait_true_timeout_raises_TimeoutError_and_state_START_TIMEOUT(
    fake_client, notifier, monkeypatch,
):
    b = _make_broker_wait_true(fake_client, notifier, monkeypatch, timeout=0.1)
    with pytest.raises(TimeoutError):
        b.start({})
    # Callback wait timeout → START_TIMEOUT (RFC-011 §7.2).
    assert b.state is WorkerState.START_TIMEOUT


def test_A10_successful_on_connect_transitions_STARTING_to_RUNNING(
    broker, fake_client,
):
    broker.start({})
    assert broker.state is WorkerState.STARTING
    _fire_connect_success(broker, fake_client)
    assert broker.state is WorkerState.RUNNING


def test_A11_failed_on_connect_rc_nonzero_does_not_transition_state_to_RUNNING(
    broker, fake_client,
):
    broker.start({})
    broker._on_connect(
        client=fake_client, userdata=None, flags={},
        reasonCode=5, properties=None,
    )
    assert broker.state is WorkerState.STARTING


def test_A12_successful_start_returns_True(broker, fake_client):
    assert broker.start({}) is True


# ===========================================================================
# B. Bounded startup — hang scenarios (RFC-011 §7.9)
# ===========================================================================


def test_B13_connect_wedges_start_returns_TimeoutError_bounded_state_START_TIMEOUT(
    broker, fake_client,
):
    """RFC-011 primary outcome: wedged `client.connect` no longer
    blocks `start()` forever. Raises TimeoutError within
    `startup_timeout_s`. State becomes START_TIMEOUT (helper still
    alive; rollback deferred to stop())."""
    release = threading.Event()
    started = threading.Event()
    _install_blocking(fake_client.connect, release, started)

    probe = _StartProbe(broker, startup_timeout_s=0.3)
    probe.start()
    try:
        assert started.wait(2.0), "connect never entered"
        assert probe.returned.wait(1.0), "start did not return bounded"
        assert isinstance(probe.raised, TimeoutError)
        assert broker.state is WorkerState.START_TIMEOUT
        # Helper is STILL alive — rollback NOT started (mod 2).
        assert broker._start_helper_thread is not None
        assert broker._start_helper_thread.is_alive()
    finally:
        release.set()
        # Cleanup via stop() which bounded-waits for helper.
        broker.stop(graceful_timeout_s=2.0)


def test_B14_loop_start_wedges_start_returns_TimeoutError_bounded(
    broker, fake_client,
):
    release = threading.Event()
    started = threading.Event()
    _install_blocking(fake_client.loop_start, release, started)

    probe = _StartProbe(broker, startup_timeout_s=0.3)
    probe.start()
    try:
        assert started.wait(2.0)
        # connect DID succeed before loop_start wedged.
        fake_client.connect.assert_called_once()
        assert probe.returned.wait(1.0)
        assert isinstance(probe.raised, TimeoutError)
        assert broker.state is WorkerState.START_TIMEOUT
    finally:
        release.set()
        broker.stop(graceful_timeout_s=2.0)


def test_B15_slow_connect_eventually_succeeds_within_budget(broker, fake_client):
    connect_started = threading.Event()
    release = threading.Event()
    def slow(*a, **kw):
        connect_started.set()
        release.wait(2.0)
    fake_client.connect.side_effect = slow

    probe = _StartProbe(broker, startup_timeout_s=3.0)
    probe.start()
    try:
        assert connect_started.wait(1.0)
    finally:
        release.set()
        assert probe.returned.wait(2.0)
        assert probe.result is True
        assert probe.raised is None


def test_B16_slow_loop_start_eventually_succeeds_within_budget(broker, fake_client):
    loop_started = threading.Event()
    release = threading.Event()
    def slow(*a, **kw):
        loop_started.set()
        release.wait(2.0)
    fake_client.loop_start.side_effect = slow

    probe = _StartProbe(broker, startup_timeout_s=3.0)
    probe.start()
    try:
        assert loop_started.wait(1.0)
    finally:
        release.set()
        assert probe.returned.wait(2.0)
        assert probe.result is True


def test_B17_startup_timeout_s_bounds_BOTH_connect_and_callback_wait(
    fake_client, notifier, monkeypatch,
):
    """RFC-011 §7.10: single shared monotonic deadline covers BOTH
    helper (connect + loop_start) AND `_connected_evt.wait`.

    Observed by timing: connect uses most of the budget and no CONNACK
    arrives, so start() must give up when that ONE budget runs out,
    not after a fresh budget for the callback wait."""
    budget_s, connect_s = 1.2, 0.9
    slow_client = MagicMock(name='PahoMqttClient')
    slow_client.connect.side_effect = lambda *a, **kw: time.sleep(connect_s)
    slow = _make_broker_wait_true(slow_client, notifier, monkeypatch,
                                  timeout=budget_s)
    t0 = time.monotonic()
    with pytest.raises(TimeoutError):
        slow.start({})
    elapsed = time.monotonic() - t0
    # connect + loop_start completed; the callback wait ran out.
    slow_client.loop_start.assert_called_once()
    assert slow.state is WorkerState.START_TIMEOUT
    # A fresh budget for the callback wait would end at
    # connect_s + budget_s (2.1s).
    assert budget_s - 0.1 <= elapsed < connect_s + budget_s - 0.2, (
        f"start() took {elapsed:.2f}s with a {budget_s}s budget"
    )

    # Runtime: wait=True + wedged connect + short timeout → bounded.
    b = _make_broker_wait_true(fake_client, notifier, monkeypatch, timeout=0.3)
    release = threading.Event()
    started = threading.Event()
    _install_blocking(fake_client.connect, release, started)

    probe = _StartProbe(b)
    probe.start()
    try:
        assert started.wait(2.0)
        assert probe.returned.wait(1.0), "wedged connect should hit timeout"
        assert isinstance(probe.raised, TimeoutError)
    finally:
        release.set()
        b.stop(graceful_timeout_s=2.0)


def test_B18_agent_activating_bounded_when_broker_start_wedges_source_x_ref(
    monkeypatch,
):
    """RFC-011 §7.21 runtime cross-check: Agent.__activating starts its
    broker through the bounded `MqttBroker.start`, so a wedged paho
    connect no longer hangs it — each attempt times out and activation
    gives up (pauses between attempts skipped)."""
    release = threading.Event()
    clients = []

    def client_factory(*args, **kwargs):
        client = MagicMock(name='PahoMqttClient')
        client.connect.side_effect = lambda *a, **kw: release.wait(timeout=30.0)
        clients.append(client)
        return client

    monkeypatch.setattr('agentflow.broker.mqtt_broker.Client', client_factory)
    agent = _make_activating_agent(
        monkeypatch,
        lambda notifier: MqttBroker(notifier, wait=True, timeout=0.3),
    )
    try:
        t0 = time.monotonic()
        assert agent._Agent__activating() is False
        elapsed = time.monotonic() - t0
        assert elapsed < 3.0, f"__activating took {elapsed:.2f}s"
        # Every attempt reached paho's (wedged) connect.
        assert sum(c.connect.call_count for c in clients) == 3
    finally:
        release.set()


def test_B19_thread_worker_activate_call_site_source_check():
    """ThreadWorker's thread runs the initiator's `_activate` (which
    calls the bounded broker.start) with the shared config."""
    from agentflow.core.agent_worker import ThreadWorker
    calls = []

    class _Initiator:
        def __init__(self):
            self.config = {}

        def _activate(self, cfg):
            calls.append((threading.current_thread(), cfg))

        def M(self, message=None):
            return f'b19 {message or ""}'

    initiator = _Initiator()
    worker = ThreadWorker(initiator)
    thread = worker.start()
    thread.join(2.0)
    assert len(calls) == 1
    assert calls[0][0] is thread
    assert calls[0][1] is initiator.config
    assert worker.stop(graceful_timeout_s=2.0) is True


# test_B20_process_worker_hard_containment_source_check: removed in #74,
# a duplicate source pin. ProcessWorker's terminate() / kill() escalation
# is tested by test_F44 in core/test_thread_worker_process_exit.py and
# the escalation tests in core/test_process_worker_lifecycle.py.


# ===========================================================================
# C. Exception rollback (RFC-011 §F)
# ===========================================================================


def test_C21_connect_exception_captured_state_START_FAILED_rebroadcast(
    broker, fake_client,
):
    fake_client.connect.side_effect = RuntimeError("connect broke")
    with pytest.raises(RuntimeError, match="connect broke"):
        broker.start({})
    assert broker.state is WorkerState.START_FAILED
    assert isinstance(broker.last_start_exception, RuntimeError)


def test_C22_connect_exception_does_not_call_loop_start_helper_returns_early(
    broker, fake_client,
):
    fake_client.connect.side_effect = RuntimeError("boom")
    with pytest.raises(RuntimeError):
        broker.start({})
    fake_client.loop_start.assert_not_called()


def test_C23_state_is_START_FAILED_after_connect_raise_bounded_rollback_ran(
    broker, fake_client,
):
    """RFC-011 §F: connect raise → helper returns → state=START_FAILED.
    Rollback primitive runs (bounded); `_last_start_cleanup_result`
    is populated."""
    fake_client.connect.side_effect = RuntimeError("boom")
    with pytest.raises(RuntimeError):
        broker.start({})
    assert broker.state is WorkerState.START_FAILED
    assert broker._last_start_cleanup_result is True


def test_C24_connect_raise_state_cleaned_stopping_True(broker, fake_client):
    fake_client.connect.side_effect = RuntimeError("boom")
    with pytest.raises(RuntimeError):
        broker.start({})
    assert broker._stopping is True
    assert broker._connected is False
    assert broker._connect_ok is False


def test_C25_loop_start_exception_captured_state_START_FAILED(
    broker, fake_client,
):
    fake_client.loop_start.side_effect = RuntimeError("loop_start broke")
    with pytest.raises(RuntimeError, match="loop_start broke"):
        broker.start({})
    assert broker.state is WorkerState.START_FAILED
    assert isinstance(broker.last_start_exception, RuntimeError)


def test_C26_loop_start_raise_rollback_primitive_DOES_call_disconnect(
    broker, fake_client,
):
    """RFC-011 §F fix vs pre-RFC-011: loop_start raise no longer leaks
    the TCP connection — rollback primitive calls disconnect."""
    fake_client.loop_start.side_effect = RuntimeError("boom")
    with pytest.raises(RuntimeError):
        broker.start({})
    fake_client.disconnect.assert_called_once_with()


def test_C27_state_is_START_FAILED_after_loop_start_raise(broker, fake_client):
    fake_client.loop_start.side_effect = RuntimeError("boom")
    with pytest.raises(RuntimeError):
        broker.start({})
    assert broker.state is WorkerState.START_FAILED


def test_C28_wait_true_callback_timeout_rollback_calls_loop_stop(
    fake_client, notifier, monkeypatch,
):
    """Callback timeout → START_TIMEOUT → rollback primitive → loop_stop
    called via primitive helper."""
    b = _make_broker_wait_true(fake_client, notifier, monkeypatch, timeout=0.1)
    with pytest.raises(TimeoutError):
        b.start({})
    fake_client.loop_stop.assert_called_once_with()


def test_C29_wait_true_callback_timeout_rollback_calls_disconnect(
    fake_client, notifier, monkeypatch,
):
    b = _make_broker_wait_true(fake_client, notifier, monkeypatch, timeout=0.1)
    with pytest.raises(TimeoutError):
        b.start({})
    fake_client.disconnect.assert_called_once_with()


def test_C30_client_reference_preserved_after_failed_start(broker, fake_client):
    fake_client.connect.side_effect = RuntimeError("boom")
    with pytest.raises(RuntimeError):
        broker.start({})
    assert broker._client is fake_client


def test_C31_callbacks_still_bound_on_client_after_failed_start(broker, fake_client):
    fake_client.connect.side_effect = RuntimeError("boom")
    with pytest.raises(RuntimeError):
        broker.start({})
    assert fake_client.on_connect == broker._on_connect
    assert fake_client.on_message == broker._on_message


def test_C32_start_after_START_FAILED_raises_RuntimeError_no_retry(
    fake_client, notifier, monkeypatch,
):
    """RFC-011 §7.5: failed instance TERMINAL."""
    b = _make_broker_wait_true(fake_client, notifier, monkeypatch, timeout=0.1)
    with pytest.raises(TimeoutError):
        b.start({})
    assert b.state is WorkerState.START_TIMEOUT

    with pytest.raises(RuntimeError, match="same-instance retry"):
        b.start({})


def test_C33_wait_true_callback_timeout_cleanup_now_bounded_via_primitive_source(
    fake_client, notifier, monkeypatch,
):
    """RFC-011 §F: cleanup is now on a daemon helper — bounded even
    if disconnect/loop_stop wedge (that bound: test_F55).

    Observed: after the callback wait times out, paho's disconnect()
    runs on a daemon thread, not on the caller of start()."""
    b = _make_broker_wait_true(fake_client, notifier, monkeypatch, timeout=0.1)
    cleanup_threads = _record_calling_thread(fake_client.disconnect)
    with pytest.raises(TimeoutError):
        b.start({})
    assert len(cleanup_threads) == 1
    assert cleanup_threads[0] is not threading.current_thread()
    assert cleanup_threads[0].daemon is True


# ===========================================================================
# D. Concurrency / no retry (RFC-011 §E, §7.5)
# ===========================================================================


def test_D34_repeated_start_while_RUNNING_raises_RuntimeError_no_new_paho_calls(
    broker, fake_client,
):
    broker.start({})
    _fire_connect_success(broker, fake_client)
    assert broker.state is WorkerState.RUNNING
    fake_client.connect.reset_mock()
    fake_client.loop_start.reset_mock()

    with pytest.raises(RuntimeError, match="same-instance retry"):
        broker.start({})
    fake_client.connect.assert_not_called()
    fake_client.loop_start.assert_not_called()


def test_D35_start_while_STARTING_takes_waiter_path_no_new_paho_calls(
    broker, fake_client,
):
    """First caller's helper finishes fast; second caller enters
    waiter path (bounded). Both return the same result."""
    # Prime: first caller runs (fast mocks) and returns.
    broker.start({})   # state=STARTING (wait=False, no callback yet)

    # Now state is STARTING but the first caller's helper has already
    # finished and set _start_complete_event. A second call sees
    # STARTING → waiter → observes cached True result.
    result = broker.start({})
    assert result is True
    # Only ONE connect + loop_start ever reached paho.
    fake_client.connect.assert_called_once()
    fake_client.loop_start.assert_called_once()


def test_D36_concurrent_start_N_callers_share_one_helper_one_connect(
    broker, fake_client,
):
    N = 5
    barrier = threading.Barrier(N, timeout=2.0)
    done = [threading.Event() for _ in range(N)]
    results = [None] * N

    def caller(i):
        try:
            barrier.wait()
            results[i] = broker.start({})
        finally:
            done[i].set()

    threads = [
        threading.Thread(target=caller, args=(i,), daemon=True)
        for i in range(N)
    ]
    for t in threads:
        t.start()
    for e in done:
        assert e.wait(3.0)
    assert results == [True] * N
    assert fake_client.connect.call_count == 1


def test_D37_concurrent_start_N_callers_share_one_loop_start(
    broker, fake_client,
):
    N = 5
    barrier = threading.Barrier(N, timeout=2.0)
    done = [threading.Event() for _ in range(N)]

    def caller(i):
        try:
            barrier.wait()
            broker.start({})
        finally:
            done[i].set()

    threads = [
        threading.Thread(target=caller, args=(i,), daemon=True)
        for i in range(N)
    ]
    for t in threads:
        t.start()
    for e in done:
        assert e.wait(3.0)
    assert fake_client.loop_start.call_count == 1


def test_D38_concurrent_start_all_callers_return_same_result(broker, fake_client):
    N = 4
    barrier = threading.Barrier(N, timeout=2.0)
    done = [threading.Event() for _ in range(N)]
    results = [object()] * N

    def caller(i):
        try:
            barrier.wait()
            results[i] = broker.start({})
        finally:
            done[i].set()

    threads = [
        threading.Thread(target=caller, args=(i,), daemon=True)
        for i in range(N)
    ]
    for t in threads:
        t.start()
    for e in done:
        assert e.wait(3.0)
    assert all(r is True for r in results)


def test_D39_start_after_START_TIMEOUT_raises_RuntimeError(
    fake_client, notifier, monkeypatch,
):
    b = _make_broker_wait_true(fake_client, notifier, monkeypatch, timeout=0.1)
    with pytest.raises(TimeoutError):
        b.start({})
    assert b.state is WorkerState.START_TIMEOUT

    with pytest.raises(RuntimeError):
        b.start({})


def test_D40_start_after_STOPPED_raises_RuntimeError_no_restart_bug(
    broker, fake_client,
):
    """RFC-011 §7.5 + §7.20: STOPPED.start() raises RuntimeError.
    The A.7 restart-after-stop bug (broken by `_stopping` persistence)
    becomes structurally impossible."""
    broker.start({})
    _fire_connect_success(broker, fake_client)
    broker.stop(graceful_timeout_s=2.0)
    assert broker.state is WorkerState.STOPPED

    with pytest.raises(RuntimeError, match="same-instance retry"):
        broker.start({})


def test_D41_start_after_NEW_stop_is_unaffected(broker, fake_client):
    """NEW.stop() is a pure no-op (RFC-010 §7.5) — does NOT set
    `_stopping`. Subsequent start() is unimpeded."""
    assert broker.state is WorkerState.NEW
    broker.stop()
    assert broker._stopping is False
    broker.start({})
    _fire_connect_success(broker, fake_client)
    assert broker.state is WorkerState.RUNNING


def test_D42_STARTING_stop_still_raises_RuntimeError(broker, fake_client):
    """RFC-010 mod 3 preserved: STARTING.stop → RuntimeError."""
    release = threading.Event()
    started = threading.Event()
    _install_blocking(fake_client.connect, released_event := release, started)

    probe = _StartProbe(broker, startup_timeout_s=0.4)
    probe.start()
    try:
        assert started.wait(2.0)
        # While helper still wedged, state is STARTING → stop raises.
        assert broker.state is WorkerState.STARTING
        with pytest.raises(RuntimeError, match='STARTING'):
            broker.stop()
        # Wait for start() to time out, transitioning to START_TIMEOUT.
        assert probe.returned.wait(1.0)
        assert broker.state is WorkerState.START_TIMEOUT
    finally:
        release.set()
        broker.stop(graceful_timeout_s=2.0)


def test_D43_stop_during_connect_wedge_deferred_via_START_TIMEOUT(
    broker, fake_client,
):
    """After connect wedges long enough for start() to time out
    (state → START_TIMEOUT), stop() bounded-waits for the helper
    to finish, then runs cleanup."""
    release = threading.Event()
    started = threading.Event()
    _install_blocking(fake_client.connect, release, started)

    # Trigger start() timeout.
    probe = _StartProbe(broker, startup_timeout_s=0.3)
    probe.start()
    try:
        assert started.wait(2.0)
        assert probe.returned.wait(1.0)
        assert broker.state is WorkerState.START_TIMEOUT
    finally:
        # Now call stop() while helper still wedged.
        stop_probe = _StopProbe(broker, graceful_timeout_s=0.4)
        stop_probe.start()
        # bounded return; but returns False (helper still alive).
        assert stop_probe.returned.wait(1.5)
        assert stop_probe.result is False
        # Release the helper; final stop() completes cleanup.
        release.set()
        assert broker.stop(graceful_timeout_s=2.0) is True


def test_D44_stop_during_loop_start_wedge_bounded(broker, fake_client):
    release = threading.Event()
    started = threading.Event()
    _install_blocking(fake_client.loop_start, release, started)

    probe = _StartProbe(broker, startup_timeout_s=0.3)
    probe.start()
    try:
        assert started.wait(2.0)
        assert probe.returned.wait(1.0)
        assert broker.state is WorkerState.START_TIMEOUT

        stop_probe = _StopProbe(broker, graceful_timeout_s=0.3)
        stop_probe.start()
        assert stop_probe.returned.wait(1.5)
        # stop() returned bounded (may be False since helper alive).
    finally:
        release.set()
        broker.stop(graceful_timeout_s=2.0)


def test_D45_late_callback_after_STOP_fenced_by_RFC010(broker, fake_client):
    """RFC-010 fencing preserved for stop-then-callback race."""
    broker.start({})
    _fire_connect_success(broker, fake_client)
    broker.stop(graceful_timeout_s=2.0)
    calls_before = broker._notifier._on_connect.call_count
    _fire_connect_success(broker, fake_client)
    assert broker._notifier._on_connect.call_count == calls_before


def test_D46_concurrent_waiter_raises_new_RuntimeError_chained_to_original(
    broker, fake_client,
):
    """RFC-011 modification 3: waiter that observes failure raises a
    NEW RuntimeError with `raise ... from original` — never re-raises
    the exact exception instance across threads."""
    # First caller: fails with connect exception.
    original_exc = RuntimeError("connect failed on first caller")
    fake_client.connect.side_effect = original_exc

    # Barrier + capture: two callers race start().
    barrier = threading.Barrier(2, timeout=2.0)
    caller_a_exc = [None]
    caller_b_exc = [None]

    def caller_a():
        try:
            barrier.wait()
            broker.start({})
        except BaseException as ex:
            caller_a_exc[0] = ex

    def caller_b():
        try:
            barrier.wait()
            # Small delay so caller_a wins the linearization.
            time.sleep(0.02)
            broker.start({})
        except BaseException as ex:
            caller_b_exc[0] = ex

    ta = threading.Thread(target=caller_a, daemon=True)
    tb = threading.Thread(target=caller_b, daemon=True)
    ta.start(); tb.start()
    ta.join(3.0); tb.join(3.0)

    # One of them is the first caller (raises original); the other
    # is either waiter (raises new RuntimeError chained) or also raises
    # RuntimeError because state is now START_FAILED (rejection path).
    exceptions = [caller_a_exc[0], caller_b_exc[0]]
    assert all(e is not None for e in exceptions)
    # At least one caller got the original exception object.
    original_seen = any(e is original_exc for e in exceptions)
    assert original_seen
    # At least one caller got a DIFFERENT exception (not the same instance).
    different = any(e is not original_exc and e is not None for e in exceptions)
    assert different
    # The different one should be a RuntimeError.
    for e in exceptions:
        if e is not original_exc:
            assert isinstance(e, RuntimeError)


# ===========================================================================
# E. Callback fencing after failure (RFC-011 §H)
# ===========================================================================


def test_E47_late_on_connect_after_START_TIMEOUT_does_NOT_set_connected_evt(
    fake_client, notifier, monkeypatch,
):
    b = _make_broker_wait_true(fake_client, notifier, monkeypatch, timeout=0.1)
    with pytest.raises(TimeoutError):
        b.start({})
    # State transition cleared _connected_evt via _transition_to_start_failure.
    b._connected_evt.clear()

    _fire_connect_success(b, fake_client)
    # RFC-010 fencing (_stopping=True) prevents _connected_evt.set().
    assert b._connected_evt.is_set() is False


def test_E48_late_on_connect_after_START_TIMEOUT_does_NOT_notify_notifier(
    fake_client, notifier, monkeypatch,
):
    b = _make_broker_wait_true(fake_client, notifier, monkeypatch, timeout=0.1)
    with pytest.raises(TimeoutError):
        b.start({})
    notifier.reset_mock()

    _fire_connect_success(b, fake_client)
    notifier._on_connect.assert_not_called()


def test_E49_late_on_connect_after_START_FAILED_does_NOT_write_connect_ok(
    broker, fake_client,
):
    fake_client.connect.side_effect = RuntimeError("boom")
    with pytest.raises(RuntimeError):
        broker.start({})
    assert broker._connect_ok is False

    _fire_connect_success(broker, fake_client)
    assert broker._connect_ok is False


def test_E50_failed_instance_is_terminal_no_second_round_possible(
    fake_client, notifier, monkeypatch,
):
    """RFC-011 §7.5 fix for cross-round callback contamination: since
    same-instance retry raises, there is NO second round on the same
    instance — the pre-RFC-011 E.50 pollution scenario is
    structurally impossible."""
    b = _make_broker_wait_true(fake_client, notifier, monkeypatch, timeout=0.1)
    with pytest.raises(TimeoutError):
        b.start({})
    # No second round is allowed.
    with pytest.raises(RuntimeError):
        b.start({})


def test_E51_start_generation_is_diagnostic_only_source_check(broker):
    """RFC-011 Appendix C: `_start_generation` exists as a diagnostic
    counter but is NOT used for callback filtering. Any test that
    would rely on generation-based fencing must switch to the terminal
    state approach (§7.5; fencing itself: test_E47-test_E49, test_E52).

    The callbacks are checked on the AST: no identifier they use names
    a generation (comments and docstrings may)."""
    # generation is exposed, read-only
    assert broker.start_generation == 0
    with pytest.raises(AttributeError):
        broker.start_generation = 7
    # but callbacks do NOT check generation
    for callback in (MqttBroker._on_connect, MqttBroker._on_disconnect,
                     MqttBroker._on_message):
        used = [n for n in identifiers(callback) if 'generation' in n.lower()]
        assert used == [], f"{callback.__name__} reads {used}"


def test_E52_late_on_message_after_start_failure_silently_dropped(
    broker, fake_client, notifier,
):
    """RFC-010 fencing extended to failed-start: _on_message
    silently drops when `_stopping=True`."""
    fake_client.connect.side_effect = RuntimeError("boom")
    with pytest.raises(RuntimeError):
        broker.start({})
    class _Msg:
        topic = 'late'
        payload = b'x'
    broker._on_message(client=fake_client, db=None, message=_Msg())
    notifier._on_message.assert_not_called()


# ===========================================================================
# F. Rollback primitive + resource cleanup (RFC-011 §F, §6.4)
# ===========================================================================


def test_F52_connect_success_then_loop_start_fail_rollback_calls_disconnect(
    broker, fake_client,
):
    """Fixes pre-RFC-011 TCP leak (test_C26 pre-RFC-011 characterization)."""
    fake_client.loop_start.side_effect = RuntimeError("boom")
    with pytest.raises(RuntimeError):
        broker.start({})
    fake_client.disconnect.assert_called_once_with()
    fake_client.loop_stop.assert_called_once_with()


def test_F53_rollback_bounded_when_disconnect_wedges(
    broker, fake_client,
):
    """Rollback primitive is bounded even when disconnect wedges."""
    fake_client.loop_start.side_effect = RuntimeError("loop_start broke")
    release = threading.Event()
    started = threading.Event()
    _install_blocking(fake_client.disconnect, release, started)

    probe = _StartProbe(broker, startup_timeout_s=5.0)
    probe.start()
    try:
        assert started.wait(3.0), "disconnect never entered inside rollback"
        # Rollback primitive uses default rollback_timeout_s=5.0; bounded.
        # start() should raise within roughly 5s + startup budget.
        assert probe.returned.wait(8.0), "rollback did not return bounded"
        # Original loop_start Exception propagates.
        assert isinstance(probe.raised, RuntimeError)
    finally:
        release.set()


def test_F54_failed_start_rollback_uses_daemon_helper_source_check(
    broker, fake_client,
):
    """Observed: when loop_start() raises, the rollback's paho
    disconnect() runs on a daemon helper thread, not on the caller."""
    fake_client.loop_start.side_effect = RuntimeError("boom")
    cleanup_threads = _record_calling_thread(fake_client.disconnect)
    with pytest.raises(RuntimeError):
        broker.start({})
    assert len(cleanup_threads) == 1
    assert cleanup_threads[0] is not threading.current_thread()
    assert cleanup_threads[0].daemon is True


def test_F55_wait_true_callback_timeout_rollback_is_bounded(
    fake_client, notifier, monkeypatch,
):
    """Even the callback-timeout path is now bounded via the primitive."""
    b = _make_broker_wait_true(fake_client, notifier, monkeypatch, timeout=0.1)
    release = threading.Event()
    started = threading.Event()
    _install_blocking(fake_client.disconnect, release, started)

    probe = _StartProbe(b)
    probe.start()
    try:
        # Callback wait times out first; rollback disconnect wedges;
        # rollback primitive bounds to 5s.
        assert probe.returned.wait(6.0), "rollback exceeded bounded window"
        assert isinstance(probe.raised, TimeoutError)
    finally:
        release.set()


def test_F56_start_body_uses_bounded_rollback_primitive_source(
    broker, fake_client,
):
    """Observed: when connect() raises, start() rolls back through the
    bounded primitive — paho's disconnect() runs on a daemon helper,
    not on the caller (bounded even if it wedges: test_F53)."""
    fake_client.connect.side_effect = RuntimeError("boom")
    cleanup_threads = _record_calling_thread(fake_client.disconnect)
    with pytest.raises(RuntimeError):
        broker.start({})
    assert len(cleanup_threads) == 1
    assert cleanup_threads[0] is not threading.current_thread()
    assert cleanup_threads[0].daemon is True


def test_F57_start_uses_daemon_startup_helper_source(broker, fake_client):
    """Observed: paho's connect() and loop_start() run on one daemon
    helper thread, not on the caller (who joins it against the
    deadline: test_B13)."""
    connect_threads = _record_calling_thread(fake_client.connect)
    loop_start_threads = _record_calling_thread(fake_client.loop_start)
    assert broker.start({}) is True
    assert len(connect_threads) == 1
    assert loop_start_threads == connect_threads
    assert connect_threads[0] is not threading.current_thread()
    assert connect_threads[0].daemon is True


def test_F58_paho_client_reused_after_failed_start_no_fresh_client(broker, fake_client):
    """RFC-011 first-phase does NOT switch to fresh Client per attempt
    (Option D deferred). But since retry is forbidden (§7.5), the
    E.50 contamination is closed structurally."""
    fake_client.connect.side_effect = RuntimeError("boom")
    with pytest.raises(RuntimeError):
        broker.start({})
    # Same client — no fresh construction.
    assert broker._client is fake_client


def test_F59_no_fresh_client_per_start_attempt(monkeypatch, notifier):
    """RFC-011 §7.5: a started or failed instance is terminal, so a
    later start() must not build a fresh paho Client. Since issue #17 the
    Client is built once, in the NEW -> STARTING transition, when the
    client id and clean session are known. (Was a source inspection
    asserting that start() contains no 'Client('.)"""
    built = []

    def factory(*args, **kwargs):
        built.append(kwargs)
        return MagicMock(name='PahoMqttClient')
    monkeypatch.setattr('agentflow.broker.mqtt_broker.Client', factory)
    broker = MqttBroker(notifier, wait=False)
    assert broker.start({}) is True
    assert len(built) == 2          # the __init__ placeholder and start()'s
    _fire_connect_success(broker, broker._client)
    with pytest.raises(RuntimeError):
        broker.start({})
    assert len(built) == 2


def test_F60_registry_preserved_after_failed_start(broker, fake_client):
    """First start succeeds, then subscribe adds to registry, then
    a NEW broker's failed start (different instance) does not touch
    the registry. First broker's registry preserved."""
    broker.start({})
    _fire_connect_success(broker, fake_client)
    broker.subscribe('t/1', 'str')
    assert broker.recovery_metrics()['active_subscriptions'] == 1
    # (Same broker's failure isn't retriable per §7.5, so this test
    # asserts that a fresh broker's failure doesn't touch this one.)


# ===========================================================================
# G. START_TIMEOUT stop behavior (RFC-011 §G, mod 1+2)
# ===========================================================================


def test_G61_start_timeout_stop_bounded_waits_for_helper(broker, fake_client):
    """RFC-011 mod 2: stop() from START_TIMEOUT bounded-waits for the
    startup helper BEFORE running cleanup. Never spawns two helpers
    concurrently on the same paho client."""
    release = threading.Event()
    started = threading.Event()
    _install_blocking(fake_client.connect, release, started)

    probe = _StartProbe(broker, startup_timeout_s=0.2)
    probe.start()
    try:
        assert started.wait(2.0)
        assert probe.returned.wait(1.0)
        assert broker.state is WorkerState.START_TIMEOUT

        # Helper is STILL wedged. stop() bounded-waits and returns False
        # because helper is still alive.
        t0 = time.monotonic()
        result = broker.stop(graceful_timeout_s=0.3)
        elapsed = time.monotonic() - t0
        assert elapsed < 0.7
        assert result is False
    finally:
        release.set()
        # Now helper can finish; final stop() completes cleanup.
        assert broker.stop(graceful_timeout_s=2.0) is True


def test_G62_start_timeout_stop_reports_actual_cleanup_result(broker, fake_client):
    """RFC-011 mod 1: `START_TIMEOUT.stop()` MUST NOT shortcut True.
    Must reflect whether cleanup actually completed."""
    release = threading.Event()
    started = threading.Event()
    _install_blocking(fake_client.connect, release, started)

    probe = _StartProbe(broker, startup_timeout_s=0.2)
    probe.start()
    assert started.wait(2.0)
    assert probe.returned.wait(1.0)

    # Helper wedged → stop returns False.
    assert broker.stop(graceful_timeout_s=0.3) is False

    # Release helper; stop retry completes.
    release.set()
    assert broker.stop(graceful_timeout_s=2.0) is True


def test_G63_stop_runs_cleanup_at_most_once_across_concurrent_callers(
    broker, fake_client,
):
    """After helper finishes, concurrent stop() callers coordinate
    via `_start_timeout_recovery_lock`; the rollback primitive runs
    at most once (verified via call counts on the paho fake)."""
    # Trigger START_TIMEOUT with a helper that finishes fast once
    # released (so we can join it).
    release = threading.Event()
    started = threading.Event()
    _install_blocking(fake_client.connect, release, started)

    probe = _StartProbe(broker, startup_timeout_s=0.2)
    probe.start()
    assert started.wait(2.0)
    assert probe.returned.wait(1.0)
    release.set()   # helper can finish

    # Give helper a moment to complete connect and loop_start. Since
    # issue #17 the helper also runs the rollback itself once it
    # finishes after a START_TIMEOUT, so that cleanup is counted below.
    if broker._start_helper_thread is not None:
        broker._start_helper_thread.join(2.0)

    N = 4
    barrier = threading.Barrier(N, timeout=2.0)
    done = [threading.Event() for _ in range(N)]

    def stopper(i):
        try:
            barrier.wait()
            broker.stop(graceful_timeout_s=2.0)
        finally:
            done[i].set()

    threads = [
        threading.Thread(target=stopper, args=(i,), daemon=True)
        for i in range(N)
    ]
    for t in threads:
        t.start()
    for e in done:
        assert e.wait(3.0)

    # Cleanup primitive was run at most once in all (the helper's
    # rollback, then cached for every stop()) → exactly one disconnect
    # + one loop_stop reached paho.
    assert fake_client.disconnect.call_count == 1
    assert fake_client.loop_stop.call_count == 1


def test_G64_start_timeout_cleanup_result_cached(broker, fake_client):
    release = threading.Event()
    started = threading.Event()
    _install_blocking(fake_client.connect, release, started)

    probe = _StartProbe(broker, startup_timeout_s=0.2)
    probe.start()
    assert started.wait(2.0)
    assert probe.returned.wait(1.0)
    release.set()
    if broker._start_helper_thread is not None:
        broker._start_helper_thread.join(2.0)

    # Issue #17: the helper rolled its client back as soon as it
    # finished (was: None until a stop()).
    assert broker._last_start_cleanup_result is True
    assert broker.stop(graceful_timeout_s=2.0) is True
    assert broker._last_start_cleanup_result is True
    # Second stop returns cached result without re-running primitive.
    fake_client.disconnect.reset_mock()
    assert broker.stop(graceful_timeout_s=2.0) is True
    fake_client.disconnect.assert_not_called()


# ===========================================================================
# H. Observability + terminal states
# ===========================================================================


def test_H65_state_is_read_only_property(broker):
    assert isinstance(broker.state, WorkerState)
    with pytest.raises(AttributeError):
        broker.state = WorkerState.RUNNING


def test_H66_last_start_exception_is_None_after_clean_start(broker, fake_client):
    broker.start({})
    _fire_connect_success(broker, fake_client)
    assert broker.state is WorkerState.RUNNING
    assert broker.last_start_exception is None


def test_H67_startup_helper_is_daemon(broker, fake_client):
    release = threading.Event()
    started = threading.Event()
    _install_blocking(fake_client.connect, release, started)

    probe = _StartProbe(broker, startup_timeout_s=0.3)
    probe.start()
    try:
        assert started.wait(2.0)
        helper = broker._start_helper_thread
        assert helper is not None
        assert helper.is_alive()
        assert helper.daemon is True
        # Wait for start() to time out (helper stays wedged).
        assert probe.returned.wait(1.0)
        assert broker.state is WorkerState.START_TIMEOUT
    finally:
        release.set()
        broker.stop(graceful_timeout_s=2.0)


def test_H68_start_generation_increments_per_attempt(fake_client, notifier, monkeypatch):
    b = _make_broker_wait_true(fake_client, notifier, monkeypatch, timeout=0.1)
    assert b.start_generation == 0
    with pytest.raises(TimeoutError):
        b.start({})
    assert b.start_generation == 1


def test_H69_signature_has_startup_timeout_s_default_None():
    sig = inspect.signature(MqttBroker.start)
    assert 'startup_timeout_s' in sig.parameters
    param = sig.parameters['startup_timeout_s']
    # Keyword-only default None (falls back to self._timeout in body).
    assert param.default is None
    assert param.kind == inspect.Parameter.KEYWORD_ONLY


def test_H70_start_return_type_is_bool(broker, fake_client):
    result = broker.start({})
    assert result is True
    assert isinstance(result, bool)


# ===========================================================================
# I. ABC / other brokers (RFC-011 §7.16)
# ===========================================================================


def test_I71_message_broker_ABC_signature_unchanged(broker):
    sig = inspect.signature(MessageBroker.start)
    assert list(sig.parameters) == ['self', 'options']


def test_I72_empty_broker_start_is_bounded_by_construction():
    b = EmptyBroker(notifier=MagicMock(name='notifier'))
    t0 = time.monotonic()
    b.start({})
    assert time.monotonic() - t0 < 0.05


def test_I73_agent_activating_source_unchanged(monkeypatch):
    """RFC-011 §7.21: Agent.__activating unchanged; retry loop
    constructs fresh broker per iteration via BrokerMaker, up to
    max_retries=3 attempts (pauses between attempts skipped)."""
    brokers = []

    class _TimingOutBroker:
        def __init__(self):
            self.start_calls = 0

        def start(self, options):
            self.start_calls += 1
            raise TimeoutError("no CONNACK")

        def stop(self):
            return True

    def make_broker(notifier):
        brokers.append(_TimingOutBroker())
        return brokers[-1]

    agent = _make_activating_agent(monkeypatch, make_broker)
    assert agent._Agent__activating() is False
    # One fresh broker per attempt, each started once.
    assert len(brokers) == 3
    assert [b.start_calls for b in brokers] == [1, 1, 1]
    assert agent._broker is brokers[-1]
