"""Downstream usage over a real MQTT connection, across processes (issue #12).

The other wire tests run every Agent in thread mode inside the test
process. These cover what they do not:

  - the request style FlowDepot's agents and tests use: a service
    registers `subscribe(topic, "str", handler)` and its handler returns
    a dict; the client publishes a BinaryParcel carrying `topic_return`
    and reads the reply in on_message;
  - a process-mode (spawned) service answering publish_sync from its
    own process;
  - a holon whose child runs in its own process: it registers with its
    parent, and the parent's terminate ends the child process.

All against the in-process loopback server (127.0.0.1 only).
"""

import os
import threading

import pytest

from agentflow.core.agent import Agent
from agentflow.core.parcel import BinaryParcel
from tests.fakes.mini_mqtt_server import MiniMqttServer

pytestmark = pytest.mark.wire

RECOGNIZE = 'svc/recognize'
WHOAMI = 'svc/whoami'
SPAWN_TIMEOUT_S = 30


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


def _subscribed(server, topic):
    last = server.last_connection_index() or 0
    return any(topic in server.topics_of_connection(i) for i in range(1, last + 1))


class RecognizeService(Agent):
    """Shaped like FlowDepot's captcha agent."""

    def __init__(self, port):
        super().__init__('recognize-service', _config(port))

    def on_activate(self):
        self.subscribe(RECOGNIZE, "str", self.recognize)

    def recognize(self, topic, pcl):
        info = pcl.content or {}
        return {'text': info['content'].decode('ascii')[::-1],
                'mime_type': info.get('mime_type'), 'topic': topic}


class Requester(Agent):
    """Shaped like FlowDepot's ValidationAgent test helper."""

    def __init__(self, port):
        super().__init__('requester', _config(port))
        self.replies = []
        self.replied = threading.Event()

    def on_activate(self):
        self.subscribe('return_topic')

    def on_message(self, topic, pcl):
        self.replies.append((topic, pcl.content))
        self.replied.set()


class WhoAmIService(Agent):
    def __init__(self, port):
        super().__init__('whoami-service', _config(port))

    def on_activate(self):
        self.subscribe(WHOAMI, topic_handler=lambda topic, pcl: {
            'pid': os.getpid(), 'echo': pcl.content})


class HolonMember(Agent):
    def __init__(self, name, port):
        super().__init__(name, _config(port))


def test_a_binary_request_gets_its_reply_in_on_message():
    server = MiniMqttServer()
    service, requester = RecognizeService(server.port), Requester(server.port)
    try:
        service.start_thread()
        assert server.wait_until(lambda: _subscribed(server, RECOGNIZE), 10)
        requester.start_thread()
        assert server.wait_until(lambda: _subscribed(server, 'return_topic'), 10)

        assert requester.publish(RECOGNIZE, BinaryParcel(
            {'content': b'56839', 'mime_type': 'image/png'}, 'return_topic'))

        assert requester.replied.wait(10), 'no reply reached on_message'
        assert requester.replies == [('return_topic', {
            'text': '93865', 'mime_type': 'image/png', 'topic': RECOGNIZE})]
    finally:
        requester.terminate()
        service.terminate()
        server.close()


def test_a_process_mode_service_answers_from_its_own_process():
    server = MiniMqttServer()
    service = WhoAmIService(server.port)
    client = Agent('whoami-client', _config(server.port))
    try:
        service.start_process()
        assert server.wait_until(lambda: _subscribed(server, WHOAMI), SPAWN_TIMEOUT_S), \
            'the spawned service never subscribed'
        client.start_thread()
        assert server.wait_until(client.is_connected, 10)

        reply = client.publish_sync(WHOAMI, 'ping', timeout=10)

        assert reply.content['echo'] == 'ping'
        assert reply.content['pid'] == service._agent_worker.work_process.pid != os.getpid()
    finally:
        client.terminate()
        service.terminate()
        server.close()


def test_a_parent_stops_its_process_mode_child():
    server = MiniMqttServer()
    parent, child = HolonMember('p', server.port), HolonMember('k.p', server.port)
    try:
        parent.start_thread()
        assert server.wait_until(parent.is_connected, 10)
        child.start_process()
        assert server.wait_until(lambda: set(parent._children) == {child.agent_id},
                                 SPAWN_TIMEOUT_S), 'the spawned child never registered'
        process = child._agent_worker.work_process
        assert process.is_alive()

        parent.terminate()

        assert server.wait_until(lambda: not process.is_alive(), 20), \
            "the parent's terminate did not end the child process"
    finally:
        child.terminate()
        parent.terminate()
        server.close()
