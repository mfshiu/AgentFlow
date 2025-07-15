import logging, os
logger = logging.getLogger(os.getenv('LOGGER_NAME'))

from .message_broker import MessageBroker
from .notifier import BrokerNotifier


class EmptyBroker(MessageBroker):
    def __init__(self, notifier:BrokerNotifier):
        super().__init__(notifier=notifier)


    ###################################
    # Implementation of MessageBroker #
    ###################################
    
    
    def start(self, options:dict):
        logger.info(f"Empty broker is starting. options:{options}")
       

    def stop(self):
        logger.info(f"Empty broker is stopping...")


    def publish(self, topic:str, payload):
        logger.info(f"topic: {topic}, payload: {payload}")
        
    
    def subscribe(self, topic:str, data_type):
        logger.info(f"topic: {topic}, data_type: {data_type}")
