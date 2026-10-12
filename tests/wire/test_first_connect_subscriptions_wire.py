"""End-to-end: a topic subscribed while the broker is still waiting for
its first CONNACK reaches the broker (issue #54).

A real paho client against the in-process loopback server (127.0.0.1
only), which delays its CONNACK so that the subscribes happen while
the MqttBroker is STARTING.
"""

import threading

import pytest

from agentflow.broker.mqtt_broker import MqttBroker
from tests.fakes.mini_mqtt_server import MiniMqttServer

pytestmark = pytest.mark.wire


class _Notifier:
    def _on_connect(self):
        pass

    def _on_disconnect(self, planned):
        pass

    def _on_message(self, topic, payload):
        pass


def _options(server):
    return {'host': '127.0.0.1', 'port': server.port, 'keepalive': 30,
            'reconnect_min_delay': 1, 'reconnect_max_delay': 1}


def test_a_subscribe_while_start_returned_early_reaches_the_broker():
    server = MiniMqttServer()
    server.connack_delay = 0.5
    broker = MqttBroker(_Notifier(), wait=False)
    try:
        assert broker.start(_options(server)) is True    # returns before the CONNACK
        broker.subscribe('early/topic', 'str')
        assert server.wait_until(
            lambda: 'early/topic' in server.topics_of_connection(server.last_connection_index() or 0), 5)
    finally:
        broker.stop()
        server.close()


def test_a_subscribe_from_another_thread_while_start_waits_reaches_the_broker():
    server = MiniMqttServer()
    server.connack_delay = 0.5
    broker = MqttBroker(_Notifier(), wait=True, timeout=5)
    try:
        starter = threading.Thread(target=broker.start, args=(_options(server),), daemon=True)
        starter.start()
        assert server.wait_until(lambda: server.connect_count() == 1, 5)   # CONNACK pending
        broker.subscribe('early/topic', 'str')
        starter.join(5)
        assert server.wait_until(
            lambda: 'early/topic' in server.topics_of_connection(server.last_connection_index()), 5)
    finally:
        broker.stop()
        server.close()
