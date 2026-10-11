"""End-to-end: the client id and clean-session flag a real Agent sends,
and a broker that rejects the client id (issue #17).

Real paho clients against the in-process loopback server (127.0.0.1
only).
"""

import time
from unittest.mock import MagicMock

import pytest

from agentflow.broker import mqtt_broker
from agentflow.broker.mqtt_broker import MqttBroker
from agentflow.core.agent import Agent
from tests.fakes.mini_mqtt_server import MiniMqttServer

pytestmark = pytest.mark.wire



def _broker_options(port, **extra):
    return {'broker_type': 'mqtt', 'host': '127.0.0.1', 'port': port, 'keepalive': 5,
            'reconnect_min_delay': 1, 'reconnect_max_delay': 1, **extra}


def _agent(name, port, **extra):
    return Agent(name, {'broker': {'broker_name': 'local', 'local': _broker_options(port, **extra)}})


def test_an_agent_connects_with_its_own_client_id_and_a_clean_session():
    server = MiniMqttServer()
    agent = _agent('b.a', server.port)
    try:
        agent.start_thread()
        assert server.wait_until(agent.is_connected, 10)
        assert server.connects[0][1] == f'b.a-{agent.agent_id[:12]}'
        assert server.clean_sessions[0][1] is True
    finally:
        agent.terminate()
        server.close()


def test_a_configured_client_id_and_persistent_session_reach_the_broker():
    server = MiniMqttServer()
    agent = _agent('b.a', server.port, client_id='{name}', clean_session=False)
    try:
        agent.start_thread()
        assert server.wait_until(agent.is_connected, 10)
        assert server.connects[0][1] == 'b.a'
        assert server.clean_sessions[0][1] is False
    finally:
        agent.terminate()
        server.close()


def test_a_rejected_client_id_fails_fast_and_is_not_retried():
    server = MiniMqttServer(connack_rc=2)       # identifier rejected
    try:
        broker = MqttBroker(MagicMock(), timeout=5)
        started = time.monotonic()
        with pytest.raises(mqtt_broker.MqttConnectRefused) as refused:
            broker.start(_broker_options(server.port))
        assert time.monotonic() - started < 2.0
        assert refused.value.retryable is False
        assert 'identifier' in str(refused.value).lower()
        time.sleep(1.5)
        assert server.connect_count() == 1, 'the refused connection was retried'
    finally:
        server.close()
