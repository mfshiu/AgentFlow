import logging, os
from agentflow.logging import log_publish

logger = logging.getLogger(os.getenv('LOGGER_NAME'))

from .message_broker import MessageBroker
from .notifier import BrokerNotifier


class RosBroker(MessageBroker):
    def __init__(self, notifier:BrokerNotifier):
        logger.info(f"Initialize...")
        
        super().__init__(notifier=notifier)



    ###################################
    # Implementation of MessageBroker #
    ###################################
    
    
    def start(self, options:dict):
        logger.info(f"Ros broker is starting...")
       

    def stop(self):
        logger.info(f"Ros broker is stopping...")


    def publish(self, topic:str, payload, *, qos: int = 0, retain: bool = False):
        log_publish(logger, topic, payload)
        
    
    def subscribe(self, topic:str, data_type, *, qos: int = 0):
        logger.info(f"topic: {topic}")
