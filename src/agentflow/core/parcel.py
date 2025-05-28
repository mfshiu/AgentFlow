from abc import ABC, abstractmethod
import json
import pickle

VERSION = 3


def ensure_size(text: str, max_length: int = 300) -> str:
    """
    Ensure a text size is less than or equal to the specified max length.
    If the text exceeds the max length, return a truncated version ending with '..'.

    Args:
        text (str): The input string.
        max_length (int): The maximum allowed length of the string. Default is 300.

    Returns:
        str: The original text if within the limit, otherwise a truncated version.
    """
    if text:
        if len(text) <= max_length:
            return text
        else:
            return text[:max_length - 2] + '..'
    else:
        return text



class Parcel(ABC):
    def __init__(self, content=None, topic_return=None):
        self.version = VERSION
        self.content = content
        self.topic_return = topic_return
        self.error = None


    def __str__(self):
        def _convert_content(content)-> str:
            """ Convert the content of the parcel to a string representation.
            This method handles different types of content, including bytes, dicts, and lists.
            Args:
                content: The content to convert, which can be of various types.
            Returns:
                str: A string representation of the content, ensuring it is readable.
            """
            if isinstance(content, bytes):
                try:
                    str_content = ensure_size(content.decode('utf-8', 'replace'))
                except Exception as e:
                    str_content = f"len(<binary data>): {len(content)}, error: {e}"  # Fallback message for undecodable bytes
            elif isinstance(content, dict):
                str_content = {key: _convert_content(value) for key, value in content.items()}
            elif isinstance(content, list):
                str_content = [_convert_content(item) for item in content]

            return str(str_content)
        
        
        return json.dumps({
            "version": self.version,
            "content": ensure_size(_convert_content(self.content)),
            "topic_return": self.topic_return,
            "error": self.error
        }, indent=4, ensure_ascii=False)


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
            'topic_return': self.topic_return,
            'error': self.error
        }


    def _set_managed_data(self, managed_data):
        self.version = managed_data['version']
        self.content = managed_data['content']
        self.topic_return = managed_data['topic_return']
        self.error = managed_data['error']


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

