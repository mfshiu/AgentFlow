"""FakeBus: an in-memory, MQTT-like hub shared by several Agents.

Unlike FakeBroker (one Agent, synchronous delivery to itself), FakeBus
lets several Agents talk to each other the way they do over a real MQTT
broker:

  - exact-topic routing (no wildcards), including delivery back to the
    publishing client when it is subscribed to the topic (MQTT 3.1.1
    has no no-local option);
  - subscriptions survive a disconnect (MqttBroker keeps a registry and
    re-subscribes on reconnect), but messages published while a
    subscriber is disconnected are lost (clean session, QoS 0).

Deliveries call Agent._on_message, which hands the message to the
Agent's dispatcher threads, so handling is asynchronous: tests wait for
conditions with FakeBus.wait_until.

No sockets, no real worker threads (each Agent gets a FakeWorker).
"""

import threading
import time
from collections import defaultdict
from typing import Callable, Dict, List, Set, Tuple

from tests.fakes.fake_broker import FakeWorker


class FakeBusBroker:
    """The MessageBroker-shaped object an Agent sees on a FakeBus."""

    def __init__(self, bus: 'FakeBus', notifier):
        self._bus = bus
        self._notifier = notifier
        self.connected = False
        self.published: List[Tuple[str, bytes]] = []
        self.published_qos: List[int] = []          # parallel to published
        self.subscribe_qos: Dict[str, int] = {}     # topic -> last requested QoS

    # -- MessageBroker interface used by Agent ------------------------------

    def start(self, options: dict):
        pass

    def stop(self):
        self.connected = False
        self._bus._detach(self)
        return True

    def subscribe(self, topic: str, data_type, *, qos: int = 0):
        self.subscribe_qos[topic] = qos
        self._bus._subscribe(self, topic)

    def unsubscribe(self, topic: str):
        self._bus._unsubscribe(self, topic)

    def publish(self, topic: str, payload, *, qos: int = 0, retain: bool = False):
        if not self.connected:
            raise RuntimeError(f'FakeBusBroker: publish while disconnected ({topic!r})')
        self.published.append((topic, payload))
        self.published_qos.append(qos)
        self._bus._route(topic, payload)

    def topics_published(self) -> List[str]:
        return [t for t, _ in self.published]


class FakeBus:
    def __init__(self, max_deliveries=None):
        """`max_deliveries` bounds the total number of deliveries; any
        beyond it are dropped (and counted in `dropped`), so a test that
        provokes a message storm terminates instead of spinning."""
        self._max_deliveries = max_deliveries
        self.dropped = 0
        self._lock = threading.Lock()
        self._subs: Dict[str, Set[FakeBusBroker]] = defaultdict(set)
        # Every delivery as (receiving broker, topic, payload).
        self.delivered: List[Tuple[FakeBusBroker, str, bytes]] = []

    # -- wiring ---------------------------------------------------------------

    def attach(self, agent) -> FakeBusBroker:
        """Give `agent` a FakeBusBroker and a FakeWorker, as if its broker
        had been started in thread mode. Does not connect it yet."""
        broker = FakeBusBroker(self, agent)
        agent._broker = broker
        agent._agent_worker = FakeWorker()
        # Normally created by Agent.__activating; _on_connect's helper
        # thread sets it before calling on_connected().
        agent._Agent__connected_event = threading.Event()
        return broker

    def connect(self, agent) -> None:
        """Simulate a successful (re)connect: CONNACK, then the broker
        notifies the Agent."""
        agent._broker.connected = True
        agent._on_connect()

    def disconnect(self, agent) -> None:
        """Simulate an unexpected connection loss."""
        agent._broker.connected = False
        agent._on_disconnect(planned=False)

    def remove(self, agent) -> None:
        """Simulate an agent that is gone for good (crashed / stopped)."""
        agent._broker.connected = False
        self._detach(agent._broker)

    # -- helpers ----------------------------------------------------------------

    @staticmethod
    def wait_until(predicate: Callable[[], bool], timeout: float = 5.0,
                   interval: float = 0.01) -> bool:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if predicate():
                return True
            time.sleep(interval)
        return predicate()

    # -- internals --------------------------------------------------------------

    def _subscribe(self, broker: FakeBusBroker, topic: str) -> None:
        with self._lock:
            self._subs[topic].add(broker)

    def _unsubscribe(self, broker: FakeBusBroker, topic: str) -> None:
        with self._lock:
            self._subs[topic].discard(broker)

    def _detach(self, broker: FakeBusBroker) -> None:
        with self._lock:
            for subscribers in self._subs.values():
                subscribers.discard(broker)

    def _route(self, topic: str, payload) -> None:
        with self._lock:
            targets = [b for b in self._subs.get(topic, ()) if b.connected]
            if self._max_deliveries is not None:
                room = max(0, self._max_deliveries - len(self.delivered))
                self.dropped += len(targets) - min(room, len(targets))
                targets = targets[:room]
            self.delivered.extend((b, topic, payload) for b in targets)
        for b in targets:
            b._notifier._on_message(topic, payload)
