"""Wire-level tests for MqttBroker automatic reconnect (issue #2, R-03).

Unlike tests/unit, these drive a REAL paho client against the
in-process loopback server in tests/fakes/mini_mqtt_server.py, so the
real paho network loop (and its reconnect logic) is exercised. Only
127.0.0.1 sockets are used; no external service is required.

Contracts verified:
  - After an unexpected connection loss the broker reconnects by
    itself, re-subscribes every registered topic on the new
    connection, and inbound/outbound traffic resumes.
  - The notifier is told about the unexpected disconnect.
  - An exception raised by notifier._on_connect does not kill paho's
    network thread (W-43).
  - `reconnect: False` restores the legacy behaviour (no reconnect).
  - stop() during reconnect back-off is bounded and ends all retries.
  - A failed start() (CONNACK refused) leaves no background retries.
"""

import threading
import time

import pytest

from agentflow.broker.mqtt_broker import MqttBroker
from agentflow.core.agent_worker import WorkerState
from tests.fakes.mini_mqtt_server import MiniMqttServer

pytestmark = pytest.mark.wire

# Remove when issue #2 is fixed (strict: an unexpected pass fails).
_ISSUE2 = pytest.mark.xfail(strict=True, reason='issue #2: MQTT auto-reconnect not implemented yet')

FAST_RECONNECT = {'reconnect_min_delay': 1, 'reconnect_max_delay': 1}


class RecordingNotifier:
    """Thread-safe BrokerNotifier stand-in that records every callback."""

    def __init__(self):
        self._lock = threading.Lock()
        self.connects = 0
        self.disconnects = []           # list of `planned` flags
        self.messages = []              # list of (topic, payload)
        self.raise_on_connect = False

    def _on_connect(self):
        with self._lock:
            self.connects += 1
        if self.raise_on_connect:
            raise RuntimeError('notifier explode')

    def _on_disconnect(self, planned):
        with self._lock:
            self.disconnects.append(planned)

    def _on_message(self, topic, payload):
        with self._lock:
            self.messages.append((topic, payload))

    def topics_received(self):
        with self._lock:
            return [t for t, _ in self.messages]


@pytest.fixture
def server():
    srv = MiniMqttServer()
    yield srv
    srv.close()


@pytest.fixture
def make_broker():
    created = []

    def _make(notifier, server, **options):
        b = MqttBroker(notifier, timeout=5)
        opts = {'host': server.host, 'port': server.port, 'keepalive': 5}
        opts.update(options)
        b.start(opts)
        created.append(b)
        return b

    yield _make
    for b in created:
        b.stop(graceful_timeout_s=3)


def _wait(predicate, timeout=10.0):
    return MiniMqttServer.wait_until(predicate, timeout=timeout)


@_ISSUE2
def test_reconnects_and_resubscribes_after_unexpected_drop(server, make_broker):
    notifier = RecordingNotifier()
    b = make_broker(notifier, server, **FAST_RECONNECT)
    b.subscribe('cmd/robot', 'str')
    assert _wait(lambda: 'cmd/robot' in server.topics_of_connection(1), 5)

    server.drop_all()

    assert _wait(lambda: server.connect_count() >= 2), \
        'broker did not reconnect after the connection was dropped'
    new_conn = server.last_connection_index()
    assert _wait(lambda: 'cmd/robot' in server.topics_of_connection(new_conn), 5), \
        'registered topic was not re-subscribed on the new connection'
    assert b.recovery_metrics()['connected'] is True
    assert b.state is WorkerState.RUNNING

    # Inbound traffic resumes on the new connection.
    assert server.publish('cmd/robot', b'after-reconnect') == 1
    assert _wait(lambda: 'cmd/robot' in notifier.topics_received(), 5)


@_ISSUE2
def test_publish_succeeds_again_after_reconnect(server, make_broker):
    notifier = RecordingNotifier()
    b = make_broker(notifier, server, **FAST_RECONNECT)
    server.drop_all()
    assert _wait(lambda: server.connect_count() >= 2)
    assert _wait(lambda: b.recovery_metrics()['connected'], 5)

    b.publish('status/robot', b'alive')

    assert _wait(lambda: ('status/robot', b'alive') in server.published(), 5)


@_ISSUE2
def test_unexpected_drop_is_reported_to_notifier(server, make_broker):
    notifier = RecordingNotifier()
    make_broker(notifier, server, **FAST_RECONNECT)

    server.drop_all()

    assert _wait(lambda: notifier.disconnects == [False], 10)
    assert _wait(lambda: notifier.connects >= 2)


@_ISSUE2
def test_notifier_on_connect_exception_does_not_kill_network_thread(
    server, make_broker,
):
    notifier = RecordingNotifier()
    notifier.raise_on_connect = True
    b = make_broker(notifier, server, **FAST_RECONNECT)
    b.subscribe('cmd/robot', 'str')
    assert _wait(lambda: 'cmd/robot' in server.topics_of_connection(1), 5), \
        'paho network thread died after notifier._on_connect raised'

    server.publish('cmd/robot', b'x')
    assert _wait(lambda: 'cmd/robot' in notifier.topics_received(), 5)

    # ... and it still reconnects later on.
    server.drop_all()
    assert _wait(lambda: server.connect_count() >= 2)


@_ISSUE2
def test_reconnect_false_keeps_legacy_no_reconnect_behaviour(server, make_broker):
    notifier = RecordingNotifier()
    b = make_broker(notifier, server, reconnect=False, **FAST_RECONNECT)

    server.drop_all()

    assert _wait(lambda: notifier.disconnects == [False], 10)
    time.sleep(2.5)     # > 2 back-off periods
    assert server.connect_count() == 1
    assert b.recovery_metrics()['connected'] is False


@_ISSUE2
def test_stop_during_reconnect_backoff_is_bounded_and_ends_retries(
    server, make_broker,
):
    notifier = RecordingNotifier()
    b = make_broker(notifier, server, **FAST_RECONNECT)
    server.reject_connections = True
    server.drop_all()
    assert _wait(lambda: server.tcp_accepts >= 2), \
        'broker never attempted to reconnect'

    t0 = time.monotonic()
    assert b.stop(graceful_timeout_s=5) is True
    assert time.monotonic() - t0 < 4.0
    assert b.state is WorkerState.STOPPED

    attempts = server.tcp_accepts
    time.sleep(2.5)
    assert server.tcp_accepts == attempts, 'reconnect attempts continued after stop()'


def _blackhole_reconnects(broker):
    """Simulate a network partition: every reconnect TCP connect blocks
    for the client's connect_timeout and then times out (as a SYN to a
    black-holed host does). Returns an Event set while a connect is
    in flight."""
    client = broker._client
    in_connect = threading.Event()

    def blocked_create_socket():
        in_connect.set()
        time.sleep(client.connect_timeout)
        raise TimeoutError('simulated black-holed host')

    client._create_socket = blocked_create_socket
    return in_connect


@_ISSUE2
def test_stop_during_partitioned_reconnect_stays_within_budget(
    server, make_broker,
):
    b = make_broker(RecordingNotifier(), server, **FAST_RECONNECT)
    in_connect = _blackhole_reconnects(b)
    server.drop_all()
    assert in_connect.wait(10), 'broker never attempted to reconnect'

    t0 = time.monotonic()
    assert b.stop(graceful_timeout_s=5) is True
    assert time.monotonic() - t0 < 4.0
    assert b.state is WorkerState.STOPPED


def test_wait_false_refused_first_connack_leaves_no_background_retries():
    srv = MiniMqttServer(connack_rc=5)      # not authorised
    try:
        b = MqttBroker(RecordingNotifier(), wait=False, timeout=5)
        assert b.start({'host': srv.host, 'port': srv.port, 'keepalive': 5,
                        **FAST_RECONNECT}) is True
        assert _wait(lambda: srv.connect_count() >= 1, 5)
        time.sleep(3.0)     # > 2 back-off periods
        assert srv.tcp_accepts == 1, \
            'wait=False broker kept retrying a refused first connection'
    finally:
        srv.close()


def test_failed_start_leaves_no_background_retries():
    srv = MiniMqttServer(connack_rc=5)      # not authorised
    try:
        b = MqttBroker(RecordingNotifier(), timeout=5)
        with pytest.raises(ConnectionError):
            b.start({'host': srv.host, 'port': srv.port, 'keepalive': 5,
                     **FAST_RECONNECT})
        attempts = srv.tcp_accepts
        time.sleep(2.5)
        assert srv.tcp_accepts == attempts, \
            'paho kept retrying in the background after start() failed'
    finally:
        srv.close()
