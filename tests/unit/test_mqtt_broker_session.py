"""MQTT client id, clean session and refused connections (issue #17,
W-44).

MqttBroker built its paho Client without a client id, protocol or
clean-session setting, so every connection used a zero-length id:
brokers assigned a different id each time, per-agent ACLs and
persistent sessions were impossible, and broker logs could not be
matched to agents. A broker refusing the connection for a reason that
retrying cannot fix (rejected id, bad credentials, not authorised) was
retried by the Agent three times, 5 s apart.

Contracts verified (with a capturing stand-in for the paho Client):
  - The client id defaults to '{name}-{agent_id:.12}' from the agent;
    the `client_id` option is a template with {name} and {agent_id}.
  - The `clean_session` option (default True) and protocol MQTT 3.1.1
    reach paho; reconnect_on_failure stays on (issue #2).
  - A malformed option raises ValueError and leaves the broker NEW.
  - A refused CONNACK raises MqttConnectRefused, a ConnectionError
    whose `retryable` is False for 1 / 2 / 4 / 5 (and their MQTT 5
    reason codes), True otherwise. The Agent does not retry a refusal
    that is not retryable.
"""

import threading
import time
from unittest.mock import MagicMock

import pytest
from paho.mqtt.client import MQTTv311
from paho.mqtt.packettypes import PacketTypes
from paho.mqtt.reasoncodes import ReasonCode

from agentflow.broker import mqtt_broker
from agentflow.broker.broker_maker import BrokerMaker
from agentflow.broker.mqtt_broker import MqttBroker
from agentflow.core.agent import Agent
from agentflow.core.agent_worker import WorkerState


AGENT_ID = '0123456789abcdef0123456789abcdef'


@pytest.fixture
def client_kwargs(monkeypatch, fake_client):
    """Replace the paho Client with a factory that records its keyword
    arguments and returns the fake client."""
    calls = []

    def factory(*args, **kwargs):
        calls.append(kwargs)
        return fake_client
    monkeypatch.setattr('agentflow.broker.mqtt_broker.Client', factory)
    return calls


def _agent_notifier(name='b.a', agent_id=AGENT_ID):
    notifier = MagicMock()
    notifier.name = name
    notifier.agent_id = agent_id
    return notifier


def _started(notifier, options):
    broker = MqttBroker(notifier, wait=False)
    assert broker.start(options) is True
    return broker


def test_the_client_id_defaults_to_the_agent_name_and_id(client_kwargs):
    _started(_agent_notifier(), {})
    kwargs = client_kwargs[-1]
    assert kwargs['client_id'] == 'b.a-0123456789ab'
    assert kwargs['clean_session'] is True
    assert kwargs['protocol'] == MQTTv311
    assert kwargs['reconnect_on_failure'] is True


@pytest.mark.parametrize('template, expected', [
    ('{name}', 'b.a'),
    ('robot-7', 'robot-7'),
    ('{name}/{agent_id}', 'b.a/' + AGENT_ID),
    ('{{literal}}-{name}', '{literal}-b.a'),
])
def test_the_client_id_option_is_a_template(client_kwargs, template, expected):
    _started(_agent_notifier(), {'client_id': template})
    assert client_kwargs[-1]['client_id'] == expected


@pytest.mark.parametrize('value, expected', [
    (False, False), (True, True), ('false', False), ('off', False), ('true', True),
])
def test_the_clean_session_option(client_kwargs, value, expected):
    _started(_agent_notifier(), {'clean_session': value})
    assert client_kwargs[-1]['clean_session'] is expected


def test_a_notifier_without_an_identity_still_gets_a_client_id(client_kwargs):
    _started(MagicMock(), {})
    client_id = client_kwargs[-1]['client_id']
    assert client_id.startswith('agentflow-') and len(client_id) == len('agentflow-') + 12


@pytest.mark.parametrize('options', [
    {'client_id': ''},
    {'client_id': 5},
    {'client_id': '{unknown}'},
    {'client_id': '{name'},
    {'client_id': 'x' * 70000},
])
def test_a_malformed_option_raises_and_leaves_the_broker_new(client_kwargs, options):
    broker = MqttBroker(_agent_notifier(), wait=False)
    with pytest.raises(ValueError):
        broker.start(options)
    assert broker._state is WorkerState.NEW
    assert broker.start({}) is True                 # can still start


def _refused_start(fake_client, rc):
    broker = MqttBroker(_agent_notifier(), wait=True, timeout=5.0)

    def refuse():
        broker._on_connect(client=fake_client, userdata=None, flags={},
                           reasonCode=rc, properties=None)
    timer = threading.Timer(0.05, refuse)
    timer.start()
    try:
        started = time.monotonic()
        with pytest.raises(mqtt_broker.MqttConnectRefused) as refused:
            broker.start({})
        assert time.monotonic() - started < 2.0
        return refused.value
    finally:
        timer.cancel()


def _connack(name):
    return ReasonCode(PacketTypes.CONNACK, name)


@pytest.mark.parametrize('rc, retryable', [
    (1, False), (2, False), (3, True), (4, False), (5, False),
    (_connack('Unsupported protocol version'), False),
    (_connack('Client identifier not valid'), False),
    (_connack('Server unavailable'), True),
    (_connack('Bad user name or password'), False),
    (_connack('Not authorized'), False),
])
def test_a_refused_connection_says_whether_retrying_can_help(client_kwargs, fake_client, rc, retryable):
    refused = _refused_start(fake_client, rc)
    assert isinstance(refused, ConnectionError)
    assert refused.retryable is retryable
    assert str(getattr(rc, 'value', rc)) in str(refused)


def test_the_agent_does_not_retry_a_refusal_that_retrying_cannot_fix(monkeypatch):
    refusal = mqtt_broker.MqttConnectRefused(
        'MQTT connect failed: 133 (Client identifier not valid)', 133)
    attempts = []

    class RefusingBroker:
        def start(self, options):
            attempts.append(options)
            raise refusal

        def stop(self):
            pass
    monkeypatch.setattr(BrokerMaker, 'create_broker', lambda self, broker_type, notifier: RefusingBroker())
    agent = Agent('a', {'broker': {'broker_name': 'x', 'x': {'broker_type': 'mqtt'}}})
    agent._Agent__terminate_event = threading.Event()

    started = time.monotonic()
    assert agent._Agent__activating() is False
    assert len(attempts) == 1
    assert time.monotonic() - started < 2.0
