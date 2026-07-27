from paho.mqtt.client import Client
from paho.mqtt.enums import CallbackAPIVersion
import logging, os, threading
from typing import Any, Dict, Optional
logger = logging.getLogger(os.getenv('LOGGER_NAME'))

from .message_broker import MessageBroker
from .notifier import BrokerNotifier


class MqttBroker(MessageBroker):
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
        # _stopping, _last_disconnect_was_planned, and the recovery
        # metric counters. The lock is NEVER held across a paho client
        # call (subscribe / unsubscribe / publish / disconnect / loop_stop).
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

        logger.info(f"MQTT broker initialized with notifier: {notifier}, wait={wait}, timeout={timeout}")
        super().__init__(notifier=notifier)

    def _on_connect(self, client, userdata, flags, reasonCode, properties):
        if reasonCode == 0:
            self._connect_ok = True
            self._connect_err = None
            logger.info(f"MQTT broker connected: {self.host}:{self.port}, keepalive={self.keepalive}")
            try:
                # RFC-005 §7.5-7.7: check stopping, then classify
                # first-connect vs reconnect and snapshot the registry
                # for recovery. All state under _state_lock; client
                # calls (recovery + notifier) run OUTSIDE the lock.
                snapshot: Dict[str, Any] = {}
                skip = False
                with self._state_lock:
                    if self._stopping:
                        logger.info(
                            "on_connect after stop; skipping recovery and notifier"
                        )
                        skip = True
                    else:
                        is_first = not self._ever_connected
                        self._ever_connected = True
                        self._connected = True
                        if not is_first:
                            snapshot = dict(self._registry)
                if skip:
                    return
                if snapshot:
                    self._recover_subscriptions(snapshot)
                self._notifier._on_connect()
            finally:
                self._connected_evt.set()
        else:
            self._connect_ok = False
            with self._state_lock:
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
        # RFC-005 §6.4: classify planned vs unexpected; clear
        # connection state. The desired-registry is preserved so that
        # a subsequent reconnect can restore it.
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
        try:
            self._notifier._on_message(message.topic, message.payload)
        except Exception as ex:
            logger.exception(ex)


    def start(self, options: dict):
        logger.info("MQTT broker is starting...")

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
            return True

        if not self._connected_evt.wait(self._timeout):
            logger.error(f"MQTT connect timeout after {self._timeout}s")
            self._client.loop_stop()
            self._client.disconnect()
            raise TimeoutError(f"MQTT connect timeout ({self.host}:{self.port})")

        if not self._connect_ok:
            self._client.loop_stop()
            self._client.disconnect()
            raise ConnectionError(f"MQTT connect failed: {self._connect_err}")

        return True

    def stop(self):
        # RFC-005 §6.5: flip _stopping under lock BEFORE the paho
        # disconnect call so any callback that arrives inline observes
        # the stopping state and short-circuits.
        with self._state_lock:
            self._stopping = True
        logger.warning("MQTT broker is stopping...")
        self._client.disconnect()
        self._client.loop_stop()

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
