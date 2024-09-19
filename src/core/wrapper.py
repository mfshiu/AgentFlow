from abc import ABC, abstractmethod
import json
import pickle


class Wrapper(ABC):
    @abstractmethod
    def wrap(data):
        pass
    
    
    @abstractmethod
    def unwrap(data):
        pass
    
    
    
class BinaryWrapper(Wrapper):
    def wrap(managed_data) -> bytes:
        return pickle.dumps(managed_data)
        
        
    def unwrap(data:bytes) -> dict:
        managed_data = pickle.loads(data)
        return managed_data
    
    
    
class TextWrapper(Wrapper):
    def wrap(data) -> str:
        managed_data = {
            "version": VERSION,
            "content": data,
        }
        return json.dumps(managed_data)
        
        
    def unwrap(data:str) -> dict:
        managed_data = json.loads(data)
        return managed_data
