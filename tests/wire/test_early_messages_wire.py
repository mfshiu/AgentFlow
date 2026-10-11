"""End-to-end: messages a broker delivers right after the CONNACK (the
queued messages of a resumed persistent session, RFC-015 §6) reach the
handlers the agent registers in on_activate or on_connected (follow-up
of issue #17).

Real Agents (thread mode) with their own MqttBroker clients against
the in-process loopback server (127.0.0.1 only), which pushes the
"queued" messages to the client right after its CONNACK.
"""

import time

import pytest

from agentflow.core.agent import Agent
from agentflow.core.parcel import TextParcel
from tests.fakes.mini_mqtt_server import MiniMqttServer

pytestmark = pytest.mark.wire



def _config(port):
    return {
        'broker': {
            'broker_name': 'local',
            'local': {
                'broker_type': 'mqtt', 'host': '127.0.0.1', 'port': port,
                'keepalive': 5, 'reconnect_min_delay': 1, 'reconnect_max_delay': 1,
            },
        },
    }


class Worker(Agent):
    def __init__(self, port):
        super().__init__('worker', _config(port))
        self.jobs = []
        self.unhandled = []

    def handle_job(self, topic, pcl):
        self.jobs.append(pcl.content)

    def on_message(self, topic, pcl):
        self.unhandled.append(topic)


class RegistersInOnActivate(Worker):
    def on_activate(self):
        time.sleep(0.5)             # setup work before the handlers exist
        self.subscribe('jobs', topic_handler=self.handle_job)


class RegistersInOnConnected(Worker):
    def on_connected(self):
        self.subscribe('jobs', topic_handler=self.handle_job)


def _run(agent_class, queued):
    server = MiniMqttServer()
    server.deliver_on_connect = [(topic, TextParcel(content).payload()) for topic, content in queued]
    agent = agent_class(server.port)
    agent.start_thread()
    return server, agent


@pytest.mark.parametrize('agent_class', [RegistersInOnActivate, RegistersInOnConnected])
def test_queued_messages_reach_handlers_registered_in_the_lifecycle_hooks(agent_class):
    server, agent = _run(agent_class, [('jobs', 'j1'), ('jobs', 'j2')])
    try:
        assert server.wait_until(lambda: sorted(agent.jobs) == ['j1', 'j2'], 6), \
            f'jobs={agent.jobs} unhandled={agent.unhandled}'
        assert 'jobs' not in agent.unhandled
    finally:
        agent.terminate()
        server.close()


def test_a_queued_message_nobody_handles_still_reaches_on_message():
    server, agent = _run(RegistersInOnActivate, [('stray', 's1')])
    try:
        assert server.wait_until(lambda: 'stray' in agent.unhandled, 6)
    finally:
        agent.terminate()
        server.close()
