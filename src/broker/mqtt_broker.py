from paho.mqtt.client import Client

from .. import system_config
from broker.message_broker import MessageBroker
from broker.notifier import BrokerNotifier
import logging


logger = logging.getLogger(system_config.LOGGER_NAME)


class MqttBroker(MessageBroker):
    def __init__(self, notifier:BrokerNotifier):
        self._client = Client()
        self.host = ""
        self.port = 0
        self.keepalive = 0

        super().__init__(notifier=notifier)


    def _on_connect(self, client:Client, userdata, flags, rc):
        logger.info(f"MQTT broker connected. url: {self.host}, port: {self.port}, keepalive: {self.keepalive}")
        # logger.debug(f"Client: {client}\nuserdata:{userdata}\nflags: {flags}\nrc: {rc}")
        self._notifier._on_connect()


    def _on_message(self, client:Client, db, message):
        try:
            self._notifier._on_message(message.topic, message.payload)
        except Exception as ex:
            logger.exception(ex)



    ###################################
    # Implementation of MessageBroker #
    ###################################
    
    
    def start(self, options:dict):
        logger.info(f"MQTT broker is starting...")
        
        self._client.on_connect = self._on_connect
        self._client.on_message = self._on_message
        if username := options.get("username"):
            self._client.username_pw_set(username, options.get("password"))
        
        self.host = options.get("host")
        self.port = options.get("port")
        self.keepalive = options.get("keepalive")
        # logger.debug(f"host:{host} port:{port} keep:{keepalive}")
        self._client.connect(self.host, self.port, self.keepalive)
        
        self._client.loop_start()


    def stop(self):
        logger.info(f"MQTT broker is stopping...")
        
        self._client.disconnect()
        self._client.loop_stop()


    def publish(self, topic:str, payload):
        return self._client.publish(topic=topic, payload=payload)
        
    
    def subscribe(self, topic:str, data_type):
        return self._client.subscribe(topic=topic)
