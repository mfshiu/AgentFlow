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
from logistic.base_logistic import BaseLogistic
import system_config


logger = logging.getLogger(system_config.LOGGER_NAME)


class Agent(BrokerNotifier):
    def __init__(self, config:dict):
        self.agent_id = str(uuid.uuid4()).replace("-", "")
        self.config = config
        self.name = f'<{self.__class__.__name__}>'
        self.head_agents = []
        self.body_agents = []
        self.logistics = {}
        
        self._message_broker = None
        self._run_interval_seconds = 1
        self._topic_handlers = {}


    @final
    def register_logistic(self, logistic:BaseLogistic):
        if not logistic.job_topic:
            raise Exception("Logistic must define the job topic.")
        self.logistics[logistic.job_topic] = logistic

        
    @final
    def start_process(self):
        self.__agent_proc = Process(target=self.__activate, args=(self.config,))
        self.__agent_proc.start()
        
        for a in self.head_agents:
            a.start()
        for a in self.body_agents:
            a.start()

        
    @final
    def start_thread(self):
        self.__agent_proc = threading.Thread(target=self.__activate, args=(self.config,))
        self.__agent_proc.start()
        
        for a in self.head_agents:
            a.start()
        for a in self.body_agents:
            a.start()


# ===========================
#  Body of Process or Thread
# ===========================
        
        
    def get_broker_type(self) -> BrokerType:
        if broker_type := self.get_config("broker_type"):
            return BrokerType(broker_type.lower())
        else:
            return BrokerType.Empty
        
    
    def get_config(self, key:str, default=None):
        return self.config.get(key, default)
        
    
    def set_config(self, key:str, value):
        self.config[key] = value


    def __activate(self, config):
        self.config = config
        
        self.__run_begin()
        self.__running()
        self.__run_end()


    def __run_begin(self):

        # Handle Ctrl-C to terminate agents
        def signal_handler(signal, frame):
            logger.warning(f"{self.short_id}> {self.name} Ctrl-C: {self.__class__.__name__}")
            self.terminate()
        if self.__agent_proc:
            signal.signal(signal.SIGINT, signal_handler)

        self.__databox = {}
        self.__databox_lock = threading.Lock()
        self.__terminate_lock = threading.Event()
        short_id = self.agent_id[:-6]
            
        self.on_begining()
        
        # Create broker
        logger.debug(f"{short_id}> Create broker.")
        if broker_type := self.get_broker_type():
            self._broker = BrokerMaker().create_broker(broker_type, self)
            self._broker.start(options=self.config)
        
        # Start interval loop
        logger.debug(f"{short_id}> Start interval loop.")
        def interval_loop():
            while not self.__terminate_lock.is_set():
                self.on_interval()
                time.sleep(self.__run_interval_seconds)
        threading.Thread(target=interval_loop).start()
            
        self.on_began()


    def is_running(self):
        return not self._terminate_lock.is_set()


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
    