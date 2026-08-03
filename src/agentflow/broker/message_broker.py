from abc import ABC, abstractmethod
from .notifier import BrokerNotifier


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
    def publish(self, topic:str, payload):
        """Publish the topic."""
        
    
    @abstractmethod
    def subscribe(self, topic:str, data_type):
        """Subscribe the topic."""


    def unsubscribe(self, topic:str) -> None:
        """Release a prior subscription. Default no-op so that existing
        MessageBroker subclasses remain valid. Concrete brokers that
        maintain a real subscription table should override."""
        return None
