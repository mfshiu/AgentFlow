import inspect
from logging import Logger
from multiprocessing import Process
import signal
import threading
import time 
from typing import final
import uuid

logger:Logger = __import__('agentflow').get_logger()
from ..broker import BrokerType
from ..broker.notifier import BrokerNotifier
from ..broker.broker_maker import BrokerMaker
from . import config


class Agent(BrokerNotifier):

    def __init__(self, agent_config:dict={}):
        logger.debug(f'self: {self}')
        self.agent_id = str(uuid.uuid4()).replace("-", "")
        self.__init_config(agent_config)
        self.name = f'<{self.__class__.__name__}>'
        self.interval_seconds = 0
        
        self._message_broker = None
        self.__topic_handlers: dict[str, function] = {}
        
        
    def __init_config(self, agent_config):
        self.config = config.default_config.copy()
        self.config.update(agent_config)
        logger.debug(f'self.config: {self.config}')
        
        if proc := self.get_config(config.ON_ACTIVATE):
            self.on_activate = proc
        
        
    @final
    def start(self):
        logger.debug(f'self: {self}')
        logger.debug(f'self.config: {self.config}')
        logger.verbose(f"__activate: {self._activate}")
        logger.verbose(f"on_activate: {self.on_activate}")
        
        if 'process' == self.config.get(config.START_METHOD, 'process'):
            self.__agent_proc = Process(target=self._activate, args=(self.config,))
        else:
            self.__agent_proc = threading.Thread(target=self._activate, args=(self.config,))
            
        self.__agent_proc.start()


# ==================
#  Agent Activating
# ==================
    def get_broker_type(self) -> BrokerType:
        if broker_type := self.get_config("broker_type"):
            return BrokerType(broker_type.lower())
        else:
            return BrokerType.Empty
        
    
    def get_config(self, key:str, default=None):
        return self.config.get(key, default)
        
    
    def set_config(self, key:str, value):
        self.config[key] = value


    def is_active(self):
        return self.__terminate_lock and not self.__terminate_lock.is_set()


    def on_activate(self):
        pass


    def on_terminating(self):
        pass


    def on_terminated(self):
        pass


    def on_begining(self):
        pass


    def on_began(self):
        pass


    def on_interval(self):
        pass
        
        
    def start_interval_loop(self, interval_seconds):
        logger.debug(f"{self.agent_id}> Start interval loop.")
        self.interval_seconds = interval_seconds

        def interval_loop():
            while self.is_active() and self.interval_seconds > 0:
                self.on_interval()
                time.sleep(self.interval_seconds)
            self.interval_seconds = 0
        threading.Thread(target=interval_loop).start()
        
        
    def stop_interval_loop(self):
        self.interval_seconds = 0


    def _activate(self, config):
        self.config = config
        
        # self.__activating()
        
        sig = inspect.signature(self.on_activate)
        if len(sig.parameters) == 0:
            self.on_activate()
        elif isinstance(sig.parameters.get('self'), Agent):
            self.on_activate(self)
        else:
            self.on_activate(self.config)

        # self.__deactivating()


    def __activating(self):
        # Handle Ctrl-C to terminate agents
        def signal_handler(signal, frame):
            logger.warning(f"{self.short_id}> {self.name} Ctrl-C: {self.__class__.__name__}")
            self.terminate()
        if self.__agent_proc:
            signal.signal(signal.SIGINT, signal_handler)

        self.__data = {}
        self.__data_lock = threading.Lock()
        self.__terminate_lock = threading.Event()
            
        self.on_begining()
        
        # Create broker
        logger.debug(f"{self.agent_id}, Create broker.")
        if broker_type := self.get_broker_type():
            self._broker = BrokerMaker().create_broker(broker_type, self)
            self._broker.start(options=self.config)


    def __deactivating(self):        
        self.on_terminating()
        
        while self.is_active():
            self.__terminate_lock.wait(1)
            
        self._broker.stop()
        
        self.on_terminated()
        

    @final
    def terminate(self):
        logger.warning(f"{self.agent_id}, {self.name}.")
        self.__terminate_lock.set()
        

# ============
#  Agent Data 
# ============
    @final
    def get_data(self, key:str):
        return self.__data.get(key)


    @final
    def pop_data(self, key:str):
        data = None
        self.__data_lock.acquire()
        if key in self.__data:
            data = self.__data.pop(key)
        self.__data_lock.release()
        return data


    @final
    def put_data(self, key:str, data):
        self.__data.acquire()
        self.__data[key] = data
        self.__data.release()


# =====================
#  Publish / Subscribe
# =====================
    @final
    def _publish(self, topic, data=None):
        topic_concrete = self.__get_outbound_topic()
        return self._broker.publish(topic_concrete, data)


    @final
    def _subscribe(self, topic, data_type="str", topic_handler=None):
        topic_concrete = f'{topic}.{self._parent.agent_id}' if self._parent else topic
        if topic_handler:
            self.__topic_handlers[topic_concrete] = topic_handler
        return self._broker.subscribe(topic_concrete, data_type)
        
        
    def _on_connect(self):
        self._subscribe("system.terminate", topic_handler=self.terminate)
        self.on_connected()


    @final
    def _on_message(self, topic:str, data):
        topic_handler = self._topic_handlers.get(topic, self.on_message)
        topic_handler(topic, data)


    def on_connected(self):
        pass


    def on_message(self, topic:str, data):
        pass
    