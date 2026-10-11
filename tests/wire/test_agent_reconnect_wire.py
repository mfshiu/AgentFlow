"""End-to-end wire test for issue #2: a real Agent (thread mode) on a
real MqttBroker survives a broker-side connection drop.

Uses the in-process loopback server from tests/fakes/mini_mqtt_server.py
(127.0.0.1 only).
"""

import threading

import pytest

from agentflow.core.agent import Agent
from agentflow.core.parcel import TextParcel
from tests.fakes.mini_mqtt_server import MiniMqttServer

pytestmark = pytest.mark.wire


class EchoAgent(Agent):
    def __init__(self, port):
        super().__init__('wireagent', {
            'broker': {
                'broker_name': 'local',
                'local': {
                    'broker_type': 'mqtt', 'host': '127.0.0.1', 'port': port,
                    'keepalive': 5,
                    'reconnect_min_delay': 1, 'reconnect_max_delay': 1,
                },
            },
        })
        self.received = []
        self.got_message = threading.Event()
        self.disconnected = threading.Event()
        self.reconnected = threading.Event()
        self.connected_at_disconnect = None

    def on_activate(self):
        self.subscribe('svc/echo', topic_handler=self.handle)

    def handle(self, topic, pcl):
        self.received.append(pcl.content)
        self.got_message.set()

    def on_disconnected(self):
        self.connected_at_disconnect = self.is_connected()
        self.disconnected.set()

    def on_reconnected(self):
        self.reconnected.set()


def test_agent_keeps_working_after_broker_drops_connection():
    server = MiniMqttServer()
    agent = EchoAgent(server.port)
    try:
        agent.start_thread()
        assert server.wait_until(
            lambda: agent.is_connected()
            and 'svc/echo' in server.topics_of_connection(1), 10)

        server.drop_all()

        assert agent.disconnected.wait(10), 'on_disconnected() was not called'
        assert agent.connected_at_disconnect is False
        assert agent.reconnected.wait(10), 'on_reconnected() was not called'
        assert agent.is_connected() is True
        new_conn = server.last_connection_index()
        assert server.wait_until(
            lambda: 'svc/echo' in server.topics_of_connection(new_conn), 5)

        server.publish('svc/echo', TextParcel('after-reconnect').payload())
        assert agent.got_message.wait(5)
        assert agent.received == ['after-reconnect']
    finally:
        agent.terminate()
        server.close()
    assert agent.is_connected() is False


def test_terminate_during_partitioned_reconnect_stops_cleanly():
    """A reconnect TCP connect blocked by a partition must not push
    terminate() past the worker's 5 s stop budget (no STOP_TIMEOUT /
    'PROCESS RESTART REQUIRED')."""
    from agentflow.core.agent_worker import WorkerState

    server = MiniMqttServer()
    agent = EchoAgent(server.port)
    try:
        agent.start_thread()
        assert server.wait_until(agent.is_connected, 10)
        client = agent._broker._client
        in_connect = threading.Event()

        def blocked_create_socket():    # simulated black-holed host
            in_connect.set()
            threading.Event().wait(client.connect_timeout)
            raise TimeoutError('simulated black-holed host')

        client._create_socket = blocked_create_socket
        server.drop_all()
        assert in_connect.wait(10)
    finally:
        agent.terminate()
        server.close()
    assert agent._agent_worker.state is WorkerState.STOPPED
