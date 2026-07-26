"""Bounded message dispatcher (RFC-004, first-phase).

Implements only what RFC-004 §6 marks as first-phase:

  - Bounded queue (queue.Queue with maxsize=queue_capacity)
  - Fixed number of daemon consumer threads
  - drop_newest overflow policy (never raises to caller)
  - Graceful shutdown with a bounded deadline
  - Idempotent stop; post-stop enqueue rejected with metrics
  - Thread-safe metrics + snapshot API
  - Legacy per-message-thread compatibility shim (deprecated)

Deliberately NOT implemented in this phase (see RFC-004):
  drop_oldest, block, and raise overflow policies; adaptive worker
  count; per-topic serial queue; asyncio; ProcessWorker integration;
  reconnect; Parcel schema changes.
"""

import logging
import os
import queue
import threading
import time
from typing import Any, Callable, Dict, List, Optional


logger = logging.getLogger(os.getenv('LOGGER_NAME'))


class MessageDispatcher:
    """Bounded queue + fixed consumer threads.

    Public surface used by Agent:
      - enqueue(task, topic=...) -> bool   (never raises to caller)
      - stop(timeout_s=None) -> bool       (idempotent)
      - metrics_snapshot() -> dict         (consistent snapshot)
      - active_workers / queue_depth / dropped_message_count /
        rejected_after_stop_count / processed_count / error_count
        (individual counters; may race across each other — use
        metrics_snapshot() for consistency)

    Consumer threads are daemon (last-resort process-exit protection —
    RFC-004 §7.7). Graceful shutdown is the normal path; daemon flag
    exists only to prevent a wedged handler from blocking interpreter
    exit.
    """

    _SHUTDOWN_SENTINEL = object()

    def __init__(
        self,
        *,
        workers: int = 8,
        queue_capacity: int = 1024,
        shutdown_timeout_s: float = 5.0,
        name: str = 'MessageDispatcher',
    ) -> None:
        if workers < 1:
            raise ValueError(f'workers must be >= 1; got {workers}')
        if queue_capacity < 1:
            raise ValueError(
                f'queue_capacity must be >= 1; got {queue_capacity}'
            )
        if shutdown_timeout_s < 0:
            raise ValueError(
                f'shutdown_timeout_s must be >= 0; got {shutdown_timeout_s}'
            )

        self._workers_count = workers
        self._queue: queue.Queue = queue.Queue(maxsize=queue_capacity)
        self._shutdown_timeout_s = shutdown_timeout_s
        self._name = name

        # Metrics — every mutation and every snapshot read is under this lock.
        self._metrics_lock = threading.Lock()
        self._active_workers = 0
        self._dropped_message_count = 0
        self._rejected_after_stop_count = 0
        self._processed_count = 0
        self._error_count = 0
        # Rate-limit caches for WARNING logs (per-topic monotonic timestamp).
        self._last_drop_warn: Dict[Any, float] = {}
        self._last_reject_warn: Dict[Any, float] = {}

        # Lifecycle state.
        self._state_lock = threading.Lock()
        self._accepting = True
        self._stopped = False
        self._stop_result: Optional[bool] = None
        # Set once stop() has finished all its work (including posting
        # sentinels and joining workers). Concurrent stop() callers
        # after the first wait on this event so they never observe an
        # incomplete _stop_result.
        self._stop_complete_event = threading.Event()

        # Start consumer threads eagerly.
        self._workers: List[threading.Thread] = []
        for i in range(workers):
            t = threading.Thread(
                target=self._consumer_loop,
                name=f'{name}-{i}',
                daemon=True,
            )
            self._workers.append(t)
            t.start()

        logger.info(
            "Dispatcher started: workers=%d queue_capacity=%d "
            "shutdown_timeout_s=%.1f policy=drop_newest",
            workers, queue_capacity, shutdown_timeout_s,
        )

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def enqueue(self, task: Callable[[], None], *, topic: Any = None) -> bool:
        """Enqueue a callable. Returns True on success; False on drop or
        post-stop rejection. Never raises to the caller — safe for the
        broker's paho loop thread (RFC-004 §7.3 broker-callback safety
        invariant).

        Linearization: the `_accepting` check and the `put_nowait` are
        performed atomically under `_state_lock`. This ensures that
        every enqueue returning True was completed before any
        concurrent stop() could post its shutdown sentinel — the task
        therefore precedes the sentinel in queue order and is
        guaranteed to be dequeued by a consumer before shutdown.
        """
        # Determine outcome under the state lock; log/metric outside.
        outcome: Optional[str] = None
        with self._state_lock:
            if not self._accepting:
                outcome = 'rejected'
            else:
                try:
                    self._queue.put_nowait(task)
                    return True
                except queue.Full:
                    outcome = 'dropped'
        # outcome is set; do the (potentially slow) log/metric outside
        # the critical section so enqueue never holds _state_lock
        # across a logger.warning I/O call.
        if outcome == 'rejected':
            self._record_rejection(topic)
        else:
            self._record_drop(topic)
        return False

    def stop(self, timeout_s: Optional[float] = None) -> bool:
        """Idempotent graceful shutdown. Returns True if all consumers
        exited within the deadline, False if any straggler remains.

        Concurrent-safe: only the first caller executes the actual
        shutdown (state flip + sentinel post + worker joins).
        Subsequent callers wait on `_stop_complete_event` and return
        the same cached outcome — never observe `_stop_result=None`.
        """
        with self._state_lock:
            already_stopped = self._stopped
            if not already_stopped:
                self._accepting = False
                self._stopped = True

        if already_stopped:
            # Wait for the first caller to finish so we can return the
            # coherent _stop_result. The wait is bounded by the first
            # call's own shutdown_timeout_s.
            self._stop_complete_event.wait()
            return bool(self._stop_result)

        if timeout_s is None:
            timeout_s = self._shutdown_timeout_s
        deadline = time.monotonic() + timeout_s

        # Post one sentinel per worker. If the queue is full, use a
        # bounded blocking put so we never wait past the deadline.
        for _ in self._workers:
            remaining = max(0.0, deadline - time.monotonic())
            try:
                self._queue.put_nowait(self._SHUTDOWN_SENTINEL)
                continue
            except queue.Full:
                pass
            if remaining <= 0:
                break
            try:
                self._queue.put(self._SHUTDOWN_SENTINEL, timeout=remaining)
            except queue.Full:
                # Deadline exhausted while trying to post sentinel;
                # any consumers that don't get one will be reported
                # as stragglers below.
                pass

        # Join consumers within the remaining budget.
        for t in self._workers:
            remaining = max(0.0, deadline - time.monotonic())
            t.join(remaining)

        stragglers = [t for t in self._workers if t.is_alive()]
        result = not stragglers

        snapshot = self.metrics_snapshot()
        if stragglers:
            logger.warning(
                "Dispatcher stop timeout: %d stragglers, queue_depth=%d, "
                "active_workers=%d, processed=%d, dropped=%d, rejected=%d, "
                "errors=%d",
                len(stragglers),
                snapshot['queue_depth'],
                snapshot['active_workers'],
                snapshot['processed_count'],
                snapshot['dropped_message_count'],
                snapshot['rejected_after_stop_count'],
                snapshot['error_count'],
            )
            for t in stragglers:
                logger.warning(
                    "  straggler: name=%s alive=%s", t.name, t.is_alive(),
                )
        else:
            logger.info(
                "Dispatcher stopped cleanly: processed=%d, dropped=%d, "
                "rejected=%d, errors=%d",
                snapshot['processed_count'],
                snapshot['dropped_message_count'],
                snapshot['rejected_after_stop_count'],
                snapshot['error_count'],
            )

        with self._state_lock:
            self._stop_result = result
        # Publish completion AFTER _stop_result is stored, so waiters
        # see a coherent value.
        self._stop_complete_event.set()
        return result

    def metrics_snapshot(self) -> Dict[str, int]:
        """Consistent snapshot of all counters + queue depth, sampled
        under a single lock acquisition (RFC-004 §7.12)."""
        with self._metrics_lock:
            return {
                'active_workers': self._active_workers,
                'queue_depth': self._queue.qsize(),
                'dropped_message_count': self._dropped_message_count,
                'rejected_after_stop_count': self._rejected_after_stop_count,
                'processed_count': self._processed_count,
                'error_count': self._error_count,
            }

    # ------------------------------------------------------------------
    # Individual metric properties (may race across each other)
    # ------------------------------------------------------------------

    @property
    def active_workers(self) -> int:
        with self._metrics_lock:
            return self._active_workers

    @property
    def queue_depth(self) -> int:
        return self._queue.qsize()

    @property
    def dropped_message_count(self) -> int:
        with self._metrics_lock:
            return self._dropped_message_count

    @property
    def rejected_after_stop_count(self) -> int:
        with self._metrics_lock:
            return self._rejected_after_stop_count

    @property
    def processed_count(self) -> int:
        with self._metrics_lock:
            return self._processed_count

    @property
    def error_count(self) -> int:
        with self._metrics_lock:
            return self._error_count

    @property
    def is_stopped(self) -> bool:
        with self._state_lock:
            return self._stopped

    # ------------------------------------------------------------------
    # Consumer loop and internal helpers
    # ------------------------------------------------------------------

    def _consumer_loop(self) -> None:
        while True:
            task = self._queue.get()
            try:
                if task is self._SHUTDOWN_SENTINEL:
                    return
                with self._metrics_lock:
                    self._active_workers += 1
                try:
                    try:
                        task()
                        with self._metrics_lock:
                            self._processed_count += 1
                    except Exception:
                        with self._metrics_lock:
                            self._error_count += 1
                        logger.exception(
                            "Dispatcher consumer trapped handler exception"
                        )
                    # BaseException subclasses (KeyboardInterrupt,
                    # SystemExit, GeneratorExit) are deliberately NOT
                    # caught here — see RFC-004 §7.10.
                finally:
                    with self._metrics_lock:
                        self._active_workers -= 1
            finally:
                # task_done runs for EVERY dequeued item, including the
                # sentinel and including tasks that raised BaseException.
                self._queue.task_done()

    def _record_drop(self, topic: Any) -> None:
        with self._metrics_lock:
            self._dropped_message_count += 1
            emit = self._should_emit_warning(self._last_drop_warn, topic)
        if emit:
            logger.warning(
                "Dispatcher queue full; dropping message on topic %r",
                topic,
            )

    def _record_rejection(self, topic: Any) -> None:
        with self._metrics_lock:
            self._rejected_after_stop_count += 1
            emit = self._should_emit_warning(self._last_reject_warn, topic)
        if emit:
            logger.warning(
                "Dispatcher stopped; rejecting message on topic %r",
                topic,
            )

    def _should_emit_warning(
        self, cache: Dict[Any, float], topic: Any,
    ) -> bool:
        """Called under _metrics_lock. Returns True at most once per
        second per topic."""
        now = time.monotonic()
        last = cache.get(topic, 0.0)
        if now - last >= 1.0:
            cache[topic] = now
            return True
        return False


class LegacyPerMessageDispatcher:
    """Byte-for-byte reproduction of the pre-RFC-004 dispatch: every
    enqueue spawns a fresh non-daemon threading.Thread with no pool, no
    queue, no backpressure, no graceful shutdown.

    Deprecated (RFC-004 §7.13). Kept only for the migration window;
    scheduled for removal in the release after next.
    """

    def enqueue(self, task: Callable[[], None], *, topic: Any = None) -> bool:
        threading.Thread(target=task).start()
        return True

    def stop(self, timeout_s: Optional[float] = None) -> bool:
        # Pre-RFC-004 Agent.terminate did not join handler threads;
        # preserved here as no-op. Returns True unconditionally so
        # callers cannot distinguish this from a clean shutdown.
        return True

    def metrics_snapshot(self) -> Dict[str, int]:
        return {
            'active_workers': 0,
            'queue_depth': 0,
            'dropped_message_count': 0,
            'rejected_after_stop_count': 0,
            'processed_count': 0,
            'error_count': 0,
        }

    # Zero counters, exposed for API parity with MessageDispatcher.
    active_workers = 0
    queue_depth = 0
    dropped_message_count = 0
    rejected_after_stop_count = 0
    processed_count = 0
    error_count = 0
    is_stopped = False
