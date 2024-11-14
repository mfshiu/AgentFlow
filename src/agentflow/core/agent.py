import inspect
import json
from logging import Logger
import queue
import threading
import time 
from typing import final
import uuid

from ..broker import BrokerType
from ..broker.notifier import BrokerNotifier
from ..broker.broker_maker import BrokerMaker
from . import config
from .config import EventHandler
from .agent_worker import Worker, ProcessWorker, ThreadWorker


logger:Logger = __import__('agentflow').get_logger()



class Agent(BrokerNotifier):
    def __init__(self, name:str, agent_config:dict={}):
        print(agent_config)
        logger.debug(f'name: {name}, agent_config: {agent_config}')
        
        self.agent_id = str(uuid.uuid4()).replace("-", "")
        logger.debug(f'agent_id: {self.agent_id}')
        self.__init_config(agent_config)
        self.name = name
        self.tag = f'{self.agent_id[:4]}'
        self.name_tag = f'{name}:{self.tag}'
        self.parent_name = name.rsplit('.', 1)[0] if '.' in name else None
        self.interval_seconds = 0
        self.__agent_worker: Worker = None
        
        self._children: dict = {}
        self._parents: dict = {}
        
        self._message_broker = None
        self.__topic_handlers: dict[str, function] = {}



# ==================
#  Agent Initializing
# ==================

        
    def __init_config(self, agent_config):
        self.config = config.default_config.copy()
        self.config.update(agent_config)
        logger.debug(f'self.config: {self.config}')
        
        # for event in EventHandler:
        #     setattr(self, str(event).lower(), self.get_config(event, getattr(self, str(event).lower(), None)))
        # self.on_activate = self.get_config(EventHandler.ON_ACTIVATE, self.on_activate)
        # self.on_children_message = self.get_config(EventHandler.ON_CHILDREN_MESSAGE, self.on_children_message)
        # self.on_message = self.get_config(EventHandler.ON_MESSAGE, self.on_message)
        # self.on_parents_message = self.get_config(EventHandler.ON_PARENTS_MESSAGE, self.on_parents_message)
        # self.on_register_child = self.get_config(EventHandler.ON_REGISTER_CHILD, self.on_register_child)
        # self.on_register_parent = self.get_config(EventHandler.ON_REGISTER_PARENT, self.on_register_parent)


    def start(self):
        logger.info(self.M(f"self.config: {self.config}"))
        co_type = self.config.get(config.CONCURRENCY_TYPE, 'process')
        logger.info(self.M(f"CONCURRENCY_TYPE: {co_type}"))
        if 'process' == co_type:
            self.start_process()
        else:
            self.start_thread()
        
        
    def start_process(self):
        logger.info(self.M(f'start_process'))
        self.__agent_worker = ProcessWorker(self)
        self.__agent_worker.start()
        
        
    def start_thread(self):
        logger.info(self.M(f'start_thread'))
        self.__agent_worker = ThreadWorker(self)
        self.__agent_worker.start()


    def terminate(self):
        logger.info(self.M(f"self.__agent_worker: {self.__agent_worker}"))
        
        if self.__agent_worker:
            self.__agent_worker.stop()
        else:
            logger.warning(self.M(f"The agent might not have started yet."))
        
        # if self.__agent_worker:
        #     self.__agent_worker.stop()
        # else:
        #     self._terminate()



# ==================
#  Agent Activating
# ==================
    def get_config(self, key:str, default=None):
        return self.config.get(key, default)
        
    
    def set_config(self, key:str, value):
        self.config[key] = value
        
    
    def get_config2(self, key:str, key2:str, default=None):
        return self.config[key].get(key2, default)
        
    
    def set_config2(self, key:str, key2:str, value):
        self.config[key][key2] = value


    def is_active(self):
        return self.__agent_worker.is_working()
    
    
    def on_activate(self):
        # logger.verbose(self.M("Hello!"))
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


    def __activating(self):
        logger.verbose(self.M("begin"))
        self.__data = {}
        self.__data_lock = threading.Lock()

        self.on_begining()

        # Create broker
        broker_config = self.get_config("broker", {'broker_type': BrokerType.Empty})
        logger.debug(self.M(f"broker_config: {broker_config}"))
        self._broker = BrokerMaker().create_broker(BrokerType(broker_config['broker_type'].lower()), self)
        is_success = True
        try:
            logger.debug(self.M("Ready to start broker.."))
            self._broker.start(options=broker_config)
        except ConnectionRefusedError:
            logger.error(self.M("Broker startup failed."))
            is_success = False
        except Exception as ex:
            logger.exception(ex)
            logger.error(self.M("Broker startup failed."))
            is_success = False
                
        logger.verbose(self.M("end"))
        return is_success


    def _activate(self, config):
        logger.verbose(self.M('Begin'))
        self.config = config
        self.terminate_event = config['terminate_event']
        
        if self.__activating():
            sig = inspect.signature(self.on_activate)
            if len(sig.parameters) == 0:
                logger.verbose(self.M("Invoke on_activate 1"))
                self.on_activate()
            elif isinstance(sig.parameters.get('self'), Agent):
                logger.verbose(self.M("Invoke on_activate 2"))
                self.on_activate(self)
            else:
                logger.verbose(self.M("Invoke on_activate 3"))
                self.on_activate(self.config)

            # Waiting for termination.
            logger.info(self.M("Running.."))
            work_queue = config['work_queue']
            while not self.terminate_event.is_set():
                try:
                    data = work_queue.get(timeout=1)
                    self._on_worker_data(data)
                except queue.Empty:
                    continue
                except KeyboardInterrupt:
                    self._terminate()
        else:
            self.terminate_event.set()

        self.__deactivating()
        logger.verbose(self.M('End'))
        
        
    def _on_worker_data(self, data):
        logger.info(self.M(data))
        if 'terminate' == data:
            self._terminate()
            
            
    def _terminate(self):
        logger.info(self.M('Terminating..'))
        
        self._notify_children('terminate')
        def stop():
            time.sleep(1)
            self.terminate_event.set()
        threading.Thread(target=stop).start()          


    def __deactivating(self):        
        logger.verbose(f"begin")
        self.on_terminating()
            
        self._broker.stop()
        
        self.on_terminated()
        logger.verbose(f"end")
        

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
        logger.verbose(self.M(f"topic: {topic}, data: {data}"))
        
        if isinstance(data, dict):
            try:
                data = json.dumps(data)
                self._broker.publish(topic, data)
            except Exception as ex:
                logger.exception(ex)
        else:
            self._broker.publish(topic, data)


    @final
    def _subscribe(self, topic, data_type:str="str", topic_handler=None):
        logger.debug(self.M(f"topic: {topic}, data_type:{data_type}"))
        
        if not isinstance(data_type, str):
            raise TypeError(f"Expected data_type to be of type 'str', but got {type(data_type).__name__}. The subscribtion of topic '{topic}' is failed.")
        
        if topic_handler:
            self.__topic_handlers[topic] = topic_handler
        return self._broker.subscribe(topic, data_type)
    
    
    def __register_child(self, child_id:str, child_info:dict):
        child_info['parent_id'] = self.agent_id
        self._children[child_id] = child_info
        logger.info(self.M(f"Add a child: {child_id}, total: {len(self._children)}"))
        self.on_register_child(child_id, child_info)


    def on_register_child(self, child_id, child_info:dict):
        logger.verbose(f"child_id: {child_id}, child_info: {child_info}")
    
    
    def __register_parent(self, parent_id:str, parent_info):
        parent_info['child_id'] = self.agent_id
        self._parents[parent_id] = parent_info
        logger.info(self.M(f"Add a parent: {parent_id}, total: {len(self._parents)}"))
        self.on_register_parent(parent_id, parent_info)


    def on_register_parent(self, parent_id, parent_info):
        logger.verbose(f"parent_id: {parent_id}, parent_info: {parent_info}")
    
    
    def _handle_children(self, topic, data):
        logger.debug(f"topic: {topic}, data type: {type(data)}, data: {data}")
       
        info = json.loads(data.decode('utf-8', 'ignore'))
        # {
        #     'child_id': agent_id,
        #     'child_name': child.name,
        #     'subject': subject,
        #     'data': data,
        #     'target_parents': [parent_id, ..] # optional
        # }
        
        if target_parents := info.get('target_parents'):
            if self.agent_id not in target_parents:
                return
        child_id = info.get('child_id')

        if "register_child" == info['subject']:
            self.__register_child(child_id, info)
            self._notify_child(child_id, 'register_parent')
            
        self.on_children_message(topic, info)


    def on_children_message(self, topic, info):
        logger.verbose(f"topic: {topic}, info: {info}")
    
    
    def _handle_parents(self, topic, data):
        logger.debug(self.M(f"topic: {topic}, data type: {type(data)}, data: {data}"))
        
        info = json.loads(data.decode('utf-8', 'ignore'))
        # {
        #     'parent_id': agent_id,
        #     'subject': subject,
        #     'data': data,
        #     'target_children': [child_id, ..]
        # }
        if target_children := info.get('target_children'):
            if not self.agent_id in target_children:
                return  # Not in the target children.
        
        if "terminate" == info['subject']:
            self._terminate()
        elif "register_parent" == info['subject']:
            self.__register_parent(info.get('parent_id'), info)
            
        self.on_parents_message(topic, info)


    def on_parents_message(self, topic, info):
        logger.verbose(f"topic: {topic}, info: {info}")
    
    
    def _notify_child(self, child_id, subject, data=None):
        logger.debug(f"child_id: {child_id}, subject: {subject}, data: {data}")

        if self._children and child_id in self._children:
            self._publish(f'{child_id}.to_child.{self.name}', {
                'parent_id': self.agent_id,
                'subject': subject,
                'data': data
            })
        else:
            logger.error("The child does not exist.")
    
    
    def _notify_children(self, subject, data=None, target_children=None, target_child_name=None):
        logger.debug(self.M(f"subject: {subject}, data: {data}"))
        
        if not self._children:
            logger.verbose(self.M('No child.'))
            return
        
        topic = f'to_child.{self.name}'
        data_send = {
            'parent_id': self.agent_id,
            'subject': subject,
            'data': data,
            }

        if target_children:
            logger.debug(self.M(f"target_children: {target_children}"))
            data_send['target_children'] = target_children
        
        if target_child_name:
            logger.debug(self.M(f"target_child_name: {target_child_name}"))
            topic = f'to_child.{target_child_name}'

        self._publish(topic, data_send)
    
    
    def _notify_parent(self, parent_id, subject, data=None):
        logger.debug(self.M(f"parent_id: {parent_id}, subject: {subject}, data: {data}"))

        if self._parents and parent_id in self._parents:
            self._publish(f'{parent_id}.to_parent.{self.parent_name}', {
                'child_id': self.agent_id,
                'subject': subject,
                'data': data
            })
        else:
            logger.error("The parent does not exist.")
    
    
    def _notify_parents(self, subject, data=None, target_parents=None):
        logger.debug(f"subject: {subject}, data: {data}")
        
        if self.parent_name:
            self._publish(f'to_parent.{self.parent_name}', data={
                'child_id': self.agent_id,
                'child_name': self.name,
                'subject': subject,
                'data': data,
                'target_parents': target_parents
            })
        else:
            logger.error(f"No any parent.")
        
        
    def _on_connect(self):
        for event in EventHandler:
            attr_name = str(event).lower()[len('EventHandler.'):]
            setattr(self, attr_name, self.get_config(event, getattr(self, attr_name, None)))

        self._subscribe(f'to_parent.{self.name}', topic_handler=self._handle_children)  # All the parents were notified by the children.
        self._subscribe(f'{self.agent_id}.to_parent.{self.name}', topic_handler=self._handle_children)  # I was the only parent notified by a child.  
        
        # logger.verbose(f"self.parent_name: {self.parent_name}")
        if self.parent_name:
            self._subscribe(f'to_child.{self.parent_name}', topic_handler=self._handle_parents) # All the children were notified by the parents.
            self._subscribe(f'to_child.{self.name}', topic_handler=self._handle_parents)    # All the children with the same name were notified by the parents.
            self._subscribe(f'{self.agent_id}.to_child.{self.parent_name}', topic_handler=self._handle_parents)    # Only this child notified by a parent.
            self._notify_parents("register_child")
        
        try:
            self.on_connected()
        except Exception as ex:
            logger.exception(ex)


    @final
    def _on_message(self, topic:str, data):
        logger.verbose(self.M(f"topic: {topic}, __topic_handlers: {self.__topic_handlers}"))
        
        topic_handler = self.__topic_handlers.get(topic, self.on_message)
        if topic_handler:
            topic_handler(topic, data)
        else:
            self.on_message(topic, data)


    def on_connected(self):
        logger.debug(self.M('on_connected'))


    def on_message(self, topic:str, data):
        pass
        
        
    def M(self, message=None):
        return f'{self.name_tag} {message}' if message else self.name_tag
            
