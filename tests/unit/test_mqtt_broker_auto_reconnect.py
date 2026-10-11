"""Unit tests for MqttBroker automatic reconnect (issue #2, R-03).

Mock-based counterpart of tests/wire/test_mqtt_broker_reconnect_wire.py:
pins the configuration and callback contracts without any socket.

Contracts verified:
  - The paho Client is built with reconnect_on_failure=True.
  - start() configures the reconnect back-off (defaults 1 s .. 30 s,
    overridable via `reconnect_min_delay` / `reconnect_max_delay`;
    invalid values fall back to the defaults instead of raising).
  - `reconnect: False` ends paho's network loop on an unexpected drop
    (legacy behaviour) by calling client.loop_stop() from the callback.
    (client.disconnect() would NOT work there: paho's _loop_rc_handle
    resets the state to CONNECTION_LOST after on_disconnect returns.)
  - _on_disconnect reports to notifier._on_disconnect(planned) unless
    stop() has fenced callbacks; notifier exceptions are contained.
  - An exception from notifier._on_connect is contained (W-43).
"""

import pytest

from agentflow.broker.mqtt_broker import MqttBroker


def _fire_connect(broker, fake_client, rc=0):
    broker._on_connect(client=fake_client, userdata=None, flags={},
                       reasonCode=rc, properties=None)


def _fire_disconnect(broker, fake_client, rc=7):
    broker._on_disconnect(client=fake_client, userdata=None, _flags={},
                          reasonCode=rc, _properties=None)


# --------------------------------------------------------------------------
# Configuration
# --------------------------------------------------------------------------

def test_client_is_constructed_with_reconnect_on_failure_enabled(
    monkeypatch, fake_client, notifier,
):
    captured = {}

    def factory(*args, **kwargs):
        captured.update(kwargs)
        return fake_client

    monkeypatch.setattr('agentflow.broker.mqtt_broker.Client', factory)
    MqttBroker(notifier=notifier, wait=False)
    assert captured.get('reconnect_on_failure') is True


def test_start_configures_default_reconnect_backoff(broker, fake_client):
    broker.start({})
    fake_client.reconnect_delay_set.assert_called_once_with(
        min_delay=1, max_delay=30)


def test_start_honours_reconnect_delay_options(broker, fake_client):
    broker.start({'reconnect_min_delay': 2, 'reconnect_max_delay': 60})
    fake_client.reconnect_delay_set.assert_called_once_with(
        min_delay=2, max_delay=60)


@pytest.mark.parametrize('options', [
    {'reconnect_min_delay': 'abc'},
    {'reconnect_max_delay': None},
    {'reconnect_min_delay': 0},
    {'reconnect_min_delay': 10, 'reconnect_max_delay': 5},
    {'reconnect_max_delay': float('inf')},
    {'reconnect_min_delay': float('-inf')},
    {'reconnect_min_delay': float('nan')},
])
def test_invalid_reconnect_delay_options_fall_back_to_defaults(
    broker, fake_client, options,
):
    broker.start(options)       # must not raise
    fake_client.reconnect_delay_set.assert_called_once_with(
        min_delay=1, max_delay=30)


def test_start_sets_default_connect_timeout(broker, fake_client):
    # A blocking reconnect TCP connect must fit inside the 5 s stop
    # budgets (paho's own default is 5 s).
    broker.start({})
    assert fake_client.connect_timeout == 2.0


def test_start_honours_connect_timeout_option(broker, fake_client):
    broker.start({'connect_timeout': 1.5})
    assert fake_client.connect_timeout == 1.5


@pytest.mark.parametrize('value', ['x', 0, -1, None, float('inf'), float('nan')])
def test_invalid_connect_timeout_falls_back_to_default(broker, fake_client, value):
    broker.start({'connect_timeout': value})    # must not raise
    assert fake_client.connect_timeout == 2.0


# --------------------------------------------------------------------------
# reconnect: False (legacy opt-out)
# --------------------------------------------------------------------------

def test_reconnect_false_ends_paho_loop_on_unexpected_drop(broker, fake_client):
    broker.start({'reconnect': False})
    _fire_connect(broker, fake_client)
    _fire_disconnect(broker, fake_client, rc=7)
    fake_client.loop_stop.assert_called_once_with()


def test_reconnect_enabled_leaves_paho_loop_running_on_unexpected_drop(
    broker, fake_client,
):
    broker.start({})
    _fire_connect(broker, fake_client)
    _fire_disconnect(broker, fake_client, rc=7)
    fake_client.loop_stop.assert_not_called()
    fake_client.disconnect.assert_not_called()


def test_wait_false_first_connection_failure_does_not_retry_forever(
    broker, fake_client,
):
    # wait=False: nobody awaits the first CONNACK and stop() refuses
    # STARTING, so a refused / dropped FIRST connection must end the
    # network loop (legacy behaviour) instead of retrying forever.
    broker.start({})
    _fire_disconnect(broker, fake_client, rc=5)
    fake_client.loop_stop.assert_called_once_with()


def test_wait_true_first_connection_failure_keeps_retrying_within_start(
    patched_client_class, notifier,
):
    # wait=True: start() is still waiting and its failure paths run the
    # rollback primitive, so paho may retry within the startup budget.
    b = MqttBroker(notifier=notifier, wait=True)
    _fire_disconnect(b, patched_client_class, rc=7)
    patched_client_class.loop_stop.assert_not_called()


# --------------------------------------------------------------------------
# Leftover socket after stop (reconnect raced with stop)
# --------------------------------------------------------------------------

def _leftover_socket(fake_client):
    import socket as socket_mod
    sock = socket_mod.socket(socket_mod.AF_INET, socket_mod.SOCK_STREAM)
    fake_client.socket.return_value = sock
    return sock


def test_stop_closes_socket_left_by_reconnect_racing_stop(broker, fake_client):
    sock = _leftover_socket(fake_client)
    broker.start({})
    _fire_connect(broker, fake_client)
    assert broker.stop() is True
    assert sock.fileno() == -1


def test_start_rollback_closes_leftover_socket(broker, fake_client):
    sock = _leftover_socket(fake_client)
    assert broker._run_client_shutdown_primitive() is True
    assert sock.fileno() == -1


# --------------------------------------------------------------------------
# Disconnect reporting
# --------------------------------------------------------------------------

def test_unexpected_disconnect_is_reported_to_notifier(
    broker, fake_client, notifier,
):
    _fire_connect(broker, fake_client)
    _fire_disconnect(broker, fake_client, rc=7)
    notifier._on_disconnect.assert_called_once_with(planned=False)


def test_disconnect_after_stop_is_not_reported_to_notifier(
    broker, fake_client, notifier,
):
    broker.start({})
    _fire_connect(broker, fake_client)
    broker.stop()
    _fire_disconnect(broker, fake_client, rc=0)
    notifier._on_disconnect.assert_not_called()


def test_notifier_on_disconnect_exception_is_contained(
    broker, fake_client, notifier,
):
    notifier._on_disconnect.side_effect = RuntimeError('notifier explode')
    _fire_connect(broker, fake_client)
    _fire_disconnect(broker, fake_client, rc=7)     # must not raise
    assert broker.recovery_metrics()['connected'] is False


# --------------------------------------------------------------------------
# W-43: notifier._on_connect exceptions must not escape into paho
# --------------------------------------------------------------------------

def test_notifier_on_connect_exception_is_contained(
    broker, fake_client, notifier,
):
    notifier._on_connect.side_effect = RuntimeError('notifier explode')
    _fire_connect(broker, fake_client)              # must not raise
    assert broker._connected_evt.is_set()
    assert broker.recovery_metrics()['connected'] is True


def test_recovery_still_runs_when_notifier_on_connect_raises(
    broker, fake_client, notifier,
):
    notifier._on_connect.side_effect = RuntimeError('notifier explode')
    _fire_connect(broker, fake_client)
    broker.subscribe('T', 'str')
    _fire_disconnect(broker, fake_client)
    fake_client.subscribe.reset_mock()

    _fire_connect(broker, fake_client)

    fake_client.subscribe.assert_called_once_with(topic='T')
