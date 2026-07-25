"""FakeBroker and FakeWorker for deterministic Agent unit tests.

Neither class opens a socket or spawns long-lived background threads.
FakeBroker.deliver() calls the notifier synchronously; the per-message
thread that Agent._on_message spawns (agent.py:562) is prod-code
behaviour and is outside FakeBroker's control. Tests that need to
observe that thread's completion wrap the registered handler with a
threading.Event-setting spy.
"""

from typing import Any, List, Optional, Tuple

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

    # ------------------------------------------------------------------
    # MessageBroker-shaped interface used by Agent
    # ------------------------------------------------------------------

    def start(self, options: dict):
        self.start_calls.append(dict(options))

    def stop(self):
        self.stop_calls += 1

    def publish(self, topic: str, payload):
        self.publish_calls.append((topic, payload))
        if self.publish_exception is not None:
            raise self.publish_exception
        if self._auto_response_active:
            try:
                req_parcel = Parcel.from_payload(payload)
            except Exception:
                return
            if req_parcel.topic_return:
                # Reply carries no topic_return, so Agent._on_message
                # takes the non-reply branch and does not re-emit.
                reply = TextParcel(self._auto_response_content)
                self.deliver(req_parcel.topic_return, reply.payload())

    def subscribe(self, topic: str, data_type):
        self.subscribe_calls.append((topic, data_type))

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
