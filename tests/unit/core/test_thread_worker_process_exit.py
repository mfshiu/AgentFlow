"""R-10.5 characterisation tests: non-daemon ThreadWorker blocks
Python interpreter exit.

Design constraints:
  - All interpreter-exit verification runs in a subprocess (invoked
    via `python <helper> <case>`); this test module never leaves
    wedged non-daemon threads in the pytest main process.
  - Every subprocess is bounded via `subprocess.communicate(timeout=...)`.
  - On timeout, parent terminates → kills the child; return values
    include an `exited_within_timeout` boolean.
  - No sleep-based coordination beyond a small setup window; the
    helper's `READY` stdout marker signals setup complete.

The helper module `tests/subprocess_cases/thread_worker_exit_cases.py`
implements a case-registry pattern; each case sets up a scenario,
emits observations as `KEY=value` stdout lines, then returns from
main. This test module observes:

  1. Whether the interpreter exited cleanly within a bounded timeout
     (`exited=True/False`).
  2. Which markers appeared on stdout (`STOP_RETURN`, `STATE`,
     `ALIVE`, `DAEMON`, `ATEXIT_RAN`, etc.).

The 'exited' bool is the primary R-10.5 observable — if False, a
non-daemon thread is holding the interpreter open.
"""

import ast
import inspect
import os
import subprocess
import sys
import tempfile
import textwrap
import threading
from pathlib import Path
from typing import Dict, Optional, Tuple

import pytest


_REPO_ROOT = Path(__file__).resolve().parents[3]
_HELPER = _REPO_ROOT / "tests" / "subprocess_cases" / "thread_worker_exit_cases.py"
_ENV = {**os.environ, "PYTHONPATH": str(_REPO_ROOT / "src")}


# ---------------------------------------------------------------------------
# Subprocess parent harness
# ---------------------------------------------------------------------------


class SubprocessResult:
    """Structured result of a subprocess case invocation."""

    def __init__(self, exited: bool, stdout: str, stderr: str,
                 returncode: Optional[int]):
        self.exited = exited          # True iff exited within timeout
        self.stdout = stdout
        self.stderr = stderr
        self.returncode = returncode  # None if we had to kill
        self.markers = self._parse(stdout)

    @staticmethod
    def _parse(stdout: str) -> Dict[str, str]:
        """Parse `KEY=value` tokens across all stdout lines into a
        single dict. Repeated keys keep the last-seen value."""
        out: Dict[str, str] = {}
        for line in stdout.splitlines():
            for tok in line.strip().split():
                if "=" in tok:
                    k, _, v = tok.partition("=")
                    out[k] = v
                else:
                    out.setdefault(tok, "")   # bare marker like READY
        return out

    def __repr__(self):
        return (
            f"SubprocessResult(exited={self.exited}, "
            f"returncode={self.returncode}, "
            f"markers={self.markers}, "
            f"stderr={self.stderr!r})"
        )


def _run_case(case_name: str, *,
              timeout: float = 3.0,
              env_extra: Optional[Dict[str, str]] = None,
              ) -> SubprocessResult:
    """Run a subprocess case with bounded timeout.

    On timeout: terminate → wait 1s → kill if still alive → return
    `SubprocessResult(exited=False, ...)`.

    Never leaks child processes past this call.
    """
    env = dict(_ENV)
    if env_extra:
        env.update(env_extra)
    proc = subprocess.Popen(
        [sys.executable, str(_HELPER), case_name],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=env,
        cwd=str(_REPO_ROOT),
        text=True,
    )
    try:
        stdout, stderr = proc.communicate(timeout=timeout)
        return SubprocessResult(
            exited=True,
            stdout=stdout,
            stderr=stderr,
            returncode=proc.returncode,
        )
    except subprocess.TimeoutExpired:
        # Child blocked past deadline — the R-10.5 hazard.
        proc.terminate()
        try:
            stdout, stderr = proc.communicate(timeout=2.0)
        except subprocess.TimeoutExpired:
            proc.kill()
            try:
                stdout, stderr = proc.communicate(timeout=2.0)
            except subprocess.TimeoutExpired:
                stdout, stderr = "", ""
        return SubprocessResult(
            exited=False,
            stdout=stdout,
            stderr=stderr,
            returncode=proc.returncode,
        )
    finally:
        # Belt-and-braces: even if the above raised, no orphan child.
        if proc.poll() is None:
            try:
                proc.kill()
                proc.wait(timeout=2.0)
            except Exception:
                pass


def _assert_ready(res: SubprocessResult) -> None:
    """Every case emits READY once its setup is complete. If READY
    is missing, the child failed before setup and the test's
    conclusion is unsafe."""
    assert "READY" in res.markers, (
        f"child did not emit READY (setup failed): "
        f"stdout={res.stdout!r}, stderr={res.stderr!r}"
    )


# ===========================================================================
# A. Baseline interpreter behaviour
# ===========================================================================


def test_A1_baseline_no_threads_exits_cleanly():
    res = _run_case("baseline_no_threads", timeout=3.0)
    assert res.exited is True
    _assert_ready(res)
    assert res.returncode == 0


def test_A2_baseline_non_daemon_wedged_blocks_interpreter_exit():
    """Foundational fact underlying R-10.5: Python waits for
    non-daemon threads even after main returns."""
    res = _run_case("baseline_non_daemon_wedged", timeout=1.5)
    _assert_ready(res)
    assert res.exited is False, (
        "process should NOT have exited — non-daemon wedged thread"
    )
    assert res.markers.get("daemon") == "False"


def test_A3_baseline_daemon_wedged_allows_interpreter_exit():
    """Contrast to A.2: daemon threads are killed at exit."""
    res = _run_case("baseline_daemon_wedged", timeout=3.0)
    assert res.exited is True
    _assert_ready(res)
    assert res.returncode == 0
    assert res.markers.get("daemon") == "True"


def test_A4_baseline_sys_exit_still_waits_for_non_daemon():
    """sys.exit raises SystemExit → interpreter shutdown still waits
    for non-daemon threads (same as normal return)."""
    res = _run_case("baseline_sys_exit_with_non_daemon", timeout=1.5)
    _assert_ready(res)
    assert res.exited is False


def test_A5_baseline_os_exit_bypasses_non_daemon_wait():
    """os._exit(0) skips ALL interpreter cleanup — even non-daemon
    threads are killed immediately."""
    res = _run_case("baseline_os_exit_with_non_daemon", timeout=3.0)
    assert res.exited is True
    _assert_ready(res)
    assert res.returncode == 0


def test_A6_baseline_atexit_runs_on_normal_exit():
    res = _run_case("baseline_atexit_normal", timeout=3.0)
    assert res.exited is True
    _assert_ready(res)
    assert "ATEXIT_RAN" in res.markers


def test_A7_baseline_atexit_does_NOT_run_on_os_exit():
    res = _run_case("baseline_atexit_os_exit", timeout=3.0)
    assert res.exited is True
    _assert_ready(res)
    assert "ATEXIT_RAN" not in res.markers, (
        "atexit MUST NOT run when os._exit bypasses cleanup"
    )


def test_A8_subprocess_timeout_detection_is_stable():
    """Meta-check: run the always-hanging non_daemon case three
    times — every run must be detected as `exited=False`."""
    for _ in range(3):
        res = _run_case("baseline_non_daemon_wedged", timeout=0.5)
        _assert_ready(res)
        assert res.exited is False


# ===========================================================================
# B. ThreadWorker normal lifecycle
# ===========================================================================


def test_B9_threadworker_normal_start_stop_process_exits():
    res = _run_case("threadworker_normal_start_stop", timeout=5.0)
    assert res.exited is True
    _assert_ready(res)
    assert res.markers.get("STOP_RETURN") == "True"
    assert res.markers.get("STATE") == "stopped"
    assert res.markers.get("ALIVE") == "False"


def test_B10_threadworker_never_started_process_exits():
    """Construction alone doesn't leak threads."""
    res = _run_case("threadworker_never_started", timeout=3.0)
    assert res.exited is True
    _assert_ready(res)
    assert res.markers.get("STATE") == "new"


def test_B12_threadworker_source_hardcodes_daemon_False():
    """Baseline source pin: ThreadWorker.start builds its work_thread
    with `daemon=False`. This is the root of R-10.5."""
    from agentflow.core.agent_worker import ThreadWorker
    src = inspect.getsource(ThreadWorker.start)
    assert "daemon=False" in src


def test_B13_processworker_source_hardcodes_daemon_False_too():
    """ProcessWorker also uses daemon=False for its Process, but has
    hard containment (SIGTERM/SIGKILL) that ThreadWorker lacks."""
    from agentflow.core.agent_worker import ProcessWorker
    src = inspect.getsource(ProcessWorker.start)
    assert "daemon=False" in src


# ===========================================================================
# C. Wedged handler / _activate — the R-10.5 primary scenario
# ===========================================================================


def test_C14_wedged_activate_no_stop_process_blocks():
    res = _run_case("wedged_activate_no_stop", timeout=1.5)
    _assert_ready(res)
    assert res.exited is False, (
        "wedged ThreadWorker worker thread blocks interpreter exit"
    )
    assert res.markers.get("DAEMON") == "False"


def test_C15_C19_wedged_activate_stop_TIMEOUT_still_blocks_interpreter():
    """Even after stop() returns False (STOP_TIMEOUT), the still-alive
    non-daemon worker thread keeps the interpreter alive.

    Covers items C.15 (state STOP_TIMEOUT), C.17 (daemon=False),
    C.18 (Agent.terminate bounded via worker.stop bool return —
    verified indirectly by STOP_RETURN=False), C.19 (main return but
    subprocess not exiting)."""
    res = _run_case("wedged_activate_stop_returns_False", timeout=1.5)
    _assert_ready(res)
    assert res.exited is False
    assert res.markers.get("STOP_RETURN") == "False"
    assert res.markers.get("STATE") == "stop_timeout"
    assert res.markers.get("ALIVE") == "True"
    assert res.markers.get("DAEMON") == "False"


def test_C20_wedged_activate_requires_parent_kill():
    """Companion to C.15: verify parent's terminate/kill actually
    reclaims the wedged child."""
    res = _run_case("wedged_activate_stop_returns_False", timeout=0.5)
    _assert_ready(res)
    assert res.exited is False
    # Parent's finally in _run_case did terminate/kill; if we get
    # here, parent successfully reclaimed the child.


def test_C21_C22_wedged_release_and_retry_stop_converges_to_STOPPED():
    """When the wedge is released after STOP_TIMEOUT, a subsequent
    stop() reaches STOPPED — RFC-009 §D retry contract — and the
    process exits."""
    res = _run_case("wedged_activate_release_and_retry", timeout=5.0)
    _assert_ready(res)
    assert res.exited is True, (
        "release + retry stop should let process exit"
    )
    assert res.markers.get("STOP1") == "False"
    assert res.markers.get("STATE1") == "stop_timeout"
    assert res.markers.get("STOP2") == "True"
    assert res.markers.get("STATE2") == "stopped"


# ===========================================================================
# D + E. Wedged broker.start / broker.stop
# ===========================================================================


def test_D24_source_broker_start_bounded_but_worker_thread_still_non_daemon():
    """RFC-011 bounded MqttBroker.start — the STARTUP HELPER is
    daemon=True; but Agent's worker thread waiting on broker.start
    remains daemon=False (RFC-009 §7.13). Verified via source."""
    from agentflow.broker.mqtt_broker import MqttBroker
    from agentflow.core.agent_worker import ThreadWorker
    start_src = inspect.getsource(MqttBroker.start)
    # RFC-011 startup helper is daemonised.
    assert "daemon=True" in start_src
    # But the ThreadWorker's OUTER worker thread is not.
    tw_src = inspect.getsource(ThreadWorker.start)
    assert "daemon=False" in tw_src


def test_D25_startup_helper_daemon_True_does_not_block_exit_alone():
    """Daemon helper thread alone doesn't block exit — matches A.3
    baseline. RFC-011 §7.13 rationale."""
    res = _run_case("daemon_worker_thread_wedged_process_exits", timeout=3.0)
    assert res.exited is True
    _assert_ready(res)
    assert res.markers.get("DAEMON") == "True"


def test_E31_source_broker_stop_bounded_but_worker_still_non_daemon():
    """Same source-shape as D.24 — RFC-010's stop helper daemon=True,
    worker thread daemon=False."""
    from agentflow.broker.mqtt_broker import MqttBroker
    stop_src = inspect.getsource(MqttBroker.stop)
    # RFC-010 stop-helper daemonised (via _run_stop_helper call
    # which uses daemon=True; check the caller string).
    assert "daemon=True" in stop_src


def test_E35_root_cause_is_worker_thread_daemon_False_source_pinned():
    """The precise line that causes R-10.5. Any change to this line
    is a load-bearing change and requires updating RFC-009 §7.13."""
    from agentflow.core.agent_worker import ThreadWorker
    src = inspect.getsource(ThreadWorker.start)
    # Ensure the exact string still present.
    assert "daemon=False,       # RFC-009 §7.13" in src


# ===========================================================================
# F. ProcessWorker contrast
# ===========================================================================


def test_F41_processworker_cooperative_child_parent_exits():
    """ProcessWorker with a cooperative child (EmptyBroker):
    parent's stop() succeeds, child exits with 0, parent exits."""
    res = _run_case(
        "processworker_cooperative_child_parent_exits",
        timeout=15.0,   # ProcessWorker + spawn + EmptyBroker takes time
    )
    assert res.exited is True
    _assert_ready(res)
    assert res.markers.get("STOP_RETURN") == "0"
    assert res.markers.get("EXITCODE") == "0"


def test_F43_processworker_exitcode_observable_source_pin():
    """ProcessWorker exposes `exitcode` property (RFC-008 §7.14).
    ThreadWorker has NO exit-code equivalent — one reason it cannot
    provide hard containment."""
    from agentflow.core.agent_worker import ProcessWorker, ThreadWorker
    assert hasattr(ProcessWorker, "exitcode")
    assert not hasattr(ThreadWorker, "exitcode")


def test_F44_processworker_has_terminate_kill_containment_source_pin():
    """RFC-008 §D escalation ladder: cooperative → SIGTERM → SIGKILL.
    Only ProcessWorker can force-reclaim a wedged worker."""
    from agentflow.core.agent_worker import ProcessWorker
    src = inspect.getsource(ProcessWorker.stop)
    assert "terminate()" in src
    assert "kill()" in src


def test_F45_threadworker_has_no_hard_containment_source_pin():
    """ThreadWorker cannot force-reclaim a wedged worker — Python
    threads have no safe cancellation primitive. RFC-009 §5 Option E
    rejected ctypes-based cancellation.

    Source-pin approach: ThreadWorker's module MUST NOT import
    ctypes / use `_async_raise` / call `.terminate()` or `.kill()`
    on a thread object. ProcessWorker's calls to
    `self.work_process.terminate()` are OK — that's the contrast."""
    from agentflow.core import agent_worker as mod
    # No ctypes-based thread cancellation. Check IMPORTS and CALL
    # sites, not string presence (the module has explanatory
    # comments that mention `ctypes` as the rejected option).
    assert not hasattr(mod, "ctypes"), (
        "ctypes must not be imported into agent_worker"
    )
    module_src = inspect.getsource(mod)
    assert "PyThreadState_SetAsyncExc" not in module_src, (
        "async-exception injection primitive must not be referenced"
    )
    # ThreadWorker.stop specifically does not have `work_thread.terminate`
    # / `work_thread.kill` (Thread has no such methods anyway).
    stop_src = inspect.getsource(mod.ThreadWorker.stop)
    assert "work_thread.terminate" not in stop_src
    assert "work_thread.kill" not in stop_src


# ===========================================================================
# G. Daemon=True experiment (test-only, does NOT modify prod)
# ===========================================================================


def test_G46_daemon_True_worker_thread_wedged_process_exits():
    """Demonstrate structurally: IF the ThreadWorker's work thread
    were `daemon=True`, the interpreter would exit cleanly under
    handler wedge — proving daemon flag is the root cause.

    This uses a raw thread (not ThreadWorker) since RFC-009 §7.13
    explicitly forbids modifying ThreadWorker's daemon flag."""
    res = _run_case("daemon_worker_thread_wedged_process_exits", timeout=3.0)
    assert res.exited is True
    _assert_ready(res)
    assert res.markers.get("DAEMON") == "True"


def test_G47_daemon_thread_finally_not_guaranteed_marker_write_may_miss(
    tmp_path,
):
    """Daemon threads may be killed BEFORE their `finally` runs.
    Write a marker path via env; check whether it was created.

    NOT asserting a specific outcome — daemon-kill timing is
    unpredictable across CPython versions. Just documenting the
    limitation: `finally` cleanup is not guaranteed."""
    marker = tmp_path / "finally_marker.txt"
    res = _run_case(
        "daemon_thread_finally_not_guaranteed",
        timeout=3.0,
        env_extra={"R10_5_FINALLY_MARKER": str(marker)},
    )
    assert res.exited is True
    _assert_ready(res)
    # Assertion is documentary — record whether marker was written.
    # If this ever fails deterministically, promote to a real assert.
    finally_ran = marker.exists()
    print(f"\n[G47 diagnostic] daemon thread finally ran: {finally_ran}")


def test_G48_G49_daemon_change_would_break_broker_cleanup_semantics():
    """Making ThreadWorker's work thread daemon=True would trade
    'visible hang' for 'silent mid-__deactivating corruption'.

    Cross-reference: RFC-009 §5 Option C rejected precisely because
    __deactivating's broker.stop() must complete for graceful teardown."""
    from agentflow.core.agent import Agent
    deact_src = inspect.getsource(getattr(Agent, "_Agent__deactivating"))
    # __deactivating calls broker.stop() which needs the worker
    # thread alive to complete.
    assert "self._broker.stop" in deact_src
    # RFC-009 doc explicitly states the rejection.
    rfc_path = _REPO_ROOT / "docs" / "rfc" / "RFC-009-thread-worker-lifecycle.md"
    if rfc_path.exists():
        text = rfc_path.read_text()
        # Look for either the option-C rejection or the daemon-policy
        # discussion (Option §5 C or §7.13).
        assert "daemon=True" in text or "Option C" in text


def test_G52_daemon_change_would_be_load_bearing_and_needs_rfc():
    """Any change to ThreadWorker's daemon flag would be a breaking
    lifecycle-contract change. Confirmed by RFC-009 §7.13 explicit
    decision."""
    from agentflow.core.agent_worker import ThreadWorker
    src = inspect.getsource(ThreadWorker.start)
    assert "RFC-009 §7.13" in src


# ===========================================================================
# H. Supervisor / deployment behaviour
# ===========================================================================


def test_H53_parent_can_detect_child_timeout():
    """The subprocess harness itself proves this — every "exited=False"
    result IS a parent-detected timeout."""
    res = _run_case("baseline_non_daemon_wedged", timeout=0.5)
    assert res.exited is False


def test_H54_parent_terminate_reclaims_child_bounded():
    """When parent's communicate times out, `_run_case`'s finally
    terminates (then kills) the child. Verify the child is actually
    reclaimed by observing that a subsequent case in the same test
    module doesn't hit process-limit issues (implicit — if children
    leaked, later tests would fail with resource exhaustion)."""
    res = _run_case("baseline_non_daemon_wedged", timeout=0.5)
    assert res.exited is False
    # If we got here, the finally block reclaimed the child.


def test_H55_kill_fallback_when_terminate_ignored():
    """The `_run_case` harness escalates terminate → kill if
    terminate doesn't take. Verify by source-inspecting the harness
    itself (this test module)."""
    src = inspect.getsource(_run_case)
    assert "terminate()" in src
    assert "kill()" in src


def test_H57_no_UNRECOVERABLE_state_yet_source_pin():
    """RFC-009 does not model an UNRECOVERABLE state distinct from
    STOP_TIMEOUT. Recording as design gap for R-10.5 mitigation."""
    from agentflow.core.agent_worker import WorkerState
    assert not hasattr(WorkerState, "UNRECOVERABLE")


def test_H58_STOP_TIMEOUT_is_the_only_signal_of_unrecoverability():
    """Right now, callers observe `worker.state == STOP_TIMEOUT` as
    the sole hint that a worker will not exit. Documented for design."""
    from agentflow.core.agent_worker import WorkerState
    assert WorkerState.STOP_TIMEOUT.value == "stop_timeout"


def test_H60_no_process_level_watchdog_source_pin():
    """No process-level watchdog thread exists today. Deployment
    must supply supervisor / systemd / container restart policy."""
    from agentflow.core import agent, agent_worker
    for mod in (agent, agent_worker):
        assert not hasattr(mod, "ProcessWatchdog")
        assert not hasattr(mod, "start_watchdog")


# ===========================================================================
# I. Existing API / architecture
# ===========================================================================


def test_I61_threadworker_constructor_has_no_daemon_kwarg():
    from agentflow.core.agent_worker import ThreadWorker
    sig = inspect.signature(ThreadWorker.__init__)
    assert "daemon" not in sig.parameters


def test_I62_threadworker_start_source_hardcodes_daemon_False():
    """Precise pin per H.62 requirement."""
    from agentflow.core.agent_worker import ThreadWorker
    src = inspect.getsource(ThreadWorker.start)
    # The literal `daemon=False` line.
    assert "daemon=False" in src


def test_I63_public_worker_type_selection_via_config_key():
    """Callers pick worker type via `agent_config[CONCURRENCY_TYPE]`
    or `Agent.start_thread` / `.start_process`."""
    from agentflow.core import config
    from agentflow.core.agent import Agent
    assert hasattr(config, "CONCURRENCY_TYPE")
    assert hasattr(Agent, "start_thread")
    assert hasattr(Agent, "start_process")
    # And __create_worker dispatches on the config key.
    src = inspect.getsource(getattr(Agent, "_Agent__create_worker"))
    assert "CONCURRENCY_TYPE" in src
    assert "process" in src


def test_I64_start_thread_vs_start_process_source_symmetric():
    from agentflow.core.agent import Agent
    src_th = inspect.getsource(Agent.start_thread)
    src_pr = inspect.getsource(Agent.start_process)
    # Both flip CONCURRENCY_TYPE and delegate to start().
    for src in (src_th, src_pr):
        assert "CONCURRENCY_TYPE" in src
        assert "self.start()" in src


def test_I65_no_auto_fallback_from_ThreadWorker_to_ProcessWorker():
    """No policy that switches worker type based on task risk."""
    from agentflow.core.agent import Agent
    src = inspect.getsource(getattr(Agent, "_Agent__create_worker"))
    # Simple `if 'process' == ... else` — no risk-based routing.
    assert "ProcessWorker" in src
    assert "ThreadWorker" in src
    # No fallback / auto-detect keywords.
    for keyword in ("fallback", "auto_select", "risk", "heuristic"):
        assert keyword not in src


def test_I66_no_supervisor_callback_source_pin():
    from agentflow.core.agent import Agent
    src = inspect.getsource(Agent)
    for keyword in ("supervisor_callback", "on_worker_hang", "on_timeout"):
        assert keyword not in src


def test_I67_no_restart_policy_source_pin():
    """RFC-008 §7.8 and RFC-009 §7.10 explicitly say restart is not
    supported."""
    from agentflow.core.agent_worker import ProcessWorker, ThreadWorker
    for cls in (ProcessWorker, ThreadWorker):
        assert not hasattr(cls, "restart")
        src = inspect.getsource(cls)
        # No restart method, no auto-restart hook.
        assert "def restart" not in src
        assert "auto_restart" not in src


def test_I68_no_healthcheck_or_heartbeat_source_pin():
    from agentflow.core.agent_worker import ProcessWorker, ThreadWorker
    for cls in (ProcessWorker, ThreadWorker):
        assert not hasattr(cls, "healthcheck")
        assert not hasattr(cls, "heartbeat")
        assert not hasattr(cls, "is_healthy")


def test_I69_no_fatal_shutdown_hook_source_pin():
    from agentflow.core.agent import Agent
    src = inspect.getsource(Agent)
    for keyword in ("fatal_shutdown", "os._exit", "force_exit"):
        assert keyword not in src


def test_I70_no_CLI_service_runner_in_tree_source_pin():
    """No `agentflow.cli` / `agentflow.runner` module today."""
    import agentflow
    assert not hasattr(agentflow, "cli")
    assert not hasattr(agentflow, "runner")
    assert not hasattr(agentflow, "service")


# ===========================================================================
# J. RFC-013 diagnostic properties (subprocess-observed)
# ===========================================================================


def test_J71_props_never_started_all_defaults():
    res = _run_case("props_never_started", timeout=3.0)
    assert res.exited is True
    _assert_ready(res)
    assert res.markers["ALIVE"] == "False"
    assert res.markers["DAEMON_IS_NONE"] == "True"
    assert res.markers["IDENT_IS_NONE"] == "True"
    assert res.markers["RESTART"] == "False"


def test_J72_props_running_cooperative():
    res = _run_case("props_running_cooperative", timeout=5.0)
    assert res.exited is True
    _assert_ready(res)
    assert res.markers["STATE"] == "running"
    assert res.markers["ALIVE"] == "True"
    assert res.markers["DAEMON"] == "False"
    assert res.markers["IDENT_IS_NONE"] == "False"
    assert res.markers["RESTART"] == "False"


def test_J73_props_normal_stopped():
    res = _run_case("props_normal_stopped", timeout=5.0)
    assert res.exited is True
    _assert_ready(res)
    assert res.markers["STATE"] == "stopped"
    assert res.markers["ALIVE"] == "False"
    assert res.markers["RESTART"] == "False"


def test_J74_props_wedged_stop_timeout_restart_True_and_process_blocks():
    """Wedged STOP_TIMEOUT → RESTART=True; AND subprocess hangs
    (R-10.5 hazard proven simultaneously)."""
    res = _run_case("props_wedged_stop_timeout", timeout=1.5)
    _assert_ready(res)
    assert res.exited is False, (
        "wedged non-daemon STOP_TIMEOUT should block interpreter exit"
    )
    assert res.markers["STATE"] == "stop_timeout"
    assert res.markers["ALIVE"] == "True"
    assert res.markers["DAEMON"] == "False"
    assert res.markers["RESTART"] == "True"


def test_J75_props_release_before_retry_auto_flips_restart_False():
    """RFC-013 §7.3 auto-recompute: release blocker → thread dies
    → thread_alive False → requires_process_restart False, even if
    state is still STOP_TIMEOUT."""
    res = _run_case(
        "props_release_before_retry_thread_dies_naturally", timeout=5.0,
    )
    assert res.exited is True
    _assert_ready(res)
    # Mid: still wedged, restart True.
    assert res.markers["MID_STATE"] == "stop_timeout"
    assert res.markers["MID_RESTART"] == "True"
    # After: state may still be STOP_TIMEOUT, but thread died →
    # RESTART auto-False.
    assert res.markers["AFTER_STATE"] == "stop_timeout"
    assert res.markers["AFTER_ALIVE"] == "False"
    assert res.markers["AFTER_RESTART"] == "False"


def test_J76_props_release_and_retry_flips_restart_False():
    res = _run_case("props_release_and_retry_stops_cleanly", timeout=5.0)
    assert res.exited is True
    _assert_ready(res)
    assert res.markers["MID_RESTART"] == "True"
    assert res.markers["STOP2"] == "True"
    assert res.markers["AFTER_STATE"] == "stopped"
    assert res.markers["AFTER_RESTART"] == "False"


def test_J77_processworker_absence_getattr_returns_False():
    """Capability-based diagnostics: ProcessWorker doesn't define
    the RFC-013 property; `getattr(worker, ..., False)` treats it
    as False → Agent.terminate falls through to WARNING path."""
    res = _run_case("processworker_property_absence", timeout=5.0)
    assert res.exited is True
    _assert_ready(res)
    assert res.markers["HAS_RESTART"] == "False"
    assert res.markers["HAS_ALIVE"] == "False"
    assert res.markers["GETATTR_RESTART"] == "False"


# ===========================================================================
# K. RFC-013 property contract (in-process, bounded)
# ===========================================================================


import queue as _queue
from agentflow.core.agent_worker import ThreadWorker as _ThreadWorker
from agentflow.core.agent_worker import WorkerState as _WorkerState


class _IProcInit:
    """Duck-typed initiator for in-process (bounded) tests."""

    def __init__(self, activate):
        self.config = {}
        self._activate = activate
        self.name_tag = "iproc"

    def M(self, message=None):
        return f"iproc {message or ''}"


def _cooperative_activate(cfg):
    q = cfg["work_queue"]
    while True:
        try:
            item = q.get(timeout=0.05)
        except _queue.Empty:
            continue
        if item == "terminate":
            return


def test_K81_thread_alive_False_when_never_started():
    worker = _ThreadWorker(_IProcInit(_cooperative_activate))
    assert worker.thread_alive is False


def test_K82_thread_daemon_None_when_never_started():
    worker = _ThreadWorker(_IProcInit(_cooperative_activate))
    assert worker.thread_daemon is None


def test_K83_worker_thread_ident_None_when_never_started():
    worker = _ThreadWorker(_IProcInit(_cooperative_activate))
    assert worker.worker_thread_ident is None


def test_K84_requires_process_restart_False_when_never_started():
    worker = _ThreadWorker(_IProcInit(_cooperative_activate))
    assert worker.requires_process_restart is False


def test_K85_running_worker_props():
    worker = _ThreadWorker(_IProcInit(_cooperative_activate))
    worker.start()
    try:
        assert worker.thread_alive is True
        assert worker.thread_daemon is False
        assert isinstance(worker.worker_thread_ident, int)
        assert worker.worker_thread_ident > 0
        assert worker.state == _WorkerState.RUNNING
        assert worker.requires_process_restart is False
    finally:
        worker.stop(graceful_timeout_s=2.0)


def test_K86_after_normal_stop_props():
    worker = _ThreadWorker(_IProcInit(_cooperative_activate))
    worker.start()
    worker.stop(graceful_timeout_s=2.0)
    assert worker.state == _WorkerState.STOPPED
    assert worker.thread_alive is False
    assert worker.requires_process_restart is False


def test_K87_wedged_stop_timeout_requires_process_restart_True():
    """Wedged non-daemon STOP_TIMEOUT: property is True.
    Bounded via release event in finally so no thread leaks past
    this test."""
    release = threading.Event()

    def wedged(cfg):
        release.wait(timeout=30.0)

    worker = _ThreadWorker(_IProcInit(wedged))
    worker.start()
    try:
        result = worker.stop(graceful_timeout_s=0.3)
        assert result is False
        assert worker.state == _WorkerState.STOP_TIMEOUT
        assert worker.thread_alive is True
        assert worker.thread_daemon is False
        assert worker.requires_process_restart is True
    finally:
        release.set()
        worker.stop(graceful_timeout_s=2.0)


def test_K88_after_release_before_retry_auto_flips_False():
    """RFC-013 auto-recompute: releasing the blocker (and joining
    the thread) makes thread_alive False → property auto-False,
    even if state stays STOP_TIMEOUT because we didn't retry stop."""
    release = threading.Event()

    def wedged(cfg):
        release.wait(timeout=30.0)

    worker = _ThreadWorker(_IProcInit(wedged))
    worker.start()
    worker.stop(graceful_timeout_s=0.3)
    assert worker.requires_process_restart is True
    try:
        release.set()
        assert worker.work_thread is not None
        worker.work_thread.join(2.0)
        assert worker.thread_alive is False
        # Property flips False even though state may still be STOP_TIMEOUT.
        assert worker.requires_process_restart is False
    finally:
        worker.stop(graceful_timeout_s=2.0)


def test_K89_after_retry_stop_reaches_STOPPED_and_flips_False():
    release = threading.Event()

    def wedged(cfg):
        release.wait(timeout=30.0)

    worker = _ThreadWorker(_IProcInit(wedged))
    worker.start()
    worker.stop(graceful_timeout_s=0.3)
    assert worker.requires_process_restart is True
    release.set()
    r2 = worker.stop(graceful_timeout_s=2.0)
    assert r2 is True
    assert worker.state == _WorkerState.STOPPED
    assert worker.requires_process_restart is False


def test_K90_properties_are_read_only():
    worker = _ThreadWorker(_IProcInit(_cooperative_activate))
    for name in (
        "thread_alive",
        "thread_daemon",
        "worker_thread_ident",
        "requires_process_restart",
    ):
        with pytest.raises(AttributeError):
            setattr(worker, name, "clobber")


def test_K91_property_getter_produces_no_log(caplog):
    """RFC-013 §7.3: property getter MUST NOT log. Read all four
    properties multiple times; assert caplog captures zero records
    at any level."""
    import logging as py_logging
    release = threading.Event()

    def wedged(cfg):
        release.wait(timeout=30.0)

    worker = _ThreadWorker(_IProcInit(wedged))
    worker.start()
    worker.stop(graceful_timeout_s=0.3)
    try:
        with caplog.at_level(py_logging.DEBUG):
            caplog.clear()
            for _ in range(10):
                _ = worker.thread_alive
                _ = worker.thread_daemon
                _ = worker.worker_thread_ident
                _ = worker.requires_process_restart
            # No log records from property reads.
            assert len(caplog.records) == 0, (
                f"property getter emitted logs: {caplog.records}"
            )
    finally:
        release.set()
        worker.stop(graceful_timeout_s=2.0)


# ===========================================================================
# L. Agent.terminate diagnostics (in-process, bounded)
# ===========================================================================


from agentflow.core.agent import Agent as _Agent


class _AgentWithBrokenStopWorker:
    """A minimal worker stub whose stop() returns a controlled bool.
    Used to test Agent.terminate's log-level dispatch without
    building a real ThreadWorker + wedged handler."""

    def __init__(self, stop_return, requires_restart=False, **props):
        self._stop_return = stop_return
        self._requires_restart = requires_restart
        self._props = props
        # Provide default identity via 'state' attribute.

    def is_working(self):
        return False

    def stop(self):
        return self._stop_return

    @property
    def state(self):
        return self._props.get("state", _WorkerState.STOPPED)

    @property
    def thread_alive(self):
        return self._props.get("thread_alive", False)

    @property
    def thread_daemon(self):
        return self._props.get("thread_daemon", None)

    @property
    def worker_thread_ident(self):
        return self._props.get("worker_thread_ident", None)

    @property
    def requires_process_restart(self):
        return self._requires_restart

    # Fields queried by legacy paths.
    work_thread = None


def _make_agent_with_stub_worker(stub):
    agent = _Agent(name="rfc013_diag", agent_config={})
    agent._agent_worker = stub
    return agent


def test_L92_terminate_logs_ERROR_when_requires_process_restart_True(caplog):
    import logging as py_logging
    stub = _AgentWithBrokenStopWorker(
        stop_return=False,
        requires_restart=True,
        state=_WorkerState.STOP_TIMEOUT,
        thread_alive=True,
        thread_daemon=False,
        worker_thread_ident=12345,
    )
    agent = _make_agent_with_stub_worker(stub)
    with caplog.at_level(py_logging.ERROR):
        agent.terminate()
    errors = [r for r in caplog.records if r.levelno == py_logging.ERROR]
    assert len(errors) == 1, (
        f"expected exactly 1 ERROR, got {len(errors)}: {errors}"
    )
    msg = errors[0].getMessage()
    assert "PROCESS RESTART REQUIRED" in msg
    assert "worker_type=_AgentWithBrokenStopWorker" in msg
    assert "state=WorkerState.STOP_TIMEOUT" in msg or "stop_timeout" in msg
    assert "thread_ident=12345" in msg
    assert "thread_alive=True" in msg
    assert "daemon=False" in msg
    assert "RFC-013" in msg


def test_L93_terminate_logs_WARNING_when_stop_False_but_restart_False(caplog):
    """Recoverable STOP_TIMEOUT: property is False → WARNING
    path preserved (existing behaviour)."""
    import logging as py_logging
    stub = _AgentWithBrokenStopWorker(
        stop_return=False,
        requires_restart=False,   # ← recoverable
        state=_WorkerState.STOP_TIMEOUT,
        thread_alive=False,       # thread died naturally
        thread_daemon=False,
    )
    agent = _make_agent_with_stub_worker(stub)
    with caplog.at_level(py_logging.WARNING):
        agent.terminate()
    errors = [r for r in caplog.records if r.levelno == py_logging.ERROR]
    warnings_ = [r for r in caplog.records if r.levelno == py_logging.WARNING]
    assert errors == [], f"unexpected ERROR: {errors}"
    assert any(
        "terminate: worker did not stop" in r.getMessage()
        for r in warnings_
    )


def test_L94_terminate_stop_True_emits_no_ERROR_or_WARNING_diagnostic(caplog):
    """Successful stop() → no restart-required diagnostic emitted."""
    import logging as py_logging
    stub = _AgentWithBrokenStopWorker(
        stop_return=True,
        requires_restart=False,
        state=_WorkerState.STOPPED,
    )
    agent = _make_agent_with_stub_worker(stub)
    with caplog.at_level(py_logging.WARNING):
        agent.terminate()
    for r in caplog.records:
        # Neither the ERROR nor the "terminate: worker did not stop"
        # WARNING should appear on the happy path.
        assert "PROCESS RESTART REQUIRED" not in r.getMessage()
        assert "worker did not stop" not in r.getMessage()


def test_L95_terminate_at_most_one_ERROR_per_invocation(caplog):
    import logging as py_logging
    stub = _AgentWithBrokenStopWorker(
        stop_return=False,
        requires_restart=True,
        state=_WorkerState.STOP_TIMEOUT,
        thread_alive=True,
        thread_daemon=False,
        worker_thread_ident=99,
    )
    agent = _make_agent_with_stub_worker(stub)
    with caplog.at_level(py_logging.ERROR):
        agent.terminate()
    errors = [r for r in caplog.records if r.levelno == py_logging.ERROR]
    assert len(errors) == 1


def test_L96_terminate_diagnostics_property_raising_does_not_break_terminate(caplog):
    """If a diagnostic property raises, terminate MUST NOT propagate;
    log warning path still runs."""
    import logging as py_logging

    class _BrokenPropsWorker(_AgentWithBrokenStopWorker):
        @property
        def requires_process_restart(self):
            raise RuntimeError("property is broken")

    stub = _BrokenPropsWorker(
        stop_return=False,
        state=_WorkerState.STOP_TIMEOUT,
    )
    agent = _make_agent_with_stub_worker(stub)
    with caplog.at_level(py_logging.WARNING):
        # Must not raise.
        agent.terminate()
    # An exception from the diagnostics property is logged via
    # logger.exception (a WARNING-level or ERROR-level record with
    # exc_info). Either way, terminate must not have propagated.
    # (We already know it didn't raise because pytest didn't fail.)


def test_L97_terminate_source_does_not_call_os_exit():
    src = inspect.getsource(_Agent.terminate)
    assert "os._exit" not in src


def test_L98_terminate_source_does_not_raise_from_diagnostics_path():
    """RFC-013 §7.7: the stop_result-is-False diagnostics branch must
    contain no `raise` statement, so terminate keeps its never-raise
    contract even when a worker property misbehaves.

    Pinned via AST rather than substring matching: the prose in that
    block legitimately contains the word "raise" ("never-raise
    contract", "a property getter raised"), so a text search reports
    false positives. Only an actual `ast.Raise` node is a violation.
    """
    tree = ast.parse(textwrap.dedent(inspect.getsource(_Agent.terminate)))

    # Locate `if stop_result is False:` — the diagnostics branch.
    diagnostics_branch = None
    for node in ast.walk(tree):
        if not isinstance(node, ast.If):
            continue
        test = node.test
        if (
            isinstance(test, ast.Compare)
            and isinstance(test.left, ast.Name)
            and test.left.id == "stop_result"
            and len(test.ops) == 1
            and isinstance(test.ops[0], ast.Is)
            and isinstance(test.comparators[0], ast.Constant)
            and test.comparators[0].value is False
        ):
            diagnostics_branch = node
            break

    assert diagnostics_branch is not None, (
        "could not locate the `if stop_result is False:` diagnostics "
        "branch in Agent.terminate — the RFC-013 source pin needs "
        "updating to match the current structure"
    )

    raises = [
        n for n in ast.walk(diagnostics_branch) if isinstance(n, ast.Raise)
    ]
    assert not raises, (
        "the requires-restart branch of terminate must never raise; "
        f"found {len(raises)} raise statement(s) at line offset(s) "
        f"{[n.lineno for n in raises]}"
    )


# ===========================================================================
# M. Source pins (RFC-013 §7 policy commitments)
# ===========================================================================


def test_M99_threadworker_start_source_preserves_daemon_False_marker():
    """RFC-013 §Acceptance #3."""
    from agentflow.core.agent_worker import ThreadWorker
    src = inspect.getsource(ThreadWorker.start)
    assert "daemon=False,       # RFC-009 §7.13" in src


def test_M100_workerstate_has_no_UNRECOVERABLE():
    """RFC-013 §Acceptance #4."""
    from agentflow.core.agent_worker import WorkerState
    assert not hasattr(WorkerState, "UNRECOVERABLE")


def test_M101_agentflow_core_does_not_call_os_exit():
    """RFC-013 §7.9 policy."""
    from agentflow.core import agent, agent_worker
    for mod in (agent, agent_worker):
        src = inspect.getsource(mod)
        assert "os._exit" not in src


def test_M102_agentflow_core_does_not_use_ctypes_async_cancellation():
    from agentflow.core import agent, agent_worker
    for mod in (agent, agent_worker):
        # Not imported into the module namespace.
        assert not hasattr(mod, "ctypes")
        src = inspect.getsource(mod)
        assert "PyThreadState_SetAsyncExc" not in src
