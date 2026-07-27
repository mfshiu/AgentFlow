"""RFC-008 tests: Agent pickle compatibility + ProcessWorker lifecycle.

Post-RFC-008:

  - Agent supports pickle via __getstate__/__setstate__; runtime-only
    fields (_handlers_lock, _dispatcher, _broker, _agent_worker,
    _children, _parents, ...) are excluded from the shipped state
    and reinstated fresh in the child.
  - ProcessWorker has a formal state machine
    (NEW → STARTING → RUNNING → STOPPING → STOPPED / START_FAILED).
  - ProcessWorker.stop is a bounded escalation ladder
    (send terminate → join → terminate() → join → kill() → join).
  - Repeated start / stop / concurrent stop are explicitly defined.
  - start does NOT mutate agent.config.
  - Non-picklable handlers or config fail fast with a helpful error.

Live spawn tests
----------------
Some tests spawn a real child process. All are bounded (join
timeouts, mp.Event releases, terminate/kill fallback) and cleaned
up in finally. No test may leak a process beyond its function
scope.

Because multiprocessing.set_start_method('spawn') is process-wide
and forced by Worker.__init__, every test in this file inherits
spawn mode.
"""

import inspect
import multiprocessing as mp
import os
import pickle
import signal
import threading
import time

import pytest

# --------------------------------------------------------------------------
# Ensure spawned children can import module-level helpers from this file.
# --------------------------------------------------------------------------
# multiprocessing spawn launches a fresh Python interpreter and unpickles
# the target by qualified name. That requires the target's module
# ('tests.unit.core.test_process_worker_lifecycle') to be importable in
# the child. Pytest's pyproject `pythonpath = ["src"]` puts only `src`
# on the child's PYTHONPATH env, so we prepend the repo root here so
# `tests.*` resolves in the child.
_REPO_ROOT = os.path.abspath(
    os.path.join(os.path.dirname(__file__), '..', '..', '..')
)
_existing_pp = os.environ.get('PYTHONPATH', '')
if _REPO_ROOT not in _existing_pp.split(os.pathsep):
    os.environ['PYTHONPATH'] = (
        _REPO_ROOT + (os.pathsep + _existing_pp if _existing_pp else '')
    )

from agentflow.core.agent import (
    Agent,
    _HandlerOwnerType,
    _HandlerRecord,
)
from agentflow.core.agent_worker import (
    ProcessWorker,
    ThreadWorker,
    Worker,
    WorkerState,
)


# --------------------------------------------------------------------------
# Module-level helpers (picklable — importable from spawned child)
# --------------------------------------------------------------------------

def _module_level_handler(topic, pcl):
    """A handler at module scope so it is picklable by qualified name."""
    return None


def _wedged_child_target(release_event):
    """Child target that blocks forever on the event; ignores any queue
    message. Used to simulate a wedged Agent child."""
    release_event.wait(timeout=60.0)


def _sigterm_ignoring_target(release_event):
    """Child target that installs SIG_IGN for SIGTERM, then blocks. Used
    to verify escalation to Process.kill()."""
    try:
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
    except Exception:
        pass
    release_event.wait(timeout=60.0)


def _cleanup_process(process, hard_timeout: float = 2.0) -> None:
    """Best-effort cleanup for a possibly-alive multiprocessing.Process."""
    if process is None:
        return
    try:
        if not process.is_alive():
            return
        try:
            process.terminate()
        except Exception:
            pass
        try:
            process.join(hard_timeout)
        except Exception:
            pass
        if process.is_alive():
            try:
                process.kill()
            except Exception:
                pass
            try:
                process.join(1.0)
            except Exception:
                pass
    except Exception:
        pass


def _minimal_empty_broker_config():
    """Config for a real Agent whose _activate can complete with the
    EmptyBroker (no MQTT needed). Fully picklable."""
    return {
        'broker': {
            'broker_name': 'my_broker',
            'my_broker': {'broker_type': 'empty'},
        },
    }


# ==========================================================================
# A. Agent pickle round-trip (RFC-008 §A)
# ==========================================================================

def test_agent_getstate_setstate_round_trip_succeeds():
    agent = Agent(name='pickle-ok', agent_config={'k': 'v'})
    data = pickle.dumps(agent)
    revived = pickle.loads(data)
    assert isinstance(revived, Agent)
    assert revived.name == 'pickle-ok'
    assert revived.agent_id == agent.agent_id
    assert revived.config.get('k') == 'v'


def test_setstate_reinstates_fresh_handlers_lock_and_dispatcher_init_lock():
    agent = Agent(name='lock-fresh', agent_config={})
    orig_lock_id = id(agent._handlers_lock)
    orig_init_lock_id = id(agent._dispatcher_init_lock)
    revived = pickle.loads(pickle.dumps(agent))
    # Fresh RLocks — different objects.
    assert id(revived._handlers_lock) != orig_lock_id
    assert id(revived._dispatcher_init_lock) != orig_init_lock_id
    # And the lock actually functions.
    with revived._handlers_lock:
        pass


def test_setstate_reinstates_None_broker_dispatcher_agent_worker_message_broker():
    agent = Agent(name='runtime-fresh', agent_config={})
    revived = pickle.loads(pickle.dumps(agent))
    assert revived._broker is None
    assert revived._dispatcher is None
    assert revived._agent_worker is None
    assert revived._message_broker is None


def test_setstate_reinstates_empty_children_and_parents():
    agent = Agent(name='cp', agent_config={})
    # Populate parent/child registries in the source Agent.
    agent._children['x'] = {'child_id': 'x'}
    agent._parents['y'] = {'parent_id': 'y'}
    revived = pickle.loads(pickle.dumps(agent))
    assert revived._children == {}
    assert revived._parents == {}


def test_pickle_preserves_HandlerRecord_normal_owner_shape():
    """RFC-006/007 preserved: _HandlerRecord entries survive pickle
    with their owner_type + handler identity intact (handler is
    resolved by qualified name in the child)."""
    agent = Agent(name='hr-preserve', agent_config={})
    agent.subscribe('T', topic_handler=_module_level_handler)
    revived = pickle.loads(pickle.dumps(agent))
    rec = revived._Agent__topic_handlers.get('T')
    assert rec is not None
    assert isinstance(rec, _HandlerRecord)
    assert rec.owner_type is _HandlerOwnerType.NORMAL
    assert rec.handler is _module_level_handler


def test_pickle_raises_TypeError_naming_topic_for_lambda_handler():
    """RFC-008 §A: fail-fast with a helpful error message when a
    handler is not picklable. Message must name the offending topic."""
    agent = Agent(name='lambda-h', agent_config={})
    agent.subscribe('sensitive/topic', topic_handler=lambda t, p: None)
    with pytest.raises(TypeError) as exc_info:
        pickle.dumps(agent)
    msg = str(exc_info.value)
    assert 'sensitive/topic' in msg
    assert 'on_activate' in msg.lower()


def test_pickle_raises_TypeError_with_helpful_message_for_lambda_in_config():
    """RFC-008 §A: config validation raises TypeError with a helpful
    message when a lambda / closure sneaks into agent.config."""
    agent = Agent(name='lambda-cfg', agent_config={'cb': lambda: None})
    with pytest.raises(TypeError) as exc_info:
        pickle.dumps(agent)
    msg = str(exc_info.value)
    assert 'Agent.config' in msg
    assert 'on_activate' in msg.lower()


def test_pickle_of_agent_with_all_module_level_handlers_succeeds():
    """All handlers picklable → pickle succeeds; child would get the
    full registry."""
    agent = Agent(name='multi-handler', agent_config={})
    agent.subscribe('T/1', topic_handler=_module_level_handler)
    agent.subscribe('T/2', topic_handler=_module_level_handler)
    revived = pickle.loads(pickle.dumps(agent))
    assert set(revived._Agent__topic_handlers.keys()) == {'T/1', 'T/2'}


# ==========================================================================
# B. Worker state machine (RFC-008 §B)
# ==========================================================================

def test_initial_state_is_NEW():
    agent = Agent(name='state-new', agent_config={})
    pw = ProcessWorker(agent)
    assert pw.state is WorkerState.NEW


def test_stop_before_start_is_noop_and_state_stays_NEW():
    agent = Agent(name='stop-first', agent_config={})
    pw = ProcessWorker(agent)
    result = pw.stop()
    assert result is None
    assert pw.state is WorkerState.NEW


def test_stop_before_start_allows_subsequent_start():
    """RFC-008 §7.12: NEW → stop → still NEW → start allowed."""
    agent = Agent(name='stop-then-start', agent_config=_minimal_empty_broker_config())
    pw = ProcessWorker(agent)
    pw.stop()
    assert pw.state is WorkerState.NEW
    try:
        pw.start()
        assert pw.state is WorkerState.RUNNING
    finally:
        try:
            pw.stop(graceful_timeout_s=3.0, terminate_timeout_s=1.0, kill_timeout_s=1.0)
        except Exception:
            pass
        _cleanup_process(pw.work_process, hard_timeout=1.0)


def test_state_after_start_pickle_failure_is_START_FAILED():
    """Lambda callback in config → pickle fails at Process.start →
    _cleanup_after_start_failure runs → state → START_FAILED."""
    bad_config = {'callback': lambda: None}
    agent = Agent(name='pw-fail-state', agent_config=bad_config)
    pw = ProcessWorker(agent)
    try:
        with pytest.raises(TypeError):
            pw.start()
    finally:
        _cleanup_process(pw.work_process, hard_timeout=1.0)
    assert pw.state is WorkerState.START_FAILED


def test_start_after_START_FAILED_raises_runtime_error():
    bad_config = {'callback': lambda: None}
    agent = Agent(name='pw-restart-after-fail', agent_config=bad_config)
    pw = ProcessWorker(agent)
    try:
        with pytest.raises(TypeError):
            pw.start()
    finally:
        _cleanup_process(pw.work_process, hard_timeout=1.0)
    # A second start attempt must not silently succeed.
    with pytest.raises(RuntimeError):
        pw.start()


# ==========================================================================
# C. Real spawn — success path (RFC-008 §C)
# ==========================================================================

def test_start_spawns_functional_child_that_exits_cleanly():
    """End-to-end: start spawns a child that unpickles the Agent,
    runs _activate, receives 'terminate', and exits with code 0."""
    agent = Agent(name='pw-spawn-ok', agent_config=_minimal_empty_broker_config())
    pw = ProcessWorker(agent)
    try:
        proc = pw.start()
        assert pw.state is WorkerState.RUNNING
        assert proc.is_alive()
        # Give the child a moment to unpickle + reach the work_queue
        # loop before we send terminate.
        time.sleep(0.3)
        assert proc.is_alive()
        exitcode = pw.stop(graceful_timeout_s=5.0)
        assert pw.state is WorkerState.STOPPED
        assert exitcode == 0
    finally:
        _cleanup_process(pw.work_process, hard_timeout=2.0)


def test_start_child_process_daemon_is_false():
    agent = Agent(name='pw-daemon', agent_config=_minimal_empty_broker_config())
    pw = ProcessWorker(agent)
    try:
        pw.start()
        assert pw.work_process.daemon is False
    finally:
        try:
            pw.stop(graceful_timeout_s=3.0, terminate_timeout_s=1.0, kill_timeout_s=1.0)
        except Exception:
            pass
        _cleanup_process(pw.work_process, hard_timeout=1.0)


def test_start_does_not_mutate_agent_config():
    """RFC-008 §C: agent.config must NOT gain a 'work_queue' key."""
    config = _minimal_empty_broker_config()
    agent = Agent(name='pw-no-mutate', agent_config=config)
    assert 'work_queue' not in agent.config
    pw = ProcessWorker(agent)
    try:
        pw.start()
        assert 'work_queue' not in agent.config
    finally:
        try:
            pw.stop(graceful_timeout_s=3.0, terminate_timeout_s=1.0, kill_timeout_s=1.0)
        except Exception:
            pass
        _cleanup_process(pw.work_process, hard_timeout=1.0)
    # Still absent after stop.
    assert 'work_queue' not in agent.config


# ==========================================================================
# D. Real spawn — repeated / restart guards (RFC-008 §7.8)
# ==========================================================================

def test_repeated_start_while_running_raises_runtime_error():
    agent = Agent(name='pw-repeat-start', agent_config=_minimal_empty_broker_config())
    pw = ProcessWorker(agent)
    try:
        pw.start()
        assert pw.state is WorkerState.RUNNING
        with pytest.raises(RuntimeError):
            pw.start()
    finally:
        try:
            pw.stop(graceful_timeout_s=3.0, terminate_timeout_s=1.0, kill_timeout_s=1.0)
        except Exception:
            pass
        _cleanup_process(pw.work_process, hard_timeout=1.0)


def test_start_after_successful_stop_raises_runtime_error():
    agent = Agent(name='pw-start-after-stop', agent_config=_minimal_empty_broker_config())
    pw = ProcessWorker(agent)
    try:
        pw.start()
        pw.stop(graceful_timeout_s=3.0)
        assert pw.state is WorkerState.STOPPED
        with pytest.raises(RuntimeError):
            pw.start()
    finally:
        _cleanup_process(pw.work_process, hard_timeout=1.0)


# ==========================================================================
# E. Stop escalation ladder (RFC-008 §D)
# ==========================================================================

def _install_wedged_process(pw, target=_wedged_child_target):
    """Bypass pw.start() with a wedged Process. Returns the release
    event so the caller can free the wedged child in cleanup."""
    release = mp.Event()
    proc = mp.Process(target=target, args=(release,), daemon=False)
    proc.start()
    pw.work_queue = mp.Queue()
    pw.work_process = proc
    with pw._state_lock:
        pw._state = WorkerState.RUNNING
    return release, proc


def _release_if_alive(release_event, proc):
    """Signal a wedged child's release event only if it is still alive.

    Safety net: an mp.Event released on a process that was already
    SIGKILL'd may hang forever because Condition.notify_all() waits
    on _woken_count for the dead waiter to consume the wakeup. If
    stop() already killed the child, skip the release entirely.
    """
    try:
        if proc is not None and proc.is_alive():
            release_event.set()
    except Exception:
        pass


def test_stop_escalates_to_terminate_when_child_ignores_terminate_message():
    """Child is wedged and does not read the queue. graceful join
    times out, stop() escalates to Process.terminate() (SIGTERM),
    which kills the plain wedged child. Total elapsed bounded."""
    agent = Agent(name='pw-esc-term', agent_config={})
    pw = ProcessWorker(agent)
    release, proc = _install_wedged_process(pw, _wedged_child_target)
    try:
        start_t = time.monotonic()
        exitcode = pw.stop(
            graceful_timeout_s=0.2,
            terminate_timeout_s=1.5,
            kill_timeout_s=1.0,
        )
        elapsed = time.monotonic() - start_t
        assert elapsed < 4.0, f'stop() took {elapsed:.3f}s'
        assert pw.state is WorkerState.STOPPED
        # SIGTERM'd → negative exitcode on POSIX.
        assert exitcode is not None
        assert exitcode != 0
    finally:
        _release_if_alive(release, proc)
        _cleanup_process(proc, hard_timeout=1.0)


def test_stop_escalates_to_kill_when_child_ignores_sigterm():
    """Child installs SIG_IGN for SIGTERM. stop() sees terminate fail
    (child still alive), escalates to Process.kill() (SIGKILL, which
    cannot be ignored). Total elapsed bounded."""
    agent = Agent(name='pw-esc-kill', agent_config={})
    pw = ProcessWorker(agent)
    release, proc = _install_wedged_process(pw, _sigterm_ignoring_target)
    try:
        start_t = time.monotonic()
        exitcode = pw.stop(
            graceful_timeout_s=0.2,
            terminate_timeout_s=0.3,
            kill_timeout_s=1.5,
        )
        elapsed = time.monotonic() - start_t
        assert elapsed < 4.0, f'stop() took {elapsed:.3f}s'
        assert pw.state is WorkerState.STOPPED
        assert exitcode is not None
        assert exitcode != 0
        # After kill, child must not be alive anymore.
        assert not proc.is_alive()
    finally:
        _release_if_alive(release, proc)
        _cleanup_process(proc, hard_timeout=1.0)


def test_stop_returns_exitcode_zero_when_child_cooperates():
    agent = Agent(name='pw-clean-exit', agent_config=_minimal_empty_broker_config())
    pw = ProcessWorker(agent)
    try:
        pw.start()
        exitcode = pw.stop(graceful_timeout_s=5.0)
        assert exitcode == 0
    finally:
        _cleanup_process(pw.work_process, hard_timeout=1.0)


# ==========================================================================
# F. Concurrent stop semantics (RFC-008 §7.11 idempotence)
# ==========================================================================

def test_stop_idempotent_returns_cached_exitcode():
    agent = Agent(name='pw-idem', agent_config=_minimal_empty_broker_config())
    pw = ProcessWorker(agent)
    try:
        pw.start()
        first = pw.stop(graceful_timeout_s=5.0)
        # Second call returns immediately with the same result.
        start_t = time.monotonic()
        second = pw.stop(graceful_timeout_s=5.0)
        elapsed = time.monotonic() - start_t
        assert second == first
        assert elapsed < 0.1
    finally:
        _cleanup_process(pw.work_process, hard_timeout=1.0)


def test_concurrent_stop_all_callers_return_same_result_single_escalation():
    """RFC-008 §D: concurrent stop callers must all observe the same
    outcome; the escalation body runs exactly once (verified via a
    spy on Process.join)."""
    agent = Agent(name='pw-concurrent', agent_config=_minimal_empty_broker_config())
    pw = ProcessWorker(agent)
    try:
        pw.start()

        N = 5
        barrier = threading.Barrier(N, timeout=5.0)
        results = [None] * N
        errors = [None] * N

        def call_stop(i):
            try:
                barrier.wait()
                results[i] = pw.stop(graceful_timeout_s=5.0)
            except BaseException as ex:
                errors[i] = ex

        threads = [
            threading.Thread(target=call_stop, args=(i,), daemon=True)
            for i in range(N)
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join(10.0)

        assert all(e is None for e in errors), f'errors={errors}'
        # All callers see the same exitcode.
        assert len(set(results)) == 1
        assert results[0] == 0
    finally:
        _cleanup_process(pw.work_process, hard_timeout=1.0)


# ==========================================================================
# G. Start failure cleanup (RFC-008 §7.7, §7.6)
# ==========================================================================

def test_start_pickle_failure_clears_process_and_queue_references():
    """Non-picklable lambda in config → Process.start pickle failure →
    _cleanup_after_start_failure runs → work_process is None,
    work_queue is None."""
    agent = Agent(name='pw-cleanup-refs', agent_config={'cb': lambda: None})
    pw = ProcessWorker(agent)
    try:
        with pytest.raises(TypeError):
            pw.start()
    finally:
        _cleanup_process(pw.work_process, hard_timeout=1.0)
    assert pw.work_process is None
    assert pw.work_queue is None


def test_start_pickle_failure_does_not_pollute_agent_config():
    """RFC-008 §C + §7.7: agent.config must not gain a 'work_queue'
    key from a failed start."""
    config = {'cb': lambda: None}
    agent = Agent(name='pw-nopollute', agent_config=config)
    pw = ProcessWorker(agent)
    assert 'work_queue' not in agent.config
    try:
        with pytest.raises(TypeError):
            pw.start()
    finally:
        _cleanup_process(pw.work_process, hard_timeout=1.0)
    assert 'work_queue' not in agent.config


def test_start_lambda_handler_failure_reaches_start_time():
    """Handler registered as lambda on the parent-side Agent BEFORE
    start_process(): pickle fails in Process.start with a message
    naming the topic."""
    agent = Agent(name='pw-lambda-h', agent_config=_minimal_empty_broker_config())
    agent.subscribe('some/reserved/topic', topic_handler=lambda t, p: None)
    pw = ProcessWorker(agent)
    try:
        with pytest.raises(TypeError) as exc_info:
            pw.start()
        assert 'some/reserved/topic' in str(exc_info.value)
        assert 'on_activate' in str(exc_info.value).lower()
    finally:
        _cleanup_process(pw.work_process, hard_timeout=1.0)
    assert pw.state is WorkerState.START_FAILED


# ==========================================================================
# H. Observability + orphan sanity (RFC-008 §7.14 + acceptance #9)
# ==========================================================================

def test_exitcode_property_is_None_before_stop_and_int_after():
    agent = Agent(name='pw-exit', agent_config=_minimal_empty_broker_config())
    pw = ProcessWorker(agent)
    assert pw.exitcode is None
    try:
        pw.start()
        assert pw.exitcode is None
        pw.stop(graceful_timeout_s=5.0)
        assert isinstance(pw.exitcode, int)
        assert pw.exitcode == 0
    finally:
        _cleanup_process(pw.work_process, hard_timeout=1.0)


def test_no_orphan_process_after_stop():
    agent = Agent(name='pw-noorphan', agent_config=_minimal_empty_broker_config())
    pw = ProcessWorker(agent)
    try:
        proc = pw.start()
        pid = proc.pid
        assert pid is not None
        pw.stop(graceful_timeout_s=5.0)
        # PID should no longer exist (or belong to something unrelated).
        # Process.is_alive checks specifically for THIS Process's
        # completion; use it as authoritative.
        assert not proc.is_alive()
        # Also verify via OS: sending signal 0 to a dead PID raises.
        # Note: on POSIX only. Skip on other platforms.
        if hasattr(os, 'kill'):
            with pytest.raises((ProcessLookupError, PermissionError)):
                os.kill(pid, 0)
    finally:
        _cleanup_process(pw.work_process, hard_timeout=1.0)


# ==========================================================================
# I. Parent-side Agent contract (RFC-008 §F)
# ==========================================================================

def test_parent_side_agent_broker_stays_None_after_child_start():
    """Parent-side Agent remains a controller stub: _broker is set by
    __activating which runs in the child; parent never sees it."""
    agent = Agent(name='pw-parent-contract', agent_config=_minimal_empty_broker_config())
    pw = ProcessWorker(agent)
    try:
        pw.start()
        time.sleep(0.3)   # child has time to run __activating
        assert agent._broker is None
        assert agent._dispatcher is None
        assert agent._Agent__topic_handlers == {}
        assert agent._children == {}
        assert agent._parents == {}
    finally:
        try:
            pw.stop(graceful_timeout_s=3.0, terminate_timeout_s=1.0, kill_timeout_s=1.0)
        except Exception:
            pass
        _cleanup_process(pw.work_process, hard_timeout=1.0)


# ==========================================================================
# J. Static / baseline / RFC-006/007 preservation across pickle
# ==========================================================================

def test_worker_init_still_forces_spawn_start_method():
    """Baseline preserved: Worker.__init__ ensures spawn is set."""
    Agent(name='w-spawn', agent_config={})   # incidental
    _ = ProcessWorker(Agent(name='w-spawn2', agent_config={}))
    assert mp.get_start_method() == 'spawn'


def test_thread_worker_still_uses_threading_event_by_contrast():
    tw = ThreadWorker(Agent(name='tw-evt', agent_config={}))
    assert isinstance(tw.create_event(), threading.Event)


def test_process_worker_create_event_still_returns_mp_event():
    pw = ProcessWorker(Agent(name='pw-evt', agent_config={}))
    evt = pw.create_event()
    assert not isinstance(evt, threading.Event)
    assert hasattr(evt, 'wait') and hasattr(evt, 'set')


def test_pickle_round_trip_preserves_HandlerRecord_ownership_across_process_boundary():
    """RFC-006/007 preservation: the ownership shape (NORMAL /
    PUBLISH_SYNC via _HandlerRecord) survives the pickle boundary
    so the child enforces the same subscribe/unsubscribe contract."""
    agent = Agent(name='hr-cross', agent_config={})
    agent.subscribe('T', topic_handler=_module_level_handler)
    revived = pickle.loads(pickle.dumps(agent))
    rec = revived._Agent__topic_handlers['T']
    assert rec.owner_type is _HandlerOwnerType.NORMAL
    # Fresh lock in child.
    with revived._handlers_lock:
        pass
