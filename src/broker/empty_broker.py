import logging

from broker.message_broker import MessageBroker
from broker.notifier import BrokerNotifier
import system_config


logger = logging.getLogger(system_config.LOGGER_NAME)


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
