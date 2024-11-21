import inspect
import json
from logging import Logger
import queue
import threading
import time 
from typing import final
import uuid

from agentflow.core.parcel import Parcel

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
        # self.parent_name = name.rsplit('.', 1)[0] if '.' in name else None
        self.parent_name = name.split('.', 1)[1] if '.' in name else None
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
            
            
    def __create_worker(self):
        if 'process' == self.config[config.CONCURRENCY_TYPE]:
            return ProcessWorker(self)
        else:
            return ThreadWorker(self)
        
        
    def _get_worker(self):
        if not self.__agent_worker:
            self.__agent_worker = self.__create_worker()
        return self.__agent_worker


    def start(self):
        if not config.CONCURRENCY_TYPE in self.config:
            self.config[config.CONCURRENCY_TYPE] = 'process'
        logger.info(self.M(f"self.config: {self.config}"))
        self._get_worker().start()
        
        self._on_start()
        
        
    def _on_start(self):
        pass
        
        
    def start_process(self):
        self.config[config.CONCURRENCY_TYPE] = 'process'
        self.start()
        
        
    def start_thread(self):
        self.config[config.CONCURRENCY_TYPE] = 'thread'
        self.start()


    def terminate(self):
        logger.info(self.M(f"self.__agent_worker: {self.__agent_worker}"))
        
        if self.__agent_worker:
            self.__agent_worker.stop()
        else:
            logger.warning(self.M(f"The agent might not have started yet."))



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
    class DataEvent:
        def __init__(self, event):
            self.event = event
            self.data = None



    @final
    def _publish(self, topic, data=None):
        logger.verbose(self.M(f"topic: {topic}, data: {data}"))
        
        pcl = data if isinstance(data, Parcel) else Parcel.from_content(data)         
        try:
            self._broker.publish(topic, pcl.payload())
        except Exception as ex:
            logger.exception(ex)

        
    def __generate_return_topic(self, topic):
        return f'{self.tag}-{int(time.time()*1000)}/{topic}'


    @final
    def _publish_sync(self, topic, data, topic_wait=None, timeout=10):
        logger.verbose(self.M(f"topic: {topic}, data: {data}, topic_wait: {topic_wait}"))
        
        if isinstance(data, Parcel):
            pcl = data
            if pcl.topic_return:
                if topic_wait:
                    logger.warning(f"The passed parameter topic_wait: {topic_wait} has been replaced with '{pcl.topic_return}'.")
            elif topic_wait:
                pcl.topic_return = topic_wait
            else:
                pcl.topic_return = self.__generate_return_topic(topic)
        else:
            pcl = Parcel.from_content(data)
            pcl.topic_return = topic_wait if topic_wait else self.__generate_return_topic(topic)
                
        data_event = Agent.DataEvent(self._get_worker().create_event())

        def handle_response(topic_resp, data_resp):
            logger.verbose(self.M(f"topic_resp: {topic_resp}, data_resp: {data_resp}"))
            data_event.data = data_resp
            data_event.event.set()

        self._subscribe(pcl.topic_return, topic_handler=handle_response)
        self._publish(topic, pcl)

        logger.verbose(self.M(f"Waitting for event: {data_event}"))
        if data_event.event.wait(timeout):
            logger.verbose(self.M(f"Waitted the event: {data_event}, event.data: {data_event.data}"))
            return data_event.data
        else:
            raise TimeoutError(f"No response received within timeout period for topic: {pcl.topic_return}.")


    @final
    def _subscribe(self, topic, data_type:str="str", topic_handler=None):
        logger.debug(self.M(f"topic: {topic}, data_type:{data_type}"))
        
        if not isinstance(data_type, str):
            raise TypeError(f"Expected data_type to be of type 'str', but got {type(data_type).__name__}. The subscribtion of topic '{topic}' is failed.")
        
        if topic_handler:
            if topic in self.__topic_handlers:
                logger.warning(self.M(f"Exist the handler for topic: {topic}"))
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
    
    
    def _handle_children(self, topic, child:dict):
        logger.debug(f"topic: {topic}, child: {child}")
        # {
        #     'child_id': agent_id,
        #     'child_name': child.name,
        #     'subject': subject,
        #     'data': data,
        #     'target_parents': [parent_id, ..] # optional
        # }
        
        if target_parents := child.get('target_parents'):
            if self.agent_id not in target_parents:
                return
        child_id = child.get('child_id')

        if "register_child" == child['subject']:
            self.__register_child(child_id, child)
            self._notify_child(child_id, 'register_parent')
            
        return self.on_children_message(topic, child)


    def on_children_message(self, topic, info):
        logger.verbose(f"topic: {topic}, info: {info}")
    
    
    def _handle_parents(self, topic, parent):
        logger.debug(self.M(f"topic: {topic}, data type: {type(parent)}, data: {parent}"))
        # {
        #     'parent_id': agent_id,
        #     'subject': subject,
        #     'data': data,
        #     'target_children': [child_id, ..]
        # }
        
        if target_children := parent.get('target_children'):
            if not self.agent_id in target_children:
                return  # Not in the target children.
        
        if "terminate" == parent['subject']:
            self._terminate()
        elif "register_parent" == parent['subject']:
            self.__register_parent(parent.get('parent_id'), parent)
            
        return self.on_parents_message(topic, parent)


    def on_parents_message(self, topic, parent):
        logger.verbose(f"topic: {topic}, parent: {parent}")
    
    
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

        threading.Thread(target=self.on_connected).start()
        # try:
        #     self.on_connected()
        # except Exception as ex:
        #     logger.exception(ex)


    @final
    def _on_message(self, topic:str, data):
        logger.verbose(self.M(f"topic: {topic}, data: {data[:200]}.."))
        pcl = Parcel.from_payload(data)
        # logger.verbose(self.M(f"managed_data: {str(pcl.managed_data)[:200]}.."))

        topic_handler = self.__topic_handlers.get(topic, self.on_message)
        logger.verbose(self.M(f"Invoke handler: {topic_handler}"))
        
        def handle_message(topic_handler, topic, content):
            data_resp = topic_handler(topic, content)
            if pcl.topic_return:
                self._publish(pcl.topic_return, data_resp)
        threading.Thread(target=handle_message, args=(topic_handler, topic, pcl.content)).start()
        # if topic_handler:
        #     logger.verbose(self.M(f"Invoke handler: {topic_handler}"))
        #     threading.Thread(target=topic_handler, args=(topic, pcl.content)).start()
        #     # topic_handler(topic, data)
        # else:
        #     threading.Thread(target=self.on_message, args=(topic, pcl.content)).start()
        #     # self.on_message(topic, data)


    def on_connected(self):
        logger.debug(self.M('on_connected'))


    def on_message(self, topic:str, data):
        pass
        
        
    def M(self, message=None):
        return f'{self.name_tag} {message}' if message else self.name_tag
            
