from abc import ABC, abstractmethod
from broker.notifier import BrokerNotifier


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
