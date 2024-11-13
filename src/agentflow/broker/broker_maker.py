from . import BrokerType
from .empty_broker import EmptyBroker
from .message_broker import MessageBroker
from .mqtt_broker import MqttBroker
from .redis_broker import RedisBroker
from .ros_broker import RosBroker
# from broker.ros_noetic_broker import RosNoeticBroker
from .notifier import BrokerNotifier


from logging import Logger
logger:Logger = __import__('agentflow').get_logger()



class BrokerMaker():
    def create_broker(self, broker_type:BrokerType, notifier:BrokerNotifier) -> MessageBroker:
        logger.debug(f'broker_type: {broker_type}')
        
        if broker_type is BrokerType.Redis:
            return RedisBroker(notifier)
        elif broker_type is BrokerType.MQTT:
            return MqttBroker(notifier)
        elif broker_type is BrokerType.ROS:
            return RosBroker(notifier)
        # elif broker_type is BrokerType.ROS:
        #     return RosNoeticBroker(notifier)
        elif broker_type is BrokerType.Empty:
            return EmptyBroker(notifier)
        else:
           raise TypeError(f"Unsupported broker type: {type(broker_type).__name__}.") 
        