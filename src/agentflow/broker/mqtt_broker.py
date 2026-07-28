from paho.mqtt.client import Client
from paho.mqtt.enums import CallbackAPIVersion
import logging, os, threading
from typing import Any, Dict, Optional
logger = logging.getLogger(os.getenv('LOGGER_NAME'))

from agentflow.core.agent_worker import WorkerState
from .message_broker import MessageBroker
from .notifier import BrokerNotifier


class MqttBroker(MessageBroker):
    """RFC-010 bounded-shutdown MqttBroker.

    Lifecycle: NEW → STARTING → RUNNING → STOPPING → STOPPED
    (or → STOP_TIMEOUT if the helper thread survives the deadline,
    or → STOP_FAILED if the helper died abnormally without setting
    the completed-normally marker, or → START_FAILED on start error).

    Shutdown contract (`stop(graceful_timeout_s=5.0) -> bool`):
      - True  = helper ran to completion (with or without captured
                paho exceptions); state is STOPPED.
      - False = helper timed out (state=STOP_TIMEOUT, retriable) OR
                helper exited abnormally (state=STOP_FAILED, cached).

    Only ONE (disconnect + loop_stop) pair reaches paho per MqttBroker
    lifecycle (RFC-010 modification 1). A STOP_TIMEOUT retry re-joins
    the SAME helper — it does not spawn a new one and does not re-issue
    the paho lifecycle calls.

    Callback fencing (RFC-010 §F): once stop() has flipped `_stopping`,
    delayed paho callbacks (_on_connect / _on_message) do not
    resurrect broker state. `_on_disconnect` may update diagnostics
    (planned/unexpected) but does not touch `_stopping` or trigger
    recovery.

    Interpreter-exit caveat (RFC-009 §H, cross-referenced by RFC-010):
    bounded `stop()` only guarantees the caller returns. If state ends
    at STOP_TIMEOUT the paho network thread is still alive; if a Worker
    with `daemon=False` is waiting on this broker, Python interpreter
    shutdown may still block. RFC-010 does NOT resolve this residual.
    """

    def __init__(self, notifier: BrokerNotifier, *, wait: bool = True, timeout: float = 10.0):
        """建立 MQTT Broker 包裝類別
        :param notifier: BrokerNotifier 實例
        :param wait: 是否等待連線完成才返回 start()
        :param timeout: 最長等待秒數
        """
        self._client = Client(callback_api_version=CallbackAPIVersion.VERSION2,
                              reconnect_on_failure=False)
        self.host = ""
        self.port = 0
        self.keepalive = 0

        # 連線事件控制
        self._connected_evt = threading.Event()
        self._connect_ok = False
        self._connect_err = None

        # 等待連線的行為設定
        self._wait = wait
        self._timeout = timeout

        # RFC-005: subscription registry + connection lifecycle state.
        # _state_lock protects _registry, _connected, _ever_connected,
        # _stopping, _last_disconnect_was_planned, the recovery metric
        # counters, AND (RFC-010) _state, _last_stop_result,
        # _last_stop_exception, _stop_helper_thread. The lock is
        # NEVER held across a paho client call (subscribe / unsubscribe
        # / publish / disconnect / loop_stop), across Thread.join,
        # across Event.wait, or across logger I/O.
        self._state_lock = threading.Lock()
        self._registry: Dict[str, Any] = {}
        self._connected: bool = False
        self._ever_connected: bool = False
        self._stopping: bool = False
        self._last_disconnect_was_planned: Optional[bool] = None
        # Metrics (mutated only under _state_lock; read via snapshot).
        self._resubscribe_success_count: int = 0
        self._resubscribe_error_count: int = 0
        self._recovery_run_count: int = 0

        # RFC-010 state machine + helper-thread coordination.
        self._state: WorkerState = WorkerState.NEW
        self._stop_complete_event = threading.Event()
        self._stop_helper_thread: Optional[threading.Thread] = None
        self._last_stop_result: bool = True
        self._last_stop_exception: Optional[BaseException] = None
        # Set to True by the helper thread only if BOTH paho calls
        # returned (with or without captured Exception). Distinguishes
        # a genuine completion from a BaseException-killed helper
        # (RFC-010 modification 2).
        self._stop_helper_completed_normally: bool = False

        logger.info(f"MQTT broker initialized with notifier: {notifier}, wait={wait}, timeout={timeout}")
        super().__init__(notifier=notifier)


    # ------------------------------------------------------------------
    # Observability (RFC-010)
    # ------------------------------------------------------------------

    @property
    def state(self) -> WorkerState:
        with self._state_lock:
            return self._state

    @property
    def last_stop_exception(self) -> Optional[BaseException]:
        """First exception captured by the stop helper (disconnect or
        loop_stop). None if stop succeeded cleanly or was never
        invoked. Never cleared — diagnostic-only."""
        return self._last_stop_exception


    # ------------------------------------------------------------------
    # Paho callbacks (RFC-010 §F: full fencing when _stopping)
    # ------------------------------------------------------------------

    def _on_connect(self, client, userdata, flags, reasonCode, properties):
        if reasonCode == 0:
            # RFC-010 §F rule: gate ALL state writes on _stopping.
            # A post-stop callback must NOT set _connect_ok, MUST NOT
            # set _connected=True, MUST NOT run recovery, MUST NOT
            # notify the notifier, MUST NOT set _connected_evt.
            skip = False
            snapshot: Dict[str, Any] = {}
            with self._state_lock:
                if self._stopping:
                    skip = True
                else:
                    self._connect_ok = True
                    self._connect_err = None
                    is_first = not self._ever_connected
                    self._ever_connected = True
                    self._connected = True
                    # NEW / STARTING → RUNNING; other states leave
                    # state unchanged (a delayed reconnect after
                    # unexpected disconnect keeps whatever the state
                    # was before). RUNNING → RUNNING is a no-op.
                    if self._state in (WorkerState.NEW, WorkerState.STARTING):
                        self._state = WorkerState.RUNNING
                    if not is_first:
                        snapshot = dict(self._registry)
            if skip:
                logger.info(
                    "on_connect(rc=0) after stop; fencing "
                    "(state / event / notifier / recovery all skipped)"
                )
                return
            try:
                logger.info(f"MQTT broker connected: {self.host}:{self.port}, keepalive={self.keepalive}")
                if snapshot:
                    self._recover_subscriptions(snapshot)
                self._notifier._on_connect()
            finally:
                self._connected_evt.set()
        else:
            # Failure branch also fences post-stop.
            with self._state_lock:
                if self._stopping:
                    logger.info(
                        f"on_connect(rc={reasonCode}) after stop; fencing"
                    )
                    return
                self._connect_ok = False
                self._connected = False
            name = getattr(reasonCode, "getName", lambda: str(reasonCode))()
            self._connect_err = f"{reasonCode} ({name})"
            logger.error(f"MQTT connection failed: {self._connect_err}")
            self._connected_evt.set()

    def _recover_subscriptions(self, snapshot: Dict[str, Any]) -> None:
        """RFC-005 §6.3 recovery loop. Iterates the snapshot OUTSIDE
        _state_lock. Before each client call, re-checks that the topic
        is still in the live registry and that stop() has not fired.
        Per-topic failures are isolated: they increment the error count
        and log at ERROR; the loop continues."""
        with self._state_lock:
            self._recovery_run_count += 1
        logger.info(f"recovery: starting for {len(snapshot)} topic(s)")
        successes = failures = 0
        for topic in snapshot:
            still_in_registry = False
            with self._state_lock:
                if self._stopping:
                    logger.warning(
                        f"recovery aborted by stop() after "
                        f"{successes + failures}/{len(snapshot)} topics"
                    )
                    break
                still_in_registry = topic in self._registry
            if not still_in_registry:
                # Topic was unsubscribed between snapshot and this
                # iteration; skip.
                continue
            try:
                self._client.subscribe(topic=topic)
                successes += 1
            except Exception as ex:
                failures += 1
                logger.exception(
                    f"recovery: resubscribe failed for topic {topic!r}: {ex}"
                )
        with self._state_lock:
            self._resubscribe_success_count += successes
            self._resubscribe_error_count += failures
        logger.info(
            f"recovery complete: {successes} succeeded, {failures} failed"
        )


    def _on_disconnect(self, client, userdata, _flags, reasonCode, _properties):
        # RFC-005 §6.4 + RFC-010 §F: classify planned vs unexpected;
        # clear connection state. This callback is deliberately NOT
        # fenced by `_stopping` — a disconnect that lands after stop
        # SHOULD still update diagnostics (planned classification)
        # and clear `_connected` so any later observer sees the truth.
        # It does NOT clear `_stopping` and does NOT trigger recovery.
        planned = False
        with self._state_lock:
            planned = self._stopping or (
                reasonCode == 0
                or getattr(reasonCode, "value", None) == 0
            )
            self._last_disconnect_was_planned = planned
            self._connected = False
        self._connect_ok = False
        self._connected_evt.clear()
        if planned:
            logger.info(f"MQTT disconnected (planned): {reasonCode}")
        else:
            logger.warning(f"MQTT disconnected (unexpected): {reasonCode}")


    def _on_message(self, client, db, message):
        # RFC-010 §F: silent drop after stop. Late callbacks must NOT
        # dispatch to the notifier (which may have been torn down or
        # whose dispatcher may have been stopped).
        with self._state_lock:
            if self._stopping:
                return
        try:
            self._notifier._on_message(message.topic, message.payload)
        except Exception as ex:
            logger.exception(ex)


    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def start(self, options: dict):
        logger.info("MQTT broker is starting...")

        # RFC-010: state transition NEW → STARTING. Non-NEW start is
        # logged but not gated (first-phase; matches pre-RFC-010
        # behaviour of not raising).
        with self._state_lock:
            if self._state != WorkerState.NEW:
                logger.warning(
                    f"MqttBroker.start called from state={self._state.value}; "
                    f"first-phase does not gate repeated start"
                )
            self._state = WorkerState.STARTING

        self._client.on_connect = self._on_connect
        self._client.on_disconnect = self._on_disconnect
        self._client.on_message = self._on_message

        self.host = options.get("host", "localhost")
        self.port = int(options.get("port", 1883))
        self.keepalive = int(options.get("keepalive", 60))

        if username := options.get("username"):
            self._client.username_pw_set(username, options.get("password"))

        # 初始化事件
        self._connected_evt.clear()
        self._connect_ok = False
        self._connect_err = None

        self._client.connect(self.host, self.port, self.keepalive)
        self._client.loop_start()

        if not self._wait:
            # State remains STARTING until _on_connect fires (or
            # transitions elsewhere via a subsequent stop() from the
            # STARTING state — which RFC-010 first-phase rejects with
            # RuntimeError, see stop()).
            return True

        if not self._connected_evt.wait(self._timeout):
            logger.error(f"MQTT connect timeout after {self._timeout}s")
            with self._state_lock:
                self._state = WorkerState.START_FAILED
            self._client.loop_stop()
            self._client.disconnect()
            raise TimeoutError(f"MQTT connect timeout ({self.host}:{self.port})")

        if not self._connect_ok:
            with self._state_lock:
                self._state = WorkerState.START_FAILED
            self._client.loop_stop()
            self._client.disconnect()
            raise ConnectionError(f"MQTT connect failed: {self._connect_err}")

        # On success, _on_connect has already transitioned state to
        # RUNNING under the lock.
        return True


    def stop(self, graceful_timeout_s: float = 5.0) -> bool:
        """RFC-010 bounded cooperative shutdown.

        Returns True when the helper thread ran to completion; False
        when the helper timed out (STOP_TIMEOUT — retriable) or died
        abnormally (STOP_FAILED — cached).

        Concurrent callers coordinate via `_stop_complete_event` with
        a BOUNDED wait (`graceful_timeout_s + 0.1s` coordination
        margin). N callers share exactly ONE helper thread and
        exactly ONE `disconnect + loop_stop` pair to paho.

        Retry from STOP_TIMEOUT does NOT spawn a new helper; it
        re-joins the existing one (RFC-010 modification 1). Same
        MqttBroker lifecycle → at most one paho stop pair, ever.

        Bounded return ONLY guarantees this caller returns. If state
        ends at STOP_TIMEOUT, the paho network thread is still alive
        and, if a Worker with daemon=False is waiting on this broker,
        interpreter shutdown may still block (RFC-009 §H).
        """
        is_waiter = False
        is_retry = False

        with self._state_lock:
            current = self._state
            if current == WorkerState.NEW:
                # Pure no-op: never started, no callbacks registered
                # on the paho client, no state to fence.
                return True
            if current == WorkerState.START_FAILED:
                # start() already cleaned paho (loop_stop + disconnect)
                # on the failure path. Fence any late callback.
                self._stopping = True
                return True
            if current == WorkerState.STOPPED:
                return True
            if current == WorkerState.STOP_FAILED:
                # Idempotent replay of the cached failure result.
                return self._last_stop_result
            if current == WorkerState.STARTING:
                # RFC-010 modification 3: first-phase raises rather
                # than trying to disconnect a half-initialised client.
                raise RuntimeError(
                    "MqttBroker.stop called while state=STARTING; "
                    "start-stop coordination is not supported in "
                    "first-phase RFC-010 — wait for start() to reach "
                    "RUNNING (or START_FAILED) before calling stop()"
                )
            if current == WorkerState.STOPPING:
                is_waiter = True
            elif current == WorkerState.RUNNING:
                # First caller — full linearization (RFC-010 §G):
                # flip _stopping + clear active connection state so
                # any inline callback observes the fenced state before
                # we return the lock.
                self._state = WorkerState.STOPPING
                self._stopping = True
                self._connected = False
                self._connect_ok = False
                self._connected_evt.clear()
                self._stop_complete_event.clear()
            elif current == WorkerState.STOP_TIMEOUT:
                # RFC-010 modification 1: retry re-joins existing
                # helper. No new spawn, no re-issue of paho calls.
                is_retry = True
                self._state = WorkerState.STOPPING
                self._stop_complete_event.clear()
            else:  # pragma: no cover — defensive
                return True

        # -- Concurrent waiter path (bounded, RFC-010 §E).
        if is_waiter:
            coordination_margin_s = 0.1
            completed = self._stop_complete_event.wait(
                graceful_timeout_s + coordination_margin_s
            )
            if completed:
                with self._state_lock:
                    return self._last_stop_result
            # Event did not fire in time — never wait forever.
            alive = (self._stop_helper_thread is not None
                     and self._stop_helper_thread.is_alive())
            try:
                logger.warning(
                    f"MqttBroker.stop coordination wait timed out "
                    f"({graceful_timeout_s + coordination_margin_s:.1f}s); "
                    f"helper alive={alive}"
                )
            except Exception:
                pass
            return not alive

        # -- First caller OR retry path.
        try:
            if is_retry:
                # RFC-010 modification 1: same helper, no new spawn,
                # no new paho calls. If the previous helper is now
                # dead, we still bounded-join for uniform state
                # transition below.
                helper = self._stop_helper_thread
            else:
                # Spawn helper — the ONLY place we ever do so.
                # daemon=True (RFC-010 §7.13 + Appendix A): helper
                # contains only paho lifecycle calls; daemonising
                # avoids dragging the interpreter down when paho is
                # itself wedged.
                helper = threading.Thread(
                    target=self._run_stop_helper,
                    name=f'MqttBrokerStop-{id(self)}',
                    daemon=True,
                )
                self._stop_helper_thread = helper
                helper.start()

            if helper is not None:
                helper.join(graceful_timeout_s)

            alive = helper is not None and helper.is_alive()
            completed_normally = self._stop_helper_completed_normally

            with self._state_lock:
                if alive:
                    # Helper still running — bounded timeout.
                    self._state = WorkerState.STOP_TIMEOUT
                    self._last_stop_result = False
                elif completed_normally:
                    # Helper finished, both paho calls attempted.
                    self._state = WorkerState.STOPPED
                    self._last_stop_result = True
                else:
                    # Helper died abnormally (e.g. BaseException
                    # propagated out of the helper body) — RFC-010
                    # modification 2: MUST NOT mislabel STOPPED.
                    self._state = WorkerState.STOP_FAILED
                    self._last_stop_result = False
        finally:
            # ALWAYS release concurrent waiters, even if the escalation
            # body raised, so the coordination path never hangs.
            self._stop_complete_event.set()

        # -- Post-lock logging (lock hygiene: never inside _state_lock).
        try:
            if self._last_stop_result:
                if self._last_stop_exception is not None:
                    logger.info(
                        f"MqttBroker stopped with captured paho "
                        f"exception during helper: "
                        f"{self._last_stop_exception!r}"
                    )
                else:
                    logger.info("MqttBroker stopped cleanly")
            elif self._state == WorkerState.STOP_TIMEOUT:
                logger.warning(
                    f"MqttBroker.stop timeout after "
                    f"{graceful_timeout_s:.1f}s; helper still alive. "
                    f"Retry stop() to re-join the SAME helper — "
                    f"disconnect/loop_stop are NOT re-issued (RFC-010 "
                    f"modification 1). Note: because worker threads "
                    f"may be daemon=False, interpreter shutdown may "
                    f"still block. See RFC-009 §H."
                )
            else:  # STOP_FAILED
                logger.error(
                    f"MqttBroker.stop: helper thread died abnormally "
                    f"without completing (likely BaseException in "
                    f"paho); last_stop_exception="
                    f"{self._last_stop_exception!r}. Construct a "
                    f"fresh MqttBroker to retry a real shutdown."
                )
        except Exception:
            pass
        return self._last_stop_result


    def _run_stop_helper(self):
        """RFC-010 §C helper body. Runs paho disconnect + loop_stop
        with per-call Exception isolation. Sets
        `_stop_helper_completed_normally = True` if BOTH calls
        returned (with or without captured Exception).

        NOTE: BaseException is NOT caught (RFC-010 §7.15) — matches
        RFC-009 §7.11 for ThreadWorker's _run_target. A BaseException
        propagating out kills the helper thread; stop() observes
        `_stop_helper_completed_normally == False` and marks
        STOP_FAILED (never STOPPED).
        """
        try:
            self._client.disconnect()
        except Exception as ex:
            if self._last_stop_exception is None:
                self._last_stop_exception = ex
            logger.exception(
                f"MqttBroker.stop: client.disconnect() raised: {ex!r}"
            )
        try:
            self._client.loop_stop()
        except Exception as ex:
            if self._last_stop_exception is None:
                self._last_stop_exception = ex
            logger.exception(
                f"MqttBroker.stop: client.loop_stop() raised: {ex!r}"
            )
        # Reached only if both calls returned (with or without a
        # captured Exception; NOT reached if BaseException propagated
        # out of either call).
        self._stop_helper_completed_normally = True


    def publish(self, topic: str, payload):
        return self._client.publish(topic=topic, payload=payload)

    def subscribe(self, topic: str, data_type):
        # RFC-005 §7.3: desired-state first; only forward when currently
        # connected. Disconnected-time subscribes rely on the next
        # _on_connect to run recovery from the registry.
        with self._state_lock:
            if self._stopping:
                logger.info(f"subscribe after stop; dropping topic {topic!r}")
                return None
            self._registry[topic] = data_type
            connected = self._connected
        if connected:
            return self._client.subscribe(topic=topic)
        return None

    def unsubscribe(self, topic: str):
        # RFC-005 §7.4: symmetric with subscribe.
        with self._state_lock:
            if self._stopping:
                logger.info(f"unsubscribe after stop; dropping topic {topic!r}")
                return None
            self._registry.pop(topic, None)
            connected = self._connected
        if connected:
            return self._client.unsubscribe(topic)
        return None

    # ---- RFC-005 observability surface (additive) ----

    @property
    def last_disconnect_was_planned(self) -> Optional[bool]:
        return self._last_disconnect_was_planned

    def recovery_metrics(self) -> Dict[str, Any]:
        """Coherent snapshot of subscription + recovery state, taken
        under a single _state_lock acquisition."""
        with self._state_lock:
            return {
                "resubscribe_success_count": self._resubscribe_success_count,
                "resubscribe_error_count": self._resubscribe_error_count,
                "recovery_run_count": self._recovery_run_count,
                "active_subscriptions": len(self._registry),
                "connected": self._connected,
                "stopping": self._stopping,
                "ever_connected": self._ever_connected,
            }
