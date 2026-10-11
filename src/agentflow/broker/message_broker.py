from abc import ABC, abstractmethod
from .notifier import BrokerNotifier


def check_qos(qos) -> int:
    """Issue #4: validate an MQTT QoS level (0, 1 or 2)."""
    if type(qos) is not int or qos not in (0, 1, 2):
        raise ValueError(f"QoS must be 0, 1 or 2, got {qos!r}")
    return qos


class MessageBroker(ABC):
    def __init__(self, notifier:BrokerNotifier):
        self._notifier = notifier
        
    
    @abstractmethod
    def start(self, options:dict):
        """Start the message broker."""
        
    
    @abstractmethod
    def stop(self):
        """Stop the message broker."""
        
    
    @abstractmethod
    def publish(self, topic:str, payload, *, qos: int = 0, retain: bool = False):
        """Publish the topic. `qos` (0, 1 or 2) and `retain` follow MQTT
        (issue #4); brokers without those notions accept and ignore
        them."""
        
    
    @abstractmethod
    def subscribe(self, topic:str, data_type, *, qos: int = 0):
        """Subscribe the topic, at `qos` where the broker supports it."""


    def unsubscribe(self, topic:str) -> None:
        """Release a prior subscription. Default no-op so that existing
        MessageBroker subclasses remain valid. Concrete brokers that
        maintain a real subscription table should override."""
        return None
