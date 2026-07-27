import inspect
import logging
import pickle
import queue
import random
import string
import threading
import time
import warnings
from dataclasses import dataclass
from enum import Enum
from tkinter import N
from typing import Callable, final, Optional
import uuid

from agentflow.core.parcel import Parcel
from agentflow.broker import BrokerType
from agentflow.broker.notifier import BrokerNotifier
from agentflow.broker.broker_maker import BrokerMaker
from agentflow.core import config
from agentflow.core.config import EventHandler
from agentflow.core.agent_worker import Worker, ProcessWorker, ThreadWorker
from agentflow.core.dispatcher import MessageDispatcher, LegacyPerMessageDispatcher


import logging, os
logger = logging.getLogger(os.getenv('LOGGER_NAME'))



class TopicWaitCollisionError(RuntimeError):
    """Raised when a topic is already reserved by an active
    publish_sync waiter and another operation would trample it
    (RFC-006, RFC-007).

    Contexts that raise this exception:
      - publish_sync-vs-publish_sync collision (RFC-006)
      - publish_sync on a topic already held by a normal subscribe
        handler (RFC-007)
      - Agent.subscribe on a topic reserved by an active publish_sync
        waiter (RFC-007)
      - Agent.unsubscribe on a topic reserved by an active
        publish_sync waiter (RFC-007)
    The exception message distinguishes the context."""


class _HandlerOwnerType(Enum):
    """Internal (RFC-007): owner tag for an Agent handler registry
    entry. NORMAL is registered via Agent.subscribe; PUBLISH_SYNC is
    the transient closure registered by Agent.publish_sync while it
    waits for a response."""
    NORMAL = "normal"
    PUBLISH_SYNC = "publish_sync"


@dataclass(frozen=True)
class _HandlerRecord:
    """Internal (RFC-007): value type for Agent.__topic_handlers.
    Bundles the caller-provided handler with an owner tag so that
    Agent.subscribe / Agent.unsubscribe can refuse to trample an
    active publish_sync waiter."""
    owner_type: _HandlerOwnerType
    handler: Callable



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
        # RFC-006: guards atomic collision-check-and-register and
        # atomic identity-check-and-pop for publish_sync. Uses RLock
        # for future-proofing (RFC-006 Appendix B).
        self._handlers_lock = threading.RLock()

        self._broker = None
        self._connected_once = False

        # RFC-004: bounded message dispatch. Dispatcher is created
        # lazily on first _on_message so that tests / lifecycles that
        # never receive a message do not pay for consumer threads.
        # DeprecationWarning for the legacy escape hatch fires eagerly
        # so users see it at Agent construction time even before any
        # message flows.
        self._dispatcher = None
        self._dispatcher_init_lock = threading.Lock()
        if self.config.get('dispatch', {}).get('mode') == 'per_message_thread':
            warnings.warn(
                "Agent dispatch mode 'per_message_thread' is deprecated "
                "(RFC-004). This escape hatch will be removed in a future "
                "release; migrate to the bounded dispatcher (the default).",
                DeprecationWarning,
                stacklevel=2,
            )


# ==================
#  Agent Pickle protocol (RFC-008 — ProcessWorker spawn compatibility)
# ==================

    # Attributes excluded from pickle. Locks and Broker / Dispatcher /
    # back-reference to Worker are runtime-only and either not
    # picklable (RLock, paho Client, threading.Event) or meaningless
    # in a child process context.
    _RUNTIME_ONLY_FIELDS = frozenset({
        '_handlers_lock',
        '_dispatcher_init_lock',
        '_dispatcher',
        '_broker',
        '_agent_worker',
        '_message_broker',
        '_children',
        '_parents',
    })

    def __getstate__(self):
        """RFC-008 §A: return a picklable, declarative-only snapshot
        of the Agent. Runtime resources (locks, broker, dispatcher,
        worker back-reference, children/parents runtime registry) are
        excluded; they will be reinstated fresh by __setstate__ in
        the unpickling process (typically a spawned child).

        Fails fast (TypeError) if the config or any registered handler
        cannot be pickled. The message names the offending topic(s)
        so callers can move handler registration into on_activate().
        """
        state = self.__dict__.copy()
        for field in Agent._RUNTIME_ONLY_FIELDS:
            state.pop(field, None)

        # Validate config is picklable so start-time failure surfaces
        # here with a helpful message rather than a bare pickle error
        # deep inside Process.start().
        try:
            pickle.dumps(state.get('config', {}))
        except Exception as ex:
            raise TypeError(
                f"Agent.config contains non-picklable content and cannot "
                f"be shipped to a spawned child process: {ex}. Move "
                f"non-picklable configuration (closures, lambdas, live "
                f"objects) into on_activate() so it is created inside "
                f"the child."
            ) from ex

        # Validate every registered handler is picklable; fail fast
        # naming the offending topic(s). Never silently omit handlers.
        handlers_key = '_Agent__topic_handlers'
        handlers = state.get(handlers_key, {}) or {}
        offending_topics = []
        for topic, record in handlers.items():
            handler = record.handler if hasattr(record, 'handler') else record
            try:
                pickle.dumps(handler)
            except Exception:
                offending_topics.append(topic)
        if offending_topics:
            raise TypeError(
                f"Agent.__topic_handlers contains non-picklable "
                f"handlers for topic(s) "
                f"{sorted(str(t) for t in offending_topics)!r}; cannot "
                f"ship to a spawned child process. Register these "
                f"handlers inside on_activate() (which runs in the "
                f"child) rather than in the parent, so they are "
                f"created locally in the child rather than pickled "
                f"across the process boundary."
            )
        return state

    def __setstate__(self, state):
        """RFC-008 §A: restore declarative state and reinstate all
        runtime-only fields with fresh instances. Called in the child
        after unpickle. Preserves RFC-006/RFC-007 ownership shape:
        _HandlerRecord entries carry their owner_type across the
        pickle boundary; a fresh RLock guards the registry in the
        child process."""
        self.__dict__.update(state)
        self._handlers_lock = threading.RLock()
        self._dispatcher_init_lock = threading.RLock()
        self._dispatcher = None
        self._broker = None
        self._agent_worker = None
        self._message_broker = None
        self._children = {}
        self._parents = {}
        if '_Agent__topic_handlers' not in self.__dict__:
            self._Agent__topic_handlers = {}


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

        # RFC-004: stop the dispatcher first so consumer threads can
        # drain their queue while the broker is still up. Idempotent —
        # safe to call from both terminate() and __deactivating().
        if self._dispatcher is not None:
            self._dispatcher.stop()

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
        
        retry = 0
        max_retries = 3
        interval = 5
        while retry < max_retries and not self.__terminate_event.is_set():
            try:
                self._broker = BrokerMaker().create_broker(
                    BrokerType(broker_config['broker_type'].lower()), self
                )
                # 等待連線成功或失敗（Max 10秒）
                self._broker.start(options=broker_config)
                logger.info(self.M("Broker started successfully."))
                return True
            except (TimeoutError, ConnectionError) as e:
                retry += 1
                logger.error(self.M(f"Broker start failed ({retry}/{max_retries}): {e}"))
                if retry >= max_retries: break
                for _ in range(interval):
                    if self.__terminate_event.is_set(): return False
                    time.sleep(1)
            except Exception as e:
                logger.exception(self.M(f"Unexpected error starting broker: {e}"))
                return False

        logger.error(self.M("Broker startup failed after retries."))
        return False

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

            logger.info(self.M("Waiting for termination..."))
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
    class DataEvent:
        def __init__(self, event=None):
            import threading
            self.event = event if event is not None else threading.Event()
            self.data: 'Parcel' = None  # type: ignore



    @final
    def publish(self, topic, data=None):
        # Fire-and-forget contract: catch every Exception so callers who
        # ignore the return value never see a broker-side failure. To
        # get the raise-on-failure variant, use publish_sync (which
        # goes through _publish_or_raise) or call _publish_or_raise
        # directly.
        try:
            self._publish_or_raise(topic, data)
        except Exception as ex:
            logger.exception(ex)


    def _publish_or_raise(self, topic, data=None) -> None:
        """Internal strict publish. Wraps `data` as a Parcel and forwards
        it to the broker. Unlike Agent.publish, propagates every broker
        exception to the caller and raises RuntimeError when no broker
        is attached. Used by publish_sync to enable fast-fail semantics.
        Marked with a single leading underscore to signal that this is
        internal-use only; API stability is not guaranteed."""
        pcl = data if isinstance(data, Parcel) else Parcel.from_content(data)
        if self._broker is None:
            raise RuntimeError("Cannot publish: no broker attached")
        self._broker.publish(topic, pcl.payload())

        
    def __generate_return_topic(self, topic):
        alphabet = string.digits + string.ascii_lowercase
        rand = ''.join(random.choice(alphabet) for _ in range(10))
        return f"{self.tag}-{rand}/{topic}"

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
            # Duplicate arriving before cleanup: keep the first response.
            if data_event.event.is_set():
                return
            data_event.data = pcl_resp
            data_event.event.set()

        # RFC-006 + RFC-007: atomic collision check + register under
        # _handlers_lock. Any pre-existing record (NORMAL or
        # PUBLISH_SYNC) is a collision; distinguish in the exception
        # message. Register as PUBLISH_SYNC owner so subscribe/unsubscribe
        # from other callers can refuse to trample the waiter (RFC-007
        # §7.3, §7.4).
        with self._handlers_lock:
            existing = self.__topic_handlers.get(pcl.topic_return)
            if existing is not None:
                if existing.owner_type is _HandlerOwnerType.PUBLISH_SYNC:
                    raise TopicWaitCollisionError(
                        f"topic_wait {pcl.topic_return!r} is already awaited "
                        f"by another publish_sync on this Agent"
                    )
                # NORMAL owner: publish_sync must not trample it.
                raise TopicWaitCollisionError(
                    f"topic_wait {pcl.topic_return!r} is already registered "
                    f"by a normal subscribe handler; publish_sync would "
                    f"trample it and is refused"
                )
            self.__topic_handlers[pcl.topic_return] = _HandlerRecord(
                _HandlerOwnerType.PUBLISH_SYNC, handle_response,
            )

        try:
            # broker.subscribe outside the collision lock (RFC-005
            # principle: never hold a framework lock across broker I/O).
            if self._broker:
                self._broker.subscribe(pcl.topic_return, "str")
            # _publish_or_raise propagates broker exceptions so that the
            # caller fails fast on publish errors instead of waiting for
            # the full response timeout (RFC-002).
            self._publish_or_raise(topic, pcl)
            if data_event.event.wait(timeout):
                return data_event.data
            raise TimeoutError(f"No response received within timeout period for topic: {pcl.topic_return}.")
        finally:
            # RFC-006 §7.5 + RFC-007 §7.8: triple check under lock —
            # record exists, owner is PUBLISH_SYNC, handler identity
            # matches. Only then pop and unsubscribe. broker.unsubscribe
            # outside the lock so we never hold a framework lock across
            # broker I/O.
            with self._handlers_lock:
                record = self.__topic_handlers.get(pcl.topic_return)
                if (record is not None
                        and record.owner_type is _HandlerOwnerType.PUBLISH_SYNC
                        and record.handler is handle_response):
                    self.__topic_handlers.pop(pcl.topic_return, None)
                    need_broker_unsubscribe = True
                else:
                    need_broker_unsubscribe = False
            if need_broker_unsubscribe:
                try:
                    if self._broker:
                        self._broker.unsubscribe(pcl.topic_return)
                except Exception as cleanup_ex:
                    logger.exception(cleanup_ex)


    @final
    def subscribe(self, topic, data_type:str="str", topic_handler=None):
        logger.debug(self.M(f"topic: {topic}, data_type:{data_type}"))

        if not isinstance(data_type, str):
            raise TypeError(f"Expected data_type to be of type 'str', but got {type(data_type).__name__}. The subscribtion of topic '{topic}' is failed.")

        if topic_handler:
            # RFC-007 §7.3: refuse to trample an active publish_sync
            # waiter; preserve warn+overwrite for normal rebind
            # (RFC-006 §7.11 preserved).
            with self._handlers_lock:
                existing = self.__topic_handlers.get(topic)
                if existing is not None and existing.owner_type is _HandlerOwnerType.PUBLISH_SYNC:
                    raise TopicWaitCollisionError(
                        f"topic {topic!r} is currently reserved by an "
                        f"active publish_sync waiter; direct subscribe "
                        f"is refused"
                    )
                if existing is not None:
                    logger.warning(self.M(f"Exist the handler for topic: {topic}"))
                self.__topic_handlers[topic] = _HandlerRecord(
                    _HandlerOwnerType.NORMAL, topic_handler,
                )

        # broker.subscribe outside the lock (RFC-005/006 lock hygiene).
        return self._broker.subscribe(topic, data_type) if self._broker else None


    @final
    def unsubscribe(self, topic: str) -> None:
        """Reverse a prior subscribe(topic, topic_handler=...) call.
        Removes the handler entry from __topic_handlers if present and
        asks the broker to unsubscribe. Idempotent: calling twice or on
        an unknown topic does not raise. Safe to call when the broker
        has not been created yet (_broker is None).

        RFC-007 §7.4: refuses to remove a publish_sync-owned entry;
        raises TopicWaitCollisionError instead.
        """
        # RFC-007: lock + owner check + pop under lock.
        with self._handlers_lock:
            existing = self.__topic_handlers.get(topic)
            if existing is not None and existing.owner_type is _HandlerOwnerType.PUBLISH_SYNC:
                raise TopicWaitCollisionError(
                    f"topic {topic!r} is currently reserved by an "
                    f"active publish_sync waiter; direct unsubscribe "
                    f"is refused"
                )
            self.__topic_handlers.pop(topic, None)
        # broker.unsubscribe outside the lock.
        if self._broker:
            self._broker.unsubscribe(topic)
    
    
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
        if self._connected_once:
            logger.warning(self.M("Already connected to the broker."))
            return
        self._connected_once = True
        logger.info(self.M("Connected to the broker."))

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

        # RFC-007 §7.7: atomic single-snapshot read of the handler
        # registry under _handlers_lock. is_specific_handler and
        # topic_handler are decided from ONE snapshot to eliminate the
        # pre-RFC-007 TOCTOU between the 'in' check and the '.get()'
        # call. Dispatch happens outside the lock via the RFC-004
        # dispatcher.
        with self._handlers_lock:
            record = self.__topic_handlers.get(topic)
            if record is not None:
                is_specific_handler = True
                topic_handler = record.handler
            else:
                is_specific_handler = False
                topic_handler = self.on_message
        # RFC-003 R-fallback-silent: only topics with a specifically
        # registered handler in __topic_handlers may trigger an
        # auto-reply.
        should_auto_reply = bool(pcl.topic_return) and is_specific_handler

        def handle_message():
            if should_auto_reply:
                try:
                    logger.debug(f"topic: {topic}, topic_return: {pcl.topic_return}")
                    data_resp = topic_handler(topic, pcl)
                except Exception as ex:
                    logger.exception(ex)
                    # RFC-003 R-exception-fresh: build a new parcel for
                    # the error echo; do NOT mutate or reuse pcl.
                    err_pcl = Parcel.from_content(None)
                    err_pcl.error = str(ex)
                    data_resp = err_pcl
                    logger.debug(data_resp)
                # RFC-003 R-strip-topic_return: the auto-reply must
                # never carry topic_return. If the handler returned a
                # Parcel whose topic_return is truthy, reconstruct
                # instead of mutating the caller's object.
                if isinstance(data_resp, Parcel) and data_resp.topic_return:
                    stripped = type(data_resp)(data_resp.content)
                    stripped.error = data_resp.error
                    data_resp = stripped
                self.publish(pcl.topic_return, data_resp)
            else:
                try:
                    topic_handler(topic, pcl)
                except Exception as ex:
                    logger.exception(ex)

        # RFC-004: dispatch via the bounded message dispatcher instead
        # of spawning one Thread per message. enqueue() never raises
        # (broker-callback safety invariant, §7.3).
        self._get_dispatcher().enqueue(handle_message, topic=topic)


    def __create_dispatcher(self):
        """RFC-004: build the dispatcher configured for this Agent.
        Default is the bounded MessageDispatcher; the legacy
        per-message-thread mode is available as a deprecated escape
        hatch."""
        dispatch_cfg = self.config.get('dispatch', {}) or {}
        mode = dispatch_cfg.get('mode', 'bounded')
        if mode == 'per_message_thread':
            return LegacyPerMessageDispatcher()
        workers = int(dispatch_cfg.get('workers', 8))
        queue_capacity = int(dispatch_cfg.get('queue_capacity', 1024))
        shutdown_timeout_s = float(dispatch_cfg.get('shutdown_timeout_s', 5.0))
        return MessageDispatcher(
            workers=workers,
            queue_capacity=queue_capacity,
            shutdown_timeout_s=shutdown_timeout_s,
            name=f'MessageDispatcher-{self.tag}',
        )


    def _get_dispatcher(self):
        """Lazy, thread-safe dispatcher accessor. First call creates
        the dispatcher (and its consumer threads)."""
        if self._dispatcher is None:
            with self._dispatcher_init_lock:
                if self._dispatcher is None:
                    self._dispatcher = self.__create_dispatcher()
        return self._dispatcher


    def on_connected(self):
        logger.debug(self.M('on_connected'))


    def on_message(self, topic:str, data):
        pass
        
        
    def M(self, message=None):
        return f'{self.name_tag} {message}' if message else self.name_tag
            
