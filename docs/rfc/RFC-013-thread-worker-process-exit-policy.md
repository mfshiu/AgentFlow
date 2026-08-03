# RFC-013 — ThreadWorker process-exit policy

- **Status**: **Implemented** (2026-08-03) — see §12 Implementation record
- **Author**: Audit follow-up
- **Depends on**: `docs/audit/05-risk-register.md` R-10.5 (non-daemon ThreadWorker work_thread blocks Python interpreter exit — runtime-confirmed via subprocess characterisation); downstream of RFC-008 (ProcessWorker lifecycle), RFC-009 (ThreadWorker lifecycle), RFC-010 (broker bounded shutdown), RFC-011 (broker bounded startup), RFC-012 (publish result contract)
- **Scope**: Give AgentFlow an explicit, defensible policy for the R-10.5 residual that RFC-009 §H, RFC-010 §7.13, and RFC-011 §0 all documented but none resolved. Ship **observability** (`requires_process_restart`, `thread_alive`, `thread_daemon`, `worker_thread_ident`), **diagnostics** (Agent.terminate ERROR log on unrecoverable state), and **documented operator guidance** (ProcessWorker for high-risk tasks; external supervisor for hard containment). **Do NOT change the daemon flag, do NOT call `os._exit`, do NOT introduce automatic ProcessWorker fallback, do NOT introduce library-internal process restart.**
- **Explicitly out of scope**: `daemon=True` ThreadWorker (rejected — see §5 Option B), async thread cancellation via ctypes / `PyThreadState_SetAsyncExc` (rejected — see §5 Option J), `os._exit` from library code (rejected — see §5 Option F), auto-fallback from ThreadWorker to ProcessWorker (deferred — see §5 Option G), per-handler risk classification, heartbeat / watchdog implementation, process manager implementation, Kubernetes operator, whole-process per-Agent isolation, broker lifecycle changes, `MessageBroker` ABC changes, `Message Schema` / `Parcel` changes.

---

## 1. Problem statement

RFC-009 §7.13 pinned `ThreadWorker`'s work_thread as `daemon=False` for a specific reason: guaranteeing that `Agent.__deactivating`'s call to `broker.stop()` completes, so paho's DISCONNECT / loop_stop / recovery-registry teardown finishes cleanly. RFC-010 §7.13 and RFC-011 §7.13 kept this discipline. The cost — documented but not resolved in those RFCs — is that a wedged worker thread blocks `sys.exit` / `main()` return / interpreter shutdown:

```
1. ThreadWorker.start builds a non-daemon work_thread (RFC-009 §7.13).
2. Agent._activate runs in that thread → __activating → broker.start
   → work-queue loop → __deactivating → broker.stop.
3. If a handler / broker.start / broker.stop / on_activate wedges:
     work_thread never exits → ThreadWorker.stop(timeout) returns
     False → STOP_TIMEOUT (RFC-009 §C).
4. Agent.terminate observes bool return, logs WARNING (RFC-009
   §7.14), returns.
5. main() returns → Python interpreter waits for all non-daemon
   threads.
6. work_thread is still alive and non-daemon → interpreter waits
   forever. Only an external SIGTERM / SIGKILL reclaims the process.
```

Runtime-confirmed via `tests/unit/core/test_thread_worker_process_exit.py` + `tests/subprocess_cases/thread_worker_exit_cases.py` (44 characterisation tests, 2026-08-03):

- **Baseline** (`test_A2–A8`): non-daemon thread wedged → `subprocess.communicate(timeout=1.5)` times out; parent must terminate/kill child to reclaim. `sys.exit(0)` does NOT bypass the wait; `os._exit(0)` does but skips `atexit` / `finally`.
- **ThreadWorker wedged handler** (`test_C14–C22`): `stop(0.3)` returns False; `state=stop_timeout`; `ALIVE=True`, `DAEMON=False`; parent subprocess never exits; release + retry stop successfully reaches STOPPED (RFC-009 §D retry contract works).
- **ProcessWorker contrast** (`test_F41–F45`): parent's `stop()` escalates to SIGTERM → SIGKILL; child exit code observable; parent exits cleanly. Hard containment only available via ProcessWorker.
- **Daemon experiment** (`test_G46–G49`): a raw `daemon=True` thread does allow interpreter exit — but `finally` may not run, `broker.stop()` in `__deactivating` may be truncated mid-flight, and there is no signal to the application layer that cleanup was skipped. **This trade-off is exactly what RFC-009 §5 Option C rejected**; RFC-013 does NOT revisit that decision.
- **Broker helpers** (`test_D24, D25, E31, E35`): RFC-010 stop helper and RFC-011 startup helper are both `daemon=True` — they alone do NOT block interpreter exit; the root cause is the outer non-daemon work_thread waiting on them.

This RFC does not claim to eliminate R-10.5 in-process. Instead it:

1. **Ships observability** so callers / operators can detect the unrecoverable state before it manifests as a hung `main()`.
2. **Ships diagnostics** so `Agent.terminate` logs at ERROR level (not just WARNING) when a process restart is required.
3. **Ships operational guidance** so deployment (systemd / container / supervisord) can hard-contain the process.
4. **Ships policy guidance** so agents that run high-risk tasks (native SDKs, untrusted handlers, non-cancellable I/O) are documented as ProcessWorker candidates.

**Explicitly rejects, from the library layer**: `daemon=True` default, `os._exit`, ctypes-based cancellation, automatic ProcessWorker fallback, automatic process restart. Those live in the operational / deployment layer.

---

## 2. Runtime evidence

Baseline before this RFC: `PYTHONPATH=src pytest tests/unit` → **569 passed, 2 xfailed** in ~60 s (post-RFC-012 + R-10.5 characterisation).

Confirmed by `tests/unit/core/test_thread_worker_process_exit.py` (44 characterisation tests, all currently PASSED against the broken state):

| # | Behaviour | Test |
|---|---|---|
| A1 | No extra threads → clean exit | `test_A1_baseline_no_threads_exits_cleanly` |
| A2 | Non-daemon wedged → subprocess does NOT exit | `test_A2_baseline_non_daemon_wedged_blocks_interpreter_exit` |
| A3 | Daemon wedged → subprocess exits | `test_A3_baseline_daemon_wedged_allows_interpreter_exit` |
| A4 | `sys.exit(0)` with non-daemon → still waits | `test_A4_baseline_sys_exit_still_waits_for_non_daemon` |
| A5 | `os._exit(0)` bypasses wait | `test_A5_baseline_os_exit_bypasses_non_daemon_wait` |
| A6 | `atexit` runs on normal exit | `test_A6_baseline_atexit_runs_on_normal_exit` |
| A7 | `atexit` does NOT run on `os._exit` | `test_A7_baseline_atexit_does_NOT_run_on_os_exit` |
| A8 | Subprocess timeout detection stable across repeats | `test_A8_subprocess_timeout_detection_is_stable` |
| B9 | ThreadWorker normal start/stop → subprocess exits | `test_B9_threadworker_normal_start_stop_process_exits` |
| B10 | Never-started worker → subprocess exits | `test_B10_threadworker_never_started_process_exits` |
| B12 | ThreadWorker source hard-codes `daemon=False` | `test_B12_threadworker_source_hardcodes_daemon_False` |
| B13 | ProcessWorker also uses `daemon=False` but has hard containment | `test_B13_processworker_source_hardcodes_daemon_False_too` |
| C14 | Wedged activate + no stop → subprocess does NOT exit | `test_C14_wedged_activate_no_stop_process_blocks` |
| C15/19 | Wedged + `stop(0.3)` → False + STOP_TIMEOUT + ALIVE=True + DAEMON=False; subprocess does NOT exit | `test_C15_C19_wedged_activate_stop_TIMEOUT_still_blocks_interpreter` |
| C20 | Parent must terminate to reclaim | `test_C20_wedged_activate_requires_parent_kill` |
| C21/22 | Release + retry stop → STOPPED; subprocess exits | `test_C21_C22_wedged_release_and_retry_stop_converges_to_STOPPED` |
| D24 | RFC-011 startup helper `daemon=True`; work_thread `daemon=False` (source) | `test_D24_source_broker_start_bounded_but_worker_thread_still_non_daemon` |
| D25 | Standalone daemon helper alone does NOT block exit | `test_D25_startup_helper_daemon_True_does_not_block_exit_alone` |
| E31 | RFC-010 stop helper `daemon=True` (source) | `test_E31_source_broker_stop_bounded_but_worker_still_non_daemon` |
| E35 | Root cause pinned to exact line in `ThreadWorker.start` | `test_E35_root_cause_is_worker_thread_daemon_False_source_pinned` |
| F41 | ProcessWorker cooperative child → parent exits with exitcode 0 | `test_F41_processworker_cooperative_child_parent_exits` |
| F43 | `ProcessWorker.exitcode` observable; ThreadWorker has no equivalent | `test_F43_processworker_exitcode_observable_source_pin` |
| F44/45 | ProcessWorker has `terminate()`/`kill()`; ThreadWorker has neither + no ctypes import | `test_F44/45` |
| G46 | Raw daemon thread + wedge → subprocess exits (proves daemon flag is root cause) | `test_G46_daemon_True_worker_thread_wedged_process_exits` |
| G47 | daemon thread `finally` marker NOT guaranteed to write on abrupt kill | `test_G47_daemon_thread_finally_not_guaranteed_marker_write_may_miss` |
| G48/49 | `daemon=True` would truncate `__deactivating`'s `broker.stop()` (source cross-ref) | `test_G48_G49_daemon_change_would_break_broker_cleanup_semantics` |
| G52 | Load-bearing decision is marked with RFC-009 §7.13 in source | `test_G52_daemon_change_would_be_load_bearing_and_needs_rfc` |
| H53-55 | Parent detects timeout; terminate/kill reclaim documented in `_run_case` | `test_H53_H54_H55` |
| H57 | No `UNRECOVERABLE` state in `WorkerState` | `test_H57_no_UNRECOVERABLE_state_yet_source_pin` |
| H58 | `STOP_TIMEOUT` is the only signal today | `test_H58_STOP_TIMEOUT_is_the_only_signal_of_unrecoverability` |
| H60 | No process-level watchdog in-tree | `test_H60_no_process_level_watchdog_source_pin` |
| I61-70 | No `daemon` kwarg; no supervisor callback; no restart / healthcheck / heartbeat / fatal shutdown hook / CLI runner | `test_I61-70` |

---

## 3. Current state

Source (`src/agentflow/core/agent_worker.py`, `ThreadWorker.start`):

```python
thread = threading.Thread(
    target=self._run_target,
    args=(cfg,),
    name=f'ThreadWorker-{self.initiator_agent.M()}',
    daemon=False,       # RFC-009 §7.13
)
```

`Agent.terminate` (source, `agent.py`):

```python
def terminate(self):
    ...
    try:
        stop_result = self._agent_worker.stop()
    except Exception as ex:
        logger.exception(...)
        return
    if stop_result is False:
        state = getattr(self._agent_worker, 'state', 'unknown')
        thread = getattr(self._agent_worker, 'work_thread', None)
        logger.warning(self.M(
            f"terminate: worker did not stop within its deadline; "
            f"state={state}, thread={thread!r}. terminate() has "
            f"returned but the worker thread may still be alive; "
            f"because daemon=False, Python interpreter shutdown "
            f"may still block on this thread (see RFC-009 §H)."
        ))
```

Observable surface today:
- `worker.state` (WorkerState enum) — but STOP_TIMEOUT is ambiguous between "will recover on retry" and "will never recover".
- `worker.work_thread` — private-ish attribute; not documented for callers.
- `worker.last_exception` (RFC-009) — only set on `_activate` Exception; not set for state-machine wedges.
- No property that answers the operator's question: "do I need to restart this process to reclaim the OS resources?"

Diagnostic level today: `WARNING`. Ops tooling typically doesn't page on WARNING; STOP_TIMEOUT + non-daemon-blocked-exit is worth an ERROR.

---

## 4. Desired state

- **`ThreadWorker` gains three read-only observability properties** (RFC-013 §7.3-§7.6):
  - `thread_alive: bool` — mirror of `work_thread.is_alive()` when a thread exists; False otherwise.
  - `thread_daemon: Optional[bool]` — mirror of `work_thread.daemon` when a thread exists; None otherwise (pin for future daemon-flag policy debate).
  - `requires_process_restart: bool` — True iff `state == STOP_TIMEOUT and thread_alive and thread_daemon is False`. This is the operator-actionable signal.
- **Optional public property**: `worker_thread_ident: Optional[int]` — mirror of `work_thread.ident` for correlating with `py-spy` / `gdb` dumps. Diagnostic-only; None when no thread.
- **`ThreadWorker.stop` signature and semantics unchanged** (RFC-013 §7.11-§7.13). STOP_TIMEOUT retains "possibly recoverable" meaning (proven by `test_C21/C22`).
- **No `UNRECOVERABLE` state** in first phase (RFC-013 §7.2). The observability property carries the same signal without adding a state that would need its own transition rules.
- **`Agent.terminate` logs at `ERROR` when `requires_process_restart` is True** (RFC-013 §7.7-§7.8). Signature, return type, and never-raise contract all preserved.
- **ProcessWorker guidance documented** (RFC-013 §7.11-§7.12). Concrete lists of task types that should use ProcessWorker vs ThreadWorker.
- **External supervisor contract documented** (RFC-013 §7.14-§7.19). systemd unit templates, container liveness probes, kill escalation, grace period recommendations.
- **Rejections explicitly listed** so future PRs cannot re-open them without a new RFC:
  - `daemon=True` default (§5 Option B): rejected — would truncate `__deactivating` cleanup.
  - `os._exit` from library (§5 Option F): rejected — bypasses `atexit` / `finally`; unsafe for library code.
  - ctypes / `PyThreadState_SetAsyncExc` (§5 Option J): rejected — unsafe on non-Python C extensions.
  - Auto-fallback to ProcessWorker (§5 Option G): deferred — needs `Agent` picklability guarantees per task type.
  - In-library process restart (§5 residual): rejected — belongs to deployment layer.

---

## 5. Options considered

### Option A — Keep `daemon=False`, document (currently active) (**recommended base**)

| Aspect | Analysis |
|---|---|
| Process exit guarantee | ✗ (relies on external supervisor) |
| Resource cleanup | ✓ (`__deactivating` runs to completion) |
| Backward compat | Perfect |
| Data loss risk | Low |
| Testability | Documented — R-10.5 characterisation locks the current behaviour |
| Operational complexity | High (needs supervisor) |
| Cross-platform | Consistent |
| Production suitability | Existing deployments already run with systemd / container supervisors |
| Verdict | **Recommended base** |

### Option B — `daemon=True` default

| Aspect | Analysis |
|---|---|
| Process exit guarantee | ✓ |
| Resource cleanup | ✗ — `finally` may not run; `broker.stop()` may be truncated (`test_G47/G48`) |
| Backward compat | Breaking (silent behaviour change) |
| Data loss risk | **High** — mid-`__deactivating` truncation means paho DISCONNECT may not send; QoS 1 in-flight messages may drop |
| Verdict | **Rejected** — same reasoning as RFC-009 §5 Option C. |

### Option C — ThreadWorker `daemon` opt-in kwarg

| Aspect | Analysis |
|---|---|
| Process exit guarantee | Conditional (caller-chosen) |
| Resource cleanup | Conditional |
| Backward compat | Compatible (default False preserves current) |
| Data loss risk | Caller decides; needs governance |
| Testability | Fine |
| Complexity | Low |
| Verdict | Deferred to a future RFC — see §Appendix B. Callers today should use ProcessWorker for the same purpose; introducing a footgun without a use case is over-engineering. |

### Option D — Add `WorkerState.UNRECOVERABLE`

| Aspect | Analysis |
|---|---|
| Observability | ✓ |
| Ambiguity with STOP_TIMEOUT | Introduces a state-transition question: when does STOP_TIMEOUT → UNRECOVERABLE? Immediately? After N retries? After a wall-clock deadline? |
| Blocker-release retry semantics | `test_C21/C22` shows retry can recover — an eagerly-UNRECOVERABLE state would foreclose that path or force a UNRECOVERABLE → STOPPING transition (state graph churn) |
| Verdict | **Rejected as first phase**. STOP_TIMEOUT stays; `requires_process_restart` property (Option E) provides the same operator signal without the state-transition ambiguity. Can be revisited in a future RFC that also introduces a policy for automatic escalation from STOP_TIMEOUT. |

### Option E — Add `requires_process_restart` property (**recommended companion**)

| Aspect | Analysis |
|---|---|
| Observability | ✓ — single boolean; queryable by caller and by `Agent.terminate` for logging |
| No new state | ✓ — computed from `state` + `thread_alive` + `thread_daemon` |
| Backward compat | Additive |
| Complexity | Trivial |
| Verdict | **Recommended** — pairs with A |

### Option F — `STOP_TIMEOUT` → `os._exit`

| Aspect | Analysis |
|---|---|
| Process exit guarantee | ✓ |
| Resource cleanup | ✗ — atexit does not run (`test_A7`); other file descriptors, sockets, subprocesses, and dispatchers in the same process are also killed abruptly |
| Backward compat | Breaking (side-effect: any co-tenant of the process is also killed) |
| Data loss risk | **Very high** — arbitrary interruption |
| Production suitability | Only appropriate at operator layer (init 1 / supervisor decides), not library code |
| Verdict | **Rejected** — library code MUST NOT call `os._exit`. Documented explicitly (§7.9). |

### Option G — Auto-fallback from ThreadWorker to ProcessWorker

| Aspect | Analysis |
|---|---|
| Process exit guarantee | ✓ (via ProcessWorker's SIGKILL escalation) |
| Complexity | High — needs Agent picklability guarantees per task; RFC-008 §A groundwork exists but doesn't cover arbitrary Agent subclasses |
| Backward compat | Breaking — silently changes worker type; shared-instance semantics lost |
| Verdict | **Deferred** to a future "task-type-aware worker selection" RFC. |

### Option H — External supervisor (systemd / container / supervisord) (**recommended companion**)

| Aspect | Analysis |
|---|---|
| Process exit guarantee | ✓ (via SIGKILL from init) |
| Resource cleanup | ✗ (killed by SIGKILL) but restarts a fresh process — the "clean slate" alternative |
| Backward compat | Perfect |
| Data loss risk | Low (operator picks grace period + kill escalation) |
| Cross-platform | systemd Linux; supervisord any Unix; container runtimes universal |
| Verdict | **Recommended** — pairs with A + E. Deployment guide included in this RFC (§9) so operators have concrete templates. |

### Option I — Whole-process per-Agent isolation

| Aspect | Analysis |
|---|---|
| Process exit guarantee | ✓ |
| Backward compat | Breaking (agent-per-process is a different deployment model) |
| Overhead | High for single-agent case; low benefit for the common case |
| Verdict | Deferred — separate RFC if a real deployment surfaces the need. |

### Option J — Native thread async cancellation via ctypes

| Aspect | Analysis |
|---|---|
| Process exit guarantee | Sometimes |
| Reliability | Documented as unsafe: does not interrupt C extensions or syscalls; can leave locks held; can crash the interpreter |
| Verdict | **Rejected**. Same rejection as RFC-009 §5 Option E. |

### Comparison summary

| Criterion | **A** | B | C | D | **E** | F | G | **H** | I | J |
|---|---|---|---|---|---|---|---|---|---|---|
| Process exit guarantee | ✗ | ✓ | opt | ✗ | ✗ | ✓ | ✓ | ✓ (ext) | ✓ | ~ |
| Resource cleanup | ✓ | ✗ | opt | ✓ | ✓ | ✗ | ✓ | ✓ | ✓ | ~ |
| Backward compat | ✓ | ✗ | ✓ | ✓ | ✓ | ✗ | ✗ | ✓ | ✗ | ✓ |
| Data loss risk | Low | **High** | opt | Low | Low | **V-High** | Low | Low | Low | High |
| Verdict | **base** | rej | future | rej-1st | **rec** | rej | future | **rec** | future | rej |

**Recommended first-phase**: **A + E + H** — keep daemon flag; add `requires_process_restart` observability + Agent.terminate ERROR log; document external supervisor pattern as the deployment-layer solution.

---

## 6. Recommended design

Adopt **A + E + H**. No source change to `ThreadWorker.start`; no state-machine change. Add three read-only properties on `ThreadWorker`. Elevate `Agent.terminate`'s log level when the properties indicate unrecoverability. Ship a deployment guide.

### 6.1 New ThreadWorker properties

```python
class ThreadWorker(Worker):
    ...
    @property
    def thread_alive(self) -> bool:
        """RFC-013 §7.4: True iff work_thread exists AND is alive."""
        t = self.work_thread
        return t is not None and t.is_alive()

    @property
    def thread_daemon(self) -> Optional[bool]:
        """RFC-013 §7.5: work_thread.daemon flag, or None if no
        thread exists. Read-only pin for the RFC-009 §7.13 policy
        decision."""
        t = self.work_thread
        return t.daemon if t is not None else None

    @property
    def worker_thread_ident(self) -> Optional[int]:
        """RFC-013 §7.6: work_thread.ident (OS thread id) for
        correlating with py-spy / gdb dumps. Diagnostic-only.
        None if no thread exists."""
        t = self.work_thread
        return t.ident if t is not None else None

    @property
    def requires_process_restart(self) -> bool:
        """RFC-013 §7.3: True iff this worker CANNOT be recovered
        in-process — a fresh process must be started to reclaim the
        wedged OS thread.

        Concretely: True when all three hold —
          - state == WorkerState.STOP_TIMEOUT
          - thread_alive
          - thread_daemon is False

        Any False in the conjunction → False. In particular, a
        STOP_TIMEOUT with the thread already dead (blocker released
        between join and check) → False; the caller may retry stop()
        and recover to STOPPED (verified by test_C21/C22)."""
        return (
            self.state == WorkerState.STOP_TIMEOUT
            and self.thread_alive
            and self.thread_daemon is False
        )
```

**No source change to `WorkerState`** — no UNRECOVERABLE member.

### 6.2 Agent.terminate ERROR log path

Existing WARNING log stays in place for `worker.stop()` returning False. When the returned False is combined with `requires_process_restart is True`, upgrade to ERROR:

```python
def terminate(self):
    ...
    if stop_result is False:
        state = getattr(self._agent_worker, 'state', 'unknown')
        thread = getattr(self._agent_worker, 'work_thread', None)
        needs_restart = getattr(
            self._agent_worker, 'requires_process_restart', False,
        )
        if needs_restart:
            ident = getattr(
                self._agent_worker, 'worker_thread_ident', None,
            )
            daemon = getattr(
                self._agent_worker, 'thread_daemon', None,
            )
            logger.error(self.M(
                f"terminate: PROCESS RESTART REQUIRED — worker "
                f"type={type(self._agent_worker).__name__}, "
                f"state={state}, thread_ident={ident}, "
                f"thread_alive=True, daemon={daemon}. "
                f"terminate() has returned but the interpreter "
                f"will NOT exit until an external supervisor "
                f"terminates this process. See RFC-013."
            ))
        else:
            logger.warning(self.M(
                f"terminate: worker did not stop within its deadline; "
                f"state={state}, thread={thread!r}. terminate() has "
                f"returned but the worker thread may still be alive; "
                f"because daemon=False, Python interpreter shutdown "
                f"may still block on this thread (see RFC-009 §H)."
            ))
```

**Never raises**. **Never calls `os._exit`**. **Never signals the process itself**. The ERROR log is the caller's actionable signal — ops tooling (log aggregation, alerting) picks it up, restart policy responds.

### 6.3 Documented operator response

RFC-013 §7.16-§7.19 spells out the operator's expected response to the ERROR log:

1. **Systemd `Restart=on-failure` / `Restart=always`**: on the next SIGTERM (`TimeoutStopSec` expiring), systemd escalates to SIGKILL and restarts.
2. **Container liveness probe**: the process's health endpoint checks whether any worker has `requires_process_restart=True` (application-layer wiring; not shipped by RFC-013 but documented as pattern); returns 503; orchestrator restarts pod.
3. **Supervisord `autorestart=true`** with a short `stopwaitsecs`; escalates to KILL on expiry.

### 6.4 ProcessWorker guidance for high-risk tasks

RFC-013 §7.11-§7.13: documented in the `ThreadWorker` docstring and in a new deployment-guide file. Concrete lists:

**Use ProcessWorker for**:
- Third-party SDKs with no reliable timeout
- Native extension calls (C/C++/Rust bindings that may hang inside FFI)
- External network libraries with their own lifecycle (unclosed sockets on `stop()`)
- Untrusted handlers (user-supplied code without cooperation guarantees)
- Any I/O that may permanently block (kernel-side wait; e.g. serial port `read` without timeout)
- Any Agent that must maintain hard containment for compliance / SLA reasons

**Use ThreadWorker for**:
- Cooperative code with explicit `terminate_event.wait(timeout=X)` patterns
- I/O with explicit timeouts (socket / requests / paho — all validated by RFC-004 through RFC-012 patterns)
- Handlers cancellable via Event / queue signal
- Trusted, in-tree handlers under project review
- Non-blocking or short-lived work

The guidance is documented; it is NOT enforced by the library. Callers who ignore it get the R-10.5 hazard.

---

## 7. Concrete decisions (all 22)

### 7.1 STOP_TIMEOUT precise semantics

`STOP_TIMEOUT` means "the cooperative `stop()` call could not observe the worker thread exit within `graceful_timeout_s`". It does NOT mean "the worker is permanently wedged". `test_C21/C22` verifies that a blocker released post-STOP_TIMEOUT followed by a retry `stop()` reaches STOPPED. RFC-013 preserves this: no automatic escalation from STOP_TIMEOUT to any "terminal wedge" state.

### 7.2 New UNRECOVERABLE state

**Not introduced in first phase.** Rationale (§5 Option D):
- Adding a state requires a transition policy (when? after N retries? wall-clock?).
- STOP_TIMEOUT already carries the "may or may not recover" signal.
- The `requires_process_restart` property (§7.3) provides the operator signal without state-graph churn.

Future RFC may revisit if a caller demonstrates a genuine need for a distinct state.

### 7.3 `requires_process_restart` semantics

True iff **all three** conditions hold:
- `state == WorkerState.STOP_TIMEOUT`
- `thread_alive == True` (work_thread exists AND is_alive)
- `thread_daemon is False` (worker is non-daemon — the actual R-10.5 blocker)

False if any of the three flips (e.g. blocker released and stop() retried to STOPPED, thread died on its own, or — hypothetically — the worker thread were made daemon). Recomputed on every read; not cached; no explicit reset.

### 7.4 `thread_alive` property

Read-only. Returns `work_thread.is_alive()` if `work_thread` is not None; False otherwise. Idempotent; no side effect.

### 7.5 `thread_daemon` property

Read-only. Returns `work_thread.daemon` if `work_thread` is not None; None otherwise. Diagnostic pin for the RFC-009 §7.13 daemon-flag decision — a future RFC can verify by asserting `thread_daemon is False` for any real production worker.

### 7.6 `worker_thread_ident` — public exposure

Read-only. Returns `work_thread.ident` (OS thread id, `int`) if `work_thread` is not None; None otherwise. **Diagnostic-only**; useful for correlating with `py-spy dump --pid <pid>` or `gdb -p <pid>` when investigating a hung worker. Not stable across restarts (each thread gets a new ident).

Public because callers who write custom `Agent.terminate` handlers (via subclass) or observability layers need it. Named `worker_thread_ident` (not just `ident`) to disambiguate from any future `agent_ident` or `broker_ident`.

### 7.7 `Agent.terminate` log level on restart-required

**ERROR**. Rationale:
- WARNING is the existing level for bounded return + thread possibly alive; keep for the recoverable-STOP_TIMEOUT case (blocker may release; retry may succeed).
- ERROR indicates "operator intervention required" — the state is unrecoverable in-process; only a process restart reclaims resources. Alerting tooling that pages on ERROR (many production ops setups) picks this up.
- CRITICAL is reserved for library-level pathological conditions that risk data corruption (none here — the process just won't exit).

The ERROR log message includes: worker type, state value, thread ident, `thread_alive=True`, daemon, and a reference to RFC-013.

### 7.8 Whether to raise

**No.** `Agent.terminate`'s never-raise contract (established through RFC-004 / RFC-008 / RFC-009 / RFC-010 / RFC-011) is preserved. Raising would break every caller that treats `terminate()` as fire-and-forget cleanup inside `finally`.

### 7.9 `Agent.terminate` return type

**Unchanged.** Signature stays `def terminate(self)` returning None. Return type widening (to `bool` / result object) would break callers that call `agent.terminate()` and discard the return. Callers who need programmatic access to the restart-required signal read `agent._agent_worker.requires_process_restart` directly.

### 7.10 New termination result object

**Not introduced.** §7.9 keeps the return None. If a future RFC introduces `AgentTerminationResult`, it can be additive via a new method (e.g. `terminate_with_result() -> AgentTerminationResult`) without breaking `terminate()`.

### 7.11 ProcessWorker selection criteria

Documented in the ThreadWorker docstring and in a new file `docs/deployment/worker-type-selection.md`:

> Use `ProcessWorker` (`agent_config[CONCURRENCY_TYPE]='process'` or `Agent.start_process()`) for:
> - Third-party SDKs that lack timeout / cancellation primitives
> - Native extension calls (`ctypes`, Rust bindings, ML frameworks with GIL-bound waits)
> - External network libraries with side-effecting `stop()` (any library that RFC-010 / RFC-011's bounded pattern cannot wrap)
> - Untrusted handlers (user-supplied code with unknown blocking behaviour)
> - Any I/O that may permanently block at the kernel layer
> - Any Agent whose loss-of-availability is worse than loss-of-in-process-cleanup

### 7.12 ThreadWorker acceptable task types

> Use `ThreadWorker` (`agent_config[CONCURRENCY_TYPE]='thread'` or `Agent.start_thread()`) for:
> - Cooperative code that reads `terminate_event.wait(timeout=X)` explicitly
> - I/O with explicit timeouts (socket / requests / paho — all patterns RFC-004 through RFC-012 preserve)
> - Handlers cancellable via Event / queue signal
> - Trusted in-tree handlers under project review
> - Non-blocking or short-lived work

### 7.13 High-risk third-party call classification

Non-exhaustive list documented as guidance:
- Any C-extension `time.sleep`-equivalent that ignores signals (e.g. some sensor SDKs' blocking `read()`)
- Any library that opens sockets without SO_RCVTIMEO
- Any `subprocess.run` without `timeout=`
- Any lock acquire without timeout
- Any queue.get without timeout
- Any GRPC/asyncio integration when the caller isn't in an asyncio context

The list is documentary — callers ultimately judge risk. RFC-013 does NOT try to enumerate all possible bad-actor libraries.

### 7.14 External supervisor responsibility

Documented as: the supervisor is responsible for hard containment. AgentFlow's library layer provides:
- Bounded cooperative shutdown (RFC-009 / RFC-010 / RFC-011)
- Observability of unrecoverable state (RFC-013 §6.1)
- ERROR log signal (RFC-013 §6.2)

The supervisor is responsible for:
- SIGTERM the process
- Waiting a grace period (configurable per supervisor)
- SIGKILL escalation
- Restart policy

### 7.15 systemd Restart strategy

Documented example (in the RFC + `docs/deployment/systemd.md`):

```ini
[Service]
ExecStart=/usr/bin/python -m agentflow.myservice
Restart=on-failure
RestartSec=5
TimeoutStopSec=10          # grace period for SIGTERM
KillMode=mixed             # SIGTERM to main, SIGKILL to rest
KillSignal=SIGTERM
FinalKillSignal=SIGKILL    # ensure hard kill after TimeoutStopSec
SendSIGKILL=yes
```

### 7.16 Container liveness / readiness

Documented pattern (application layer, not shipped by library):

```yaml
livenessProbe:
  httpGet:
    path: /healthz
    port: 8080
  initialDelaySeconds: 30
  periodSeconds: 10
  timeoutSeconds: 3
  failureThreshold: 3        # 30s of failures → restart
terminationGracePeriodSeconds: 15   # SIGTERM grace before SIGKILL
```

The application's `/healthz` handler queries all Agent workers for `requires_process_restart is True` and returns 503 if any are True.

### 7.17 Shutdown grace period

Recommended default: **10 seconds** total grace from SIGTERM to SIGKILL. Rationale:
- RFC-010 / RFC-011 defaults are 5 s each for stop / startup helper — worst combined ~15s
- Typical `Agent.terminate` at defaults: dispatcher.stop 5s + worker.stop 5s = 10s
- ThreadWorker.stop default 5s + broker cleanup 5s = 10s

Deployment SHOULD set grace period ≥ Agent's `terminate()` bounded return + some slack (recommended 15s). If the grace period is shorter, the supervisor SIGKILLs mid-cleanup — accept the trade-off consciously.

### 7.18 Kill escalation responsibility

**Supervisor**. AgentFlow does not self-terminate the process. Every operational tool (systemd, containerd, kubernetes, supervisord) has native SIGTERM → SIGKILL escalation; use it.

### 7.19 Logging and metrics requirements

Log requirements (already enforced by §7.7):
- ERROR when `requires_process_restart` is True on `Agent.terminate` observation.
- WARNING when `worker.stop()` returns False but restart not required (recoverable STOP_TIMEOUT).

Metrics requirements: **out of scope for RFC-013**. Deferred to a future "AgentFlow observability" RFC that would introduce a common metrics surface (Prometheus, OpenTelemetry). Callers who want metrics today can wrap `Agent.terminate` or poll `worker.requires_process_restart` from their own metrics layer.

### 7.20 Backward compatibility

All new properties are additive. `Agent.terminate` signature preserved. Log-level change (WARNING → ERROR) is a diagnostic upgrade — callers that filter on log level may see one more ERROR line per unrecoverable STOP_TIMEOUT (rare; only when handlers wedge).

Existing tests that observe the WARNING log continue to see it for the recoverable case. Tests that trigger the wedge scenario (`test_F1` in `test_thread_worker_lifecycle.py`) may need refactor to accept EITHER WARNING or ERROR depending on `requires_process_restart` state at the time of the assertion.

### 7.21 Acceptance criteria

See §10.

### 7.22 Rollback plan

See §11.

---

## 8. Backward compatibility

### Source compatibility

| Symbol | Before | After | Compatibility |
|---|---|---|---|
| `ThreadWorker.thread_alive` | not defined | new read-only property | Additive |
| `ThreadWorker.thread_daemon` | not defined | new read-only property | Additive |
| `ThreadWorker.worker_thread_ident` | not defined | new read-only property | Additive |
| `ThreadWorker.requires_process_restart` | not defined | new read-only property | Additive |
| `ThreadWorker.start` | daemon=False (RFC-009 §7.13) | unchanged | Full |
| `ThreadWorker.stop` | bounded, returns bool | unchanged | Full |
| `WorkerState` enum | 8 members | unchanged | Full (no UNRECOVERABLE) |
| `Agent.terminate` | signature `def terminate(self)`; returns None; never raises | unchanged signature; ERROR log path added for restart-required case | Signature preserved. Log-level change is diagnostic; callers filtering on log level may see one more ERROR line per unrecoverable STOP_TIMEOUT. |
| `Agent.terminate` return type | None | None | Full |
| `Agent.terminate` never-raise contract | preserved | preserved | Full |
| ProcessWorker / dispatcher / broker / Parcel / wire | — | unchanged | Full |

### Behavioural compatibility

- Callers that never touch the new properties: zero change.
- Callers that read `worker.state`: see STOP_TIMEOUT with the same semantics (may recover on retry).
- Callers that grep logs for the WARNING message: continue to see it for the recoverable case; see a NEW ERROR line for the restart-required case.
- Log aggregators / alerting tools: gain a new ERROR signal to page on.
- Supervisors (systemd / container): no change — the process behaves the same, but the log signal is clearer.

### Wire compatibility

None. No wire changes.

---

## 9. Interaction with prior RFCs / test migration plan

### Prior RFCs

- **RFC-001 (R-02)**: unaffected.
- **RFC-002 (R-13 publish error propagation)**: unaffected.
- **RFC-003 (R-05 auto-reply)**: unaffected.
- **RFC-004 (R-04 bounded dispatch)**: unaffected.
- **RFC-005 (R-03 subscription recovery)**: unaffected.
- **RFC-006 / RFC-007 (R.4 / R-14)**: unaffected.
- **RFC-008 (R-06 ProcessWorker lifecycle)**: **complementary**. ProcessWorker retains SIGTERM/SIGKILL escalation; RFC-013 documents ProcessWorker as the recommended choice for high-risk tasks.
- **RFC-009 (R-10 ThreadWorker lifecycle)**: **directly follows RFC-009 §H**. That RFC documented R-10.5 as an accepted residual; RFC-013 accepts the same residual and adds observability + diagnostics + operator guidance. Daemon-flag decision (RFC-009 §7.13) preserved.
- **RFC-010 (R-10.4 broker bounded shutdown)**: unaffected. Stop helper stays daemon=True; that alone is not the R-10.5 blocker.
- **RFC-011 (R-10.6 broker bounded startup)**: unaffected. Startup helper stays daemon=True.
- **RFC-012 (R-10.7 publish result contract)**: unaffected.

### Test migration plan

Tests in `tests/unit/core/test_thread_worker_process_exit.py` (44 characterisation tests, 2026-08-03) partition as follows post-RFC-013:

| Test category | Post-RFC-013 action | Count |
|---|---|---|
| A (baseline) | **Keep** — Python interpreter behaviour unchanged | 8 |
| B (ThreadWorker normal) | **Keep** + add 4 new tests for the properties | 4+4 |
| C (wedged handler) | **Extend** — after asserting `state==STOP_TIMEOUT`, additionally assert `requires_process_restart is True`; after retry+release, assert `is False` | 4 |
| D (broker.start wedge, source) | **Keep** | 2 |
| E (broker.stop wedge, source) | **Keep** | 2 |
| F (ProcessWorker contrast) | **Keep** + add 1 test asserting ProcessWorker doesn't expose `requires_process_restart` (or exposes it as False always) | 4+1 |
| G (daemon experiment) | **Keep** — Option B rejection documented | 4 |
| H (supervisor) | **Extend** — new tests for ERROR log presence on restart-required case | 5+2 |
| I (existing API) | **Extend** — `test_I57_no_UNRECOVERABLE_state_yet_source_pin` becomes `test_I57_STOP_TIMEOUT_still_the_state_no_UNRECOVERABLE_added` (assert unchanged) | 10 |

**New tests to add** (~8):
- `test_thread_alive_property_reflects_work_thread_is_alive`
- `test_thread_daemon_property_reflects_work_thread_daemon_flag`
- `test_worker_thread_ident_property_matches_work_thread_ident`
- `test_requires_process_restart_False_when_never_started`
- `test_requires_process_restart_True_on_wedged_STOP_TIMEOUT`
- `test_requires_process_restart_False_after_recovery_via_retry_stop`
- `test_agent_terminate_logs_ERROR_when_requires_process_restart_True`
- `test_agent_terminate_logs_WARNING_when_stop_False_but_thread_died_naturally`

**Adjacent test suites**:
- `test_thread_worker_lifecycle.py`: `test_F1_agent_terminate_returns_bounded_when_broker_stop_wedges_and_logs_WARNING` needs refactor. Post-RFC-013 the wedged-broker case would log ERROR (restart required) instead of / in addition to WARNING. Either loosen the assertion to accept both, or split into two tests.
- All other suites: unchanged.

### Documentation deliverables

Beyond source and tests, RFC-013 ships:

- `docs/deployment/worker-type-selection.md` — new file
- `docs/deployment/systemd.md` — new file (Section §7.15 template + rationale)
- `docs/deployment/container-liveness.md` — new file (Section §7.16 template + rationale)
- `docs/audit/02-runtime-message-flow.md` — new §2.14 subsection on R-10.5 policy
- `docs/audit/05-risk-register.md` — R-10.5 status update (Open → Partially Resolved, with residual clarified as "cannot be fully resolved in-process; supervisor required")
- `docs/audit/06-test-coverage.md` — new suite composition row for R-10.5 tests

---

## 10. Acceptance criteria

Before RFC-013's implementation PR can merge:

1. `PYTHONPATH=src pytest tests/unit` reports **all-pass** with **exactly 2 strict xfailed** remaining.
   - Baseline before implementation: 569 passed, 2 xfailed.
   - Target after implementation: ~ 580 passed, 2 xfailed (~ 44 characterisation tests extended; ~ 8 new tests added; ~ 1 adjacent test refactored).
2. `ThreadWorker.thread_alive`, `ThreadWorker.thread_daemon`, `ThreadWorker.worker_thread_ident`, `ThreadWorker.requires_process_restart` all present as read-only properties with the semantics defined in §7.3-§7.6.
3. `ThreadWorker.start` source retains `daemon=False,       # RFC-009 §7.13` verbatim.
4. `WorkerState` enum has no `UNRECOVERABLE` member.
5. `thread_alive == work_thread.is_alive()` for any live worker; False when no thread exists.
6. `thread_daemon == work_thread.daemon` for any live worker; None when no thread exists.
7. `requires_process_restart is True` iff (STOP_TIMEOUT AND `thread_alive` AND `thread_daemon is False`).
8. After blocker release + retry stop reaches STOPPED, `requires_process_restart is False`.
9. When worker never started, `requires_process_restart is False`.
10. When worker ran normally to STOPPED, `requires_process_restart is False`.
11. `Agent.terminate` logs at ERROR (not WARNING) when `worker.stop() is False` AND `requires_process_restart is True`.
12. `Agent.terminate` logs at WARNING when `worker.stop() is False` but `requires_process_restart is False` (existing path).
13. `Agent.terminate` never raises regardless of restart-required state.
14. `Agent.terminate` never calls `os._exit`.
15. `ProcessWorker` still uses SIGTERM/SIGKILL escalation (RFC-008 §D unchanged).
16. Subprocess tests still demonstrate that a wedged ThreadWorker blocks interpreter exit (RFC-013 does not resolve R-10.5 in-process; it accepts the residual and adds observability).
17. Supervisor contract documented in `docs/deployment/`.
18. R-01 to R-10.7 tests all pass unchanged (~ 1 test refactored per §9 for the log-level change).
19. This RFC file has status changed from `Draft` to `Implemented` in the same PR.
20. `docs/audit/05-risk-register.md` R-10.5 status changed to `Partially Resolved (observability + diagnostics + operator guidance shipped; hard containment remains supervisor responsibility)` in the same PR.

### Out of scope (deferred to future RFCs)

- `WorkerState.UNRECOVERABLE` — deferred with rationale (§5 Option D).
- `daemon=True` opt-in kwarg — deferred to a future "high-risk-handler containment" RFC (§Appendix B).
- Auto-fallback to ProcessWorker — deferred (§5 Option G).
- Metrics API — deferred to a future observability RFC (§7.19).
- Kubernetes operator / helm chart — deployment-layer, not library.
- Cross-agent process supervision — separate architecture RFC.

---

## 11. Rollback plan

Rollback trigger — any of:

- Callers observe `requires_process_restart` semantics that are surprising in production (e.g. false positives during transient broker disconnects) — very unlikely because the property is computed from three deterministic conditions.
- The ERROR log level flood alerting tools with too-frequent pages — mitigation: log rate-limiter at application layer, not RFC rollback.
- A regression in R-01 to R-10.7 tests.

Rollback procedure — single `git revert` of the merge commit. Because:

- All new properties are additive; no caller in-tree depends on them (they're introduced by this RFC).
- `Agent.terminate` behavioural change is log-level only; reverting reverts to WARNING for both cases.
- No state-machine change; no schema change; no wire change.
- Documentation reverts alongside.

Not rollback-safe: any change bundled in the same PR that modifies `WorkerState` enum, `ThreadWorker.start`, `ProcessWorker`, or any broker code. This RFC forbids bundling.

Post-rollback state: R-10.5 returns to "Open / Documented" (its pre-RFC-013 status). Callers lose the observability property; ops tooling loses the ERROR log signal; deployment guide files removed.

Interim mitigation without revert:
- Callers can inspect `worker.state == WorkerState.STOP_TIMEOUT and worker.work_thread.is_alive()` directly — the property is a convenience wrapper, not a semantics change.
- Ops tooling can grep the existing WARNING log line pattern for `state=stop_timeout` to synthesize the same signal.

---

## 12. Implementation record (2026-08-03)

### 12.1 Shipped scope

**`src/agentflow/core/agent_worker.py`** — four additive read-only
properties on `ThreadWorker`:

| Property | Type | Contract |
|---|---|---|
| `thread_alive` | `bool` | `work_thread is not None and work_thread.is_alive()` |
| `thread_daemon` | `Optional[bool]` | `work_thread.daemon`; `None` when no thread |
| `worker_thread_ident` | `Optional[int]` | `work_thread.ident`; `None` when no thread, and `None` before `start()` per CPython's `Thread.ident` semantics |
| `requires_process_restart` | `bool` | conjunction of the three conditions in §12.2 |

All four are **computed on every read — no cache, no logging, no state
mutation**, and hold no lock: each snapshots `self.work_thread` into a
local, then calls `Thread.is_alive()` outside any lock section (RFC-009
§F lock hygiene preserved). They are therefore safe to poll from any
thread, including a health-probe thread.

**`src/agentflow/core/agent.py`** — `Agent.terminate` diagnostics on the
`worker.stop() is False` path (§12.4).

**Contracts explicitly unchanged**: `daemon=False,       # RFC-009 §7.13`
retained verbatim in `ThreadWorker.start` (source-pinned by
`test_M99`); `STOP_TIMEOUT` remains **retriable** — a subsequent
`stop()` may still reach `STOPPED`; `WorkerState` gains no
`UNRECOVERABLE` member (`test_M100`); `ThreadWorker.stop` signature and
`bool` return unchanged; ProcessWorker, broker lifecycle,
`MessageDispatcher`, and `Parcel` / wire untouched.

### 12.2 `requires_process_restart` — precise semantics

`True` **iff all three hold at the instant of the read**:

1. `state == WorkerState.STOP_TIMEOUT`
2. `thread_alive is True`
3. `thread_daemon is False`

**What `True` means**: right now there is a live non-daemon worker
thread that missed its stop deadline. Because it is non-daemon, the
interpreter will not exit while it runs, so **guaranteeing process exit
requires external containment (supervisor `SIGKILL`) or a process
restart**.

**What `True` does NOT mean**: that the thread is permanently
unrecoverable. The blocking call may still return on its own and a
retried `stop()` may still succeed. The property asserts a present
condition, not a prognosis.

### 12.3 Recovery — the property is self-clearing

It is present-tense, not a latch. No reset call exists or is needed:

- **Blocker releases naturally, thread exits, no retry issued** →
  `thread_alive` becomes `False` → property `False`, **even though
  `state` may still read `STOP_TIMEOUT`**. State records history; the
  property reports now. Verified by `test_J75` / `test_K88`.
- **Blocker releases and `stop()` is retried** → `state` becomes
  `STOPPED` → property `False`. Verified by `test_J76` / `test_K89`.

### 12.4 `Agent.terminate` diagnostics

When `worker.stop()` returns `False`, terminate reads worker type,
`state`, and — capability-based via `getattr(worker, name, default)` —
`thread_alive`, `thread_daemon`, `worker_thread_ident`, and
`requires_process_restart`. A worker not exposing them (ProcessWorker,
`FakeWorker`) yields `False` and takes the pre-existing path.

- `requires_process_restart is True` → **`ERROR`**, containing worker
  type, state, thread ident, alive, daemon, the phrase *"external
  process restart or supervisor containment required"*, and an RFC-013
  reference. **At most one such ERROR per `terminate()` invocation** —
  the branch runs at most once; there is no cache and no
  deduplication across separate calls, and no background re-alerting.
- `requires_process_restart is False` → the pre-existing **`WARNING`**
  path, unchanged.

Signature and return type unchanged; the **never-raise contract is
preserved** — the diagnostic reads are themselves wrapped so a worker
property that raises cannot break it (`test_L96`), and the branch
contains no `raise` statement (`test_L98`, AST-pinned). The library
**does not call `os._exit`** anywhere in `src/agentflow/`
(`test_L97` / `test_M101`), and uses no ctypes async thread
cancellation (`test_M102`).

### 12.5 Runtime evidence (subprocess characterisation, retained)

The 44 characterisation cases were retained and extended rather than
replaced; the suite now stands at **73 tests**. They continue to
demonstrate, by real subprocess exit-code and timeout observation:

- A wedged **non-daemon** ThreadWorker thread **prevents interpreter
  exit** — the process must be killed by the parent (`test_A2`,
  `test_C14`, `test_C15_C19`, `test_C20`, `test_J74`).
- **`sys.exit()` does not bypass** the non-daemon join at shutdown
  (`test_A4`).
- A **daemon** thread lets the process exit, but its `finally` block
  may be **truncated** — the marker write can be missed (`test_A3`,
  `test_G46`, `test_G47`). This is the concrete reason Option B stays
  rejected.
- **`os._exit` skips `atexit`** and cleanup entirely (`test_A5`,
  `test_A7`) — why the library never calls it.
- **ProcessWorker provides bounded terminate/kill escalation** and an
  observable exit code (`test_F41`, `test_F43`, `test_F44`), which
  ThreadWorker structurally cannot (`test_F45`).
- **Final hard containment is the external supervisor's
  responsibility** — the parent process reclaims the child by
  `terminate()` then `kill()` (`test_H54`, `test_H55`).

### 12.6 Test results

| Suite | Result |
|---|---|
| `tests/unit/core/test_thread_worker_process_exit.py` | **73 passed** |
| `tests/unit/core/test_thread_worker_lifecycle.py` + `test_process_worker_lifecycle.py` | **68 passed** (no refactor needed — the §7.20 / §9 concern that `test_F1` might need to accept ERROR-or-WARNING did **not** materialise) |
| `tests/unit/test_mqtt_broker_shutdown.py` + `test_mqtt_broker_startup_bounded.py` | **127 passed** |
| **Complete `tests/unit`** | **598 passed, 2 xfailed** — clean run, zero regressions across RFC-001 … RFC-012 |

The 2 pre-existing strict xfails (RFC-006 multi-caller `topic_wait`;
multi-handler-per-topic fan-out) are **retained unchanged**, per §10
criterion 1.

> **Pre-existing flaky test — outside RFC-013's scope, fixed separately.**
> `tests/unit/core/test_agent_publish_sync_concurrency.py::test_atomic_ownership_under_concurrent_race_stress`
> was observed failing intermittently during this work (~7% per run,
> `completed == 2`). It was reproduced **failing at clean `HEAD` in a
> separate worktree with none of the RFC-013 changes applied**,
> confirming it is pre-existing and unrelated to this RFC. **RFC-013
> makes no claim to fix it, and no RFC-013 change touched it.**
>
> It was subsequently root-caused and fixed as a standalone
> **R-14 / RFC-006 test-quality** change on 2026-08-03: the test's
> barrier synchronised only *entry* to `publish_sync`, so a
> late-scheduled caller could acquire ownership after the owner's
> `finally` released it — sequential ownership transfer, which is
> correct behaviour, not a defect in RFC-006's guard. The test now
> enforces the overlap it intends to observe. **No production code
> changed.** See `docs/audit/06-test-coverage.md` for the full analysis.

### 12.7 Acceptance criteria (§10) outcome

Criteria 2–19 met as recorded above. Criterion 1's *count* differs from
the drafted estimate — the draft predicted "~580 passed" from a
"569 passed" baseline; the actual clean result is **598 passed,
2 xfailed**, because the process-exit suite landed 73 tests rather than
the estimated ~52. The substantive requirement (all-pass, exactly 2
strict xfails retained) is met. Criterion 20 is satisfied by the
R-10.5 update recorded in `docs/audit/05-risk-register.md`, with the
overall label **Partially Resolved / Operationally Mitigated** (§12.8).

### 12.8 R-10.5 final status

RFC-013 **does not resolve R-10.5 in-process**, by design:

| Dimension | Status |
|---|---|
| **Detection** | **Resolved** — `requires_process_restart` + ERROR diagnostics make the condition observable and machine-checkable |
| **In-process forced thread termination** | **Not supported by design** — Options B / F / J rejected (§5); no safe CPython mechanism exists |
| **Operational containment** | **Supported** — via ProcessWorker for high-risk handlers, or an external supervisor for hard containment |
| **Overall** | **Partially Resolved / Operationally Mitigated** — deliberately **not** marked fully Resolved |

---

## Appendix A — Why `requires_process_restart` is a property, not a state

Making it a state (Option D) requires transition rules:
- When does STOP_TIMEOUT → UNRECOVERABLE fire? Immediately? After N failed retries? On a wall-clock deadline?
- What state does a successful retry transition FROM? UNRECOVERABLE → STOPPED, or through some intermediate state?
- Who observes UNRECOVERABLE? Callers of `.state`? Callers of `.requires_process_restart`?

A property computed on the fly avoids all these questions. It reflects the current truth at read time — if the blocker released between the last STOP_TIMEOUT and now, the property becomes False without any state transition. Retries then work naturally.

If a future RFC needs a distinct state (e.g. because a metric like "time-in-STOP_TIMEOUT" is desired), it can add UNRECOVERABLE and derive it from the property + a wall-clock predicate. The additive shape here does not preclude that path.

## Appendix B — Why `daemon=True` opt-in is deferred, not rejected outright

Option B (default `daemon=True`) is **rejected** in this RFC because the default choice matters: silently truncating `__deactivating` cleanup is a data-loss risk. Option C (opt-in `daemon` kwarg) is **deferred** rather than rejected because:

- A caller who *knows* their handler is short-lived and doesn't own broker resources might legitimately want daemon behaviour.
- The same caller could achieve equivalent isolation by running the Agent in ProcessWorker mode — RFC-013 recommends this (§7.11).
- Adding a kwarg without a validated use case is over-engineering.

If a future deployment demonstrates a genuine need (e.g. a light-weight polling Agent that must not block interpreter exit), a follow-up RFC can revisit Option C with concrete criteria and test-coverage requirements.

## Appendix C — Why we do NOT call `os._exit` from library code

`os._exit(N)`:
- Bypasses `atexit` handlers (`test_A7` — no atexit runs).
- Bypasses `finally` blocks in unwinding stacks.
- Bypasses `sys.excepthook`.
- Bypasses Python's own shutdown cleanup (module `__del__`, weakref finalizers).
- Bypasses buffered stdout / stderr flush (log messages may be lost).
- Kills all threads in the process — including cotenant threads that had nothing to do with the wedged worker.

A library that calls `os._exit` on behalf of the application:
- Cannot know which of the above the application depends on.
- Removes the application's ability to observe / log / persist state before exit.
- Makes the library untrustworthy for embedding in larger applications.

`os._exit` is appropriate ONLY at the top of the application's own `main()` (usually via a supervisor tool, or as a last-resort abort by the application after logging). RFC-013 §7.9 makes this policy explicit.

## Appendix D — Why we do NOT use ctypes for thread cancellation

`ctypes.pythonapi.PyThreadState_SetAsyncExc(thread_id, SystemExit)`:
- Raises SystemExit in the target thread at the next Python bytecode boundary.
- Does NOT interrupt C extension calls (blocking `read()`, `send()`, `sleep()` in native code).
- Does NOT interrupt syscalls (kernel-side wait).
- Can leave locks held by the target thread — the interrupted code path may have acquired a mutex mid-flight.
- Documented as unsafe for external use in CPython source comments.
- PyPy behaviour is undefined.

For the R-10.5 scenarios that matter — wedged native SDK calls, kernel-side I/O — ctypes cancellation does not help. And for the scenarios where it might help (pure-Python busy loops), the Agent handler could have been made cooperative. ProcessWorker is the safe answer for the former; discipline is the answer for the latter.

## Appendix E — Deployment templates

> **These are illustrative templates and an integration contract — not
> shipped code.** AgentFlow implements **no health endpoint, no HTTP
> server, and no metrics exporter**. The `/healthz` handler below shows
> the *shape* of a check you write in your own application on top of
> the properties RFC-013 does expose; wiring it up, choosing a web
> framework, and deciding whether a stuck agent should recycle the
> whole process are all application-layer decisions. Grace periods and
> kill escalation are the responsibility of systemd / the container
> runtime / supervisord — not of this library.
>
> Expanded operator guidance lives in
> [`docs/deployment/worker-type-selection.md`](../deployment/worker-type-selection.md),
> [`docs/deployment/systemd.md`](../deployment/systemd.md), and
> [`docs/deployment/container-liveness.md`](../deployment/container-liveness.md).

### E.1 systemd unit template

```ini
[Unit]
Description=AgentFlow Agent Runner (%i)
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=agentflow
Group=agentflow
WorkingDirectory=/opt/agentflow
ExecStart=/opt/agentflow/venv/bin/python -m my_agentflow_service --instance=%i
Restart=on-failure
RestartSec=5

# RFC-013 §7.15/§7.17 — grace period + kill escalation
TimeoutStopSec=15
KillMode=mixed
KillSignal=SIGTERM
SendSIGKILL=yes
FinalKillSignal=SIGKILL

# Log AgentFlow's ERROR-level restart-required signals via journald
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=multi-user.target
```

### E.2 Container liveness / readiness

Application layer wires the `/healthz` endpoint (framework of your choice):

```python
# Simplified sketch — not part of RFC-013 shipped code.
from typing import List
def healthz(agents: List[Agent]) -> tuple[int, str]:
    """Return (status, message). 200 = OK, 503 = needs restart."""
    unrecoverable = [
        a for a in agents
        if a._agent_worker is not None
        and getattr(a._agent_worker, 'requires_process_restart', False)
    ]
    if unrecoverable:
        names = ", ".join(a.name for a in unrecoverable)
        return 503, f"agents require process restart: {names}"
    return 200, "ok"
```

Kubernetes deployment:

```yaml
spec:
  terminationGracePeriodSeconds: 15
  containers:
    - name: agentflow
      livenessProbe:
        httpGet:
          path: /healthz
          port: 8080
        initialDelaySeconds: 30
        periodSeconds: 10
        timeoutSeconds: 3
        failureThreshold: 3
      lifecycle:
        preStop:
          exec:
            # Graceful signal; container runtime SIGKILLs after grace period.
            command: ["/bin/kill", "-SIGTERM", "1"]
```

### E.3 supervisord

```ini
[program:agentflow]
command=/opt/agentflow/venv/bin/python -m my_agentflow_service
autostart=true
autorestart=true
startretries=3
stopwaitsecs=15                ; RFC-013 §7.17 grace period
stopsignal=TERM
killasgroup=true               ; ensure PID group is killed on stopwait expiry
stopasgroup=true
```
