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


    def _on_disconnect(self, planned: bool) -> None:
        """Called when the connection to the broker is lost (issue #2).
        `planned` is True for an orderly disconnect. Brokers that
        reconnect automatically call `_on_connect` again once the
        connection is restored. Default no-op so that existing
        BrokerNotifier subclasses remain valid."""
        return None
