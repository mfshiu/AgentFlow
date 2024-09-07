import logging
from multiprocessing import Process
import signal
import threading
import time 
from typing import final
import uuid

from broker import BrokerType
from broker.notifier import BrokerNotifier
from broker.broker_maker import BrokerMaker
import system_config


logger = logging.getLogger(system_config.LOGGER_NAME)


class Agent(BrokerNotifier):
    def __init__(self, config:dict):
        self.agent_id = str(uuid.uuid4()).replace("-", "")
        self.config = config
        self.name = f'<{self.__class__.__name__}>'
        self.interval_seconds = 0
        
        self._message_broker = None
        self.__topic_handlers[str, function] = {}
        self._parent = None
        self._is_head = False

        self.__head_agents = []
        self.__body_agents = []
        
        
    @final
    def add_head_agent(self, agent:Agent):
        agent._set_parent(self, True)
        self.__head_agents.append(HeadAgent(agent))
        
        
    @final
    def add_body_agent(self, agent:Agent):
        agent._set_parent(self)
        self.__body_agents.append(agent)
    
    
    @final
    def _set_parent(self, parent:Agent, is_head=False):
        self._parent = parent
        self._is_head = True
        
        
    @final
    def start_process(self):
        self.__agent_proc = Process(target=self.__activate, args=(self.config,))
        self.__agent_proc.start()
        
        for a in self.__head_agents:
            a.start()
        for a in self.__body_agents:
            a.start()

        
    @final
    def start_thread(self):
        self.__agent_proc = threading.Thread(target=self.__activate, args=(self.config,))
        self.__agent_proc.start()
        
        for a in self.__head_agents:
            a.start()
        for a in self.__body_agents:
            a.start()


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


    def is_active(self):
        return not self.__terminate_lock.is_set()


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
        
    
    def set_config(self, key:str, value):
        self.config[key] = value
        
        
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


    def __activate(self, config):
        self.config = config
        
        self.__activating()
        self.on_active()
        self.__deactivating()


    def __activating(self):
        # Handle Ctrl-C to terminate agents
        def signal_handler(signal, frame):
            logger.warning(f"{self.short_id}> {self.name} Ctrl-C: {self.__class__.__name__}")
            self.terminate()
        if self.__agent_proc:
            signal.signal(signal.SIGINT, signal_handler)

        self.__databox = {}
        self.__databox_lock = threading.Lock()
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
        logger.warn(f"{self.agent_id}, {self.name}.")
        self.__terminate_lock.set()


# =====================
#  Publish / Subscribe
# =====================
    def __get_inbound_topic(self, topic):
        if self._parent:
            return f'{topic}.{self._parent.agent_id}'
        else:
            return topic


    def __get_outbound_topic(self, topic):
        if self._parent:
            if self._parent._parent:
                return f'{topic}.{self._parent._parent.agent_id}'
            else:
                return topic
        else:
            return topic


    @final
    def _pubout(self, topic, data=None):
        topic_concrete = self.__get_outbound_topic()
        return self._broker.publish(topic_concrete, data)


    @final
    def _subout(self, topic, data_type="str", topic_handler=None):
        topic_concrete = f'{topic}.{self._parent.agent_id}' if self._parent else topic
        if topic_handler:
            self.__topic_handlers[topic_concrete] = topic_handler
        return self._broker.subscribe(topic_concrete, data_type)


    @final
    def _pubin(self, topic, data=None):
        topic_concrete = f'{topic}.{self._parent.agent_id}' if self._parent else topic
        return self._broker.publish(topic_concrete, data)


    @final
    def _subin(self, topic, data_type="str", topic_handler=None):
        topic_concrete = f'{topic}.{self._parent.agent_id}' if self._parent else topic
        if topic_handler:
            self.__topic_handlers[topic] = topic_handler
        return self._broker.subscribe(topic_concrete, data_type)
        
        
    def _on_connect(self):
        pass
        

# ==========
#  Data Box
# ==========


    @final
    def get_data(self, key:str):
        return self.__databox.get(key)


    @final
    def pop_data(self, key:str):
        data = None
        self.__databox_lock.acquire()
        if key in self.__databox:
            data = self.__databox.pop(key)
        self.__databox_lock.release()
        return data


    @final
    def put_data(self, key:str, data):
        self.__databox.acquire()
        self.__databox[key] = data
        self.__databox.release()
    