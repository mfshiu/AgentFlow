import json
import pickle


VERSION = "2"


class Parcel():
    def __init__(self, content=None, home_topic=None):
        self.managed_data = dict()
        self.content = None
        self.home_topic:str = None
        self.wrap = self.__wrap_json

        self.__set_managed_data({
            'version': VERSION,
            'content': content,
            'home_topic': home_topic
        })        
        
        
    def __set_managed_data(self, managed_data):
        self.managed_data = managed_data
        self.content = managed_data.get('content')
        self.home_topic = managed_data.get('home_topic')
        self.wrap = self.__wrap_binary if self.content and Parcel.__is_binary_content(self.content) else self.__wrap_json
        
        
    def __is_binary_content(content):
        return isinstance(content, bytes) or isinstance(content, bytearray)
        
        
    def __wrap_binary(self):
        return pickle.dumps(self.managed_data)
        
        
    def __wrap_json(self):
        return json.dumps(self.managed_data)
    
    
    def from_bytes(payload):
        parcel = Parcel()
        parcel.load_bytes(payload)
        return parcel
    
    
    def from_text(payload):
        parcel = Parcel()
        parcel.load_text(payload)
        return parcel
    
    
    def load_bytes(self, payload):
        self.__set_managed_data(pickle.loads(payload))


    def load_text(self, payload):
        self.__set_managed_data(json.loads(payload))


    def payload(self):
        return self.wrap()
    
    
    def get(self, key, default=None):
        return self.managed_data.get(key, default)
    
    
    def set(self, key, value):
        self.managed_data[key] = value
