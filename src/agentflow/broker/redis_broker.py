logger = __import__('agentflow').get_logger()
from .message_broker import MessageBroker
from .notifier import BrokerNotifier


class RedisBroker(MessageBroker):
    def __init__(self, notifier:BrokerNotifier):
        logger.info(f"Initialize...")
        
        super().__init__(notifier=notifier)



    ###################################
    # Implementation of MessageBroker #
    ###################################
    
    
    def start(self, options:dict):
        logger.info(f"Redis broker is starting...")
       

    def stop(self):
        logger.info(f"Redis broker is stopping...")


    def publish(self, topic:str, payload):
        logger.info(f"topic: {topic}, payload: {payload}")
        
    
    def subscribe(self, topic:str, data_type):
        logger.info(f"topic: {topic}")
