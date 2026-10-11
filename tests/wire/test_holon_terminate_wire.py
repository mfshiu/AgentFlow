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

pytestmark = pytest.mark.wire


class WireAgent(Agent):
    def __init__(self, name, port, holon=None):
        super().__init__(name, {
            'broker': {
                'broker_name': 'local',
                'local': {
                    'broker_type': 'mqtt', 'host': '127.0.0.1', 'port': port,
                    'keepalive': 5,
                    'reconnect_min_delay': 1, 'reconnect_max_delay': 1,
                },
            },
            'holon': holon,
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


def test_replica_terminate_does_not_stop_its_same_name_sibling():
    """Issue #38: two mid-tier replicas named b.a share to_child.b.a.
    Stopping one must not stop the other."""
    server = MiniMqttServer()
    root = WireAgent('a', server.port)
    m1, m2 = WireAgent('b.a', server.port), WireAgent('b.a', server.port)
    leaf = WireAgent('c.b.a', server.port)
    agents = (root, m1, m2, leaf)
    try:
        for a in agents:
            a.start_thread()
        assert _wait(lambda: set(root._children) == {m1.agent_id, m2.agent_id}
                     and leaf.agent_id in m1._children)

        m1.terminate()

        assert _wait(lambda: not m1.is_active(), timeout=15)
        assert not _wait(lambda: not m2.is_active(), timeout=3), \
            "a same-name replica's terminate stopped its sibling"
    finally:
        for a in agents:
            a.terminate()
        server.close()


_CONTROL_SUBJECTS = {'terminate', 'register_child', 'register_parent', 'parent_online'}


@pytest.mark.xfail(strict=True, reason='issue #13: holon control messages are not authenticated')
def test_holon_sharing_a_control_secret_converges_and_terminates():
    """Issue #13: with holon.control_secret, membership and terminate
    still work over a real MQTT link, and every control message on the
    wire is signed."""
    server = MiniMqttServer()
    holon = {'control_secret': 'wire-test-secret-0123456789'}
    root, mid, leaf = (WireAgent(n, server.port, holon) for n in ('a', 'b.a', 'c.b.a'))
    agents = (leaf, mid, root)
    try:
        for a in agents:
            a.start_thread()
        assert _wait(lambda: set(root._children) == {mid.agent_id}
                     and set(mid._children) == {leaf.agent_id})

        root.terminate()

        for a in agents:
            assert _wait(lambda: not a.is_active(), timeout=15), f'{a.name} still active'
        control = [c for c in (Parcel.from_payload(p).content for _, p in server.published())
                   if isinstance(c, dict) and c.get('subject') in _CONTROL_SUBJECTS]
        assert control and all('auth' in c for c in control)
    finally:
        for a in agents:
            a.terminate()
        server.close()
