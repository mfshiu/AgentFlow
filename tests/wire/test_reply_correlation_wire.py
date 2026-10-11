"""End-to-end: publish_sync between two real Agents over MQTT carries a
correlation id on the request and gets it back on the reply (issue #36).

Two thread-mode Agents, each with its own MqttBroker client, against the
in-process loopback server (127.0.0.1 only).
"""

import pytest

from agentflow.core.agent import Agent
from agentflow.core.parcel import Parcel
from tests.fakes.mini_mqtt_server import MiniMqttServer

pytestmark = pytest.mark.wire


def _config(port):
    return {
        'broker': {
            'broker_name': 'local',
            'local': {
                'broker_type': 'mqtt', 'host': '127.0.0.1', 'port': port,
                'keepalive': 5,
                'reconnect_min_delay': 1, 'reconnect_max_delay': 1,
            },
        },
    }


class EchoService(Agent):
    def __init__(self, port):
        super().__init__('echo-service', _config(port))

    def on_activate(self):
        self.subscribe('svc/echo', topic_handler=lambda topic, pcl: {'echo': pcl.content})


def test_publish_sync_round_trip_is_correlated():
    server = MiniMqttServer()
    service, client = EchoService(server.port), Agent('client', _config(server.port))
    try:
        service.start_thread()
        assert server.wait_until(
            lambda: service.is_connected() and 'svc/echo' in server.topics_of_connection(1), 10)
        client.start_thread()
        assert server.wait_until(client.is_connected, 10)

        reply = client.publish_sync('svc/echo', 'ping', timeout=5)

        assert reply.content == {'echo': 'ping'}
        request = [Parcel.from_payload(p) for t, p in server.published() if t == 'svc/echo'][0]
        assert request.correlation_id
        assert reply.correlation_id == request.correlation_id
    finally:
        client.terminate()
        service.terminate()
        server.close()
