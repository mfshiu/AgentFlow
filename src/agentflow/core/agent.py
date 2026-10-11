import inspect
import logging
import math
import pickle
import queue
import re
import secrets
import string
import threading
import time
import warnings
from dataclasses import dataclass, field
from enum import Enum
from typing import Callable, final, Optional
import uuid

from agentflow.core import holon_auth
from agentflow.core.parcel import Parcel, parse_pickle_globals
from agentflow.broker import BrokerType
from agentflow.broker.notifier import BrokerNotifier
from agentflow.broker.broker_maker import BrokerMaker
from agentflow.core import config
from agentflow.core.config import EventHandler
from agentflow.core.agent_worker import Worker, ProcessWorker, ThreadWorker
from agentflow.core.dispatcher import MessageDispatcher, LegacyPerMessageDispatcher


import logging, os
logger = logging.getLogger(os.getenv('LOGGER_NAME'))

# Issue #1: keys accepted in the optional `codec` config section.
_CODEC_KEYS = frozenset({'accept_pickle', 'pickle_allowed_globals'})

# Issue #13: keys accepted in the optional `holon` config section.
_HOLON_KEYS = frozenset({
    'control_secret', 'control_max_age', 'max_children', 'trust_unregistered_parents',
})
_MIN_CONTROL_SECRET_BYTES = 16
_DEFAULT_CONTROL_MAX_AGE_S = 300.0
# Issue #13: a terminate from a parent that has not registered (yet)
# waits this long for that parent's register_parent; at most
# _MAX_PENDING_TERMINATES wait at a time.
_PENDING_TERMINATE_TTL_S = 10.0
_MAX_PENDING_TERMINATES = 8
# Issue #13: holon control topics, to_child.* / to_parent.* and their
# {agent_id}.-prefixed unicast forms.
_HOLON_CONTROL_TOPIC = re.compile(r'(?:[0-9a-f]{32}\.)?to_(?:child|parent)\.')


def _is_holon_control_topic(topic) -> bool:
    return isinstance(topic, str) and _HOLON_CONTROL_TOPIC.match(topic) is not None


@dataclass(frozen=True)
class _HolonOptions:
    """Issue #13: the parsed `holon` config section."""
    control_key: Optional[bytes] = field(default=None, repr=False)
    control_max_age: float = _DEFAULT_CONTROL_MAX_AGE_S
    max_children: Optional[int] = None
    trust_unregistered_parents: bool = False


def _config_for_log(cfg):
    """Issue #13: `cfg` with holon.control_secret masked, for logging.
    Other credentials in the config are #14."""
    holon = cfg.get('holon') if isinstance(cfg, dict) else None
    if isinstance(holon, dict) and holon.get('control_secret') is not None:
        return {**cfg, 'holon': {**holon, 'control_secret': '***'}}
    return cfg


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
        logger.debug(f'name: {name}, agent_config: {_config_for_log(agent_config)}')
        
        self.agent_id = str(uuid.uuid4()).replace("-", "")
        logger.debug(f'agent_id: {self.agent_id}')
        self.__init_config(agent_config)
        # Issue #1: inbound decoding policy, validated up front so a bad
        # `codec` config fails at construction rather than per message.
        self._codec_options = self.__parse_codec_config()
        # Issue #13: holon control-plane policy, validated the same way.
        self._holon_options = self.__parse_holon_config()
        self.name = name
        self.tag = f'{self.agent_id[:4]}'
        self.name_tag = f'{name}:{self.tag}'
        # self.parent_name = name.rsplit('.', 1)[0] if '.' in name else None
        self.parent_name = name.split('.', 1)[1] if '.' in name else None
        self.interval_seconds = 0
        self._agent_worker: Optional["Worker"] = None

        self._children: dict = {}
        self._parents: dict = {}
        # Issue #6: makes "is this a new member?" + insert atomic, so a
        # repeated registration never fires the hooks twice.
        self._holon_lock = threading.Lock()
        # Children whose first on_register_child is still running.
        self._children_registering: set = set()
        # Issue #5: _terminate runs its body once (test-and-set).
        self._terminate_lock = threading.Lock()
        self._terminating = False
        # Issue #13: terminates from parents that have not registered
        # yet: parent_id -> (deadline, topic, message).
        self._pending_terminates: dict = {}
        # Issue #13: refused holon messages per kind, for logging.
        self._holon_rejections: dict = {}

        self._message_broker = None
        self.__topic_handlers: dict[str, _HandlerRecord] = {}
        # RFC-006: guards atomic collision-check-and-register and
        # atomic identity-check-and-pop for publish_sync. Uses RLock
        # for future-proofing (RFC-006 Appendix B).
        self._handlers_lock = threading.RLock()

        self._broker = None
        self._connected_once = False
        # Issue #2: current broker link state, maintained by the
        # BrokerNotifier callbacks (_on_connect / _on_disconnect).
        self._broker_connected = False

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
        '_holon_lock',
        '_children_registering',
        '_terminate_lock',
        '_pending_terminates',
        '_holon_rejections',
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
        self._holon_lock = threading.Lock()
        self._children_registering = set()
        self._terminate_lock = threading.Lock()
        self._terminating = False
        self._pending_terminates = {}
        self._holon_rejections = {}
        self._broker_connected = False
        if '_Agent__topic_handlers' not in self.__dict__:
            self._Agent__topic_handlers = {}


# ==================
#  Agent Initializing
# ==================

    def __init_config(self, agent_config):
        self.config = config.default_config.copy()
        self.config.update(agent_config)
        logger.debug(f'self.config: {_config_for_log(self.config)}')


    def __parse_codec_config(self):
        """Issue #1: the optional `codec` config section.

          accept_pickle          (bool, default True) — False refuses
                                 BinaryParcel (pickle) payloads entirely.
          pickle_allowed_globals (list of 'module.name' / 'module:qualname')
                                 — extra globals BinaryParcel payloads may
                                 reference, e.g. ['datetime.datetime'].
                                 Only immutable types and builtin functions
                                 are honoured; Python-defined classes
                                 (Enum, dataclass, ...) are refused — send
                                 such values as plain data instead. Each
                                 allowed type's constructor is callable by
                                 any publisher, so list only types whose
                                 construction has no side effects.

        Read once at construction. Raises TypeError / ValueError on a
        malformed section, including unknown keys."""
        codec = self.config.get('codec')
        if codec is None:
            codec = {}
        if not isinstance(codec, dict):
            raise TypeError(
                f"Agent config 'codec' must be a dict, got {type(codec).__name__}"
            )
        unknown = set(codec) - _CODEC_KEYS
        if unknown:
            raise ValueError(
                f"Unknown Agent config 'codec' keys {sorted(map(str, unknown))}; "
                f"expected {sorted(_CODEC_KEYS)}"
            )
        accept_pickle = codec.get('accept_pickle', True)
        if not isinstance(accept_pickle, bool):
            raise TypeError(
                f"Agent config 'codec.accept_pickle' must be a bool, "
                f"got {accept_pickle!r}"
            )
        allowed = codec.get('pickle_allowed_globals')
        if allowed is None:
            allowed = ()
        if not isinstance(allowed, (list, tuple, set, frozenset)):
            raise TypeError(
                f"Agent config 'codec.pickle_allowed_globals' must be a list "
                f"of 'module.name' strings, got {allowed!r}"
            )
        return {
            'accept_pickle': accept_pickle,
            'pickle_allowed_globals': parse_pickle_globals(allowed),
        }


    def __parse_holon_config(self) -> _HolonOptions:
        """Issue #13: the optional `holon` config section.

          control_secret             (str or bytes, at least 16 bytes) —
                                     sign holon control messages
                                     (terminate, register_child,
                                     register_parent, parent_online) with
                                     HMAC-SHA256 and drop unsigned or
                                     forged ones. Every agent of the holon
                                     must share it. See holon_auth.
          control_max_age            (seconds, default 300) — how far a
                                     signed control message's timestamp
                                     may be from the local clock.
          max_children               (int >= 1, default unlimited) —
                                     refuse new children beyond this many.
                                     Members never expire yet (#6), so
                                     leave headroom for children that
                                     restart.
          trust_unregistered_parents (bool, default False) — honour a
                                     terminate from any agent named like
                                     our parent, as before #13, not only
                                     from registered parents.

        Read once at construction. Raises TypeError / ValueError on a
        malformed section, including unknown keys. Error messages never
        contain the secret."""
        holon = self.config.get('holon')
        if holon is None:
            holon = {}
        if not isinstance(holon, dict):
            raise TypeError(
                f"Agent config 'holon' must be a dict, got {type(holon).__name__}"
            )
        unknown = set(holon) - _HOLON_KEYS
        if unknown:
            raise ValueError(
                f"Unknown Agent config 'holon' keys {sorted(map(str, unknown))}; "
                f"expected {sorted(_HOLON_KEYS)}"
            )

        secret = holon.get('control_secret')
        if isinstance(secret, str):
            secret = secret.encode('utf-8')
        elif isinstance(secret, (bytes, bytearray)):
            secret = bytes(secret)
        elif secret is not None:
            raise TypeError(
                f"Agent config 'holon.control_secret' must be a str or bytes, "
                f"got {type(secret).__name__}"
            )
        if secret is not None and len(secret) < _MIN_CONTROL_SECRET_BYTES:
            raise ValueError(
                f"Agent config 'holon.control_secret' must be at least "
                f"{_MIN_CONTROL_SECRET_BYTES} bytes"
            )

        max_age = holon.get('control_max_age', _DEFAULT_CONTROL_MAX_AGE_S)
        if isinstance(max_age, bool) or not isinstance(max_age, (int, float)):
            raise TypeError(
                f"Agent config 'holon.control_max_age' must be a number of "
                f"seconds, got {max_age!r}"
            )
        if not 0 < max_age < math.inf:
            raise ValueError(
                f"Agent config 'holon.control_max_age' must be positive and "
                f"finite, got {max_age!r}"
            )

        max_children = holon.get('max_children')
        if max_children is not None:
            if isinstance(max_children, bool) or not isinstance(max_children, int):
                raise TypeError(
                    f"Agent config 'holon.max_children' must be an int, "
                    f"got {max_children!r}"
                )
            if max_children < 1:
                raise ValueError(
                    f"Agent config 'holon.max_children' must be at least 1, "
                    f"got {max_children!r}"
                )

        trust = holon.get('trust_unregistered_parents', False)
        if not isinstance(trust, bool):
            raise TypeError(
                f"Agent config 'holon.trust_unregistered_parents' must be a "
                f"bool, got {trust!r}"
            )
        return _HolonOptions(secret, float(max_age), max_children, trust)
            
            
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
        logger.info(self.M(f"self.config: {_config_for_log(self.config)}"))
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
        """Fire-and-forget termination — never raises.

        Order (unchanged, RFC-004 / RFC-009 §7.16):
          1. dispatcher.stop() — bounded per RFC-004
          2. worker.stop()     — bounded per RFC-008 (ProcessWorker)
                                 or RFC-009 (ThreadWorker)

        RFC-009 §E: this method inspects the worker.stop() return
        value. Post-RFC-009 ThreadWorker returns bool (True=stopped,
        False=STOP_TIMEOUT). When False, we log a WARNING with the
        worker state; we do NOT raise. Legacy workers that return
        None (e.g. FakeWorker in tests) are treated as success.

        Bounded return of terminate() ONLY guarantees this method
        itself returns. If the worker ended at STOP_TIMEOUT and the
        thread is non-daemon (RFC-009 §7.13), Python interpreter
        shutdown may still block on that thread. This is a
        documented limitation, not a bug — see RFC-009 §H.
        """
        logger.info(self.M(f"self.__agent_worker: {self._agent_worker}"))

        # RFC-004: stop the dispatcher first so consumer threads can
        # drain their queue while the broker is still up. Idempotent —
        # safe to call from both terminate() and __deactivating().
        if self._dispatcher is not None:
            try:
                self._dispatcher.stop()
            except Exception as ex:
                # Preserve fire-and-forget contract: dispatcher.stop
                # is already bounded and idempotent (RFC-004); log
                # any exception and continue to worker.stop().
                logger.exception(self.M(
                    f"terminate: dispatcher.stop() raised: {ex!r}"
                ))

        if not self._agent_worker:
            logger.warning(self.M(f"The agent might not have started yet."))
            return

        # RFC-009 §E: bounded worker.stop() with observed return value.
        try:
            stop_result = self._agent_worker.stop()
        except Exception as ex:
            logger.exception(self.M(
                f"terminate: worker.stop() raised: {ex!r}"
            ))
            return

        if stop_result is False:
            # RFC-013 diagnostics: capability-based reads so this path
            # works for any Worker subclass (ThreadWorker exposes the
            # new properties; ProcessWorker / FakeWorker do not — for
            # those, `getattr(..., default)` gracefully falls back and
            # `requires_process_restart` becomes False → the existing
            # WARNING path is taken).
            #
            # The diagnostics block itself is guarded so a misbehaving
            # property (e.g. a subclass that raises from `.state`)
            # cannot break Agent.terminate's never-raise contract.
            needs_restart = False
            state = 'unknown'
            worker_type = type(self._agent_worker).__name__
            thread = None
            alive = None
            daemon = None
            ident = None
            try:
                state = getattr(self._agent_worker, 'state', 'unknown')
                thread = getattr(self._agent_worker, 'work_thread', None)
                needs_restart = bool(getattr(
                    self._agent_worker, 'requires_process_restart', False,
                ))
                alive = getattr(self._agent_worker, 'thread_alive', None)
                daemon = getattr(self._agent_worker, 'thread_daemon', None)
                ident = getattr(
                    self._agent_worker, 'worker_thread_ident', None,
                )
            except Exception as diag_ex:
                # A property getter raised — do not let that break
                # terminate's never-raise contract. Fall through to
                # the WARNING path with what we already gathered.
                try:
                    logger.exception(self.M(
                        f"terminate: diagnostics property raised "
                        f"(continuing with best-effort values): "
                        f"{diag_ex!r}"
                    ))
                except Exception:
                    pass
                needs_restart = False

            if needs_restart:
                # RFC-013 §7.7: ERROR-level signal — operator /
                # supervisor intervention required. At most one ERROR
                # per Agent.terminate invocation (this branch runs at
                # most once).
                try:
                    logger.error(self.M(
                        f"terminate: PROCESS RESTART REQUIRED — "
                        f"worker_type={worker_type}, state={state}, "
                        f"thread_ident={ident}, thread_alive={alive}, "
                        f"daemon={daemon}. terminate() has returned "
                        f"but the worker thread is a non-daemon thread "
                        f"in STOP_TIMEOUT — external process restart "
                        f"or supervisor containment required. Python "
                        f"interpreter will NOT exit until an external "
                        f"supervisor terminates this process. See "
                        f"RFC-013."
                    ))
                except Exception:
                    pass
            else:
                # Existing WARNING path preserved (recoverable
                # STOP_TIMEOUT: worker thread may still be alive but
                # is not the R-10.5 blocker — e.g. thread died on its
                # own, or worker doesn't expose the RFC-013 property).
                logger.warning(self.M(
                    f"terminate: worker did not stop within its deadline; "
                    f"state={state}, thread={thread!r}. terminate() has "
                    f"returned but the worker thread may still be alive; "
                    f"because daemon=False, Python interpreter shutdown "
                    f"may still block on this thread (see RFC-009 §H)."
                ))



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


    def is_connected(self) -> bool:
        """True while this agent's broker link is up (issue #2).

        After an unexpected loss the broker reconnects automatically;
        meanwhile this returns False and publishes fail. The state is
        per process: in process mode (the default) the caller-side
        Agent object never connects, so query it from inside the agent
        (handlers / hooks) instead. Brokers that never report a
        connection (e.g. EmptyBroker) always yield False."""
        return self._broker_connected


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
        # Issue #5: run once. A worker stop, a parent's terminate and any
        # echo of our own broadcast may all call this; only the first
        # notifies the children and starts the delayed stop.
        with self._terminate_lock:
            if self._terminating:
                return
            self._terminating = True
        try:
            logger.warning(self.M('Terminating..'))

            self._notify_children('terminate')
            def stop():
                time.sleep(1)
                self.__terminate_event.set()
            threading.Thread(target=stop).start()
        except BaseException:
            # The body did not finish (e.g. Thread.start failed, or a
            # KeyboardInterrupt landed): let the next request retry it,
            # or the agent could never stop.
            with self._terminate_lock:
                self._terminating = False
            raise


    def __deactivating(self):
        """Fire-and-forget deactivation — never raises.

        RFC-010 §H integration: observes `broker.stop()`'s bool
        return. On False (STOP_TIMEOUT or STOP_FAILED per RFC-010),
        logs a WARNING with the broker's state and last_stop_exception.
        Legacy brokers whose `stop()` returns None are treated as
        success. Any exception from `broker.stop()` is logged and
        swallowed (parity with the Agent.terminate never-raise
        contract).

        Bounded return of __deactivating() only guarantees this
        method returns; it does NOT guarantee the paho network
        thread was reclaimed. See RFC-010 Appendix A / RFC-009 §H.
        """
        self.on_terminating()

        if self._broker:
            try:
                stopped = self._broker.stop()
            except Exception as ex:
                logger.exception(self.M(
                    f"__deactivating: broker.stop() raised: {ex!r}"
                ))
            else:
                if stopped is False:
                    state = getattr(self._broker, 'state', 'unknown')
                    last_exc = getattr(
                        self._broker, 'last_stop_exception', None,
                    )
                    logger.warning(self.M(
                        f"broker.stop() did not complete within its "
                        f"deadline; state={state}, "
                        f"last_stop_exception={last_exc!r}. "
                        f"__deactivating has returned but the paho "
                        f"network thread may still be alive. See "
                        f"RFC-010 Appendix A / RFC-009 §H."
                    ))
            # Issue #2: the broker fences notifier callbacks once stop()
            # has begun (RFC-010 §F), so no _on_disconnect will arrive.
            self._broker_connected = False

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
        # Issue #13: from `secrets`, not the seedable, predictable `random`.
        rand = ''.join(secrets.choice(alphabet) for _ in range(10))
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
    
    
    def __register_child(self, child_id:str, child_info:dict) -> Optional[bool]:
        """Record a child. Returns whether the caller should answer with
        register_parent, or None if the child was refused: issue #13, a
        new child beyond holon.max_children.

        Issue #6: children re-register on every (re)connect and on a
        parent's parent_online, so registration is idempotent: a known
        child only refreshes its entry and the hook fires once. While the
        first registration's hook is still running, a duplicate gets no
        answer; the first registration answers once its hook returns, so
        register_parent never overtakes on_register_child."""
        child_info['parent_id'] = self.agent_id
        cap = self._holon_options.max_children
        with self._holon_lock:
            is_new = child_id not in self._children
            refused = is_new and cap is not None and len(self._children) >= cap
            if not refused:
                self._children[child_id] = child_info
                total = len(self._children)
                if is_new:
                    self._children_registering.add(child_id)
                elif child_id in self._children_registering:
                    return False
        if refused:
            self.__log_holon_rejection(
                'refused children', f"Refused child {child_id!r}: holon.max_children={cap} reached.")
            return None
        if not is_new:
            logger.debug(self.M(f"Child re-registered: {child_id}"))
            return True
        try:
            logger.info(self.M(f"Add a child: {child_id}, total: {total}"))
            self.on_register_child(child_id, child_info)
        finally:
            with self._holon_lock:
                self._children_registering.discard(child_id)
        return True


    def on_register_child(self, child_id, child_info:dict):
        pass
    
    
    def __register_parent(self, parent_id:str, parent_info):
        """Record a parent. Issue #6: idempotent, as for
        __register_child. Returns this parent's deferred terminate as
        (topic, message) if one is waiting (issue #13), else None."""
        parent_info['child_id'] = self.agent_id
        with self._holon_lock:
            is_new = parent_id not in self._parents
            self._parents[parent_id] = parent_info
            total = len(self._parents)
            pending = self._pending_terminates.pop(parent_id, None)
        if is_new:
            logger.info(self.M(f"Add a parent: {parent_id}, total: {total}"))
            self.on_register_parent(parent_id, parent_info)
        else:
            logger.debug(self.M(f"Parent re-registered: {parent_id}"))
        if pending is not None and pending[0] >= time.monotonic():
            return pending[1], pending[2]
        return None


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

        if not self.__is_authentic_control(topic, child):
            return None
        child.pop(holon_auth.AUTH_FIELD, None)

        if child_id:=child.get('child_id'):
            if "register_child" == child.get('subject'):
                answer = self.__register_child(child_id, child)
                if answer is None:
                    return None     # Issue #13: refused (holon.max_children).
                if answer:
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
        
        if parent.get('parent_id') == self.agent_id:
            # Issue #5: our own broadcast on to_child.{self.name}, echoed
            # back by the broker. Never act on it (a mid-tier agent's own
            # terminate used to re-trigger itself endlessly) and never
            # pass it to on_parents_message.
            return None

        sender_name = parent.get('parent_name')
        if sender_name is not None and sender_name != self.parent_name:
            # Issue #38: to_child.{X} also carries messages that are not
            # from our parent: a same-name replica's broadcast to its own
            # children (on to_child.{self.name}), or a message addressed
            # by name to agents named like our parent (on
            # to_child.{self.parent_name}). Only an agent named like our
            # parent addresses us. Senders older than #38 omit the name;
            # their messages are still accepted.
            return None

        if target_children := parent.get('target_children'):
            if not self.agent_id in target_children:
                return  # Not in the target children.

        if not self.__is_authentic_control(topic, parent):
            return None
        parent.pop(holon_auth.AUTH_FIELD, None)

        if "terminate" == parent.get('subject'):
            if not self.__accept_terminate(topic, parent):
                return None
            self._terminate()
        elif "register_parent" == parent.get('subject'):
            if topic != f'{self.agent_id}.to_child.{self.parent_name}':
                # Issue #13: parents answer register_child only on our
                # own channel. Honouring one on a broadcast topic would
                # let anyone register as our parent without knowing any
                # agent id, and then terminate us.
                self.__log_holon_rejection(
                    'misdirected register_parent', f"Ignored a register_parent on {topic!r}.")
                return None
            if parent_id := parent.get('parent_id'):
                if deferred := self.__register_parent(parent_id, parent):
                    # Issue #13: this parent's terminate overtook its
                    # register_parent; act on it now.
                    self._terminate()
                    self.on_parents_message(*deferred)
        elif "parent_online" == parent.get('subject'):
            # Issue #6: a parent (re)connected and asks its children to
            # register. Internal: never forwarded to on_parents_message.
            # Only honoured on the parent channel: the same message also
            # arrives on to_child.{self.name}, as our own broadcast
            # echoed back or a same-name replica's.
            parent_id = parent.get('parent_id')
            if (parent_id and parent_id != self.agent_id
                    and topic == f'to_child.{self.parent_name}'):
                self.__register_with_parent(parent_id)
            return None
            
        return self.on_parents_message(topic, parent)


    def on_parents_message(self, topic, parent):
        pass


    def __is_authentic_control(self, topic, message) -> bool:
        """Issue #13: with holon.control_secret set, a control message
        must be signed for the topic it arrived on. Other subjects, and
        every message when no secret is set, pass."""
        key = self._holon_options.control_key
        if key is None or not holon_auth.is_control(message):
            return True
        reason = holon_auth.verify(key, topic, message, self._holon_options.control_max_age)
        if reason is None:
            return True
        self.__log_holon_rejection(
            'unauthenticated control messages',
            f"Dropped {message.get('subject')!r} on {topic!r}: {reason}.")
        return False


    def __accept_terminate(self, topic, parent) -> bool:
        """Issue #13: honour a terminate only from a registered parent.

        One from an agent named like our parent that has not registered
        may have overtaken its register_parent (the dispatcher does not
        order messages), so it waits for that registration for
        _PENDING_TERMINATE_TTL_S; at most _MAX_PENDING_TERMINATES wait,
        the oldest is dropped first. holon.trust_unregistered_parents
        restores the pre-#13 behaviour."""
        if self._holon_options.trust_unregistered_parents:
            return True
        parent_id = parent.get('parent_id')
        if not isinstance(parent_id, str) or not parent_id:
            self.__log_holon_rejection(
                'unattributed terminates', f"Ignored a terminate without a parent_id on {topic!r}.")
            return False
        now = time.monotonic()
        with self._holon_lock:
            if parent_id in self._parents:
                return True
            pending = self._pending_terminates
            for pid in [p for p, entry in pending.items() if entry[0] < now]:
                del pending[pid]
            pending.pop(parent_id, None)
            if len(pending) >= _MAX_PENDING_TERMINATES:
                del pending[next(iter(pending))]
            pending[parent_id] = (now + _PENDING_TERMINATE_TTL_S, topic, parent)
        self.__log_holon_rejection(
            'deferred terminates',
            f"Deferred a terminate from unregistered parent {parent_id!r} until it registers.")
        return False


    def __log_holon_rejection(self, kind, text):
        """Issue #13: log a refused holon message at WARNING the 1st,
        2nd, 4th, 8th, ... time per kind and at DEBUG otherwise, so a
        flood of forged messages cannot flood the log. The count is
        approximate under concurrent dispatch."""
        count = self._holon_rejections.get(kind, 0) + 1
        self._holon_rejections[kind] = count
        if count & (count - 1) == 0:
            logger.warning(self.M(f"{text} ({count} {kind} so far)"))
        else:
            logger.debug(self.M(text))


    def __publish_holon(self, topic, message):
        """Issue #13: publish a holon message, signed for `topic` when it
        is a control message and holon.control_secret is set."""
        key = self._holon_options.control_key
        if key is not None and holon_auth.is_control(message):
            message = holon_auth.sign(key, topic, message)
        return self.publish(topic, message)
    
    
    def _notify_child(self, child_id, subject, data=None):
        logger.debug(f"child_id: {child_id}, subject: {subject}, data: {data}")

        if self._children and child_id in self._children:
            self.__publish_holon(f'{child_id}.to_child.{self.name}', {
                'parent_id': self.agent_id,
                'parent_name': self.name,     # issue #38
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
            'parent_name': self.name,     # issue #38
            'subject': subject,
            'data': data,
            }

        if target_children:
            logger.debug(self.M(f"target_children: {target_children}"))
            data_send['target_children'] = target_children
        
        if target_child_name:
            logger.debug(self.M(f"target_child_name: {target_child_name}"))
            topic = f'to_child.{target_child_name}'

        self.__publish_holon(topic, data_send)
    
    
    def _notify_parent(self, parent_id, subject, data=None):
        logger.debug(self.M(f"parent_id: {parent_id}, subject: {subject}, data: {data}"))

        if self._parents and parent_id in self._parents:
            self.__publish_holon(f'{parent_id}.to_parent.{self.parent_name}', {
                'child_id': self.agent_id,
                'subject': subject,
                'data': data
            })
        else:
            logger.error("The parent does not exist.")
    
    
    def __register_with_parent(self, parent_id):
        """Issue #6: answer one parent's parent_online on its own
        channel, so same-name parent replicas are not all delivered (and
        then discard) every child's answer."""
        self.__publish_holon(f'{parent_id}.to_parent.{self.parent_name}', {
            'child_id': self.agent_id,
            'child_name': self.name,
            'subject': 'register_child',
            'data': None,
            'target_parents': [parent_id],
        })


    def _announce_parent_online(self):
        """Issue #6: ask any children of this agent to (re)register.
        Published directly rather than through _notify_children, which
        returns early while _children is still empty."""
        self.__publish_holon(f'to_child.{self.name}', {
            'parent_id': self.agent_id,
            'parent_name': self.name,     # issue #38
            'subject': 'parent_online',
            'data': None,
        })


    def _notify_parents(self, subject, data=None, target_parents=None):
        logger.debug(f"subject: {subject}, data: {data}")
        
        if self.parent_name:
            self.__publish_holon(f'to_parent.{self.parent_name}', {
                'child_id': self.agent_id,
                'child_name': self.name,
                'subject': subject,
                'data': data,
                'target_parents': target_parents
            })
        else:
            logger.error(f"No any parent.")
        
        
    def _on_connect(self):
        self._broker_connected = True
        if self._connected_once:
            # Issue #2: reconnected. The broker has already re-subscribed
            # every topic from its registry, so the one-time setup below
            # (holon subscriptions, on_connected) is not repeated.
            logger.info(self.M("Reconnected to the broker."))
            # Issue #6: membership messages may have been missed while
            # offline; re-register and ask children to do the same.
            if self.parent_name:
                self._notify_parents("register_child")
            self._announce_parent_online()
            self._run_connection_hook(self.on_reconnected)
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
        # Issue #6: children that started first registered with nobody.
        self._announce_parent_online()

        def handle_connected():
            time.sleep(1)
            self.__connected_event.set()
            self.on_connected()
        threading.Thread(target=handle_connected).start()


    def _on_disconnect(self, planned: bool = False):
        """BrokerNotifier callback (issue #2): the broker link was lost.
        Calls on_disconnected() once per unexpected loss of an
        established connection."""
        was_connected = self._broker_connected
        self._broker_connected = False
        if not was_connected or planned:
            return
        logger.warning(self.M(
            "Disconnected from the broker; waiting for automatic reconnect."
        ))
        self._run_connection_hook(self.on_disconnected)


    def _run_connection_hook(self, hook):
        """Run a connection hook off the broker's network thread, so a
        slow or failing hook can neither delay nor kill reconnects."""
        hook_name = getattr(hook, '__name__', None) or type(hook).__name__

        def run():
            try:
                hook()
            except Exception as ex:
                logger.exception(self.M(f"{hook_name} raised: {ex!r}"))
        threading.Thread(
            target=run, name=f'{self.name_tag}-{hook_name}', daemon=True,
        ).start()


    @final
    def _on_message(self, topic:str, data):
        # logger.debug(self.M(f"topic: {topic}, data: {data}"))
        pcl = Parcel.from_payload(data, **self._codec_options)

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
        if should_auto_reply and _is_holon_control_topic(pcl.topic_return):
            # Issue #13: a requester must not be able to point our reply
            # at a holon control topic (confused deputy). The handler
            # still runs; only the reply is withheld.
            should_auto_reply = False
            self.__log_holon_rejection(
                'replies into control topics',
                f"Withheld the reply to {topic!r}: topic_return "
                f"{pcl.topic_return!r} is a holon control topic.")

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


    def on_disconnected(self):
        """Hook (issue #2): the broker link was lost unexpectedly. The
        broker keeps reconnecting; publishes fail until on_reconnected().
        Runs on a short-lived daemon thread."""
        pass


    def on_reconnected(self):
        """Hook (issue #2): the broker link is back after an unexpected
        loss and every subscription has been restored. Unlike
        on_connected(), which runs once, this runs after every
        reconnect. Runs on a short-lived daemon thread."""
        pass


    def on_message(self, topic:str, data):
        pass
        
        
    def M(self, message=None):
        return f'{self.name_tag} {message}' if message else self.name_tag
            
