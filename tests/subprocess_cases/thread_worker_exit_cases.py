"""Subprocess cases for R-10.5 characterisation.

Each case is a top-level function selected by CLI arg:

    python tests/subprocess_cases/thread_worker_exit_cases.py <case_name>

Cases set up a scenario, print `READY` (flushed) on stdout so the
parent knows setup is done, emit any observations as `KEY=value`
lines (also flushed), then return from `main()`. The parent test
observes:

  - Whether the interpreter exits cleanly (bounded via
    subprocess.communicate(timeout=...))
  - The emitted key=value lines to correlate with expected state

Cases MUST NOT sleep for wall-time coordination beyond a small
setup window; use `threading.Event` release from a helper thread
that the parent test can kill. Wedged threads intentionally block
for a long timeout (30s) so the parent's kill / terminate is what
actually reclaims them.
"""

import atexit
import os
import queue as queue_mod
import sys
import threading
import time


# Extend sys.path so `agentflow` and `tests.fakes` resolve regardless
# of whether the subprocess is launched with PYTHONPATH or not.
_HERE = os.path.abspath(os.path.dirname(__file__))
_REPO = os.path.abspath(os.path.join(_HERE, "..", ".."))
_SRC = os.path.join(_REPO, "src")
for p in (_SRC, _REPO):
    if p not in sys.path:
        sys.path.insert(0, p)


def _emit(marker: str, value=None, **kv) -> None:
    """Print a stdout marker in one of three shapes:

      - `_emit("READY")`               -> `READY`
      - `_emit("STOP", value=True)`    -> `STOP=True`
      - `_emit("HDR", k1=1, k2=2)`     -> `HDR k1=1 k2=2`

    The parent's parser reads each whitespace-delimited token as
    either `KEY=VALUE` or a bare marker; the value-form above emits
    `MARKER=VALUE` so the marker itself carries the observation."""
    if value is not None:
        line = f"{marker}={value}"
        if kv:
            line += " " + " ".join(f"{k}={v}" for k, v in kv.items())
    elif kv:
        parts = [marker] + [f"{k}={v}" for k, v in kv.items()]
        line = " ".join(parts)
    else:
        line = marker
    print(line, flush=True)


# ---------------------------------------------------------------------------
# Case registry
# ---------------------------------------------------------------------------


CASES: dict = {}


def register(name: str):
    def deco(fn):
        CASES[name] = fn
        return fn
    return deco


# ===========================================================================
# A. Baseline interpreter behaviour
# ===========================================================================


@register("baseline_no_threads")
def _c_baseline_no_threads():
    """No extra threads. Process should exit cleanly."""
    _emit("READY")


@register("baseline_non_daemon_wedged")
def _c_baseline_non_daemon_wedged():
    """Spawn a non-daemon thread that blocks on an Event.
    Process should NOT exit — parent's communicate must time out."""
    release = threading.Event()
    t = threading.Thread(target=release.wait, args=(30.0,), daemon=False)
    t.start()
    _emit("READY", daemon=t.daemon)


@register("baseline_daemon_wedged")
def _c_baseline_daemon_wedged():
    """Spawn a daemon thread that blocks. Process should exit
    cleanly (daemon threads are killed at interpreter exit)."""
    release = threading.Event()
    t = threading.Thread(target=release.wait, args=(30.0,), daemon=True)
    t.start()
    _emit("READY", daemon=t.daemon)


@register("baseline_sys_exit_with_non_daemon")
def _c_baseline_sys_exit_with_non_daemon():
    """sys.exit(0) with a live non-daemon thread — interpreter still
    waits for the non-daemon thread."""
    release = threading.Event()
    t = threading.Thread(target=release.wait, args=(30.0,), daemon=False)
    t.start()
    _emit("READY")
    sys.exit(0)


@register("baseline_os_exit_with_non_daemon")
def _c_baseline_os_exit_with_non_daemon():
    """os._exit(0) bypasses all interpreter cleanup — even non-daemon
    threads are killed immediately."""
    release = threading.Event()
    t = threading.Thread(target=release.wait, args=(30.0,), daemon=False)
    t.start()
    _emit("READY")
    os._exit(0)


@register("baseline_atexit_normal")
def _c_baseline_atexit_normal():
    """atexit handler runs on normal interpreter exit."""
    atexit.register(lambda: print("ATEXIT_RAN", flush=True))
    _emit("READY")


@register("baseline_atexit_os_exit")
def _c_baseline_atexit_os_exit():
    """atexit handler does NOT run when os._exit bypasses cleanup."""
    atexit.register(lambda: print("ATEXIT_RAN", flush=True))
    _emit("READY")
    os._exit(0)


# ===========================================================================
# B. ThreadWorker normal lifecycle
# ===========================================================================


def _make_thread_worker(activate):
    """Build a ThreadWorker with a duck-typed initiator."""
    from agentflow.core.agent_worker import ThreadWorker

    class _FakeInit:
        def __init__(self):
            self.config = {}
            self._activate = activate
            self.name_tag = 'sub'

        def M(self, message=None):
            return f'sub {message or ""}'

    return ThreadWorker(_FakeInit())


def _default_cooperative_activate(cfg):
    q = cfg['work_queue']
    while True:
        try:
            item = q.get(timeout=0.05)
        except queue_mod.Empty:
            continue
        if item == 'terminate':
            return


@register("threadworker_normal_start_stop")
def _c_threadworker_normal_start_stop():
    """Cooperative worker: start → stop → STOPPED → process exits."""
    worker = _make_thread_worker(_default_cooperative_activate)
    worker.start()
    result = worker.stop(graceful_timeout_s=2.0)
    _emit("STOP_RETURN", value=result)
    _emit("STATE", value=worker.state.value)
    _emit("ALIVE", value=worker.work_thread.is_alive())
    _emit("READY")


@register("threadworker_never_started")
def _c_threadworker_never_started():
    """Construct but don't start. No worker thread → process exits."""
    worker = _make_thread_worker(_default_cooperative_activate)
    _emit("STATE", value=worker.state.value)
    _emit("READY")


# ===========================================================================
# C. Wedged handler / _activate
# ===========================================================================


def _wedged_activate_factory(release: threading.Event):
    def _activate(cfg):
        release.wait(timeout=30.0)   # parent will kill before this expires
    return _activate


@register("wedged_activate_no_stop")
def _c_wedged_activate_no_stop():
    """Worker wedged in _activate; main() never calls stop().
    Non-daemon worker thread alive → process does NOT exit."""
    release = threading.Event()
    worker = _make_thread_worker(_wedged_activate_factory(release))
    worker.start()
    _emit("DAEMON", value=worker.work_thread.daemon)
    _emit("READY")


@register("wedged_activate_stop_returns_False")
def _c_wedged_activate_stop_returns_False():
    """Worker wedged; stop(0.3) → STOP_TIMEOUT, False. Worker still
    alive → process does NOT exit."""
    release = threading.Event()
    worker = _make_thread_worker(_wedged_activate_factory(release))
    worker.start()
    result = worker.stop(graceful_timeout_s=0.3)
    _emit("STOP_RETURN", value=result)
    _emit("STATE", value=worker.state.value)
    _emit("ALIVE", value=worker.work_thread.is_alive())
    _emit("DAEMON", value=worker.work_thread.daemon)
    _emit("READY")


@register("wedged_activate_release_and_retry")
def _c_wedged_activate_release_and_retry():
    """Wedged; first stop times out; release the wedge; second stop
    reaches STOPPED. Process should exit."""
    release = threading.Event()
    worker = _make_thread_worker(_wedged_activate_factory(release))
    worker.start()
    r1 = worker.stop(graceful_timeout_s=0.3)
    _emit("STOP1", value=r1)
    _emit("STATE1", value=worker.state.value)
    release.set()
    r2 = worker.stop(graceful_timeout_s=2.0)
    _emit("STOP2", value=r2)
    _emit("STATE2", value=worker.state.value)
    _emit("READY")


# ---------------------------------------------------------------------------
# RFC-013 diagnostic-property scenarios
# ---------------------------------------------------------------------------


@register("props_never_started")
def _c_props_never_started():
    """Never started → thread_alive False, thread_daemon None,
    worker_thread_ident None, requires_process_restart False."""
    worker = _make_thread_worker(_default_cooperative_activate)
    _emit("ALIVE", value=worker.thread_alive)
    _emit("DAEMON_IS_NONE", value=(worker.thread_daemon is None))
    _emit("IDENT_IS_NONE", value=(worker.worker_thread_ident is None))
    _emit("RESTART", value=worker.requires_process_restart)
    _emit("READY")


@register("props_running_cooperative")
def _c_props_running_cooperative():
    """Cooperative RUNNING worker: alive=True, daemon=False, ident
    non-None, restart=False (state RUNNING, not STOP_TIMEOUT)."""
    worker = _make_thread_worker(_default_cooperative_activate)
    worker.start()
    _emit("STATE", value=worker.state.value)
    _emit("ALIVE", value=worker.thread_alive)
    _emit("DAEMON", value=worker.thread_daemon)
    _emit("IDENT_IS_NONE", value=(worker.worker_thread_ident is None))
    _emit("RESTART", value=worker.requires_process_restart)
    # Clean up so subprocess can exit.
    worker.stop(graceful_timeout_s=2.0)
    _emit("READY")


@register("props_normal_stopped")
def _c_props_normal_stopped():
    """After clean stop: STOPPED, alive=False, restart=False."""
    worker = _make_thread_worker(_default_cooperative_activate)
    worker.start()
    worker.stop(graceful_timeout_s=2.0)
    _emit("STATE", value=worker.state.value)
    _emit("ALIVE", value=worker.thread_alive)
    _emit("RESTART", value=worker.requires_process_restart)
    _emit("READY")


@register("props_wedged_stop_timeout")
def _c_props_wedged_stop_timeout():
    """Wedged stop → STOP_TIMEOUT + alive=True + daemon=False →
    requires_process_restart=True. Emit BEFORE the subprocess
    itself gets killed by parent — parent uses a short timeout to
    also verify the R-10.5 hang."""
    release = threading.Event()
    worker = _make_thread_worker(_wedged_activate_factory(release))
    worker.start()
    worker.stop(graceful_timeout_s=0.3)
    _emit("STATE", value=worker.state.value)
    _emit("ALIVE", value=worker.thread_alive)
    _emit("DAEMON", value=worker.thread_daemon)
    _emit("RESTART", value=worker.requires_process_restart)
    _emit("READY")
    # NOTE: subprocess intentionally does NOT release the wedge —
    # parent terminates/kills to reclaim (verified via _run_case).


@register("props_release_before_retry_thread_dies_naturally")
def _c_props_release_before_retry():
    """Wedged → stop times out (STOP_TIMEOUT) → release blocker
    (thread dies) but do NOT retry stop(). State stays STOP_TIMEOUT
    but thread_alive flips False → requires_process_restart
    auto-False."""
    release = threading.Event()
    worker = _make_thread_worker(_wedged_activate_factory(release))
    worker.start()
    worker.stop(graceful_timeout_s=0.3)
    _emit("MID_STATE", value=worker.state.value)
    _emit("MID_RESTART", value=worker.requires_process_restart)
    release.set()
    if worker.work_thread is not None:
        worker.work_thread.join(2.0)
    _emit("AFTER_STATE", value=worker.state.value)
    _emit("AFTER_ALIVE", value=worker.thread_alive)
    _emit("AFTER_RESTART", value=worker.requires_process_restart)
    _emit("READY")


@register("props_release_and_retry_stops_cleanly")
def _c_props_release_and_retry():
    """Wedged → stop times out → release + retry stop → STOPPED →
    restart=False."""
    release = threading.Event()
    worker = _make_thread_worker(_wedged_activate_factory(release))
    worker.start()
    worker.stop(graceful_timeout_s=0.3)
    _emit("MID_RESTART", value=worker.requires_process_restart)
    release.set()
    r2 = worker.stop(graceful_timeout_s=2.0)
    _emit("STOP2", value=r2)
    _emit("AFTER_STATE", value=worker.state.value)
    _emit("AFTER_RESTART", value=worker.requires_process_restart)
    _emit("READY")


@register("processworker_property_absence")
def _c_processworker_property_absence():
    """ProcessWorker does NOT define RFC-013 properties. Agent's
    capability-based getattr treats them as False → falls through
    to existing WARNING path (never to ERROR path)."""
    from agentflow.core.agent_worker import ProcessWorker

    class _StubInit:
        config = {}
        name_tag = 'stub'
        def M(self, message=None):
            return f'stub {message or ""}'

    pw = ProcessWorker(_StubInit())
    _emit("HAS_RESTART", value=hasattr(pw, 'requires_process_restart'))
    _emit("HAS_ALIVE", value=hasattr(pw, 'thread_alive'))
    _emit("GETATTR_RESTART",
          value=getattr(pw, 'requires_process_restart', False))
    _emit("READY")


# ===========================================================================
# G. daemon=True experiment (test-only subclass; NOT modifying prod)
# ===========================================================================


@register("daemon_worker_thread_wedged_process_exits")
def _c_daemon_worker_thread_wedged_process_exits():
    """Bypass ThreadWorker's daemon=False hard-code by directly
    building a daemon=True worker thread. Prove that if the worker
    thread were daemon, the process would exit even under wedge —
    demonstrating that daemon flag is the root cause of R-10.5."""
    release = threading.Event()

    def wedged():
        release.wait(timeout=30.0)

    t = threading.Thread(target=wedged, daemon=True)
    t.start()
    _emit("DAEMON", value=t.daemon)
    _emit("ALIVE", value=t.is_alive())
    _emit("READY")


@register("daemon_thread_finally_not_guaranteed")
def _c_daemon_thread_finally_not_guaranteed():
    """When a daemon thread is killed at interpreter exit, its
    `finally` block may NOT run. Demonstrates the trade-off documented
    in RFC-009 §7.13."""
    release = threading.Event()
    marker_path = os.environ.get("R10_5_FINALLY_MARKER")

    def wedged():
        try:
            release.wait(timeout=30.0)
        finally:
            # Attempt to write a marker file. If daemon killed
            # abruptly this may not run OR may be partially completed.
            if marker_path:
                try:
                    with open(marker_path, "w") as f:
                        f.write("FINALLY_RAN\n")
                        f.flush()
                except Exception:
                    pass

    t = threading.Thread(target=wedged, daemon=True)
    t.start()
    _emit("READY")


# ===========================================================================
# F. ProcessWorker contrast
# ===========================================================================


@register("processworker_cooperative_child_parent_exits")
def _c_processworker_cooperative():
    """ProcessWorker with a cooperative child (EmptyBroker). Parent's
    stop() succeeds cleanly; both parent and child exit."""
    from agentflow.core.agent import Agent
    from agentflow.core.agent_worker import ProcessWorker

    cfg = {
        'CONCURRENCY_TYPE': 'process',
        'broker': {
            'broker_name': 'my_broker',
            'my_broker': {'broker_type': 'empty'},
        },
    }
    agent = Agent(name='pw_coop', agent_config=cfg)
    pw = ProcessWorker(agent)
    proc = pw.start()
    # Let child reach the work-queue loop.
    time.sleep(0.3)
    result = pw.stop(
        graceful_timeout_s=3.0,
        terminate_timeout_s=1.0,
        kill_timeout_s=1.0,
    )
    _emit("STOP_RETURN", value=result)
    _emit("EXITCODE", value=pw.exitcode)
    _emit("READY")


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------


def main(argv):
    if len(argv) < 2 or argv[1] not in CASES:
        print(
            f"Usage: {argv[0]} <case>\nCases: {sorted(CASES)}",
            file=sys.stderr,
        )
        sys.exit(2)
    CASES[argv[1]]()


if __name__ == "__main__":
    main(sys.argv)
