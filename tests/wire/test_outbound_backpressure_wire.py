"""End-to-end: a stalled link turns into backpressure instead of
unbounded memory, and the pending bytes drain once the link moves again
(issue #35).

A real Agent (thread mode) with its own MqttBroker client against the
in-process loopback server (127.0.0.1 only), which stops reading the
agent's connection so that its TCP buffers, then paho's queue, fill.
"""

import pytest

from agentflow.core.agent import Agent
from tests.fakes.mini_mqtt_server import MiniMqttServer

pytestmark = pytest.mark.wire

MiB = 1024 * 1024


def _flood(server):
    """The 'flood' publishes the server has read."""
    return [payload for topic, payload in server.published() if topic == 'flood']


def test_a_stalled_link_turns_into_backpressure_and_drains_afterwards():
    server = MiniMqttServer()
    agent = Agent('sender', {'broker': {'broker_name': 'local', 'local': {
        'broker_type': 'mqtt', 'host': '127.0.0.1', 'port': server.port, 'keepalive': 30,
        'reconnect_min_delay': 1, 'reconnect_max_delay': 1, 'max_pending_bytes': 2 * MiB,
    }}})
    try:
        agent.start_thread()
        assert server.wait_until(agent.is_connected, 10)
        connection = server.last_connection_index()
        server.stall(connection)

        payload = b'x' * 65536
        accepted = 0
        for _ in range(1600):                   # 100 MiB: far beyond buffers + limit
            if not agent.publish('flood', payload):
                break
            accepted += 1
        else:
            pytest.fail('1600 publishes over a stalled link were all accepted')
        assert agent._broker.recovery_metrics()['pending_publish_bytes'] <= 2 * MiB
        assert agent.publish_backpressure_count == 1
        # The link really is stalled: a second later the server has read
        # at most the packet it was reading when stalled (a healthy link
        # would have delivered everything). Kernel buffers may still
        # absorb part of the backlog meanwhile (Linux grows them), so
        # the pending bytes are not a reliable sign of the stall.
        server.wait_until(lambda: False, 1.0)
        assert len(_flood(server)) <= 1 < accepted

        server.resume(connection)
        assert server.wait_until(
            lambda: agent._broker.recovery_metrics()['pending_publish_bytes'] == 0, 15)
        assert server.wait_until(lambda: len(_flood(server)) == accepted, 15)
        assert agent.publish('flood', payload) is True
    finally:
        agent.terminate()
        server.close()
