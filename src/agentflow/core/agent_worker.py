import multiprocessing
import os
import queue
import threading
from enum import Enum
from typing import Optional

import logging
logger = logging.getLogger(os.getenv('LOGGER_NAME'))



class WorkerState(Enum):
    """Explicit worker lifecycle state.

    RFC-008 introduced this enum for ProcessWorker.
    RFC-009 reuses it for ThreadWorker and adds two members
    (`STOP_TIMEOUT`, `FAILED`) that only apply to the thread-mode
    side. ProcessWorker never enters STOP_TIMEOUT (it can escalate
    to SIGKILL) and never enters FAILED (the child's exit code is
    the failure signal instead).

    NEW           — freshly constructed; start() may be called.
    STARTING      — start() has begun the spawn / thread creation.
    RUNNING       — worker (child process / worker thread) is alive.
    STOPPING      — stop() is executing shutdown for the first caller.
    STOPPED       — stop() finished cleanly; result cached. Terminal
                    unless retry from STOP_TIMEOUT re-enters STOPPING.
    START_FAILED  — start() raised; resources cleaned up. Terminal.
    STOP_TIMEOUT  — (RFC-009, ThreadWorker only) cooperative stop
                    timed out; the thread is still alive. Retriable
                    via a subsequent stop() call.
    FAILED        — (RFC-009, ThreadWorker only) _activate raised an
                    Exception (not BaseException) that was captured
                    into `last_exception`; the thread ended.
    """
    NEW = 'new'
    STARTING = 'starting'
    RUNNING = 'running'
    STOPPING = 'stopping'
    STOPPED = 'stopped'
    START_FAILED = 'start_failed'
    STOP_TIMEOUT = 'stop_timeout'
    FAILED = 'failed'



class Worker:
    def __init__(self, initiator_agent):
        if not multiprocessing.get_start_method(allow_none=True):
            multiprocessing.set_start_method('spawn')
        self.initiator_agent = initiator_agent
        self.work_process = None
        self.work_thread = None


    def create_event(self):
        return None


    def is_working(self):
        if self.work_process:
            return self.work_process.is_alive()
        elif self.work_thread:
            return self.work_thread.is_alive()
        else:
            return False


    def send_data(self, data):
        pass


    def start(self):
        pass


    def stop(self):
        pass



# Concrete strategy for using processes
class ProcessWorker(Worker):
    """RFC-008-compliant ProcessWorker.

    Lifecycle: NEW → STARTING → RUNNING → STOPPING → STOPPED (or
    START_FAILED on start error). Restart is not supported; construct
    a fresh worker.

    Parent-side Agent contract (RFC-008 §F): under process mode, the
    parent-side Agent instance is a lifecycle controller. Its own
    publish/subscribe/publish_sync calls are NOT proxied to the
    child. To interact with the running Agent from another process,
    construct a separate Agent that connects to the same broker.
    """

    def __init__(self, initiator_agent):
        super().__init__(initiator_agent)
        # RFC-008 state machine.
        self._state_lock = threading.RLock()
        self._state = WorkerState.NEW
        self._stop_complete_event = threading.Event()
        self._exitcode: Optional[int] = None
        self.work_queue = None


    def create_event(self):
        return multiprocessing.Event()


    @property
    def state(self) -> WorkerState:
        with self._state_lock:
            return self._state


    @property
    def exitcode(self) -> Optional[int]:
        return self._exitcode


    def start(self):
        # RFC-008 §B: single-shot state machine transitions.
        with self._state_lock:
            current = self._state
            if current in (WorkerState.STARTING, WorkerState.RUNNING):
                raise RuntimeError(
                    f"ProcessWorker.start called while state={current.value}"
                )
            if current in (WorkerState.STOPPING, WorkerState.STOPPED,
                           WorkerState.START_FAILED):
                raise RuntimeError(
                    f"ProcessWorker cannot be restarted "
                    f"(state={current.value}); construct a fresh worker"
                )
            # current == NEW
            self._state = WorkerState.STARTING

        try:
            # RFC-008 §C: do not mutate agent.config. Ship a shallow
            # copy that owns the work_queue reference.
            self.work_queue = multiprocessing.Queue()
            child_config = dict(self.initiator_agent.config)
            child_config['work_queue'] = self.work_queue

            self.work_process = multiprocessing.Process(
                target=self.initiator_agent._activate,
                args=(child_config,),
                daemon=False,   # RFC-008 §7.15
            )
            self.work_process.start()
        except BaseException:
            self._cleanup_after_start_failure()
            with self._state_lock:
                self._state = WorkerState.START_FAILED
            raise

        with self._state_lock:
            self._state = WorkerState.RUNNING
        try:
            logger.info(
                f"{self.initiator_agent.M('ProcessWorker started')}: "
                f"pid={self.work_process.pid}"
            )
        except Exception:
            pass
        return self.work_process


    def _cleanup_after_start_failure(self):
        """RFC-008 §D: bounded rollback on start failure.

        - If a child was launched despite the error, terminate+kill it.
        - Close and drop the work_queue.
        - Do NOT touch agent.config (RFC-008 §C: agent.config was not
          mutated by this design).
        """
        proc = self.work_process
        self.work_process = None
        if proc is not None:
            try:
                if proc.is_alive():
                    try:
                        proc.terminate()
                    except (ProcessLookupError, ValueError, OSError):
                        pass
                    try:
                        proc.join(1.0)
                    except Exception:
                        pass
                if proc.is_alive():
                    try:
                        proc.kill()
                    except (ProcessLookupError, ValueError, OSError):
                        pass
                    try:
                        proc.join(1.0)
                    except Exception:
                        pass
            except Exception:
                pass

        if self.work_queue is not None:
            try:
                self.work_queue.close()
            except Exception:
                pass
            try:
                self.work_queue.join_thread()
            except Exception:
                pass
            self.work_queue = None


    def send_data(self, data):
        if self.work_queue is None:
            return
        self.work_queue.put(data)


    def stop(self,
             graceful_timeout_s: float = 5.0,
             terminate_timeout_s: float = 2.0,
             kill_timeout_s: float = 1.0) -> Optional[int]:
        """RFC-008 §D: bounded shutdown escalation ladder.

        Total wall time bounded by:
          graceful_timeout_s + terminate_timeout_s + kill_timeout_s

        Idempotent: repeated calls return the cached first-call
        exitcode. Concurrent calls: only the first executes the
        escalation; others wait on _stop_complete_event and return
        the same exitcode.
        """
        # State-machine transitions.
        with self._state_lock:
            current = self._state
            if current == WorkerState.NEW:
                # RFC-008 §7.12: stop-before-start is a no-op; state
                # stays NEW so a subsequent start() is still allowed.
                return None
            if current == WorkerState.START_FAILED:
                # Nothing to stop; resources already cleaned.
                return None
            if current == WorkerState.STOPPED:
                # Idempotent replay.
                return self._exitcode
            if current == WorkerState.STOPPING:
                # Concurrent caller — wait for the first to finish.
                already_stopping = True
            elif current == WorkerState.STARTING:
                raise RuntimeError(
                    "ProcessWorker.stop called while start is in progress"
                )
            else:  # RUNNING
                already_stopping = False
                self._state = WorkerState.STOPPING

        if already_stopping:
            self._stop_complete_event.wait()
            return self._exitcode

        # First (and only) caller: execute the escalation.
        try:
            proc = self.work_process
            if proc is None:
                # Defensive: RUNNING implies process exists.
                self._exitcode = None
            else:
                # Step 1: cooperative terminate via queue.
                try:
                    self.send_data('terminate')
                except Exception:
                    pass
                # Step 2: graceful join.
                try:
                    proc.join(graceful_timeout_s)
                except Exception:
                    pass
                # Step 3-4: SIGTERM if still alive.
                if proc.is_alive():
                    try:
                        logger.warning(
                            self.initiator_agent.M(
                                "ProcessWorker: graceful stop deadline "
                                "exceeded; issuing terminate()"
                            )
                        )
                    except Exception:
                        pass
                    try:
                        proc.terminate()
                    except (ProcessLookupError, ValueError, OSError):
                        pass
                    try:
                        proc.join(terminate_timeout_s)
                    except Exception:
                        pass
                # Step 5-6: SIGKILL if still alive.
                if proc.is_alive():
                    try:
                        logger.warning(
                            self.initiator_agent.M(
                                "ProcessWorker: terminate deadline "
                                "exceeded; issuing kill()"
                            )
                        )
                    except Exception:
                        pass
                    try:
                        proc.kill()
                    except (ProcessLookupError, ValueError, OSError):
                        pass
                    try:
                        proc.join(kill_timeout_s)
                    except Exception:
                        pass
                if proc.is_alive():
                    try:
                        logger.error(
                            self.initiator_agent.M(
                                "ProcessWorker: child still alive after "
                                "kill(); abandoning (potential zombie)"
                            )
                        )
                    except Exception:
                        pass
                self._exitcode = proc.exitcode

            # Cleanup queue — bounded (join_thread is bounded by the
            # queue's own drain; if the feeder is stuck we still exit
            # via best-effort exception swallowing).
            if self.work_queue is not None:
                try:
                    self.work_queue.close()
                except Exception:
                    pass
                try:
                    self.work_queue.join_thread()
                except Exception:
                    pass
        finally:
            # ALWAYS mark stopped and release concurrent waiters —
            # even if an exception escaped the escalation body — so
            # other callers do not hang on _stop_complete_event.
            with self._state_lock:
                self._state = WorkerState.STOPPED
            self._stop_complete_event.set()

        try:
            logger.info(
                self.initiator_agent.M(
                    f"ProcessWorker stopped: exitcode={self._exitcode}"
                )
            )
        except Exception:
            pass
        return self._exitcode



# Concrete strategy for using threads
class ThreadWorker(Worker):
    """RFC-009 bounded cooperative ThreadWorker.

    Lifecycle: NEW → STARTING → RUNNING → STOPPING → STOPPED
    (or → STOP_TIMEOUT if the worker thread survives the deadline;
    or → FAILED if _activate raised an Exception).

    Shared-instance model (RFC-009 §7.15): ThreadWorker deliberately
    SHARES `initiator_agent.config` with the caller and writes
    `config['work_queue']` in place. This is the behaviour the
    thread-mode Agent API depends on for parent-side send_data /
    publish / subscribe. Contrast RFC-008 ProcessWorker which builds
    a copy.

    Cancellation policy (RFC-009 §7.10, §5 Option E rejected):
    Python threads cannot be safely cancelled from outside. `stop()`
    is cooperative — it posts 'terminate' on the work queue and
    waits `graceful_timeout_s` for the thread to exit. If the
    thread is wedged, stop() returns False and state → STOP_TIMEOUT;
    the thread reference is retained so retries and `is_working()`
    still reflect the truth. This RFC does NOT use ctypes async
    exception injection or any other forced-kill mechanism.

    Daemon / interpreter-exit caveat (RFC-009 §7.13, §H):
    `work_thread.daemon = False`. `stop()` returning bounded ONLY
    guarantees that `Agent.terminate()` itself returns; if the
    thread is left in STOP_TIMEOUT it stays alive and, because it
    is non-daemon, Python interpreter shutdown will still block on
    it. This RFC does NOT claim to fully resolve the orphan / non-
    daemon exit risk.
    """

    def __init__(self, initiator_agent):
        super().__init__(initiator_agent)
        # RFC-009 state machine. RLock so a stop() called from a
        # handler (which is dispatched from _on_message → dispatcher
        # → the worker thread's own tree) does not deadlock on itself.
        self._state_lock = threading.RLock()
        self._state = WorkerState.NEW
        # Set by whichever caller finishes an escalation attempt.
        # Cleared by the first caller of each new attempt so the
        # STOP_TIMEOUT retry path is race-free.
        self._stop_complete_event = threading.Event()
        # Cached first-attempt outcome; read by concurrent waiters.
        self._last_stop_result: bool = True
        # Uncaught Exception (never BaseException — RFC-009 §7.11)
        # captured by the _run_target wrapper.
        self._last_exception: Optional[BaseException] = None
        self.work_queue: Optional[queue.Queue] = None


    def create_event(self):
        return threading.Event()


    # ------------------------------------------------------------------
    # Observability
    # ------------------------------------------------------------------

    @property
    def state(self) -> WorkerState:
        with self._state_lock:
            return self._state


    @property
    def last_exception(self) -> Optional[BaseException]:
        """RFC-009 §7.12. Last uncaught Exception captured from
        `initiator_agent._activate`, or None. Never cleared."""
        return self._last_exception


    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def start(self):
        """RFC-009 §B: single-shot state machine.

        Repeated start() from any state other than NEW raises
        RuntimeError. Callers who need a fresh lifecycle must
        construct a fresh ThreadWorker (parity with RFC-008).
        """
        with self._state_lock:
            current = self._state
            if current != WorkerState.NEW:
                raise RuntimeError(
                    f"ThreadWorker.start called while state={current.value}; "
                    f"restart is not supported — construct a fresh worker"
                )
            self._state = WorkerState.STARTING

        try:
            # RFC-009 §7.15: DELIBERATE in-place mutation of the shared
            # config dict. Thread mode's whole point is shared object
            # identity between the caller and the worker thread; the
            # caller's `send_data` and the worker thread's activate
            # loop must reach the SAME `work_queue` reference. This
            # diverges from RFC-008 ProcessWorker (which builds a
            # copy) on purpose.
            self.work_queue = queue.Queue()
            cfg = self.initiator_agent.config
            cfg['work_queue'] = self.work_queue

            thread = threading.Thread(
                target=self._run_target,
                args=(cfg,),
                name=f'ThreadWorker-{self.initiator_agent.M()}',
                daemon=False,       # RFC-009 §7.13
            )
            self.work_thread = thread
            # Transition STARTING → RUNNING BEFORE `thread.start()` so
            # that `_run_target` (which may run immediately after
            # thread.start() returns) never observes STARTING. If
            # thread.start() itself raises, we roll back to
            # START_FAILED below.
            with self._state_lock:
                self._state = WorkerState.RUNNING
            thread.start()
        except BaseException:
            # Thread.start() itself failed (e.g. OS thread limit).
            # RFC-009 §B: state=START_FAILED, drop the reference, and
            # re-raise the original exception unchanged.
            with self._state_lock:
                self._state = WorkerState.START_FAILED
            self.work_thread = None
            raise

        try:
            logger.info(self.initiator_agent.M(
                f"ThreadWorker started: thread={self.work_thread!r}"
            ))
        except Exception:
            pass
        return self.work_thread


    def _run_target(self, cfg):
        """Wraps `initiator_agent._activate` per RFC-009 §B.

        Catches `Exception` only — NOT `BaseException`. KeyboardInterrupt,
        SystemExit and GeneratorExit propagate as usual, and in that
        case the thread dies without updating our state (stop() will
        observe `thread.is_alive() == False` with `last_exception is
        None` and mark STOPPED — a limitation documented in RFC-009
        §7.11 / §H).
        """
        try:
            self.initiator_agent._activate(cfg)
        except Exception as ex:
            # RFC-009 §7.11 / §7.12
            self._last_exception = ex
            logger.exception(self.initiator_agent.M(
                f"ThreadWorker: _activate raised; captured as last_exception: {ex!r}"
            ))
            with self._state_lock:
                self._state = WorkerState.FAILED
            return
        # Normal self-exit path. Only update RUNNING → STOPPED.
        # If state was already claimed by stop() (STOPPING /
        # STOP_TIMEOUT) or by the FAILED except branch, leave it —
        # in particular we must not silently upgrade STOP_TIMEOUT to
        # STOPPED behind the caller's back if _activate happened to
        # return just after stop() gave up. (start() sets RUNNING
        # BEFORE thread.start() to close the STARTING window.)
        with self._state_lock:
            if self._state is WorkerState.RUNNING:
                self._state = WorkerState.STOPPED


    def send_data(self, data):
        logger.debug(self.initiator_agent.M(f"data: {data}"))
        if self.work_queue is None:
            return
        self.work_queue.put(data)


    def stop(self, graceful_timeout_s: float = 5.0) -> bool:
        """RFC-009 §C: bounded cooperative shutdown.

        Returns True when the worker thread has exited (or was never
        started, or was already stopped, or ran to FAILED). Returns
        False when the thread survived `graceful_timeout_s` — caller
        observes STOP_TIMEOUT via `self.state` and may retry.

        Idempotent: repeated calls after STOPPED return the cached
        True. Concurrent callers wait on `_stop_complete_event` with
        a bounded timeout and return the same cached result.

        Bounded return of stop() ONLY guarantees Agent.terminate()
        returns. If state ends at STOP_TIMEOUT and the thread is
        non-daemon (RFC-009 §7.13), Python interpreter shutdown may
        still block on this thread. See RFC-009 §H.
        """
        is_waiter = False
        # -- Short state-lock section (no I/O, no join, no send_data).
        with self._state_lock:
            current = self._state
            if current == WorkerState.NEW:
                # RFC-009 §7.6 stop-before-start is a no-op; state
                # stays NEW so a subsequent start() is still allowed.
                return True
            if current in (WorkerState.STOPPED, WorkerState.START_FAILED):
                return True
            if current == WorkerState.FAILED:
                # Thread already ended via captured Exception. If it
                # somehow is still alive (should not happen — FAILED
                # is only set from inside the wrapper after activate
                # returned via except), fall through to the retry
                # path so we still bound the join.
                if self.work_thread is None or not self.work_thread.is_alive():
                    return True
                self._state = WorkerState.STOPPING
                self._stop_complete_event.clear()
            elif current == WorkerState.STARTING:
                raise RuntimeError(
                    "ThreadWorker.stop called while start is in progress"
                )
            elif current == WorkerState.STOPPING:
                is_waiter = True
            elif current in (WorkerState.RUNNING, WorkerState.STOP_TIMEOUT):
                # First caller for this attempt. RFC-009 §D allows a
                # STOP_TIMEOUT → STOPPING retry with a fresh budget.
                self._state = WorkerState.STOPPING
                self._stop_complete_event.clear()
            else:  # pragma: no cover — defensive
                return True

        # -- Concurrent waiter path (bounded, RFC-009 §E modification).
        if is_waiter:
            coordination_margin_s = 0.1
            completed = self._stop_complete_event.wait(
                graceful_timeout_s + coordination_margin_s
            )
            if completed:
                with self._state_lock:
                    return self._last_stop_result
            # Event did not fire in time. Do NOT wait forever; report
            # what we can observe about the thread and log a warning.
            alive = (self.work_thread is not None
                     and self.work_thread.is_alive())
            try:
                logger.warning(self.initiator_agent.M(
                    f"ThreadWorker.stop coordination wait timed out "
                    f"({graceful_timeout_s + coordination_margin_s:.1f}s); "
                    f"thread alive={alive}"
                ))
            except Exception:
                pass
            return not alive

        # -- First-caller path.
        try:
            # Best-effort cooperative signal. Any exception here means
            # the queue is unusable; we still try to join.
            try:
                self.send_data('terminate')
            except Exception:
                pass
            try:
                if self.work_thread is not None:
                    self.work_thread.join(graceful_timeout_s)
            except Exception:
                pass

            alive = (self.work_thread is not None
                     and self.work_thread.is_alive())
            with self._state_lock:
                if alive:
                    # RFC-009 §C: timeout branch.
                    self._state = WorkerState.STOP_TIMEOUT
                    self._last_stop_result = False
                elif self._last_exception is not None:
                    # RFC-009 §C: thread exited via captured Exception.
                    self._state = WorkerState.FAILED
                    self._last_stop_result = True
                else:
                    # RFC-009 §C: clean cooperative exit.
                    self._state = WorkerState.STOPPED
                    self._last_stop_result = True
        finally:
            # ALWAYS release concurrent waiters, even if the escalation
            # body raised, so the coordination path never hangs.
            self._stop_complete_event.set()

        # -- Post-lock logging (never inside _state_lock).
        try:
            if self._last_stop_result:
                if self._state == WorkerState.FAILED:
                    logger.info(self.initiator_agent.M(
                        f"ThreadWorker stopped after _activate exception: "
                        f"{self._last_exception!r}"
                    ))
                else:
                    logger.info(self.initiator_agent.M(
                        "ThreadWorker stopped"
                    ))
            else:
                logger.warning(self.initiator_agent.M(
                    f"ThreadWorker.stop timeout after {graceful_timeout_s:.1f}s; "
                    f"thread={self.work_thread!r} still alive. "
                    f"Retry stop() to try again. Note: because daemon=False, "
                    f"interpreter shutdown may still block on this thread."
                ))
        except Exception:
            pass
        return self._last_stop_result
