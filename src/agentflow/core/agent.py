import copy
from collections import OrderedDict, deque
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
from agentflow.core.parcel import Parcel, ensure_size, parse_pickle_globals
from agentflow.broker import BrokerType
from agentflow.broker.notifier import BrokerNotifier
from agentflow.broker.broker_maker import BrokerMaker
from agentflow.broker.message_broker import check_qos
from agentflow.core import config
from agentflow.core.config import EventHandler
from agentflow.core.agent_worker import Worker, ProcessWorker, ThreadWorker
from agentflow.core.dispatcher import MessageDispatcher, LegacyPerMessageDispatcher


import logging, os
logger = logging.getLogger(os.getenv('LOGGER_NAME'))

# Issue #1: keys accepted in the optional `codec` config section.
_CODEC_KEYS = frozenset({'accept_pickle', 'pickle_allowed_globals', 'max_payload_bytes'})

# Issue #34: payload limits. A payload larger than codec.max_payload_bytes
# is dropped without decoding; at most dispatch.max_queued_bytes of
# received payloads wait for a dispatcher thread (or are held, see #17).
_DEFAULT_MAX_PAYLOAD_BYTES = 16 * 1024 * 1024
_DEFAULT_MAX_QUEUED_BYTES = 256 * 1024 * 1024
# A dropped request is decoded on the receiving thread, to be answered
# "overloaded" (issue #4), only up to this size.
_DROP_REPLY_DECODE_LIMIT = 64 * 1024


def _payload_size(data) -> int:
    try:
        return len(data)
    except TypeError:
        return 0


def _positive_int(value, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError(f"Agent config '{name}' must be a positive int, got {value!r}")
    if value < 1:
        raise ValueError(f"Agent config '{name}' must be a positive int, got {value!r}")
    return value

# Issue #4: holon control messages and publish_sync requests / replies
# travel at QoS 1 (at least once), and the topics they arrive on are
# subscribed at QoS 1. Application publish / subscribe default to QoS 0.
_CONTROL_QOS = 1
# Issue #4: how many recently received message ids an agent remembers to
# drop redeliveries (QoS 1 resends follow a reconnect closely).
_DEDUP_WINDOW = 4096
# Follow-up of #17: at most this many messages with no handler are held
# while the lifecycle hooks are still registering handlers.
_EARLY_MESSAGE_LIMIT = 4096
# Follow-up of #17: held messages are released at the latest this long
# after the gate closed, even if a hook (e.g. a long-running
# on_connected loop) has not returned.
_EARLY_MESSAGE_MAX_HOLD_S = 10.0
# Releasing held messages waits at most this long for room in the
# dispatcher queue and byte budget before it stops waiting.
_REPLAY_ROOM_TIMEOUT_S = 1.0


def _topic_matches(subscription: str, topic: str) -> bool:
    """MQTT-style match of `topic` against a subscription that may use
    the + (one level) and # (this and every lower level) wildcards."""
    sub_levels, topic_levels = subscription.split('/'), topic.split('/')
    for i, level in enumerate(sub_levels):
        if level == '#':
            return True
        if i >= len(topic_levels):
            return False
        if level != '+' and level != topic_levels[i]:
            return False
    return len(sub_levels) == len(topic_levels)

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
# Issue #13: how long a terminating parent waits for on_register_child
# hooks that are still running, so those children get register_parent
# before its terminate.
_REGISTRATION_DRAIN_S = 3.0
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
      - publish_sync on a topic already held by a normal subscribe
        handler (RFC-007)
      - Agent.subscribe on a topic reserved by an active publish_sync
        waiter (RFC-007)
      - Agent.unsubscribe on a topic reserved by an active
        publish_sync waiter (RFC-007)
    The exception message distinguishes the context. Concurrent
    publish_sync calls on one topic_wait no longer raise it; they
    share the topic (RFC-014 §8, superseding RFC-006's fail-fast)."""


class _HandlerOwnerType(Enum):
    """Internal (RFC-007): owner tag for an Agent handler registry
    entry. NORMAL is registered via Agent.subscribe; PUBLISH_SYNC is
    the _ReplyRouter registered by Agent.publish_sync while calls wait
    for replies on the topic (RFC-014 §8)."""
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


class _ReplyRouter:
    """RFC-014 §8 (issue #36): the publish_sync calls waiting on one
    reply topic, by correlation id. It is the topic's PUBLISH_SYNC
    handler, so concurrent calls can share a topic_wait.

    A reply goes to the caller whose correlation id it carries. One
    without an id (a responder older than RFC-014) goes to the caller
    waiting only while no other call has shared the topic. Once calls
    have overlapped it cannot be attributed, even after all but one
    have left (it may answer a call that already timed out), and nobody
    takes it rather than risk handing a caller another's answer
    (RFC-006 Option D, FIFO, stays rejected). The router is dropped
    when its last caller leaves, so a topic_wait reused without overlap
    behaves as before. The first reply a caller receives wins."""

    def __init__(self, owner_tag: str):
        self._owner_tag = owner_tag
        self._lock = threading.Lock()
        self._waiters: dict = {}        # correlation id -> Agent.DataEvent
        self._shared = False            # calls have overlapped on the topic
        self._unattributed = 0

    def add(self, correlation_id: str, waiter) -> None:
        with self._lock:
            if self._waiters:
                self._shared = True
            self._waiters[correlation_id] = waiter

    def remove(self, correlation_id: str) -> bool:
        """Remove a caller. Returns whether none is left."""
        with self._lock:
            self._waiters.pop(correlation_id, None)
            return not self._waiters

    def __call__(self, topic, reply) -> None:
        reply_id = reply.correlation_id
        unattributed = 0
        with self._lock:
            waiting = len(self._waiters)
            if reply_id is not None:
                waiter = self._waiters.get(reply_id)
            elif waiting == 1 and not self._shared:
                waiter = next(iter(self._waiters.values()))
            else:
                waiter = None
                if waiting:
                    self._unattributed += 1
                    unattributed = self._unattributed
            if waiter is not None:
                if not waiter.event.is_set():
                    waiter.data = reply
                    waiter.event.set()
                return
        if unattributed:
            # WARNING on the 1st, 2nd, 4th, ... occurrence, so that a
            # flood cannot flood the log.
            level = logging.WARNING if unattributed & (unattributed - 1) == 0 else logging.DEBUG
            logger.log(level, f"{self._owner_tag} Ignored a reply without a correlation id "
                              f"on {topic!r}: calls have shared that topic, so it cannot be "
                              f"attributed. Its responder predates RFC-014.")
        else:
            # A reply to another request, e.g. a late reply to an
            # earlier call with the same topic_wait.
            logger.debug(f"{self._owner_tag} Ignored a reply on {topic!r} to another "
                         f"request ({reply_id!r}).")



class Agent(BrokerNotifier):
    def __init__(self, name:str, agent_config:dict={}):
        logger.debug(f'name: {name}, agent_config: {_config_for_log(agent_config)}')
        
        self.agent_id = str(uuid.uuid4()).replace("-", "")
        logger.debug(f'agent_id: {self.agent_id}')
        self.__init_config(agent_config)
        # Issue #1: inbound decoding policy, validated up front so a bad
        # `codec` config fails at construction rather than per message.
        self._codec_options = self.__parse_codec_config()
        # Issue #34: payload size limits (codec / dispatch sections).
        self._max_payload_bytes, self._max_queued_bytes = self.__parse_payload_limits()
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
        # RFC-014 §8: a publish_sync call joins its reply topic and
        # subscribes, or leaves it and unsubscribes, under this lock, so
        # a call that joins while the last one leaves never subscribes
        # before that unsubscribe lands. Reentrant: a failed subscribe
        # leaves while holding it.
        self._reply_topics_lock = threading.RLock()
        # Issue #4: the message ids received most recently, oldest
        # first; a redelivered id is dropped (see __seen_before).
        self._recent_message_ids: OrderedDict = OrderedDict()
        self._dedup_lock = threading.Lock()
        # Issue #15: messages dropped because they were not valid
        # AgentFlow parcels; see decode_failure_count.
        self._decode_failures = 0
        self._decode_lock = threading.Lock()
        # Issue #34: bytes of received payloads queued for a dispatcher
        # thread, and drops for exceeding that budget.
        self._queued_bytes = 0
        self._byte_budget_drops = 0
        self._queued_bytes_lock = threading.Lock()
        # Follow-up of #17: messages with no handler that arrive while
        # lifecycle hooks are still registering handlers are held here
        # (see __await_hook) and released once every awaited hook has
        # returned. Only agents run through _activate are gated.
        self._early_lock = threading.Lock()
        self._early_messages: deque = deque()
        self._awaiting_hooks: set = set()
        self._early_dropped = 0
        self._held_bytes = 0
        self._lifecycle_gating = False
        self._gate_timer: Optional[threading.Timer] = None
        # Topics passed to subscribe() in this process (guarded by
        # _early_lock). A message on one of them is never held: it is
        # the agent's own traffic, not a resumed session's leftover.
        self._session_topics: set = set()

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
        '_reply_topics_lock',
        '_recent_message_ids',
        '_dedup_lock',
        '_decode_lock',
        '_queued_bytes_lock',
        '_early_lock',
        '_early_messages',
        '_awaiting_hooks',
        '_gate_timer',
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
        self._reply_topics_lock = threading.RLock()
        self._recent_message_ids = OrderedDict()
        self._dedup_lock = threading.Lock()
        self._decode_lock = threading.Lock()
        self._queued_bytes = 0
        self._queued_bytes_lock = threading.Lock()
        self._early_lock = threading.Lock()
        self._early_messages = deque()
        self._awaiting_hooks = set()
        self._gate_timer = None
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


    def __parse_payload_limits(self):
        """Issue #34: `codec.max_payload_bytes` (default 16 MiB; larger
        payloads are dropped without decoding) and
        `dispatch.max_queued_bytes` (default 256 MiB; received payloads
        waiting for a dispatcher thread, or held before the handlers
        exist, may not exceed it). Raises TypeError / ValueError unless
        each is a positive int."""
        codec = self.config.get('codec') or {}
        dispatch = self.config.get('dispatch') or {}
        max_payload = codec.get('max_payload_bytes', _DEFAULT_MAX_PAYLOAD_BYTES)
        max_queued = dispatch.get('max_queued_bytes', _DEFAULT_MAX_QUEUED_BYTES)
        return (_positive_int(max_payload, 'codec.max_payload_bytes'),
                _positive_int(max_queued, 'dispatch.max_queued_bytes'))


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
        try:
            max_age = float(max_age)
        except OverflowError:
            raise ValueError(
                f"Agent config 'holon.control_max_age' is out of range"
            ) from None
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
        return _HolonOptions(secret, max_age, max_children, trust)
            
            
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
                if getattr(e, 'retryable', True) is False:
                    # Issue #17: the broker refused the connection for a
                    # reason the same configuration cannot overcome
                    # (client id rejected, bad credentials, not
                    # authorised): retrying only delays the failure.
                    logger.error(self.M(f"Broker refused the connection; not retrying: {e}"))
                    return False
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

        # Follow-up of #17: a resumed persistent session delivers its
        # queued messages right after the CONNACK, before on_activate
        # (or on_connected, see _on_connect) has registered the
        # handlers. Hold messages with no handler until they return.
        self._lifecycle_gating = True
        self.__await_hook('on_activate')
        try:
            activated = self.__activating()
            if activated:
                sig = inspect.signature(self.on_activate)
                if len(sig.parameters) == 0:
                    self.on_activate()
                elif isinstance(sig.parameters.get('self'), Agent):
                    self.on_activate(self)
                else:
                    self.on_activate(self.config)
        finally:
            self.__hook_done('on_activate')

        if activated:
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

            self.__await_registrations(_REGISTRATION_DRAIN_S)
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


    def __await_registrations(self, timeout):
        """Issue #13: wait, at most `timeout` seconds, until no
        on_register_child hook is running. Such a child has not been
        sent register_parent yet (#6), so without a control secret it
        holds our terminate back until register_parent arrives, and our
        broker stops about 1 s after the terminate."""
        deadline = time.monotonic() + timeout
        while True:
            with self._holon_lock:
                running = len(self._children_registering)
            if not running:
                return
            if time.monotonic() >= deadline:
                logger.warning(self.M(
                    f"Terminating while {running} child registration hook(s) still run."))
                return
            time.sleep(0.01)


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
    def publish(self, topic, data=None, *, qos=0) -> bool:
        # Fire-and-forget contract: catch every Exception so callers who
        # ignore the return value never see a broker-side failure. To
        # get the raise-on-failure variant, use publish_sync (which
        # goes through _publish_or_raise) or call _publish_or_raise
        # directly. Issue #4: `qos` (0, 1 or 2) is checked up front; an
        # invalid one is a programming error and raises ValueError.
        # Returns True when the broker accepted the message and False
        # when it did not (the failure is logged); it used to return
        # None either way.
        check_qos(qos)
        try:
            self._publish_or_raise(topic, data, qos=qos)
            return True
        except Exception as ex:
            logger.exception(ex)
            return False


    def _publish_or_raise(self, topic, data=None, *, qos=0) -> None:
        """Internal strict publish. Wraps `data` as a Parcel and forwards
        it to the broker. Unlike Agent.publish, propagates every broker
        exception to the caller and raises RuntimeError when no broker
        is attached. Used by publish_sync to enable fast-fail semantics.
        Marked with a single leading underscore to signal that this is
        internal-use only; API stability is not guaranteed."""
        pcl = data if isinstance(data, Parcel) else Parcel.from_content(data)
        # Issue #4: every message sent carries a fresh id, so that a
        # receiver can drop a redelivery. Set on a copy: the caller's
        # Parcel is not modified.
        if pcl is data:
            pcl = copy.copy(pcl)
        pcl.message_id = uuid.uuid4().hex
        if self._broker is None:
            raise RuntimeError("Cannot publish: no broker attached")
        self._broker.publish(topic, pcl.payload(), qos=qos)

        
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

        # RFC-014 (issue #36): a fresh, unpredictable id per request, set
        # on a copy so that the caller's Parcel is not modified.
        correlation_id = secrets.token_hex(16)
        request = copy.copy(pcl)
        request.correlation_id = correlation_id

        data_event = Agent.DataEvent(self._get_worker().create_event())
        reply_topic = pcl.topic_return
        router = self.__join_reply_topic(reply_topic, correlation_id, data_event)

        try:
            # _publish_or_raise propagates broker exceptions so that the
            # caller fails fast on publish errors instead of waiting for
            # the full response timeout (RFC-002).
            self._publish_or_raise(topic, request, qos=_CONTROL_QOS)
            if data_event.event.wait(timeout):
                reply = data_event.data
                # RFC-014: the id served only to match this reply. Clear
                # it so that forwarding this Parcel by hand to another
                # requester (e.g. a gateway answering in on_message)
                # does not carry a foreign id that requester would
                # ignore. The Parcel was decoded for this call alone.
                reply.correlation_id = None
                return reply
            raise TimeoutError(f"No response received within timeout period for topic: {reply_topic}.")
        finally:
            self.__leave_reply_topic(reply_topic, correlation_id, router)


    def __join_reply_topic(self, reply_topic, correlation_id, waiter) -> _ReplyRouter:
        """Register a publish_sync call on its reply topic.

        RFC-014 §8: concurrent calls share a reply topic through one
        _ReplyRouter, registered as the topic's PUBLISH_SYNC handler, so
        Agent.subscribe / unsubscribe still refuse to trample it
        (RFC-007 §7.3, §7.4). The first call subscribes the topic. A
        topic owned by a normal subscribe handler is refused with
        TopicWaitCollisionError (RFC-006 / RFC-007).

        Under _reply_topics_lock (see __init__). The broker is still
        called outside _handlers_lock (RFC-005)."""
        with self._reply_topics_lock:
            with self._handlers_lock:
                record = self.__topic_handlers.get(reply_topic)
                if record is None:
                    router = _ReplyRouter(self.name_tag)
                    self.__topic_handlers[reply_topic] = _HandlerRecord(
                        _HandlerOwnerType.PUBLISH_SYNC, router,
                    )
                    first = True
                elif (record.owner_type is _HandlerOwnerType.PUBLISH_SYNC
                        and isinstance(record.handler, _ReplyRouter)):
                    router = record.handler
                    first = False
                else:
                    # NORMAL owner: publish_sync must not trample it.
                    raise TopicWaitCollisionError(
                        f"topic_wait {reply_topic!r} is already registered "
                        f"by a normal subscribe handler; publish_sync would "
                        f"trample it and is refused"
                    )
                router.add(correlation_id, waiter)
            if first and self._broker:
                try:
                    self._broker.subscribe(reply_topic, "str", qos=_CONTROL_QOS)
                except BaseException:
                    # RFC-006 §7.6: leave the registry clean, re-raise.
                    self.__leave_reply_topic(reply_topic, correlation_id, router)
                    raise
        return router


    def __leave_reply_topic(self, reply_topic, correlation_id, router) -> None:
        """Unregister a publish_sync call. The last call to leave removes
        the router and unsubscribes the topic. RFC-006 §7.5 / RFC-007
        §7.8 triple check: only a record that still holds this router
        as a PUBLISH_SYNC handler is removed. Under _reply_topics_lock;
        the broker is called outside _handlers_lock."""
        with self._reply_topics_lock:
            with self._handlers_lock:
                empty = router.remove(correlation_id)
                record = self.__topic_handlers.get(reply_topic)
                last = (empty and record is not None
                        and record.owner_type is _HandlerOwnerType.PUBLISH_SYNC
                        and record.handler is router)
                if last:
                    self.__topic_handlers.pop(reply_topic, None)
            if last and self._broker:
                try:
                    self._broker.unsubscribe(reply_topic)
                except Exception as cleanup_ex:
                    logger.exception(cleanup_ex)


    @final
    def subscribe(self, topic, data_type:str="str", topic_handler=None, *, qos=0):
        logger.debug(self.M(f"topic: {topic}, data_type:{data_type}"))

        if not isinstance(data_type, str):
            raise TypeError(f"Expected data_type to be of type 'str', but got {type(data_type).__name__}. The subscribtion of topic '{topic}' is failed.")
        check_qos(qos)      # issue #4: before anything is registered
        with self._early_lock:
            self._session_topics.add(topic)

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
        return self._broker.subscribe(topic, data_type, qos=qos) if self._broker else None


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
        with self._early_lock:
            self._session_topics.discard(topic)
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
        restores the pre-#13 behaviour.

        With holon.control_secret set, the terminate has already been
        authenticated: its sender holds the holon's secret and is named
        like our parent. It is honoured without waiting for a
        registration that may never come.

        Without a secret, this check stops only senders that cannot
        observe the holon's traffic: parent ids appear in every parent
        broadcast."""
        if self._holon_options.trust_unregistered_parents:
            return True
        parent_id = parent.get('parent_id')
        if not isinstance(parent_id, str) or not parent_id:
            self.__log_holon_rejection(
                'unattributed terminates', f"Ignored a terminate without a parent_id on {topic!r}.")
            return False
        if self._holon_options.control_key is not None:
            return True
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
            try:
                message = holon_auth.sign(key, topic, message)
            except (TypeError, ValueError, RecursionError) as ex:
                # Never raise, like publish. Unsigned, it would be
                # dropped by every receiver anyway.
                logger.error(self.M(
                    f"Not sending {message.get('subject')!r} to {topic!r}: "
                    f"its data cannot be signed ({ex})."))
                return None
        return self.publish(topic, message, qos=_CONTROL_QOS)
    
    
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

        self.subscribe(f'to_parent.{self.name}', topic_handler=self._handle_children, qos=_CONTROL_QOS)  # All the parents were notified by the children.
        self.subscribe(f'{self.agent_id}.to_parent.{self.name}', topic_handler=self._handle_children, qos=_CONTROL_QOS)  # I was the only parent notified by a child.
        
        # logger.verbose(f"self.parent_name: {self.parent_name}")
        if self.parent_name:
            self.subscribe(f'to_child.{self.parent_name}', topic_handler=self._handle_parents, qos=_CONTROL_QOS) # All the children were notified by the parents.
            self.subscribe(f'to_child.{self.name}', topic_handler=self._handle_parents, qos=_CONTROL_QOS)    # All the children with the same name were notified by the parents.
            self.subscribe(f'{self.agent_id}.to_child.{self.parent_name}', topic_handler=self._handle_parents, qos=_CONTROL_QOS)    # Only this child notified by a parent.
            self._notify_parents("register_child")
        # Issue #6: children that started first registered with nobody.
        self._announce_parent_online()

        if self._lifecycle_gating:
            # Follow-up of #17: handlers may be registered in on_connected.
            self.__await_hook('on_connected')

        def handle_connected():
            try:
                time.sleep(1)
                self.__connected_event.set()
                self.on_connected()
            finally:
                self.__hook_done('on_connected')
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
        """BrokerNotifier callback, on the broker's network thread.

        Issue #34: only cheap, topic-based work happens here, so that a
        large or slow payload cannot stall network I/O and keepalive. A
        payload over codec.max_payload_bytes is dropped at once.
        Everything else is routed as raw bytes, and decoded on a
        dispatcher thread (__handle)."""
        size = _payload_size(data)
        if size > self._max_payload_bytes:
            self.__count_decode_failure(topic, ValueError(
                f"payload of {size} bytes exceeds codec.max_payload_bytes="
                f"{self._max_payload_bytes}"))
            return
        self.__route(topic, data, size)


    def __route(self, topic, data, size):
        """Queue a received message for its handler (RFC-004 / RFC-007).

        Follow-up of #17: while lifecycle hooks are still registering
        handlers, a message on a topic that has no handler and was not
        subscribed in this process (a resumed session's leftover) is
        held instead, and replayed here once they have returned.

        Issue #34: the message is queued undecoded, within the
        dispatch.max_queued_bytes budget. One the queue cannot take is
        dropped, without decoding unless it is small enough to be
        answered (see __drop)."""
        with self._early_lock:
            if (self._awaiting_hooks and not self.__has_handler(topic)
                    and not self.__subscribed_in_session(topic)):
                self.__hold(topic, data, size)
                return

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

        # Created first: a dispatcher that fails to start must not leave
        # bytes reserved.
        dispatcher = self._get_dispatcher()
        if not self.__reserve_queued_bytes(size):
            self.__drop(topic, data, size, is_specific_handler, over_byte_budget=True)
            return

        def task():
            self.__release_queued_bytes(size)
            self.__handle(topic, data, is_specific_handler, topic_handler)

        # RFC-004: dispatch via the bounded message dispatcher instead
        # of spawning one Thread per message. enqueue() never raises
        # (broker-callback safety invariant, §7.3); the dispatcher counts
        # and logs its own drops.
        if not dispatcher.enqueue(task, topic=topic):
            self.__release_queued_bytes(size)
            self.__drop(topic, data, size, is_specific_handler, over_byte_budget=False)


    def __handle(self, topic, data, is_specific_handler, topic_handler):
        """Decode, deduplicate and handle one message, on a dispatcher
        thread (issue #34)."""
        try:
            pcl = Parcel.from_payload(data, **self._codec_options)
        except Exception as ex:
            # Issue #15: not a valid AgentFlow parcel (another MQTT
            # client's JSON, an unsupported version, garbage). Count it
            # instead of letting it escape, which logged a full
            # traceback for every such message. Decoders raise
            # ParcelDecodeError; anything else they raise on untrusted
            # bytes is counted the same way.
            self.__count_decode_failure(topic, ex)
            return
        if pcl.message_id is not None and self.__seen_before(pcl.message_id):
            # Issue #4: a redelivery, e.g. a QoS 1 resend of a message
            # whose acknowledgement was lost across a reconnect.
            logger.debug(self.M(f"Dropped a redelivered message on {topic!r} ({pcl.message_id!r})."))
            return

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
            # never carry topic_return. RFC-014 (issue #36): it
            # carries the request's correlation id. If the handler
            # returned a Parcel that differs in either, reply with a
            # shallow copy instead of mutating the caller's object;
            # a copy keeps a subclass's own state and never calls
            # its constructor.
            if not isinstance(data_resp, Parcel):
                data_resp = Parcel.from_content(data_resp)
            elif data_resp.topic_return or data_resp.correlation_id != pcl.correlation_id:
                data_resp = copy.copy(data_resp)
                data_resp.topic_return = None
            data_resp.correlation_id = pcl.correlation_id
            self.publish(pcl.topic_return, data_resp, qos=_CONTROL_QOS)
        else:
            try:
                topic_handler(topic, pcl)
            except Exception as ex:
                logger.exception(ex)


    def __drop(self, topic, data, size, is_specific_handler, *, over_byte_budget):
        """A message the queue could not take (queue full or stopping, or
        over dispatch.max_queued_bytes).

        Issue #4: a dropped request is answered "overloaded" at once so
        that its requester fails fast. That needs its topic_return and
        correlation id, so it is decoded here, on the receiving thread,
        only when small enough (_DROP_REPLY_DECODE_LIMIT) and only when
        it would have been auto-replied to (RFC-003)."""
        if over_byte_budget:
            with self._queued_bytes_lock:
                self._byte_budget_drops += 1
                drops = self._byte_budget_drops
            level = logging.WARNING if drops & (drops - 1) == 0 else logging.DEBUG
            logger.log(level, self.M(
                f"Dropped a message of {size} bytes on {ensure_size(repr(topic), 200)}: "
                f"dispatch.max_queued_bytes={self._max_queued_bytes} would be exceeded "
                f"({drops} dropped so far)."))
        if not is_specific_handler or size > _DROP_REPLY_DECODE_LIMIT:
            return
        try:
            pcl = Parcel.from_payload(data, **self._codec_options)
        except Exception as ex:
            self.__count_decode_failure(topic, ex)
            return
        if not pcl.topic_return or _is_holon_control_topic(pcl.topic_return):
            return
        if pcl.message_id is not None and self.__seen_before(pcl.message_id):
            # A redelivered copy of a request that was handled, answered,
            # or is still waiting in the queue. Recording the id here
            # makes "overloaded" true: a waiting original, or a later
            # redelivery, is then dropped as a duplicate instead of run.
            return
        dropped = Parcel.from_content(None)
        dropped.error = ("overloaded: the request was dropped before it was handled "
                         "(dispatcher queue or byte budget full, or stopping)")
        dropped.correlation_id = pcl.correlation_id
        self.publish(pcl.topic_return, dropped, qos=_CONTROL_QOS)


    def __reserve_queued_bytes(self, size) -> bool:
        """Issue #34: account `size` against dispatch.max_queued_bytes.
        Returns False, reserving nothing, if it does not fit."""
        with self._queued_bytes_lock:
            if self._queued_bytes + size > self._max_queued_bytes:
                return False
            self._queued_bytes += size
            return True


    def __queued_bytes_fit(self, size) -> bool:
        with self._queued_bytes_lock:
            return self._queued_bytes + size <= self._max_queued_bytes


    def __release_queued_bytes(self, size) -> None:
        with self._queued_bytes_lock:
            self._queued_bytes -= size


    def __has_handler(self, topic) -> bool:
        with self._handlers_lock:
            return topic in self.__topic_handlers


    def __hold(self, topic, data, size) -> None:
        """Follow-up of #17: keep a message, undecoded, for release by
        __hook_done. Called under _early_lock. Beyond _EARLY_MESSAGE_LIMIT
        messages or dispatch.max_queued_bytes (issue #34) the message is
        dropped and counted; logged at WARNING on the 1st, 2nd, 4th, ...
        drop."""
        if (len(self._early_messages) >= _EARLY_MESSAGE_LIMIT
                or self._held_bytes + size > self._max_queued_bytes):
            self._early_dropped += 1
            dropped = self._early_dropped
            level = logging.WARNING if dropped & (dropped - 1) == 0 else logging.DEBUG
            logger.log(level, self.M(
                f"Dropped a message on {topic!r} that arrived before the handlers were "
                f"registered: the hold is full ({dropped} dropped so far)."))
            return
        self._early_messages.append((topic, data, size))
        self._held_bytes += size


    def __subscribed_in_session(self, topic) -> bool:
        """Whether `topic` matches a subscribe() made in this process.
        Called under _early_lock."""
        if topic in self._session_topics:
            return True
        return any(_topic_matches(sub, topic) for sub in self._session_topics
                   if '+' in sub or '#' in sub)


    def __await_hook(self, hook: str) -> None:
        """Follow-up of #17: hold leftover messages (see __route) until
        `hook`, and every other awaited hook, has returned; see
        __hook_done. When the gate closes, a timer opens it after
        _EARLY_MESSAGE_MAX_HOLD_S whatever the hooks are doing."""
        timer = None
        with self._early_lock:
            if not self._awaiting_hooks:
                timer = threading.Timer(_EARLY_MESSAGE_MAX_HOLD_S, self.__release_after_deadline)
                timer.daemon = True
                self._gate_timer = timer
            self._awaiting_hooks.add(hook)
        if timer is not None:
            timer.start()


    def __hook_done(self, hook: str) -> None:
        """Follow-up of #17: `hook` has returned. When no awaited hook is
        left, release the held messages."""
        with self._early_lock:
            self._awaiting_hooks.discard(hook)
            if self._awaiting_hooks:
                return
            held = list(self._early_messages)
            self._early_messages.clear()
            self._held_bytes = 0
            timer, self._gate_timer = self._gate_timer, None
        if timer is not None:
            timer.cancel()
        self.__replay(held)


    def __release_after_deadline(self) -> None:
        """Follow-up of #17: a hook is still running after
        _EARLY_MESSAGE_MAX_HOLD_S (e.g. a long-running on_connected
        loop): stop holding and release what is held."""
        with self._early_lock:
            if not self._awaiting_hooks:
                return
            pending = sorted(self._awaiting_hooks)
            self._awaiting_hooks.clear()
            held = list(self._early_messages)
            self._early_messages.clear()
            self._held_bytes = 0
            self._gate_timer = None
        logger.warning(self.M(
            f"Stopped holding early messages after {_EARLY_MESSAGE_MAX_HOLD_S:g} s although "
            f"{', '.join(pending)} had not returned; releasing {len(held)} held message(s)."))
        self.__replay(held)


    def __replay(self, held) -> None:
        """Follow-up of #17: route held messages, in arrival order, on a
        background thread so that the hook that opened the gate is not
        delayed. Each one waits for room in the dispatcher queue and in
        the byte budget (#34), so that the release does not burst past
        them and turn requests into 'overloaded' drops. Deduplication
        runs when each is handled."""
        if not held:
            return
        logger.info(self.M(
            f"Releasing {len(held)} message(s) that arrived before the handlers were registered."))

        def run():
            dispatcher = self._get_dispatcher()
            capacity = getattr(dispatcher, 'queue_capacity', None)
            throttle = True
            for topic, data, size in held:
                if throttle:
                    throttle = self.__wait_for_room(dispatcher, capacity, size)
                self.__route(topic, data, size)
        threading.Thread(target=run, name=f'{self.name_tag}-early-replay', daemon=True).start()


    def __wait_for_room(self, dispatcher, capacity, size) -> bool:
        """Wait until the dispatcher queue has room for one more message
        (if it is bounded) and the byte budget for `size` more bytes.
        Returns False if there was none for _REPLAY_ROOM_TIMEOUT_S (the
        consumers are stuck), so that the caller stops waiting."""
        deadline = time.monotonic() + _REPLAY_ROOM_TIMEOUT_S
        while ((capacity is not None and dispatcher.queue_depth >= capacity)
               or not self.__queued_bytes_fit(size)):
            if time.monotonic() >= deadline or dispatcher.is_stopped:
                return False
            time.sleep(0.002)
        return True


    @property
    def decode_failure_count(self) -> int:
        """Issue #15: how many received messages were dropped because
        they could not be decoded as an AgentFlow Parcel, including those
        refused undecoded for exceeding codec.max_payload_bytes (#34)."""
        with self._decode_lock:
            return self._decode_failures


    def __count_decode_failure(self, topic, ex) -> None:
        """Issue #15: count an undecodable message and log it at WARNING
        on the 1st, 2nd, 4th, ... failure and at DEBUG otherwise, without
        a traceback, so that a stream of foreign or malformed messages
        cannot flood the log."""
        with self._decode_lock:
            self._decode_failures += 1
            count = self._decode_failures
        level = logging.WARNING if count & (count - 1) == 0 else logging.DEBUG
        # The reason may carry sender-controlled text (e.g. a refused
        # pickle global's name): escape it and bound its length.
        reason = ensure_size(repr(f"{type(ex).__name__}: {ex}"), 300)
        logger.log(level, self.M(
            f"Dropped an undecodable message on {ensure_size(repr(topic), 200)}: "
            f"{reason} ({count} so far)."))


    def __seen_before(self, message_id: str) -> bool:
        """Issue #4: whether `message_id` arrived recently, recording it
        if not. Handling each id once keeps QoS 1 redeliveries from
        causing duplicate side effects (CLAUDE.md invariant 3). Only the
        last _DEDUP_WINDOW ids are remembered."""
        with self._dedup_lock:
            if message_id in self._recent_message_ids:
                self._recent_message_ids.move_to_end(message_id)
                return True
            self._recent_message_ids[message_id] = None
            if len(self._recent_message_ids) > _DEDUP_WINDOW:
                self._recent_message_ids.popitem(last=False)
            return False


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
            
