"""Verify MqttBroker.start behaviour with a mocked paho Client.

Target module: agentflow.broker.mqtt_broker (real code, no placeholder).
No real network I/O is performed; the paho Client is replaced by a
MagicMock via the `patched_client_class` fixture in tests/conftest.py.
"""

import threading

import pytest


# --------------------------------------------------------------------------
# Callback wiring
# --------------------------------------------------------------------------

def test_start_sets_on_connect_callback(broker, fake_client):
    broker.start({})
    assert fake_client.on_connect == broker._on_connect


def test_start_sets_on_disconnect_callback(broker, fake_client):
    broker.start({})
    assert fake_client.on_disconnect == broker._on_disconnect


def test_start_sets_on_message_callback(broker, fake_client):
    broker.start({})
    assert fake_client.on_message == broker._on_message


# --------------------------------------------------------------------------
# Connection options
# --------------------------------------------------------------------------

def test_start_uses_default_host_port_keepalive_when_options_empty(broker, fake_client):
    broker.start({})
    fake_client.connect.assert_called_once_with("localhost", 1883, 60)
    assert broker.host == "localhost"
    assert broker.port == 1883
    assert broker.keepalive == 60


def test_start_passes_configured_host_port_keepalive(broker, fake_client):
    broker.start({"host": "example.invalid", "port": 12345, "keepalive": 42})
    fake_client.connect.assert_called_once_with("example.invalid", 12345, 42)
    assert broker.host == "example.invalid"
    assert broker.port == 12345
    assert broker.keepalive == 42


def test_start_coerces_string_port_and_keepalive_to_int(broker, fake_client):
    broker.start({"host": "h", "port": "1234", "keepalive": "7"})
    fake_client.connect.assert_called_once_with("h", 1234, 7)
    assert broker.port == 1234
    assert broker.keepalive == 7


# --------------------------------------------------------------------------
# Loop start
# --------------------------------------------------------------------------

def test_start_calls_loop_start_exactly_once(broker, fake_client):
    broker.start({})
    fake_client.loop_start.assert_called_once_with()


def test_start_calls_connect_before_loop_start(broker, fake_client):
    call_order = []
    fake_client.connect.side_effect = lambda *a, **kw: call_order.append("connect")
    fake_client.loop_start.side_effect = lambda *a, **kw: call_order.append("loop_start")
    broker.start({})
    assert call_order == ["connect", "loop_start"]


# --------------------------------------------------------------------------
# Return value + wait=False fast path
# --------------------------------------------------------------------------

def test_start_returns_true_when_wait_is_false(broker, fake_client):
    assert broker.start({}) is True


def test_start_does_not_wait_for_event_when_wait_is_false(broker, fake_client):
    # If wait=False were respected, this call must return before any
    # on_connect callback fires (no timeout, no blocking).
    import time

    start_ts = time.monotonic()
    broker.start({})
    elapsed = time.monotonic() - start_ts
    assert elapsed < 0.5, f"start() blocked for {elapsed:.3f}s despite wait=False"


# --------------------------------------------------------------------------
# wait=True success / timeout / failure paths
# --------------------------------------------------------------------------

def _make_broker(fake_client, notifier, monkeypatch, *, wait, timeout):
    monkeypatch.setattr(
        "agentflow.broker.mqtt_broker.Client",
        lambda *a, **kw: fake_client,
    )
    from agentflow.broker.mqtt_broker import MqttBroker

    return MqttBroker(notifier=notifier, wait=wait, timeout=timeout)


def test_start_wait_true_returns_true_after_successful_on_connect(
    fake_client, notifier, monkeypatch
):
    broker = _make_broker(fake_client, notifier, monkeypatch, wait=True, timeout=5.0)

    def fire_success():
        broker._on_connect(
            client=fake_client,
            userdata=None,
            flags={},
            reasonCode=0,
            properties=None,
        )

    timer = threading.Timer(0.05, fire_success)
    timer.start()
    try:
        assert broker.start({}) is True
    finally:
        timer.cancel()
    notifier._on_connect.assert_called_once_with()


def test_start_wait_true_raises_timeout_when_no_on_connect(
    fake_client, notifier, monkeypatch
):
    broker = _make_broker(fake_client, notifier, monkeypatch, wait=True, timeout=0.1)
    with pytest.raises(TimeoutError):
        broker.start({})
    fake_client.loop_stop.assert_called_once_with()
    fake_client.disconnect.assert_called_once_with()


def test_start_wait_true_raises_connection_error_on_failure_reason_code(
    fake_client, notifier, monkeypatch
):
    broker = _make_broker(fake_client, notifier, monkeypatch, wait=True, timeout=5.0)

    def fire_failure():
        broker._on_connect(
            client=fake_client,
            userdata=None,
            flags={},
            reasonCode=5,
            properties=None,
        )

    timer = threading.Timer(0.05, fire_failure)
    timer.start()
    try:
        with pytest.raises(ConnectionError):
            broker.start({})
    finally:
        timer.cancel()
    fake_client.loop_stop.assert_called_once_with()
    fake_client.disconnect.assert_called_once_with()
    notifier._on_connect.assert_not_called()
