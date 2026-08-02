from paho.mqtt.client import Client
from paho.mqtt.enums import CallbackAPIVersion
import logging, os, threading, time
from enum import Enum
from typing import Any, Dict, Optional
logger = logging.getLogger(os.getenv('LOGGER_NAME'))

from agentflow.core.agent_worker import WorkerState
from .message_broker import MessageBroker
from .notifier import BrokerNotifier


# ---------------------------------------------------------------------------
# RFC-012: publish result contract
# ---------------------------------------------------------------------------

# Pinned locally rather than imported from paho.mqtt.enums so this
# module is insulated from paho version restructuring. paho's own
# MQTT_ERR_SUCCESS has always been 0 (v1 and v2).
MQTT_ERR_SUCCESS = 0


class MqttPublishReason(Enum):
    """RFC-012 §7.4-§7.5 stable short codes carried on
    MqttPublishError.reason.

    Stability guarantee: enum VALUES (the strings) are frozen for
    log-mining. Adding new members is allowed; renaming existing
    members / values is a breaking log-schema change and requires
    its own RFC (see RFC-012 Appendix D).
    """
    BROKER_NOT_RUNNING = 'broker_not_running'
    BROKER_STOPPING = 'broker_stopping'
    BROKER_DISCONNECTED = 'broker_disconnected'
    PAHO_REJECTED = 'paho_rejected'
    UNSUPPORTED_RESULT = 'unsupported_result'


class MqttPublishError(RuntimeError):
    """RFC-012: raised by MqttBroker.publish when the publish request
    was rejected — either by the RFC-012 state gate (pre-call
    snapshot) or by paho's immediate publish result.

    Subclass of RuntimeError so that RFC-002's fire-and-forget
    contract on Agent.publish continues to catch it via
    `except Exception`.

    Attributes:
      topic       — the MQTT topic passed to publish() (str)
      rc          — paho MQTTErrorCode int, or None when the gate
                    rejected before paho was called
      mid         — paho message id (int), or None when unavailable
      reason      — MqttPublishReason enum member (stable short code)
      state       — WorkerState at pre-call snapshot time, or None
                    when not relevant to the failure classification
      result_type — str name of the paho return type, or None
                    (populated for UNSUPPORTED_RESULT diagnostics)
      detail      — short human-readable extra context, or None
                    (populated for UNSUPPORTED_RESULT rc/mid coercion
                    failures)

    Message format (frozen — RFC-012 Appendix D):
        MQTT publish failed: topic=<repr>, rc=<int|None>,
        mid=<int|None>, reason=<code>[, state=<state.value>]
        [, result_type=<type>][, detail=<repr>]
    """

    def __init__(
        self,
        topic: str,
        reason: MqttPublishReason,
        *,
        rc: Optional[int] = None,
        mid: Optional[int] = None,
        state: Optional[WorkerState] = None,
        result_type: Optional[str] = None,
        detail: Optional[str] = None,
    ):
        self.topic = topic
        self.rc = rc
        self.mid = mid
        self.reason = reason
        self.state = state
        self.result_type = result_type
        self.detail = detail

        # Build the human-readable message. Field order and separator
        # are stable for log-mining (RFC-012 Appendix D).
        parts = [
            f"topic={topic!r}",
            f"rc={rc}",
            f"mid={mid}",
            f"reason={reason.value}",
        ]
        if state is not None:
            parts.append(f"state={state.value}")
        if result_type is not None:
            parts.append(f"result_type={result_type}")
        if detail is not None:
            parts.append(f"detail={detail!r}")
        super().__init__("MQTT publish failed: " + ", ".join(parts))


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

        # RFC-011 bounded-startup coordination + observability.
        self._start_complete_event = threading.Event()
        self._start_helper_thread: Optional[threading.Thread] = None
        # Set True by the startup helper only if BOTH paho calls returned
        # (with or without captured Exception). Distinguishes a
        # BaseException-killed helper from a normal helper exit
        # (RFC-011 modification 2 / §7.7).
        self._start_helper_completed_normally: bool = False
        self._last_start_result: bool = False
        self._last_start_exception: Optional[BaseException] = None
        # RFC-011 modification 1: separate cleanup result for START_TIMEOUT
        # → stop() coordination. None until the rollback primitive is
        # actually run; True/False = last run outcome.
        self._last_start_cleanup_result: Optional[bool] = None
        # Diagnostic-only counter (RFC-011 §7.22 / Appendix C). NOT used
        # for callback filtering — see Appendix C for why generation
        # integers cannot solve cross-attempt callback contamination.
        self._start_generation: int = 0
        # Serialises the START_TIMEOUT recovery path so concurrent
        # stop() callers do not double-run the rollback primitive.
        self._start_timeout_recovery_lock = threading.Lock()

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

    @property
    def last_start_exception(self) -> Optional[BaseException]:
        """RFC-011 §7.22: first exception captured by the startup path
        (helper or caller). None if start succeeded cleanly or was never
        invoked. Never cleared — diagnostic-only."""
        return self._last_start_exception

    @property
    def start_generation(self) -> int:
        """RFC-011 §7.22 / Appendix C: monotonically incrementing
        counter of start() attempts on this instance. Diagnostic-only;
        NOT used for callback filtering (see Appendix C for why the
        naive form does not work)."""
        return self._start_generation


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

    def start(self, options: dict, *,
              startup_timeout_s: Optional[float] = None) -> bool:
        """RFC-011 bounded cooperative startup.

        Runs `client.connect()` + `client.loop_start()` on a daemon
        helper thread; joins with `startup_timeout_s`; on wait=True,
        additionally awaits `_on_connect` callback within the SAME
        deadline (single monotonic budget). On any failure path
        (helper wedge, connect raise, loop_start raise, callback wait
        timeout, callback rc!=0), invokes a private bounded
        client-shutdown primitive with its own 5.0s budget.

        Returns True on success. Raises TimeoutError on START_TIMEOUT,
        ConnectionError on rc!=0, the original paho Exception on
        START_FAILED, RuntimeError on non-NEW start attempt.

        RFC-011 modification 4 — wait=False contract: True ONLY means
        connect + loop_start were initiated (helper completed both
        paho calls bounded). It does NOT mean the broker is connected;
        state stays STARTING until `_on_connect(rc=0)` fires later.

        RFC-011 modification 5 — same-instance retry NOT supported.
        START_TIMEOUT / START_FAILED / STOPPED / any non-NEW state
        raises RuntimeError WITHOUT modifying any lifecycle flag.
        Construct a fresh MqttBroker to try again.
        """
        # `startup_timeout_s` defaults to the broker-instance timeout
        # (constructor arg, default 10.0). Preserves backward compat
        # with `MqttBroker(wait=True, timeout=0.1).start({})`.
        if startup_timeout_s is None:
            startup_timeout_s = float(self._timeout)

        is_waiter = False

        # -- Phase 0: linearization under _state_lock.
        # RFC-011 modification 5: check state BEFORE modifying flags.
        # Only reset lifecycle flags in the NEW branch.
        with self._state_lock:
            current = self._state
            if current == WorkerState.NEW:
                self._state = WorkerState.STARTING
                # Reset per-attempt flags ATOMICALLY with state.
                self._stopping = False
                self._connected = False
                self._connect_ok = False
                self._connect_err = None
                self._connected_evt.clear()
                self._start_complete_event.clear()
                self._start_generation += 1
                self._last_start_result = False
                self._last_start_exception = None
                self._last_start_cleanup_result = None
                self._start_helper_completed_normally = False
            elif current == WorkerState.STARTING:
                is_waiter = True
            else:
                # RFC-011 §7.5: failed instance is terminal.
                # Do NOT modify any flags.
                raise RuntimeError(
                    f"MqttBroker.start called from state={current.value}; "
                    f"same-instance retry is not supported in first-phase "
                    f"RFC-011 — construct a fresh MqttBroker instance"
                )

        # -- Concurrent waiter path (bounded, RFC-011 §E / mod 3).
        if is_waiter:
            coord_margin_s = 0.1
            completed = self._start_complete_event.wait(
                startup_timeout_s + coord_margin_s
            )
            if completed:
                with self._state_lock:
                    success = self._last_start_result
                    exc = self._last_start_exception
                if success:
                    return True
                # RFC-011 modification 3: waiter raises a NEW RuntimeError
                # chained to the original — never re-raises the same
                # exception instance across threads (avoids traceback /
                # __context__ mutation hazards).
                if exc is not None:
                    raise RuntimeError(
                        "MqttBroker.start failed in another caller"
                    ) from exc
                raise RuntimeError(
                    "MqttBroker.start failed in another caller"
                )
            # Coordination event did not fire in time — bounded fallback.
            alive = (self._start_helper_thread is not None
                     and self._start_helper_thread.is_alive())
            try:
                logger.warning(
                    f"MqttBroker.start coordination wait timed out "
                    f"({startup_timeout_s + coord_margin_s:.1f}s); "
                    f"helper alive={alive}"
                )
            except Exception:
                pass
            raise TimeoutError(
                f"MqttBroker.start concurrent waiter timeout after "
                f"{startup_timeout_s + coord_margin_s:.1f}s"
            )

        # -- First-caller path.
        # Callback binding (lock-external, no paho I/O yet).
        self._client.on_connect = self._on_connect
        self._client.on_disconnect = self._on_disconnect
        self._client.on_message = self._on_message
        self.host = options.get("host", "localhost")
        self.port = int(options.get("port", 1883))
        self.keepalive = int(options.get("keepalive", 60))
        if username := options.get("username"):
            self._client.username_pw_set(username, options.get("password"))

        # Spawn daemon startup helper.
        helper = threading.Thread(
            target=self._run_startup_helper,
            name=f'MqttBrokerStart-{id(self)}',
            daemon=True,
        )
        self._start_helper_thread = helper
        deadline = time.monotonic() + startup_timeout_s
        helper.start()

        try:
            # -- Phase 1: bounded join for connect + loop_start.
            remaining = max(0.0, deadline - time.monotonic())
            helper.join(remaining)

            if helper.is_alive():
                # RFC-011 modification 2: helper still wedged past
                # deadline. DO NOT spawn rollback helper — startup
                # helper is still touching the paho client. Only
                # fence state; stop() will later coordinate.
                exc = TimeoutError(
                    f"MqttBroker.start timeout after "
                    f"{startup_timeout_s:.1f}s waiting for "
                    f"connect / loop_start (helper still alive; "
                    f"rollback deferred to stop())"
                )
                self._transition_to_start_failure(
                    WorkerState.START_TIMEOUT, exc,
                )
                raise exc

            # Helper finished. Check outcome.
            if not self._start_helper_completed_normally:
                # BaseException killed helper OR completed_normally
                # never set. Helper is dead → safe to run rollback.
                cap_exc = self._last_start_exception
                if cap_exc is None:
                    cap_exc = RuntimeError(
                        "MqttBroker startup helper died abnormally "
                        "without completing (likely BaseException from "
                        "paho); construct a fresh broker to retry"
                    )
                    with self._state_lock:
                        self._last_start_exception = cap_exc
                self._transition_to_start_failure(
                    WorkerState.START_FAILED, cap_exc,
                )
                self._run_client_shutdown_primitive_and_cache()
                raise cap_exc

            if self._last_start_exception is not None:
                # Helper captured Exception (connect or loop_start).
                # Helper is dead → safe to run rollback.
                cap_exc = self._last_start_exception
                self._transition_to_start_failure(
                    WorkerState.START_FAILED, cap_exc,
                )
                self._run_client_shutdown_primitive_and_cache()
                raise cap_exc

            # -- Phase 2: connect + loop_start succeeded.
            if not self._wait:
                # RFC-011 modification 4: wait=False True ONLY means
                # connect + loop_start were initiated. State stays
                # STARTING until _on_connect(rc=0) fires.
                with self._state_lock:
                    self._last_start_result = True
                try:
                    logger.info(
                        f"MQTT broker startup initiated (wait=False): "
                        f"host={self.host}, port={self.port}"
                    )
                except Exception:
                    pass
                return True

            # wait=True: await callback within the SAME remaining budget.
            remaining = max(0.0, deadline - time.monotonic())
            if not self._connected_evt.wait(remaining):
                # Callback wait timeout. Startup helper has finished;
                # safe to run rollback.
                exc = TimeoutError(
                    f"MqttBroker.start timeout after "
                    f"{startup_timeout_s:.1f}s waiting for _on_connect "
                    f"callback ({self.host}:{self.port})"
                )
                self._transition_to_start_failure(
                    WorkerState.START_TIMEOUT, exc,
                )
                self._run_client_shutdown_primitive_and_cache()
                raise exc

            if not self._connect_ok:
                exc = ConnectionError(
                    f"MQTT connect failed: {self._connect_err}"
                )
                self._transition_to_start_failure(
                    WorkerState.START_FAILED, exc,
                )
                self._run_client_shutdown_primitive_and_cache()
                raise exc

            # Success — `_on_connect` already transitioned state to RUNNING.
            with self._state_lock:
                self._last_start_result = True
            try:
                logger.info(
                    f"MQTT broker startup succeeded: "
                    f"host={self.host}, port={self.port}"
                )
            except Exception:
                pass
            return True
        finally:
            # ALWAYS release concurrent waiters, even if we raised.
            self._start_complete_event.set()


    def _transition_to_start_failure(self, new_state: WorkerState,
                                     exception: BaseException):
        """RFC-011 §H: atomic transition into a startup failure state.
        Sets _stopping=True in the SAME lock section so callback
        fencing is immediate (parity with RFC-010 §G). Only actually
        transitions if the current state is STARTING; avoids
        clobbering a concurrent failure reason."""
        with self._state_lock:
            if self._state == WorkerState.STARTING:
                self._state = new_state
                self._stopping = True
                self._connected = False
                self._connect_ok = False
                self._connected_evt.clear()
                if self._last_start_exception is None:
                    self._last_start_exception = exception
                self._last_start_result = False


    def _run_startup_helper(self):
        """RFC-011 §C startup-helper body. Runs `connect + loop_start`
        with per-call `except Exception` isolation. Sets
        `_start_helper_completed_normally = True` only if BOTH calls
        returned (with or without captured Exception). BaseException
        propagates and kills the helper (matches RFC-009 §7.11 /
        RFC-010 §7.15)."""
        try:
            self._client.connect(self.host, self.port, self.keepalive)
        except Exception as ex:
            if self._last_start_exception is None:
                self._last_start_exception = ex
            try:
                logger.exception(
                    f"MqttBroker.start: client.connect() raised: {ex!r}"
                )
            except Exception:
                pass
            # Do NOT proceed to loop_start when connect raised — the
            # rollback primitive will handle the (nonexistent) socket.
            self._start_helper_completed_normally = True
            return
        try:
            self._client.loop_start()
        except Exception as ex:
            if self._last_start_exception is None:
                self._last_start_exception = ex
            try:
                logger.exception(
                    f"MqttBroker.start: client.loop_start() raised: {ex!r}"
                )
            except Exception:
                pass
        # Reached iff no BaseException propagated.
        self._start_helper_completed_normally = True


    def _run_client_shutdown_primitive(self, *,
                                       rollback_timeout_s: float = 5.0
                                       ) -> bool:
        """RFC-011 §6.4 / §E: state-agnostic, coordination-free
        bounded client-shutdown primitive. Spawns a daemon helper
        that runs `disconnect + loop_stop` with per-call Exception
        isolation; caller bounded-joins.

        Returns True if the helper completed within budget; False
        otherwise (helper still alive after `rollback_timeout_s`).

        Does NOT touch `_state`, `_stop_complete_event`, or
        `_start_complete_event` — the caller owns those. Also does
        NOT overwrite failure state to STOPPED (§E requirement).

        Must NOT be called while the startup helper is still alive
        (RFC-011 modification 2 — would race on the same paho client).
        """
        completed = threading.Event()

        def body():
            try:
                try:
                    self._client.disconnect()
                except Exception as ex:
                    try:
                        logger.exception(
                            f"client-shutdown primitive: disconnect() "
                            f"raised: {ex!r}"
                        )
                    except Exception:
                        pass
                try:
                    self._client.loop_stop()
                except Exception as ex:
                    try:
                        logger.exception(
                            f"client-shutdown primitive: loop_stop() "
                            f"raised: {ex!r}"
                        )
                    except Exception:
                        pass
            finally:
                completed.set()

        helper = threading.Thread(
            target=body,
            name=f'MqttBrokerCleanup-{id(self)}',
            daemon=True,
        )
        helper.start()
        result = completed.wait(rollback_timeout_s)
        if not result:
            try:
                logger.warning(
                    f"client-shutdown primitive: timeout after "
                    f"{rollback_timeout_s:.1f}s; helper still running "
                    f"(paho wedged inside cleanup)"
                )
            except Exception:
                pass
        return result


    def _run_client_shutdown_primitive_and_cache(self, *,
                                                 rollback_timeout_s: float = 5.0
                                                 ) -> bool:
        """Wraps `_run_client_shutdown_primitive` and caches the result
        into `_last_start_cleanup_result` so subsequent `stop()` from
        START_TIMEOUT / START_FAILED can inspect it without re-running
        the primitive."""
        result = self._run_client_shutdown_primitive(
            rollback_timeout_s=rollback_timeout_s,
        )
        with self._state_lock:
            self._last_start_cleanup_result = result
        return result


    def _start_timeout_recovery(self, graceful_timeout_s: float) -> bool:
        """RFC-011 §F / modification 1+2: dedicated recovery path when
        stop() is called on a broker in START_TIMEOUT state.

          - If the startup helper is still alive: bounded-join it
            first (avoids two helpers concurrently touching the same
            paho client). If it's still alive after the join → return
            False (cannot proceed with cleanup safely).
          - If the startup helper has finished and cleanup was already
            run: return the cached `_last_start_cleanup_result`.
          - Otherwise: run the client-shutdown primitive AT MOST ONCE
            (serialised by `_start_timeout_recovery_lock`), cache the
            result, and return it.

        Bounded — never waits without a timeout.
        """
        # Fast path: cleanup already ran (concurrent caller finished).
        with self._state_lock:
            cached = self._last_start_cleanup_result
        if cached is not None:
            return cached

        # Serialise so concurrent callers do not double-run the primitive.
        # Bounded acquire — return False if we cannot acquire in time.
        acquired = self._start_timeout_recovery_lock.acquire(
            timeout=graceful_timeout_s + 0.1
        )
        if not acquired:
            try:
                logger.warning(
                    f"MqttBroker.stop from START_TIMEOUT: could not "
                    f"acquire recovery lock within "
                    f"{graceful_timeout_s + 0.1:.1f}s; another caller "
                    f"is running cleanup"
                )
            except Exception:
                pass
            # Return whatever the other caller has cached so far.
            with self._state_lock:
                return (self._last_start_cleanup_result
                        if self._last_start_cleanup_result is not None
                        else False)

        try:
            # Re-check after acquiring lock (another caller may have
            # finished cleanup between the fast path and here).
            with self._state_lock:
                cached = self._last_start_cleanup_result
            if cached is not None:
                return cached

            # Bounded-wait for startup helper to finish. RFC-011
            # modification 2: MUST NOT run the primitive while startup
            # helper is still alive (would race on same paho client).
            startup_helper = self._start_helper_thread
            if startup_helper is not None and startup_helper.is_alive():
                startup_helper.join(graceful_timeout_s)
                if startup_helper.is_alive():
                    try:
                        logger.warning(
                            f"MqttBroker.stop from START_TIMEOUT: "
                            f"startup helper still alive after "
                            f"{graceful_timeout_s:.1f}s bounded wait; "
                            f"deferring cleanup (would race on paho client)"
                        )
                    except Exception:
                        pass
                    # Do NOT cache False permanently — a later stop()
                    # retry can try again once the helper finishes.
                    return False

            # Startup helper has finished. Safe to run cleanup once.
            result = self._run_client_shutdown_primitive_and_cache(
                rollback_timeout_s=graceful_timeout_s,
            )
            return result
        finally:
            self._start_timeout_recovery_lock.release()


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

        is_start_timeout_recovery = False

        with self._state_lock:
            current = self._state
            if current == WorkerState.NEW:
                # Pure no-op: never started, no callbacks registered
                # on the paho client, no state to fence.
                return True
            if current == WorkerState.START_FAILED:
                # RFC-011 §7.19: start()'s failure path already ran the
                # cleanup primitive; reflect its actual result rather
                # than blindly returning True.
                self._stopping = True
                if self._last_start_cleanup_result is False:
                    return False
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
                    "RUNNING (or START_FAILED / START_TIMEOUT) before "
                    "calling stop()"
                )
            if current == WorkerState.START_TIMEOUT:
                # RFC-011 modification 1: dedicated recovery path
                # (bounded-wait for startup helper, then run cleanup
                # primitive at most once).
                is_start_timeout_recovery = True
            elif current == WorkerState.STOPPING:
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

        # -- RFC-011 START_TIMEOUT recovery path (bounded, mod 1+2).
        if is_start_timeout_recovery:
            return self._start_timeout_recovery(graceful_timeout_s)

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
        """RFC-012 publish with state gate + immediate-result validation.

        Raises MqttPublishError when:
          - the pre-call state gate rejects (state != RUNNING, or
            _stopping=True, or _connected=False);
          - paho returned an unsupported result shape;
          - paho's immediate result rc != MQTT_ERR_SUCCESS.

        Returns paho's original result unchanged on success.

        State gate — IMPORTANT (RFC-012 modification 2):

          The gate is a PRE-CALL best-effort snapshot; it is NOT a
          full linearization barrier with stop(). Concurrent stop()
          on another thread may flip `_stopping=True` AFTER our
          snapshot but BEFORE `client.publish()` runs, in which
          case this publish will still reach paho. paho's immediate
          rc (checked below the gate) is the second, authoritative
          layer of validation; a truly rejected publish will surface
          as MqttPublishError(PAHO_REJECTED). A full stop-linearized
          barrier is out of scope for this RFC (see Appendix E) and
          is deferred to a future "publish/stop operation barrier"
          RFC.

        Lock hygiene:
          `_state_lock` is held only across the short snapshot; it
          is released BEFORE `client.publish()` is called (parity
          with RFC-005 / RFC-010 / RFC-011 lock hygiene).
        """
        # -- Pre-call state snapshot (best-effort — NOT linearized
        # with stop; see docstring).
        with self._state_lock:
            state = self._state
            stopping = self._stopping
            connected = self._connected

        # -- Gate priority order (RFC-012 §B modification 2):
        #    1. stopping wins over state (fresh stop request)
        #    2. state != RUNNING
        #    3. state == RUNNING but not currently connected
        if stopping:
            raise MqttPublishError(
                topic=topic,
                reason=MqttPublishReason.BROKER_STOPPING,
                state=state,
            )
        if state is not WorkerState.RUNNING:
            raise MqttPublishError(
                topic=topic,
                reason=MqttPublishReason.BROKER_NOT_RUNNING,
                state=state,
            )
        if not connected:
            raise MqttPublishError(
                topic=topic,
                reason=MqttPublishReason.BROKER_DISCONNECTED,
                state=state,
            )

        # -- paho call OUTSIDE the state lock.
        # A raise from paho itself propagates unchanged (RFC-002
        # convention preserved for Exception paths).
        result = self._client.publish(topic=topic, payload=payload)

        # -- Result normalisation + rc validation (authoritative
        # layer; catches races where stop() started after our
        # snapshot).
        rc, mid = self._normalise_publish_result(topic, result)
        if rc != MQTT_ERR_SUCCESS:
            raise MqttPublishError(
                topic=topic,
                reason=MqttPublishReason.PAHO_REJECTED,
                rc=rc,
                mid=mid,
            )
        return result


    def _normalise_publish_result(self, topic: str, result):
        """RFC-012 §C: parse the paho publish() return into (rc, mid).

        Supported shapes:
          - paho v2: MQTTMessageInfo (or any object with .rc)
          - paho v1: tuple (rc, mid[, ...])
          - test fakes: any of the above

        Unsupported shapes (raise MqttPublishError UNSUPPORTED_RESULT):
          - None
          - arbitrary objects without .rc or [0] index access

        Coercion failures (int(rc) / int(mid) raise): re-raised as
        MqttPublishError UNSUPPORTED_RESULT with the original
        exception as __cause__ (via `raise ... from`).
        """
        # Case 1: paho v2 or duck-typed object with .rc attribute.
        if hasattr(result, 'rc'):
            raw_rc = result.rc
            raw_mid = getattr(result, 'mid', None)
            try:
                rc = int(raw_rc)
            except (TypeError, ValueError) as ex:
                raise MqttPublishError(
                    topic=topic,
                    reason=MqttPublishReason.UNSUPPORTED_RESULT,
                    result_type=type(result).__name__,
                    detail="invalid rc",
                ) from ex
            mid: Optional[int] = None
            if raw_mid is not None:
                try:
                    mid = int(raw_mid)
                except (TypeError, ValueError) as ex:
                    raise MqttPublishError(
                        topic=topic,
                        reason=MqttPublishReason.UNSUPPORTED_RESULT,
                        rc=rc,
                        result_type=type(result).__name__,
                        detail="invalid mid",
                    ) from ex
            return rc, mid

        # Case 2: paho v1 (rc, mid) tuple (or longer).
        if isinstance(result, tuple) and len(result) >= 1:
            try:
                rc = int(result[0])
            except (TypeError, ValueError) as ex:
                raise MqttPublishError(
                    topic=topic,
                    reason=MqttPublishReason.UNSUPPORTED_RESULT,
                    result_type=type(result).__name__,
                    detail="invalid rc",
                ) from ex
            mid = None
            if len(result) >= 2 and result[1] is not None:
                try:
                    mid = int(result[1])
                except (TypeError, ValueError) as ex:
                    raise MqttPublishError(
                        topic=topic,
                        reason=MqttPublishReason.UNSUPPORTED_RESULT,
                        rc=rc,
                        result_type=type(result).__name__,
                        detail="invalid mid",
                    ) from ex
            return rc, mid

        # Case 3: unsupported shape (None, arbitrary object).
        raise MqttPublishError(
            topic=topic,
            reason=MqttPublishReason.UNSUPPORTED_RESULT,
            result_type=type(result).__name__,
        )

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
