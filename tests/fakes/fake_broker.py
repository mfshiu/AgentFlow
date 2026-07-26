"""FakeBroker and FakeWorker for deterministic Agent unit tests.

Neither class opens a socket or spawns long-lived background threads.
FakeBroker.deliver() calls the notifier synchronously; the per-message
thread that Agent._on_message spawns (agent.py:562) is prod-code
behaviour and is outside FakeBroker's control. Tests that need to
observe that thread's completion wrap the registered handler with a
threading.Event-setting spy.
"""

import threading
from typing import Any, List, Optional, Set, Tuple

from agentflow.core.parcel import Parcel, TextParcel


class FakeWorker:
    """Duck-typed stub for agentflow.core.agent_worker.Worker.

    Agent.publish_sync (agent.py:336) only calls .create_event() on the
    worker. Returning None causes Agent.DataEvent (agent.py:297) to fall
    back to threading.Event(), which needs no multiprocessing setup.

    This class deliberately does NOT inherit from Worker so that
    multiprocessing.set_start_method('spawn') (agent_worker.py:13-14) is
    not triggered as a global side effect across the test suite.
    """

    def create_event(self):
        return None


class FakeBroker:
    """In-memory broker for Agent characterization tests.

    Implements only the methods that Agent actually calls on its
    self._broker attribute:
      - start(options)
      - stop()
      - publish(topic, payload)
      - subscribe(topic, data_type)

    Does NOT define unsubscribe. Agent's current code never calls one
    (grep-verified in agent.py); the empty `unsubscribe_calls` list is
    kept as a positive witness that the API is missing (Risk R-02).
    """

    def __init__(self, notifier):
        self._notifier = notifier
        # Call recorders (append-only; deterministic).
        self.start_calls: List[dict] = []
        self.stop_calls: int = 0
        self.publish_calls: List[Tuple[str, Any]] = []
        self.subscribe_calls: List[Tuple[str, Any]] = []
        self.unsubscribe_calls: List[str] = []
        # Behaviour switches.
        self.publish_exception: Optional[BaseException] = None
        self._auto_response_active: bool = False
        self._auto_response_content: Any = None
        # Self-echo mode + subscription set (added for R-05 loop tests).
        self.subscribed_topics: Set[str] = set()
        self._self_echo_enabled: bool = False
        self.max_publish_dispatches: Optional[int] = None
        self._publish_lock = threading.Lock()

    # ------------------------------------------------------------------
    # MessageBroker-shaped interface used by Agent
    # ------------------------------------------------------------------

    def start(self, options: dict):
        self.start_calls.append(dict(options))

    def stop(self):
        self.stop_calls += 1

    def publish(self, topic: str, payload):
        # Bound check first (thread-safe): once max is reached, silently
        # drop further publishes so bounded reply-loop tests terminate.
        with self._publish_lock:
            if (self.max_publish_dispatches is not None and
                    len(self.publish_calls) >= self.max_publish_dispatches):
                return
            self.publish_calls.append((topic, payload))
        if self.publish_exception is not None:
            raise self.publish_exception
        if self._auto_response_active:
            try:
                req_parcel = Parcel.from_payload(payload)
            except Exception:
                pass
            else:
                if req_parcel.topic_return:
                    # Reply carries no topic_return, so Agent._on_message
                    # takes the non-reply branch and does not re-emit.
                    reply = TextParcel(self._auto_response_content)
                    self.deliver(req_parcel.topic_return, reply.payload())
        if self._self_echo_enabled and topic in self.subscribed_topics:
            # Loop-scenario delivery: send the payload back through the
            # notifier as if a paho self-published message came in.
            try:
                self.deliver(topic, payload)
            except Exception:
                pass

    def subscribe(self, topic: str, data_type):
        self.subscribe_calls.append((topic, data_type))
        self.subscribed_topics.add(topic)

    def unsubscribe(self, topic: str) -> None:
        # Record the call. FakeBroker holds no real subscription table,
        # so there is nothing else to tear down.
        self.unsubscribe_calls.append(topic)
        self.subscribed_topics.discard(topic)

    def enable_self_echo(self, max_dispatches: int = 50) -> None:
        """Turn on self-echo delivery: every publish() to a currently
        subscribed topic will synchronously invoke deliver() with the
        same payload, as a paho broker would for a client subscribed
        to the topic it publishes to. Bounded by `max_dispatches` so
        that reply-loop tests cannot spin forever."""
        self._self_echo_enabled = True
        self.max_publish_dispatches = max_dispatches

    # ------------------------------------------------------------------
    # Test-only helpers
    # ------------------------------------------------------------------

    def deliver(self, topic: str, payload: bytes) -> None:
        """Synchronously invoke notifier._on_message(topic, payload).

        For Agent notifiers, this triggers Agent._on_message
        (agent.py:536-562), which spawns a per-message thread. That
        thread is short-lived and outside FakeBroker's control; callers
        who need to observe its completion should wrap the target
        handler with a spy that sets a threading.Event.
        """
        self._notifier._on_message(topic, payload)

    def auto_respond_with(self, content: Any) -> None:
        """Configure FakeBroker so that every subsequent publish() call
        synchronously delivers TextParcel(content) to the request's
        topic_return, if present."""
        self._auto_response_active = True
        self._auto_response_content = content

    def stop_auto_responding(self) -> None:
        self._auto_response_active = False
        self._auto_response_content = None

    def last_published_parcel(self) -> Optional[Parcel]:
        if not self.publish_calls:
            return None
        _topic, payload = self.publish_calls[-1]
        return Parcel.from_payload(payload)

    def last_subscribed_topic(self) -> Optional[str]:
        if not self.subscribe_calls:
            return None
        return self.subscribe_calls[-1][0]
