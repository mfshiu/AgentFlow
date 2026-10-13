"""End-to-end: a subscription the broker refuses with SUBACK 0x80 is
reported, and not counted as active (issue #53).

A real paho client against the in-process loopback server (127.0.0.1
only), which refuses the topics in its reject_topics set.
"""

import logging

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


def test_a_refused_subscription_is_reported_and_retried_on_reconnect(caplog):
    caplog.set_level(logging.WARNING)
    server = MiniMqttServer()
    server.reject_topics = {'secure/denied'}
    broker = MqttBroker(_Notifier(), wait=True, timeout=5)
    try:
        broker.start({'host': '127.0.0.1', 'port': server.port, 'keepalive': 30,
                      'reconnect_min_delay': 1, 'reconnect_max_delay': 1})
        broker.subscribe('secure/denied', 'str')
        broker.subscribe('ok/topic', 'str')

        def counts():
            metrics = broker.recovery_metrics()
            return metrics.get('active_subscriptions'), metrics.get('failed_subscriptions')
        assert server.wait_until(lambda: counts() == (1, 1), 5), counts()
        # Logged on paho's thread after the count: wait for it too.
        assert server.wait_until(
            lambda: any('secure/denied' in r.getMessage() for r in list(caplog.records)
                        if r.levelno == logging.WARNING), 5)

        server.reject_topics = set()                       # the ACL now allows it
        server.drop_all()
        assert server.wait_until(lambda: counts() == (2, 0), 10), counts()   # granted
        assert 'secure/denied' in server.topics_of_connection(server.last_connection_index())
    finally:
        broker.stop()
        server.close()
