from paho.mqtt.client import Client
from paho.mqtt.enums import CallbackAPIVersion
import logging, os, threading
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

        logger.info(f"MQTT broker initialized with notifier: {notifier}, wait={wait}, timeout={timeout}")
        super().__init__(notifier=notifier)

    def _on_connect(self, client, userdata, flags, reasonCode, properties):
        if reasonCode == 0:
            self._connect_ok = True
            self._connect_err = None
            logger.info(f"MQTT broker connected: {self.host}:{self.port}, keepalive={self.keepalive}")
            try:
                self._notifier._on_connect()
            finally:
                self._connected_evt.set()
        else:
            self._connect_ok = False
            name = getattr(reasonCode, "getName", lambda: str(reasonCode))()
            self._connect_err = f"{reasonCode} ({name})"
            logger.error(f"MQTT connection failed: {self._connect_err}")
            self._connected_evt.set()


    def _on_disconnect(self, client, userdata, _flags, reasonCode, _properties):
        logger.warning(f"MQTT disconnected: {reasonCode}")


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
        logger.warning("MQTT broker is stopping...")
        self._client.disconnect()
        self._client.loop_stop()

    def publish(self, topic: str, payload):
        return self._client.publish(topic=topic, payload=payload)

    def subscribe(self, topic: str, data_type):
        return self._client.subscribe(topic=topic)

    def unsubscribe(self, topic: str):
        return self._client.unsubscribe(topic)
