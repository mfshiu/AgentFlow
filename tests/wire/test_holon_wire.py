"""End-to-end holon membership over a real MQTT connection (issue #6).

Real Agents (thread mode), each with its own MqttBroker client, against
the in-process loopback server from tests/fakes/mini_mqtt_server.py
(127.0.0.1 only). The child starts before its parent, the server then
drops every connection, and finally the parent is replaced by a new
instance; membership must converge each time, and the new parent's
terminate must reach the child.
"""

import pytest

from agentflow.core.agent import Agent
from tests.fakes.mini_mqtt_server import MiniMqttServer

# Remove the xfail when issue #6 is fixed (strict: an unexpected pass fails).
pytestmark = [pytest.mark.wire,
              pytest.mark.xfail(strict=True, reason='issue #6: holon membership does not converge yet')]


class WireHolonAgent(Agent):
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
        self.registered_children = []
        self.registered_parents = []

    def on_register_child(self, child_id, child_info):
        self.registered_children.append(child_id)

    def on_register_parent(self, parent_id, parent_info):
        self.registered_parents.append(parent_id)


def _wait(predicate, timeout=10.0):
    return MiniMqttServer.wait_until(predicate, timeout=timeout)


def _membership(parent, child):
    return (set(parent._children) == {child.agent_id}
            and parent.agent_id in child._parents)


def test_holon_membership_converges_across_start_order_drop_and_restart():
    server = MiniMqttServer()
    child = WireHolonAgent('k.p', server.port)
    parent = WireHolonAgent('p', server.port)
    new_parent = None
    try:
        child.start_thread()                    # child first
        assert _wait(child.is_connected)
        parent.start_thread()
        assert _wait(lambda: _membership(parent, child)), \
            'parent did not learn about a child that started first'

        server.drop_all()                       # broker drops everyone
        assert _wait(lambda: server.connect_count() >= 4)
        assert _wait(lambda: parent.is_connected() and child.is_connected())
        assert _wait(lambda: False, timeout=1.0) is False   # let re-registration settle
        assert _membership(parent, child)
        assert parent.registered_children == [child.agent_id]
        assert child.registered_parents == [parent.agent_id]

        parent._broker.stop()                   # parent "crashes" (no cascade)
        new_parent = WireHolonAgent('p', server.port)
        new_parent.start_thread()
        assert _wait(lambda: _membership(new_parent, child)), \
            'restarted parent did not learn about the existing child'

        new_parent.terminate()                  # cascades to the child
        assert _wait(lambda: not child.is_active()), \
            "parent's terminate did not reach the child"
    finally:
        for a in (new_parent, parent, child):
            if a is not None:
                a.terminate()
        server.close()
