import asyncio
import inspect
import logging
import queue
import threading
import time 
from tkinter import N
from typing import final, Optional
import uuid

from agentflow.core.parcel import Parcel
from agentflow.broker import BrokerType
from agentflow.broker.notifier import BrokerNotifier
from agentflow.broker.broker_maker import BrokerMaker
from agentflow.core import config
from agentflow.core.config import EventHandler
from agentflow.core.agent_worker import Worker, ProcessWorker, ThreadWorker


import logging, os
logger = logging.getLogger(os.getenv('LOGGER_NAME'))



class Agent(BrokerNotifier):
    def __init__(self, name:str, agent_config:dict={}):
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
        self._agent_worker: Optional["Worker"] = None

        self._children: dict = {}
        self._parents: dict = {}
        
        self._message_broker = None
        self.__topic_handlers: dict[str, function] = {}
        
        self._broker = None
        

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
        if not self._agent_worker:
            self._agent_worker = self.__create_worker()
        return self._agent_worker


    def start(self):
        if not config.CONCURRENCY_TYPE in self.config:
            self.config[config.CONCURRENCY_TYPE] = 'process'
        logger.info(self.M(f"self.config: {self.config}"))
        self.work_process = self._get_worker().start()
        
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
        logger.info(self.M(f"self.__agent_worker: {self._agent_worker}"))
        
        if self._agent_worker:
            self._agent_worker.stop()
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
        if isinstance(config2:=self.config[key], dict):
            return config2.get(key2, default)
        else:
            raise TypeError(f"Expected config[{key}] to be a dict, but got {type(config2).__name__}. Returning default value: {default}.")
        
    
    def set_config2(self, key:str, key2:str, value):
        if isinstance(config2:=self.config[key], dict):
            config2.setdefault(key2, value)
        else:
            raise TypeError(f"Expected config[{key}] to be a dict, but got {type(config2).__name__}.")


    def is_active(self):
        return self._agent_worker is not None and self._agent_worker.is_working()


    def on_activating(self):
        pass
    
    
    def on_activate(self, config=None):
        pass


    def on_terminating(self):
        pass


    def on_terminated(self):
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
        self.__data = {}
        self.__data_lock = threading.Lock()
        self.__connected_event = threading.Event()

        self.on_activating()

        # Create broker with retry
        broker_config_all = self.get_config("broker", {'broker_type': BrokerType.Empty})
        if not broker_config_all or not isinstance(broker_config_all, dict):
            logger.error(self.M("Broker configuration is missing or invalid."))
            return False
        logger.debug(self.M(f"broker_config_all: {broker_config_all}"))
        broker_name = broker_config_all['broker_name']
        broker_config = broker_config_all[broker_name]
        
        retry_count = 0
        max_retries = 1  # 可根據需求調整或設為 None 表示無限重試
        retry_interval = 5  # 每次重試間隔秒數

        is_success = False
        while not is_success and (max_retries is None or retry_count < max_retries):
            try:
                logger.debug(self.M("Creating broker..."))
                self._broker = BrokerMaker().create_broker(
                    BrokerType(broker_config['broker_type'].lower()), self)
                logger.debug(self.M("Ready to start broker.."))
                self._broker.start(options=broker_config)
                is_success = True
            except ConnectionRefusedError as e:
                logger.error(self.M(f"Broker startup failed (ConnectionRefusedError). Retrying...\n{e}"))
            except Exception as e:
                logger.error(self.M(f"Broker startup failed. Retrying...\n{e}"))

            if not is_success:
                logger.debug(self.M("Waiting for retry..."))
                retry_count += 1
                for _ in range(retry_interval):
                    if self.__terminate_event.is_set():
                        return False
                    time.sleep(1)

        if is_success:
            self.__connected_event.wait()
            logger.info(self.M("Broker started successfully."))
        else:
            logger.error(self.M(f"Broker startup failed after {max_retries} retries."))

        return is_success


    def _activate(self, config):
        self.config = config
        self.__terminate_event = threading.Event()

        if self.__activating():
            sig = inspect.signature(self.on_activate)
            if len(sig.parameters) == 0:
                self.on_activate()
            elif isinstance(sig.parameters.get('self'), Agent):
                self.on_activate(self)
            else:
                self.on_activate(self.config)

            # Waiting for termination.
            logger.info(self.M("Running.."))
            work_queue = config['work_queue']
            while not self.__terminate_event.is_set():
                try:
                    data = work_queue.get(timeout=1)
                    self._on_worker_data(data)
                except queue.Empty:
                    continue
                except KeyboardInterrupt:
                    self._terminate()
        else:
            self.__terminate_event.set()

        self.__deactivating()
        
        
    def _on_worker_data(self, data):
        logger.info(self.M(data))
        if 'terminate' == data:
            self._terminate()
            
            
    def _terminate(self):
        logger.warning(self.M('Terminating..'))
        
        self._notify_children('terminate')
        def stop():
            time.sleep(1)
            self.__terminate_event.set()
        threading.Thread(target=stop).start()          


    def __deactivating(self):        
        self.on_terminating()
            
        if self._broker:
            self._broker.stop()
        
        self.on_terminated()
        

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
        self.__data_lock.acquire()
        self.__data[key] = data
        self.__data_lock.release()


# =====================
#  Publish / Subscribe
# =====================
    # class DataEvent:
    #     def __init__(self):
    #         try:
    #             self.loop = asyncio.get_running_loop()
    #             self.future: asyncio.Future = self.loop.create_future()
    #             self.async_mode = True
    #         except RuntimeError:
    #             self.event = threading.Event()
    #             self.data = None
    #             self.async_mode = False

    #     def set_data(self, data):
    #         if self.async_mode:
    #             if not self.future.done():
    #                 self.future.set_result(data)
    #         else:
    #             self.data = data
    #             self.event.set()

    #     def wait(self, timeout: float = 30):
    #         if self.async_mode:
    #             return asyncio.wait_for(self.future, timeout)
    #         else:
    #             if self.event.wait(timeout):
    #                 return self.data
    #             else:
    #                 raise TimeoutError("No response received within timeout period.")
    class DataEvent:
        def __init__(self, event=None):
            import threading
            self.event = event if event is not None else threading.Event()
            self.data: 'Parcel' = None  # type: ignore



    @final
    def publish(self, topic, data=None):
        pcl = data if isinstance(data, Parcel) else Parcel.from_content(data)      
        try:
            if self._broker:
                self._broker.publish(topic, pcl.payload())
            else:
                logger.error("Cannot publish: _broker is None.")
        except Exception as ex:
            logger.exception(ex)

        
    def __generate_return_topic(self, topic):
        return f'{self.tag}-{int(time.time()*1000)}/{topic}'


    # def publish_sync(self, topic, data=None, topic_wait=None, timeout=30) -> 'Parcel':
    #     if isinstance(data, Parcel):
    #         pcl = data
    #         if pcl.topic_return:
    #             if topic_wait:
    #                 logger.warning(f"The passed parameter topic_wait: {topic_wait} has been replaced with '{pcl.topic_return}'.")
    #         elif topic_wait:
    #             pcl.topic_return = topic_wait
    #         else:
    #             pcl.topic_return = self.__generate_return_topic(topic)
    #     else:
    #         pcl = Parcel.from_content(data)
    #         pcl.topic_return = topic_wait if topic_wait else self.__generate_return_topic(topic)

    #     data_event = Agent.DataEvent()

    #     def handle_response(topic_resp, pcl_resp: Parcel):
    #         data_event.set_data(pcl_resp)

    #     self.subscribe(pcl.topic_return, topic_handler=handle_response)
    #     self.publish(topic, pcl)

    #     result = data_event.wait(timeout)

    #     if asyncio.iscoroutine(result):
    #         # 非同步模式，需執行 coroutine 並取得結果
    #         loop = asyncio.get_event_loop()
    #         result = loop.run_until_complete(result)

    #     if not isinstance(result, Parcel):
    #         raise TimeoutError(f"No response received within timeout period for topic: {pcl.topic_return}.")
    #     return result    
    @final
    def publish_sync(self, topic, data=None, topic_wait=None, timeout=30)->Parcel:
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

        def handle_response(topic_resp, pcl_resp:Parcel):
            # logger.verbose(self.M(f"topic_resp: {topic_resp}, data_resp: {str(pcl_resp)[:400]}.."))
            data_event.data = pcl_resp
            data_event.event.set()

        self.subscribe(pcl.topic_return, topic_handler=handle_response)
        self.publish(topic, pcl)

        if data_event.event.wait(timeout):
            return data_event.data
        else:
            raise TimeoutError(f"No response received within timeout period for topic: {pcl.topic_return}.")


    @final
    def subscribe(self, topic, data_type:str="str", topic_handler=None):
        logger.debug(self.M(f"topic: {topic}, data_type:{data_type}"))
        
        if not isinstance(data_type, str):
            raise TypeError(f"Expected data_type to be of type 'str', but got {type(data_type).__name__}. The subscribtion of topic '{topic}' is failed.")
        
        if topic_handler:
            if topic in self.__topic_handlers:
                logger.warning(self.M(f"Exist the handler for topic: {topic}"))
            self.__topic_handlers[topic] = topic_handler

        return self._broker.subscribe(topic, data_type) if self._broker else None
    
    
    def __register_child(self, child_id:str, child_info:dict):
        child_info['parent_id'] = self.agent_id
        self._children[child_id] = child_info
        logger.info(self.M(f"Add a child: {child_id}, total: {len(self._children)}"))
        self.on_register_child(child_id, child_info)


    def on_register_child(self, child_id, child_info:dict):
        pass
    
    
    def __register_parent(self, parent_id:str, parent_info):
        parent_info['child_id'] = self.agent_id
        self._parents[parent_id] = parent_info
        logger.info(self.M(f"Add a parent: {parent_id}, total: {len(self._parents)}"))
        self.on_register_parent(parent_id, parent_info)


    def on_register_parent(self, parent_id, parent_info):
        pass
    
    
    def _handle_children(self, topic, pcl:Parcel):
        child: dict = pcl.content if isinstance(pcl.content, dict) else {}
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

        if child_id:=child.get('child_id'):
            if "register_child" == child['subject']:
                self.__register_child(child_id, child)
                self._notify_child(child_id, 'register_parent')
            
        return self.on_children_message(topic, child)


    def on_children_message(self, topic, info):
        pass
    
    
    def _handle_parents(self, topic, pcl:Parcel):
        parent: dict = pcl.content if isinstance(pcl.content, dict) else {}
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
        
        if "terminate" == parent.get('subject'):
            self._terminate()
        elif "register_parent" == parent.get('subject'):
            if parent_id := parent.get('parent_id'):
                self.__register_parent(parent_id, parent)
            
        return self.on_parents_message(topic, parent)


    def on_parents_message(self, topic, parent):
        pass
    
    
    def _notify_child(self, child_id, subject, data=None):
        logger.debug(f"child_id: {child_id}, subject: {subject}, data: {data}")

        if self._children and child_id in self._children:
            self.publish(f'{child_id}.to_child.{self.name}', {
                'parent_id': self.agent_id,
                'subject': subject,
                'data': data
            })
        else:
            logger.error("The child does not exist.")
    
    
    def _notify_children(self, subject, data=None, target_children=None, target_child_name=None):
        logger.debug(self.M(f"subject: {subject}, data: {data}"))
        
        if not self._children:
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

        self.publish(topic, data_send)
    
    
    def _notify_parent(self, parent_id, subject, data=None):
        logger.debug(self.M(f"parent_id: {parent_id}, subject: {subject}, data: {data}"))

        if self._parents and parent_id in self._parents:
            self.publish(f'{parent_id}.to_parent.{self.parent_name}', {
                'child_id': self.agent_id,
                'subject': subject,
                'data': data
            })
        else:
            logger.error("The parent does not exist.")
    
    
    def _notify_parents(self, subject, data=None, target_parents=None):
        logger.debug(f"subject: {subject}, data: {data}")
        
        if self.parent_name:
            self.publish(f'to_parent.{self.parent_name}', data={
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
            setattr(self, attr_name, self.get_config(str(event), getattr(self, attr_name, None)))

        self.subscribe(f'to_parent.{self.name}', topic_handler=self._handle_children)  # All the parents were notified by the children.
        self.subscribe(f'{self.agent_id}.to_parent.{self.name}', topic_handler=self._handle_children)  # I was the only parent notified by a child.  
        
        # logger.verbose(f"self.parent_name: {self.parent_name}")
        if self.parent_name:
            self.subscribe(f'to_child.{self.parent_name}', topic_handler=self._handle_parents) # All the children were notified by the parents.
            self.subscribe(f'to_child.{self.name}', topic_handler=self._handle_parents)    # All the children with the same name were notified by the parents.
            self.subscribe(f'{self.agent_id}.to_child.{self.parent_name}', topic_handler=self._handle_parents)    # Only this child notified by a parent.
            self._notify_parents("register_child")

        def handle_connected():
            time.sleep(1)
            self.__connected_event.set()
            self.on_connected()
        threading.Thread(target=handle_connected).start()


    @final
    def _on_message(self, topic:str, data):
        # logger.debug(self.M(f"topic: {topic}, data: {data}"))        
        pcl = Parcel.from_payload(data)

        topic_handler = self.__topic_handlers.get(topic, self.on_message)
        
        def handle_message(topic_handler, topic, p:Parcel):
            if p.topic_return:
                try:
                    logger.debug(f"topic: {topic}, topic_return: {p.topic_return}")
                    data_resp = topic_handler(topic, p)
                except Exception as ex:
                    logger.exception(ex)
                    p.error = str(ex)
                    p.content = None
                    data_resp = p
                    logger.debug(data_resp)
                finally:
                    self.publish(pcl.topic_return, data_resp)
            else:
                try:
                    topic_handler(topic, p)
                except Exception as ex:
                    logger.exception(ex)

        threading.Thread(target=handle_message, args=(topic_handler, topic, pcl)).start()


    def on_connected(self):
        logger.debug(self.M('on_connected'))


    def on_message(self, topic:str, data):
        pass
        
        
    def M(self, message=None):
        return f'{self.name_tag} {message}' if message else self.name_tag
            
