import multiprocessing
import os
import queue
import threading
from enum import Enum
from typing import Optional

import logging
logger = logging.getLogger(os.getenv('LOGGER_NAME'))



class WorkerState(Enum):
    """RFC-008 §B: explicit lifecycle state for ProcessWorker.

    NEW           — freshly constructed; start() may be called.
    STARTING      — start() has begun the spawn.
    RUNNING       — child process is alive.
    STOPPING      — stop() is executing the escalation ladder.
    STOPPED       — stop() finished; exitcode cached. Terminal.
    START_FAILED  — start() raised; resources cleaned up. Terminal.
    """
    NEW = 'new'
    STARTING = 'starting'
    RUNNING = 'running'
    STOPPING = 'stopping'
    STOPPED = 'stopped'
    START_FAILED = 'start_failed'



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
    def __init__(self, initiator_agent):
        super().__init__(initiator_agent)


    def create_event(self):
        return threading.Event()


    def start(self):
        logger.debug("Thread worker")
        self.work_queue = queue.Queue()

        cfg = self.initiator_agent.config
        cfg['work_queue'] = self.work_queue
        self.work_thread = threading.Thread(target=self.initiator_agent._activate, args=(cfg,))
        self.work_thread.start()

        return self.work_thread


    def send_data(self, data):
        logger.debug(self.initiator_agent.M(f"data: {data}"))
        self.work_queue.put(data)


    def stop(self):
        logger.debug(self.initiator_agent.M("Stopping.."))
        self.send_data('terminate')
        self.work_thread.join()  # Wait for the process to finish
        logger.debug(self.initiator_agent.M("Stopped."))
