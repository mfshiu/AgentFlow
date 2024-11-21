from abc import ABC, abstractmethod
import json
import pickle


VERSION = 3


class Parcel(ABC):
    def __init__(self, content=None, topic_return:str=None):
        self.version = VERSION
        self.content = content
        self.topic_return:str = topic_return


    @staticmethod
    def from_content(content) -> 'Parcel':
        is_binary = content and (isinstance(content, bytes) or isinstance(content, bytearray))
        if is_binary:
            return BinaryParcel(content)
        else:
            return TextParcel(content)
        
        
    @staticmethod
    def from_payload(payload):
        if BinaryParcel.is_payload(payload):
            pcl = BinaryParcel.from_payload(payload)
        elif TextParcel.is_payload(payload):
            pcl = TextParcel.from_payload(payload)
        else:
            raise TypeError('Not valid Parcel payload.')

        return pcl
    
    
    @staticmethod
    def is_payload(payload):
        return BinaryParcel.is_payload(payload) or TextParcel.is_payload(payload)
    
    
    def _get_managed_data(self):
        return {
            'version': VERSION,
            'content': self.content,
            'topic_return': self.topic_return
        }


    def _set_managed_data(self, managed_data):
        self.version = managed_data['version']
        self.content = managed_data['content']
        self.topic_return = managed_data['topic_return']


    @abstractmethod
    def payload(self):
        pass
    
    
    # Subscription operations
    
    def __getitem__(self, key):
        return self.get(key)


    def __setitem__(self, key, value):
        self.set(key, value)


    def get(self, key, default=None):
        if isinstance(self.content, dict):
            return self.content.get(key, default)
        else:
            raise TypeError("self.content is not a dictionary. Get operation is not allowed.")


    def set(self, key, value):
        if not self.content:
            self.content = dict()
            
        if isinstance(self.content, dict):
            self.content[key] = value
        else:
            raise TypeError("self.content is not a dictionary. Set operation is not allowed.")



class BinaryParcel(Parcel):
    HEAD = "application/pickle|"
    
    def __init__(self, content=None, topic_return=None):
        super().__init__(content, topic_return)       
    
    
    @staticmethod
    def from_payload(payload):
        pcl = BinaryParcel()
        pcl._set_managed_data(pickle.loads(payload[len(BinaryParcel.HEAD):]))
        return pcl
    
    
    @staticmethod
    def is_payload(payload):
        return payload and payload.startswith(BinaryParcel.HEAD.encode())


    def payload(self):
        return BinaryParcel.HEAD.encode() + pickle.dumps(self._get_managed_data())



class TextParcel(Parcel):
    HEAD = "text/json|"
    
    def __init__(self, content=None, topic_return=None):
        super().__init__(content, topic_return)       
        
        
    @staticmethod
    def from_payload(payload):
        pcl = TextParcel()
        pcl._set_managed_data(json.loads(payload[len(TextParcel.HEAD):]))
        return pcl
    
    
    @staticmethod
    def is_payload(payload):
        return payload and payload.startswith(TextParcel.HEAD.encode())


    def payload(self):
        return TextParcel.HEAD + json.dumps(self._get_managed_data())

