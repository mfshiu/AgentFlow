import json
import pickle


VERSION = "1"


class Parcel():
    def __init__(self, content=None):
        self.managed_data = {
            'version': VERSION,
            'content': content,
            'home_topic': None
        }
        
        self.wrap = self.__wrap_binary if content and Parcel.__is_binary_content(content) else self.__wrap_json
        
        
    def __is_binary_content(content):
        return isinstance(content, bytes) or isinstance(content, bytearray)
        
        
    def __wrap_binary(self):
        return pickle.dumps(self.managed_data)
        
        
    def __wrap_json(self):
        return json.dumps(self.managed_data)
    
    
    def load_bytes(self, payload):
        self.managed_data = pickle.loads(payload)  
        self.wrap = self.__wrap_binary


    def load_text(self, payload):
        self.managed_data = json.loads(payload)  
        self.wrap = self.__wrap_json


    def payload(self):
        return self.wrap()
