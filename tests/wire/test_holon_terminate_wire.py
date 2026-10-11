"""End-to-end: a terminate cascades through a 3-level holon without an
echo storm (issue #5, W-03).

Real Agents (thread mode) with their own MqttBroker clients against the
in-process loopback server (127.0.0.1 only). The server echoes a
client's own publishes back to it, as MQTT 3.1.1 brokers do, which is
what used to make the mid-tier agent re-run its terminate endlessly.
"""

import pytest

from agentflow.core.agent import Agent
from agentflow.core.parcel import Parcel
from tests.fakes.mini_mqtt_server import MiniMqttServer

# Skipped (not xfail) until issue #5 is fixed: on a real connection the
# echo storm would spawn thousands of threads before failing.
pytestmark = [pytest.mark.wire,
              pytest.mark.skip(reason='issue #5: mid-tier terminate echo storm not fixed yet')]


class WireAgent(Agent):
    def __init__(self, name, port):
        super().__init__(name, {
            'broker': {
                'broker_name': 'local',
                'local': {
                    'broker_type': 'mqtt', 'host': '127.0.0.1', 'port': port,
                    'keepalive': 5,
                    'reconnect_min_delay': 1, 'reconnect_max_delay': 1,
                },
            },
        })


def _wait(predicate, timeout=10.0):
    return MiniMqttServer.wait_until(predicate, timeout=timeout)


def test_terminate_cascades_through_three_levels_without_echo_storm():
    server = MiniMqttServer()
    root, mid, leaf = (WireAgent(n, server.port) for n in ('a', 'b.a', 'c.b.a'))
    agents = (leaf, mid, root)
    try:
        for a in agents:                    # leaf first: membership converges (#6)
            a.start_thread()
        assert _wait(lambda: set(root._children) == {mid.agent_id}
                     and set(mid._children) == {leaf.agent_id})

        root.terminate()

        for a in agents:
            assert _wait(lambda: not a.is_active(), timeout=15), f'{a.name} still active'
        mid_terminates = [
            1 for topic, payload in server.published()
            if topic == 'to_child.b.a'
            and Parcel.from_payload(payload).content.get('subject') == 'terminate'
        ]
        assert len(mid_terminates) == 1, f'mid-tier broadcast terminate {len(mid_terminates)} times'
    finally:
        for a in agents:
            a.terminate()
        server.close()
