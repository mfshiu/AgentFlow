"""End-to-end: a QoS 1 message whose acknowledgement was lost is resent
by paho after the reconnect, and the receiving agent handles it once
(issue #4, RFC-015).

Two thread-mode Agents with their own MqttBroker clients against the
in-process loopback server (127.0.0.1 only). The server routes the
first PUBLISH but withholds its PUBACK, then cuts the sender's
connection; paho resends the message (DUP) on the new connection, the
server routes it again, and the receiver drops it by message id.
"""

import time

import pytest

from agentflow.core.agent import Agent
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


class Recorder(Agent):
    def __init__(self, port):
        super().__init__('recorder', _config(port))
        self.events = []

    def on_activate(self):
        self.subscribe('svc/events', topic_handler=lambda topic, pcl: self.events.append(pcl.content),
                       qos=1)


def test_a_qos_1_message_resent_after_a_reconnect_is_handled_once():
    server = MiniMqttServer()
    recorder, sender = Recorder(server.port), Agent('sender', _config(server.port))
    try:
        recorder.start_thread()
        assert server.wait_until(
            lambda: recorder.is_connected() and 'svc/events' in server.topics_of_connection(1), 10)
        sender.start_thread()
        assert server.wait_until(sender.is_connected, 10)
        sender_connection = server.last_connection_index()

        server.withhold_puback = True               # the acknowledgement is lost
        assert sender.publish('svc/events', 'e1', qos=1) is True
        assert server.wait_until(lambda: recorder.events == ['e1'], 5)

        server.withhold_puback = False
        server.drop(sender_connection)              # only the sender reconnects
        assert server.wait_until(
            lambda: [t for t, _ in server.published()].count('svc/events') == 2, 10), \
            'paho did not resend the unacknowledged QoS 1 message'
        time.sleep(0.5)
        assert recorder.events == ['e1'], 'the resent message was handled twice'
    finally:
        sender.terminate()
        recorder.terminate()
        server.close()
