"""Agent connection-state visibility (issue #2, R-03).

Contracts verified:
  - Agent.is_connected() reflects the broker connection as reported
    through the BrokerNotifier callbacks (_on_connect / _on_disconnect).
  - An unexpected disconnect of a previously connected agent calls
    on_disconnected() exactly once, off the calling (paho) thread.
  - A reconnect calls on_reconnected(), NOT on_connected() again, and
    does not re-run the one-time holon subscriptions (the broker's
    subscription registry restores them).
  - Planned disconnects and disconnects before the first connect do
    not call on_disconnected().
  - Exceptions raised by the hooks never propagate into the caller.
  - Connection state is runtime-only: it does not survive pickling
    (process mode ships the Agent to a fresh child).
  - BrokerNotifier._on_disconnect has a default no-op implementation,
    so existing BrokerNotifier subclasses remain valid.
"""

import pickle
import threading

import pytest

from agentflow.broker.notifier import BrokerNotifier
from agentflow.core.agent import Agent

from tests.fakes.fake_broker import FakeBroker, FakeWorker

# Remove when issue #2 is fixed (strict: an unexpected pass fails).
pytestmark = pytest.mark.xfail(strict=True, reason='issue #2: MQTT auto-reconnect not implemented yet')


HOOK_TIMEOUT = 3.0


class RecordingAgent(Agent):
    def __init__(self, name='conn.parent'):
        super().__init__(name=name, agent_config={})
        self.disconnected = threading.Event()
        self.reconnected = threading.Event()
        self.connected_calls = 0
        self.hook_threads = []
        self.raise_in_hooks = False

    def on_connected(self):
        self.connected_calls += 1

    def on_disconnected(self):
        self.hook_threads.append(threading.current_thread())
        self.disconnected.set()
        if self.raise_in_hooks:
            raise RuntimeError('hook explode')

    def on_reconnected(self):
        self.hook_threads.append(threading.current_thread())
        self.reconnected.set()
        if self.raise_in_hooks:
            raise RuntimeError('hook explode')


def _make_agent(name='conn.parent'):
    a = RecordingAgent(name=name)
    b = FakeBroker(notifier=a)
    a._broker = b
    a._agent_worker = FakeWorker()
    # Normally created by Agent.__activating; _on_connect's helper
    # thread sets it before calling on_connected().
    a._Agent__connected_event = threading.Event()
    return a, b


def test_is_connected_is_false_before_any_connection():
    a, _ = _make_agent()
    assert a.is_connected() is False


def test_is_connected_tracks_connect_and_disconnect():
    a, _ = _make_agent()
    a._on_connect()
    assert a.is_connected() is True
    a._on_disconnect(planned=False)
    assert a.is_connected() is False
    a._on_connect()
    assert a.is_connected() is True


def test_unexpected_disconnect_calls_on_disconnected_off_the_caller_thread():
    a, _ = _make_agent()
    a._on_connect()
    a._on_disconnect(planned=False)
    assert a.disconnected.wait(HOOK_TIMEOUT)
    assert a.hook_threads[0] is not threading.current_thread()


def test_planned_disconnect_does_not_call_on_disconnected():
    a, _ = _make_agent()
    a._on_connect()
    a._on_disconnect(planned=True)
    assert a.is_connected() is False
    assert not a.disconnected.wait(0.3)


def test_disconnect_before_first_connect_does_not_call_on_disconnected():
    a, _ = _make_agent()
    a._on_disconnect(planned=False)
    assert not a.disconnected.wait(0.3)


def test_repeated_disconnect_calls_on_disconnected_once():
    a, _ = _make_agent()
    a._on_connect()
    a._on_disconnect(planned=False)
    assert a.disconnected.wait(HOOK_TIMEOUT)
    a.disconnected.clear()
    a._on_disconnect(planned=False)
    assert not a.disconnected.wait(0.3)


def test_reconnect_calls_on_reconnected_but_not_on_connected_again():
    a, b = _make_agent()
    a._on_connect()
    first_subscribes = list(b.subscribe_calls)
    assert first_subscribes, 'first connect should subscribe holon topics'

    a._on_disconnect(planned=False)
    a._on_connect()

    assert a.reconnected.wait(HOOK_TIMEOUT)
    assert b.subscribe_calls == first_subscribes
    # on_connected runs once, ~1 s after the first connect.
    threading.Event().wait(1.5)
    assert a.connected_calls == 1


def test_hook_exceptions_do_not_propagate():
    a, _ = _make_agent()
    a.raise_in_hooks = True
    a._on_connect()
    a._on_disconnect(planned=False)      # must not raise
    a._on_connect()                      # must not raise
    assert a.disconnected.wait(HOOK_TIMEOUT)
    assert a.reconnected.wait(HOOK_TIMEOUT)
    assert a.is_connected() is True


def test_hooks_without_dunder_name_still_run():
    import functools
    a, _ = _make_agent()
    fired = threading.Event()
    a.on_reconnected = functools.partial(lambda ev: ev.set(), fired)
    a._on_connect()
    a._on_disconnect(planned=False)
    a._on_connect()
    assert fired.wait(HOOK_TIMEOUT)


def test_is_connected_is_false_after_deactivation():
    # MqttBroker fences notifier._on_disconnect once stop() has begun
    # (RFC-010 §F), so the Agent must clear the flag itself.
    a, _ = _make_agent()
    a._on_connect()
    a._Agent__deactivating()
    assert a.is_connected() is False


def test_connection_state_does_not_survive_pickling():
    a = Agent(name='conn.pickle', agent_config={})
    a._broker_connected = True
    clone = pickle.loads(pickle.dumps(a))
    assert clone.is_connected() is False


def test_broker_notifier_default_on_disconnect_is_a_noop():
    class LegacyNotifier(BrokerNotifier):
        def _on_connect(self):
            pass

        def _on_message(self, topic, payload):
            pass

    assert LegacyNotifier()._on_disconnect(planned=False) is None
