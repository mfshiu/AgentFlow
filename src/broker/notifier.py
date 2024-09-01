from abc import ABC, abstractmethod


class BrokerNotifier(ABC):
        
    def __init__(self):
        pass
        
    
    @abstractmethod
    def _on_connect(self):        
        """Called when connected to the broker."""
        
    
    @abstractmethod
    def _on_message(self, topic:str, payload):        
        """Called when a message comes in."""
