"""Shared fixtures for the deterministic AgentFlow test suite.

Design constraints (Phase 1):
  - No test may open a real socket, spawn a subprocess, or start a real
    thread that outlives the test.
  - No test may depend on a fixed host, port, credential, or environment
    variable.
  - All paho.mqtt.client.Client interactions must go through a MagicMock
    injected by the `patched_client_class` fixture.
"""

from unittest.mock import MagicMock

import pytest


@pytest.fixture
def fake_client():
    """A MagicMock that stands in for `paho.mqtt.client.Client`.

    Attribute assignment on a MagicMock is preserved verbatim, so tests can
    assert `fake_client.on_connect == broker._on_connect` after `start()`.
    Method calls are recorded on the mock's `call_args` / `call_args_list`.
    """
    return MagicMock(name="PahoMqttClient")


@pytest.fixture
def notifier():
    """A MagicMock that stands in for a `BrokerNotifier` instance.

    The real BrokerNotifier is an ABC with two abstract methods
    (_on_connect, _on_message). A bare MagicMock provides both as
    auto-generated MagicMock attributes, which is what the broker calls.
    """
    return MagicMock(name="BrokerNotifier")


@pytest.fixture
def patched_client_class(monkeypatch, fake_client):
    """Replace `agentflow.broker.mqtt_broker.Client` with a factory that
    returns our `fake_client`.

    This must run before `MqttBroker(...)` is instantiated because the
    constructor calls `Client(...)` immediately (mqtt_broker.py:17).
    Returns the fake_client for direct assertions in tests that also want
    to construct MqttBroker themselves (e.g. wait=True variants).
    """
    monkeypatch.setattr(
        "agentflow.broker.mqtt_broker.Client",
        lambda *args, **kwargs: fake_client,
    )
    return fake_client


@pytest.fixture
def broker(patched_client_class, notifier):
    """A `MqttBroker` wired to the fake paho client.

    Constructed with `wait=False` so `start()` does not block waiting for
    an on_connect callback that no real broker will fire. Tests that need
    to exercise the wait=True path construct their own MqttBroker inline.
    """
    from agentflow.broker.mqtt_broker import MqttBroker

    return MqttBroker(notifier=notifier, wait=False)
