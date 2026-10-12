"""End-to-end: a handler that forwards a request with publish_sync (the
paper's Loading Coordinator, Algorithm 3) gets its answer at once, even
with a single dispatcher worker (issue #7).

Two thread-mode Agents, each with its own MqttBroker client, against the
in-process loopback server (127.0.0.1 only).
"""

import time

import pytest

from agentflow.core.agent import Agent
from agentflow.core.parcel import TextParcel
from tests.fakes.mini_mqtt_server import MiniMqttServer

pytestmark = pytest.mark.wire


def _config(port, **dispatch):
    return {
        'broker': {
            'broker_name': 'local',
            'local': {
                'broker_type': 'mqtt', 'host': '127.0.0.1', 'port': port,
                'keepalive': 5,
                'reconnect_min_delay': 1, 'reconnect_max_delay': 1,
            },
        },
        'dispatch': dispatch,
    }


class Service(Agent):
    def __init__(self, port):
        super().__init__('service', _config(port))

    def on_activate(self):
        self.subscribe('svc/work', topic_handler=lambda topic, pcl: f'done {pcl.content}')


class Coordinator(Agent):
    def __init__(self, port):
        super().__init__('coordinator', _config(port, workers=1))
        self.results = []

    def on_activate(self):
        self.subscribe('jobs', topic_handler=self.forward)

    def forward(self, topic, pcl):
        started = time.monotonic()
        try:
            outcome = self.publish_sync('svc/work', pcl.content, timeout=3).content
        except Exception as ex:
            outcome = ex
        self.results.append((outcome, time.monotonic() - started))


def test_a_coordinator_forwards_from_its_handler_with_one_worker():
    server = MiniMqttServer()
    service, coordinator = Service(server.port), Coordinator(server.port)
    try:
        service.start_thread()
        coordinator.start_thread()
        assert server.wait_until(
            lambda: {'svc/work', 'jobs'} <= {t for _, t in server.subscriptions}, 10)

        server.publish('jobs', TextParcel('job-1').payload())
        assert server.wait_until(lambda: coordinator.results, 6)
        [(outcome, elapsed)] = coordinator.results
        assert outcome == 'done job-1' and elapsed < 2.0
    finally:
        coordinator.terminate()
        service.terminate()
        server.close()
