"""RFC-010 tests: MqttBroker bounded shutdown lifecycle.

Post-RFC-010 contract:

  - `MqttBroker.stop(graceful_timeout_s=5.0) -> bool`. True = helper
    ran to completion; False = STOP_TIMEOUT (retriable, same helper)
    OR STOP_FAILED (helper died abnormally, cached).
  - Same MqttBroker lifecycle → at most ONE (disconnect + loop_stop)
    pair reaches paho. STOP_TIMEOUT retry re-joins the SAME helper.
  - Concurrent stop() coordinated via `_stop_complete_event` with
    a BOUNDED wait (never `.wait()` without timeout).
  - Callback fencing (§F): post-stop `_on_connect` does NOT set
    `_connected` / `_connect_ok` / `_connected_evt` / recovery /
    notifier; `_on_message` silent-drops; `_on_disconnect` still
    updates planned/unexpected diagnostics but does not touch
    `_stopping` or trigger recovery.
  - stop() at linearization: `_stopping=True`, `_connected=False`,
    `_connect_ok=False`, `_connected_evt.clear()` — all under lock.
  - STARTING.stop() raises RuntimeError (first-phase).
  - `Agent.__deactivating` observes bool return, WARNING on False,
    never raises.
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


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _prime_connected(broker, fake_client):
    """Fire `_on_connect(rc=0)` so the broker transitions to RUNNING
    and subscribe/unsubscribe forward to the paho client."""
    broker._on_connect(
        client=fake_client, userdata=None, flags={},
        reasonCode=0, properties=None,
    )
    fake_client.subscribe.reset_mock()
    fake_client.unsubscribe.reset_mock()


class _StopProbe:
    """Runs `broker.stop(...)` from a daemon controller thread; reports
    outcome via Event so the main thread makes bounded observations
    without blocking on a wedged fake."""

    def __init__(self, broker, **stop_kwargs):
        self.broker = broker
        self.stop_kwargs = stop_kwargs
        self.returned = threading.Event()
        self.result: Optional[bool] = None
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


# ===========================================================================
# A. Basic stop lifecycle (post-RFC-010 contract)
# ===========================================================================


def test_A1_stop_from_RUNNING_reaches_paho_disconnect(broker, fake_client):
    _prime_connected(broker, fake_client)
    assert broker.stop(graceful_timeout_s=2.0) is True
    fake_client.disconnect.assert_called_once_with()


def test_A2_stop_from_RUNNING_reaches_paho_loop_stop(broker, fake_client):
    _prime_connected(broker, fake_client)
    assert broker.stop(graceful_timeout_s=2.0) is True
    fake_client.loop_stop.assert_called_once_with()


def test_A3_stop_calls_disconnect_before_loop_stop_in_helper(broker, fake_client):
    order = []
    fake_client.disconnect.side_effect = lambda *a, **kw: order.append('disconnect')
    fake_client.loop_stop.side_effect = lambda *a, **kw: order.append('loop_stop')
    _prime_connected(broker, fake_client)
    assert broker.stop(graceful_timeout_s=2.0) is True
    assert order == ['disconnect', 'loop_stop']


def test_A4_linearization_flips_stopping_and_clears_active_state_atomically(
    broker, fake_client,
):
    """RFC-010 §G modification 4: at stop linearization, immediately
    set `_stopping=True`, `_connected=False`, `_connect_ok=False`,
    `_connected_evt.clear()` under the same lock section — so any
    inline callback that lands during helper execution observes the
    fenced state."""
    _prime_connected(broker, fake_client)
    assert broker.recovery_metrics()['connected'] is True
    assert broker._connect_ok is True
    assert broker._connected_evt.is_set() is True

    observed = {}

    def capture(*a, **kw):
        observed['stopping'] = broker._stopping
        observed['connected'] = broker.recovery_metrics()['connected']
        observed['connect_ok'] = broker._connect_ok
        observed['connected_evt_set'] = broker._connected_evt.is_set()

    fake_client.disconnect.side_effect = capture
    assert broker.stop(graceful_timeout_s=2.0) is True
    assert observed['stopping'] is True
    assert observed['connected'] is False
    assert observed['connect_ok'] is False
    assert observed['connected_evt_set'] is False


def test_A5_stop_from_NEW_is_pure_noop_returning_True_no_paho_calls(broker, fake_client):
    """RFC-010: NEW.stop() is a pure no-op. Callbacks are not
    registered on the paho client until start(), so no fencing is
    needed and `_stopping` remains False (so subsequent start() is
    unimpeded)."""
    assert broker.state is WorkerState.NEW
    assert broker.stop() is True
    assert broker.state is WorkerState.NEW
    assert broker._stopping is False
    fake_client.disconnect.assert_not_called()
    fake_client.loop_stop.assert_not_called()


def test_A6_stop_from_STOPPED_is_idempotent_no_new_paho_calls(broker, fake_client):
    _prime_connected(broker, fake_client)
    assert broker.stop(graceful_timeout_s=2.0) is True
    assert broker.state is WorkerState.STOPPED
    fake_client.disconnect.reset_mock()
    fake_client.loop_stop.reset_mock()
    # Repeated stop returns cached True without re-calling paho.
    assert broker.stop() is True
    assert broker.stop() is True
    fake_client.disconnect.assert_not_called()
    fake_client.loop_stop.assert_not_called()


def test_A7_state_transitions_NEW_STARTING_RUNNING_STOPPING_STOPPED_happy_path(
    broker, fake_client,
):
    assert broker.state is WorkerState.NEW
    broker.start({})
    # wait=False fixture: state stays STARTING until _on_connect fires.
    assert broker.state is WorkerState.STARTING
    _prime_connected(broker, fake_client)
    assert broker.state is WorkerState.RUNNING
    assert broker.stop(graceful_timeout_s=2.0) is True
    assert broker.state is WorkerState.STOPPED


def test_A8_planned_disconnect_classified_correctly_after_stop(broker, fake_client):
    _prime_connected(broker, fake_client)
    assert broker.stop(graceful_timeout_s=2.0) is True
    # Even a NON-zero reason after stop is classified as planned
    # because _stopping is True.
    broker._on_disconnect(
        client=fake_client, userdata=None, _flags={},
        reasonCode=5, _properties=None,
    )
    assert broker.last_disconnect_was_planned is True


def test_A9_stop_preserves_subscription_registry(broker, fake_client):
    _prime_connected(broker, fake_client)
    broker.subscribe('t/1', 'str')
    broker.subscribe('t/2', 'str')
    assert broker.recovery_metrics()['active_subscriptions'] == 2
    assert broker.stop(graceful_timeout_s=2.0) is True
    assert broker.recovery_metrics()['active_subscriptions'] == 2


def test_A10_subscribe_and_unsubscribe_after_stop_return_None(broker, fake_client):
    _prime_connected(broker, fake_client)
    assert broker.stop(graceful_timeout_s=2.0) is True
    fake_client.subscribe.reset_mock()
    fake_client.unsubscribe.reset_mock()
    assert broker.subscribe('t/x', 'str') is None
    assert broker.unsubscribe('t/x') is None
    fake_client.subscribe.assert_not_called()
    fake_client.unsubscribe.assert_not_called()


# ===========================================================================
# B. Idempotency / concurrency (post-RFC-010)
# ===========================================================================


def test_B11_repeated_stop_is_idempotent_no_extra_paho_calls(broker, fake_client):
    _prime_connected(broker, fake_client)
    assert broker.stop(graceful_timeout_s=2.0) is True
    assert broker.stop() is True
    assert broker.stop() is True
    # Exactly ONE (disconnect + loop_stop) pair reached paho.
    assert fake_client.disconnect.call_count == 1
    assert fake_client.loop_stop.call_count == 1


def test_B12_concurrent_stop_callers_share_one_helper_and_one_paho_pair(
    broker, fake_client,
):
    _prime_connected(broker, fake_client)
    N = 5
    barrier = threading.Barrier(N, timeout=2.0)
    done = [threading.Event() for _ in range(N)]
    results = [None] * N

    def caller(i):
        try:
            barrier.wait()
            results[i] = broker.stop(graceful_timeout_s=2.0)
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
    # All observe the same cached True (helper cooperated).
    assert results == [True] * N
    # Exactly ONE disconnect + loop_stop pair reached paho.
    assert fake_client.disconnect.call_count == 1
    assert fake_client.loop_stop.call_count == 1


def test_B13_concurrent_stop_returns_same_bool_for_every_caller(
    broker, fake_client,
):
    _prime_connected(broker, fake_client)
    N = 4
    barrier = threading.Barrier(N, timeout=2.0)
    done = [threading.Event() for _ in range(N)]
    results = [object()] * N

    def caller(i):
        try:
            barrier.wait()
            results[i] = broker.stop(graceful_timeout_s=2.0)
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


def test_B14_stop_before_start_is_noop_returning_True_state_stays_NEW(
    broker, fake_client,
):
    assert broker.state is WorkerState.NEW
    assert broker.stop() is True
    assert broker.state is WorkerState.NEW
    fake_client.disconnect.assert_not_called()
    fake_client.loop_stop.assert_not_called()


def test_B15_stop_after_start_failure_returns_True_no_paho_double_call(
    fake_client, notifier, monkeypatch,
):
    """RFC-011: start(wait=True) callback timeout path transitions to
    `START_TIMEOUT` (not START_FAILED — which is reserved for paho
    Exception / rc!=0 failures). Because the callback timeout runs
    the bounded rollback primitive (helper thread), paho is called
    exactly once. Subsequent stop() from START_TIMEOUT returns True
    from the cached `_last_start_cleanup_result` without re-calling
    paho."""
    monkeypatch.setattr(
        'agentflow.broker.mqtt_broker.Client', lambda *a, **kw: fake_client,
    )
    b = MqttBroker(notifier=notifier, wait=True, timeout=0.1)
    with pytest.raises(TimeoutError):
        b.start({})
    # RFC-011 §7.2: callback wait timeout → START_TIMEOUT.
    assert b.state is WorkerState.START_TIMEOUT
    # start()'s failure cleanup already invoked paho once each.
    assert fake_client.disconnect.call_count == 1
    assert fake_client.loop_stop.call_count == 1
    # stop() from START_TIMEOUT (helper already finished + cleanup ran) → True.
    assert b.stop() is True
    assert fake_client.disconnect.call_count == 1
    assert fake_client.loop_stop.call_count == 1


def test_B16_STARTING_stop_raises_RuntimeError_first_phase(broker, fake_client):
    """RFC-010 modification 3: STARTING.stop() raises RuntimeError
    rather than trying to disconnect a half-initialised client."""
    broker.start({})
    assert broker.state is WorkerState.STARTING
    with pytest.raises(RuntimeError, match='STARTING'):
        broker.stop()


def test_B17_on_disconnect_during_stop_classifies_as_planned(broker, fake_client):
    """Inline `_on_disconnect` fired during helper execution: state
    is already STOPPING with `_stopping=True`, so it must classify
    as planned regardless of reasonCode."""
    seen = {}

    def slow_loop_stop(*a, **kw):
        broker._on_disconnect(
            client=fake_client, userdata=None, _flags={},
            reasonCode=5, _properties=None,
        )
        seen['classified'] = broker.last_disconnect_was_planned

    fake_client.loop_stop.side_effect = slow_loop_stop
    _prime_connected(broker, fake_client)
    assert broker.stop(graceful_timeout_s=2.0) is True
    assert seen['classified'] is True


def test_B18_stop_concurrent_with_subscribe_makes_subscribe_a_noop(
    broker, fake_client,
):
    seen = {}

    def inline_subscribe(*a, **kw):
        seen['result'] = broker.subscribe('t/late', 'str')
        seen['in_registry'] = 't/late' in broker._registry

    fake_client.disconnect.side_effect = inline_subscribe
    _prime_connected(broker, fake_client)
    assert broker.stop(graceful_timeout_s=2.0) is True
    assert seen['result'] is None
    assert seen['in_registry'] is False


def test_B19_stop_during_recovery_aborts_recovery_loop_early(broker, fake_client):
    _prime_connected(broker, fake_client)
    broker.subscribe('t/1', 'str')
    broker.subscribe('t/2', 'str')
    broker.subscribe('t/3', 'str')
    broker._on_disconnect(
        client=fake_client, userdata=None, _flags={},
        reasonCode=0, _properties=None,
    )
    fake_client.subscribe.reset_mock()

    stop_fired = threading.Event()

    def subscribe_side_effect(*a, **kw):
        if not stop_fired.is_set():
            stop_fired.set()
            # Reach into the state directly (avoid the RuntimeError
            # from STARTING check by ensuring state is RUNNING).
            broker.stop(graceful_timeout_s=1.0)

    fake_client.subscribe.side_effect = subscribe_side_effect
    broker._on_connect(
        client=fake_client, userdata=None, flags={},
        reasonCode=0, properties=None,
    )
    assert fake_client.subscribe.call_count < 3


def test_B20_disconnect_inline_on_disconnect_callback_does_not_deadlock(
    broker, fake_client,
):
    def _inline_on_disconnect(*a, **kw):
        broker._on_disconnect(
            client=fake_client, userdata=None, _flags={},
            reasonCode=0, _properties=None,
        )

    fake_client.disconnect.side_effect = _inline_on_disconnect
    _prime_connected(broker, fake_client)

    probe = _StopProbe(broker, graceful_timeout_s=2.0)
    probe.start()
    assert probe.returned.wait(2.5), "stop() deadlocked with inline _on_disconnect"
    assert probe.result is True
    assert broker.last_disconnect_was_planned is True


# ===========================================================================
# C. Bounded stop when paho wedges (post-RFC-010)
# ===========================================================================


def test_C21_stop_returns_False_STOP_TIMEOUT_when_disconnect_wedges(
    broker, fake_client,
):
    _prime_connected(broker, fake_client)
    release = threading.Event()
    started = threading.Event()
    _install_blocking(fake_client.disconnect, release, started)

    probe = _StopProbe(broker, graceful_timeout_s=0.3)
    probe.start()
    try:
        assert started.wait(2.0), "disconnect was never entered"
        assert probe.returned.wait(1.0), "stop() did not return bounded"
        assert probe.result is False
        assert broker.state is WorkerState.STOP_TIMEOUT
        # loop_stop is stuck in the helper alongside disconnect —
        # helper hasn't reached loop_stop yet.
        fake_client.loop_stop.assert_not_called()
    finally:
        release.set()
        # Give the helper a moment to finish so the daemon thread
        # exits cleanly. Retry stop() re-joins.
        broker.stop(graceful_timeout_s=2.0)


def test_C22_stop_returns_False_STOP_TIMEOUT_when_loop_stop_wedges(
    broker, fake_client,
):
    _prime_connected(broker, fake_client)
    release = threading.Event()
    started = threading.Event()
    _install_blocking(fake_client.loop_stop, release, started)

    probe = _StopProbe(broker, graceful_timeout_s=0.3)
    probe.start()
    try:
        assert started.wait(2.0)
        assert probe.returned.wait(1.0)
        assert probe.result is False
        assert broker.state is WorkerState.STOP_TIMEOUT
        # disconnect DID succeed before loop_stop wedged.
        fake_client.disconnect.assert_called_once_with()
    finally:
        release.set()
        broker.stop(graceful_timeout_s=2.0)


def test_C23_STOP_TIMEOUT_retry_reuses_same_helper_no_new_disconnect_loop_stop(
    broker, fake_client,
):
    """RFC-010 modification 1: retry re-joins the SAME helper. The
    disconnect and loop_stop mocks are called at most ONCE across
    the lifetime of the broker."""
    _prime_connected(broker, fake_client)
    release = threading.Event()
    started = threading.Event()
    _install_blocking(fake_client.disconnect, release, started)

    # First stop() call — times out.
    assert broker.stop(graceful_timeout_s=0.2) is False
    assert broker.state is WorkerState.STOP_TIMEOUT
    original_helper = broker._stop_helper_thread
    assert original_helper is not None
    assert original_helper.is_alive()

    # Retry — still wedged, returns False, SAME helper.
    assert broker.stop(graceful_timeout_s=0.2) is False
    assert broker.state is WorkerState.STOP_TIMEOUT
    assert broker._stop_helper_thread is original_helper

    # disconnect was invoked exactly ONCE (in the helper); the retry
    # did NOT invoke it a second time.
    assert fake_client.disconnect.call_count == 1
    assert fake_client.loop_stop.call_count == 0   # still wedged

    release.set()
    # Cleanup retry: helper finishes, state → STOPPED.
    assert broker.stop(graceful_timeout_s=2.0) is True
    assert broker.state is WorkerState.STOPPED
    # Even after full cleanup: still exactly one disconnect + one loop_stop.
    assert fake_client.disconnect.call_count == 1
    assert fake_client.loop_stop.call_count == 1


def test_C24_STOP_TIMEOUT_retry_reaches_STOPPED_when_paho_unwedges(
    broker, fake_client,
):
    _prime_connected(broker, fake_client)
    release = threading.Event()
    started = threading.Event()
    _install_blocking(fake_client.disconnect, release, started)

    assert broker.stop(graceful_timeout_s=0.2) is False
    assert broker.state is WorkerState.STOP_TIMEOUT
    release.set()
    assert broker.stop(graceful_timeout_s=2.0) is True
    assert broker.state is WorkerState.STOPPED


def test_C25_STOP_TIMEOUT_retry_stays_STOP_TIMEOUT_when_still_wedged(
    broker, fake_client,
):
    _prime_connected(broker, fake_client)
    release = threading.Event()
    started = threading.Event()
    _install_blocking(fake_client.disconnect, release, started)

    try:
        assert broker.stop(graceful_timeout_s=0.2) is False
        assert broker.state is WorkerState.STOP_TIMEOUT
        assert broker.stop(graceful_timeout_s=0.2) is False
        assert broker.state is WorkerState.STOP_TIMEOUT
    finally:
        release.set()
        broker.stop(graceful_timeout_s=2.0)


def test_C26_agent_terminate_still_bounded_when_broker_stop_wedges_source_x_ref():
    """Cross-reference: `Agent.__deactivating` observes `broker.stop`
    bool return. Even when broker.stop returns False (STOP_TIMEOUT),
    Agent.terminate remains bounded via ThreadWorker.stop (RFC-009).
    Source-inspection here; the runtime scenario is in the
    ThreadWorker suite."""
    from agentflow.core.agent import Agent
    src = inspect.getsource(getattr(Agent, '_Agent__deactivating'))
    assert 'self._broker.stop()' in src
    assert 'stopped is False' in src
    assert 'logger.warning' in src


def test_C27_process_worker_hard_containment_still_available_source_check():
    from agentflow.core.agent_worker import ProcessWorker
    src = inspect.getsource(ProcessWorker.stop)
    assert 'terminate()' in src
    assert 'kill()' in src


# ===========================================================================
# D. Exception behavior (post-RFC-010)
# ===========================================================================


def test_D28_disconnect_exception_captured_into_last_stop_exception(
    broker, fake_client,
):
    """RFC-010 §7.9: paho disconnect raise is captured, not raised
    to the caller. Helper continues to loop_stop."""
    fake_client.disconnect.side_effect = RuntimeError("disconnect broke")
    _prime_connected(broker, fake_client)
    result = broker.stop(graceful_timeout_s=2.0)
    assert result is True   # helper completed normally
    assert isinstance(broker.last_stop_exception, RuntimeError)
    assert str(broker.last_stop_exception) == "disconnect broke"
    assert broker.state is WorkerState.STOPPED


def test_D29_disconnect_exception_does_not_prevent_loop_stop(broker, fake_client):
    """RFC-010 §7.8: even when disconnect raises, loop_stop STILL
    runs — resource-leak fix vs the pre-RFC-010 behaviour."""
    fake_client.disconnect.side_effect = RuntimeError("boom")
    _prime_connected(broker, fake_client)
    assert broker.stop(graceful_timeout_s=2.0) is True
    fake_client.loop_stop.assert_called_once_with()


def test_D30_loop_stop_exception_captured_helper_still_completes_STOPPED(
    broker, fake_client,
):
    fake_client.loop_stop.side_effect = RuntimeError("loop_stop broke")
    _prime_connected(broker, fake_client)
    result = broker.stop(graceful_timeout_s=2.0)
    assert result is True
    assert broker.state is WorkerState.STOPPED
    assert isinstance(broker.last_stop_exception, RuntimeError)


def test_D31_first_captured_exception_is_retained_when_both_raise(
    broker, fake_client,
):
    fake_client.disconnect.side_effect = ValueError("disconnect_err")
    fake_client.loop_stop.side_effect = TypeError("loop_stop_err")
    _prime_connected(broker, fake_client)
    assert broker.stop(graceful_timeout_s=2.0) is True
    # RFC-010 §7.9: earlier exception (disconnect) retained.
    assert isinstance(broker.last_stop_exception, ValueError)


def test_D32_stop_state_STOPPED_and_stopping_True_after_captured_exception(
    broker, fake_client,
):
    fake_client.disconnect.side_effect = RuntimeError("x")
    _prime_connected(broker, fake_client)
    assert broker.stop(graceful_timeout_s=2.0) is True
    assert broker._stopping is True
    assert broker.state is WorkerState.STOPPED


def test_D33_BaseException_in_helper_marks_STOP_FAILED_not_STOPPED(
    broker, fake_client,
):
    """RFC-010 modification 2 + §7.15: BaseException in paho kills
    the helper without setting completed_normally. stop() MUST NOT
    mislabel STOPPED — state becomes STOP_FAILED, result=False."""
    class MyKI(BaseException):
        pass
    fake_client.disconnect.side_effect = MyKI("simulated interrupt")
    _prime_connected(broker, fake_client)

    result = broker.stop(graceful_timeout_s=2.0)
    assert result is False
    assert broker.state is WorkerState.STOP_FAILED

    # Idempotent replay of STOP_FAILED returns cached False.
    assert broker.stop() is False
    assert broker.state is WorkerState.STOP_FAILED


# ===========================================================================
# E. Callback-after-stop fencing (post-RFC-010 §F)
# ===========================================================================


def test_E34_delayed_on_connect_rc0_after_stop_skips_notifier_and_recovery(
    broker, fake_client, notifier,
):
    _prime_connected(broker, fake_client)
    broker.subscribe('t/1', 'str')
    notifier.reset_mock()
    fake_client.subscribe.reset_mock()

    assert broker.stop(graceful_timeout_s=2.0) is True

    broker._on_connect(
        client=fake_client, userdata=None, flags={},
        reasonCode=0, properties=None,
    )
    notifier._on_connect.assert_not_called()
    fake_client.subscribe.assert_not_called()


def test_E35_delayed_on_disconnect_after_stop_marks_planned(broker, fake_client):
    _prime_connected(broker, fake_client)
    assert broker.stop(graceful_timeout_s=2.0) is True
    broker._on_disconnect(
        client=fake_client, userdata=None, _flags={},
        reasonCode=7, _properties=None,
    )
    assert broker.last_disconnect_was_planned is True


def test_E36_delayed_on_message_after_stop_is_silently_dropped(
    broker, fake_client, notifier,
):
    """RFC-010 §F rule 1 / modification 4: post-stop `_on_message`
    silent-drops. Notifier is NOT invoked."""
    _prime_connected(broker, fake_client)
    assert broker.stop(graceful_timeout_s=2.0) is True

    class _Msg:
        topic = 'late/topic'
        payload = b'late-payload'

    broker._on_message(client=fake_client, db=None, message=_Msg())
    notifier._on_message.assert_not_called()


def test_E37_delayed_on_connect_after_stop_does_not_set_connected_event(
    broker, fake_client,
):
    """RFC-010 §F rule 3 fix: post-stop `_on_connect` MUST NOT set
    `_connected_evt` (the previous `finally` was unconditional)."""
    _prime_connected(broker, fake_client)
    assert broker.stop(graceful_timeout_s=2.0) is True
    # stop's linearization already cleared the event.
    assert broker._connected_evt.is_set() is False

    broker._on_connect(
        client=fake_client, userdata=None, flags={},
        reasonCode=0, properties=None,
    )
    # Fencing preserved: event stays cleared.
    assert broker._connected_evt.is_set() is False


def test_E38_delayed_on_connect_after_stop_does_not_write_connect_ok(
    broker, fake_client,
):
    """RFC-010 §F rule 2 fix: post-stop `_on_connect(rc=0)` MUST NOT
    write `_connect_ok=True` (the previous write was outside the
    lock and unfenced)."""
    _prime_connected(broker, fake_client)
    assert broker.stop(graceful_timeout_s=2.0) is True
    # stop cleared _connect_ok.
    assert broker._connect_ok is False

    broker._on_connect(
        client=fake_client, userdata=None, flags={},
        reasonCode=0, properties=None,
    )
    # Fencing preserved: _connect_ok stays False.
    assert broker._connect_ok is False
    # _connected also stays False.
    assert broker.recovery_metrics()['connected'] is False


def test_E39_delayed_on_connect_after_stop_does_not_transition_state(
    broker, fake_client,
):
    """State remains STOPPED — the fenced callback must not upgrade
    it back to RUNNING."""
    _prime_connected(broker, fake_client)
    assert broker.stop(graceful_timeout_s=2.0) is True
    assert broker.state is WorkerState.STOPPED

    broker._on_connect(
        client=fake_client, userdata=None, flags={},
        reasonCode=0, properties=None,
    )
    assert broker.state is WorkerState.STOPPED


def test_E40_delayed_on_connect_rc_nonzero_after_stop_also_fences(
    broker, fake_client,
):
    _prime_connected(broker, fake_client)
    assert broker.stop(graceful_timeout_s=2.0) is True
    prev_err = broker._connect_err

    broker._on_connect(
        client=fake_client, userdata=None, flags={},
        reasonCode=5, properties=None,
    )
    # Fencing preserved: no _connect_err write, no _connected_evt set.
    assert broker._connect_err == prev_err
    assert broker._connected_evt.is_set() is False


# ===========================================================================
# F. Concurrent waiter bounded
# ===========================================================================


def test_F41_waiter_uses_bounded_event_wait_by_source_inspection():
    src = inspect.getsource(MqttBroker.stop)
    assert '_stop_complete_event.wait()' not in src, (
        "waiter must not perform an unbounded event.wait()"
    )
    assert re.search(r'_stop_complete_event\.wait\(\s*\S', src)


def test_F42_waiter_returns_bounded_when_first_caller_wedges(broker, fake_client):
    """First caller wedges inside helper join; a second concurrent
    caller enters the waiter path and returns bounded via
    `_stop_complete_event.wait(graceful_timeout_s + 0.1)`."""
    _prime_connected(broker, fake_client)
    release = threading.Event()
    started = threading.Event()
    _install_blocking(fake_client.disconnect, release, started)

    # First caller — will time out at graceful_timeout_s=0.4.
    probe1 = _StopProbe(broker, graceful_timeout_s=0.4)
    probe1.start()
    assert started.wait(2.0)

    # Second caller enters after state is STOPPING — waiter path.
    # Wait a moment to ensure state has flipped.
    time.sleep(0.02)
    probe2 = _StopProbe(broker, graceful_timeout_s=0.4)
    probe2.start()

    try:
        assert probe1.returned.wait(1.5)
        assert probe2.returned.wait(1.5)
        # Both observe the same result (False — STOP_TIMEOUT).
        assert probe1.result is False
        assert probe2.result is False
        # disconnect was invoked exactly once by the ONE helper.
        assert fake_client.disconnect.call_count == 1
    finally:
        release.set()
        broker.stop(graceful_timeout_s=2.0)


# ===========================================================================
# G. State cleanup on stop linearization (RFC-010 §G)
# ===========================================================================


def test_G43_stop_linearization_clears_connected_flag_immediately(
    broker, fake_client,
):
    _prime_connected(broker, fake_client)
    assert broker.recovery_metrics()['connected'] is True
    assert broker.stop(graceful_timeout_s=2.0) is True
    assert broker.recovery_metrics()['connected'] is False


def test_G44_stop_linearization_clears_connect_ok_flag_immediately(
    broker, fake_client,
):
    _prime_connected(broker, fake_client)
    assert broker._connect_ok is True
    assert broker.stop(graceful_timeout_s=2.0) is True
    assert broker._connect_ok is False


def test_G45_stop_linearization_clears_connected_event_immediately(
    broker, fake_client,
):
    _prime_connected(broker, fake_client)
    assert broker._connected_evt.is_set() is True
    assert broker.stop(graceful_timeout_s=2.0) is True
    assert broker._connected_evt.is_set() is False


# ===========================================================================
# H. Observability surface + comparison
# ===========================================================================


def test_H46_state_is_read_only_property_reflecting_lock_protected_field(broker):
    assert isinstance(broker.state, WorkerState)
    with pytest.raises(AttributeError):
        broker.state = WorkerState.RUNNING   # read-only property


def test_H47_last_stop_exception_is_None_after_clean_stop(broker, fake_client):
    _prime_connected(broker, fake_client)
    assert broker.stop(graceful_timeout_s=2.0) is True
    assert broker.last_stop_exception is None


def test_H48_helper_thread_is_daemon(broker, fake_client):
    _prime_connected(broker, fake_client)
    release = threading.Event()
    started = threading.Event()
    _install_blocking(fake_client.disconnect, release, started)

    probe = _StopProbe(broker, graceful_timeout_s=0.3)
    probe.start()
    try:
        assert started.wait(2.0)
        # While the helper is still alive, inspect its daemon flag.
        helper = broker._stop_helper_thread
        assert helper is not None
        assert helper.is_alive()
        assert helper.daemon is True
    finally:
        release.set()
        broker.stop(graceful_timeout_s=2.0)


def test_H49_empty_broker_stop_is_idempotent_and_bounded():
    b = EmptyBroker(notifier=MagicMock(name='notifier'))
    for _ in range(5):
        assert b.stop() is None


def test_H50_message_broker_ABC_signature_unchanged(broker):
    """RFC-010 §7.16: ABC signature preserved."""
    sig = inspect.signature(MessageBroker.stop)
    assert list(sig.parameters) == ['self']


def test_H51_mqtt_broker_stop_signature_has_graceful_timeout_s_default_5(
    broker,
):
    sig = inspect.signature(MqttBroker.stop)
    assert 'graceful_timeout_s' in sig.parameters
    assert sig.parameters['graceful_timeout_s'].default == 5.0
    assert sig.return_annotation is bool


# ===========================================================================
# I. Agent.__deactivating integration (RFC-010 §H)
# ===========================================================================


def test_I52_agent_deactivating_source_observes_broker_stop_result():
    from agentflow.core.agent import Agent
    src = inspect.getsource(getattr(Agent, '_Agent__deactivating'))
    assert 'self._broker.stop()' in src
    assert 'stopped is False' in src
    assert 'logger.warning' in src
    # never-raise contract preserved.
    assert 'except Exception' in src


def test_I53_agent_deactivating_treats_None_return_as_success():
    """Legacy brokers (EmptyBroker) return None from stop(); the
    observation path treats None as success (only False triggers
    the WARNING)."""
    from agentflow.core.agent import Agent
    src = inspect.getsource(getattr(Agent, '_Agent__deactivating'))
    # The guard is `stopped is False` (not `not stopped`) so None
    # falls through as success.
    assert 'stopped is False' in src
    # Sanity: not the truthy shortcut.
    assert 'if not stopped' not in src
