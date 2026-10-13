"""Placeholder and broken broker backends (issue #42, W-41).

`broker_type: redis` and `broker_type: ros` selected classes whose
methods only log. The agent logged "Broker started successfully", ran
on_activate and then dropped every message, so publish_sync timed out
with nothing to say why. The wheel also shipped dds_broker.py, which
needs RTI Connext and never creates the client it uses, and
ros_noetic_broker.py, which cannot be imported at all.

Contracts verified:
  - Starting an agent on the redis or ros placeholder fails at once:
    on_activate does not run, nothing claims the broker started, the
    error says the backend is not available, without a traceback, the
    start is not retried, and the agent's worker ends.
  - Any other NotImplementedError from a broker start is still
    reported as unexpected, with its traceback.
  - The placeholder classes refuse to start, publish or subscribe with
    BrokerNotAvailableError (a NotImplementedError), which names the
    'mqtt' and 'empty' broker types.
  - Every module the package ships can be imported (also issue #41:
    agentflow.logistic.logistic imported a top-level `agent` module).
"""

import importlib
import logging
import pkgutil
import threading
import time
from unittest import mock

import pytest

import agentflow
from agentflow.broker import placeholder_broker
from agentflow.broker.broker_maker import BrokerMaker
from agentflow.broker.empty_broker import EmptyBroker
from agentflow.broker.redis_broker import RedisBroker
from agentflow.broker.ros_broker import RosBroker
from agentflow.core.agent import Agent


xfail_42r = pytest.mark.xfail(strict=True, reason='issue #42 review: BrokerNotAvailableError')

PLACEHOLDERS = [('redis', RedisBroker), ('ros', RosBroker)]


class _Recorder(Agent):
    def __init__(self, name, agent_config):
        super().__init__(name, agent_config)
        self.activated = threading.Event()

    def on_activate(self):
        self.activated.set()


def _wait_until(predicate, timeout_s):
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.02)
    return predicate()


@pytest.mark.parametrize('broker_type', [name for name, _ in PLACEHOLDERS])
def test_an_agent_on_a_placeholder_broker_stops_instead_of_running(broker_type, caplog, monkeypatch):
    caplog.set_level(logging.INFO)
    created = []
    real_create = BrokerMaker.create_broker

    def counting_create(self, kind, notifier):
        created.append(kind)
        return real_create(self, kind, notifier)

    monkeypatch.setattr(BrokerMaker, 'create_broker', counting_create)
    agent = _Recorder(f'placeholder_{broker_type}', {
        'CONCURRENCY_TYPE': 'thread',
        'broker': {'broker_name': 'b', 'b': {'broker_type': broker_type}},
    })
    agent.start()
    try:
        assert _wait_until(lambda: not agent.is_active(), 5.0), 'the agent kept running'
        assert not agent.activated.is_set(), 'on_activate ran on a broker that does nothing'
        assert 'Broker started successfully' not in caplog.text
        assert len(created) == 1, f'the start was retried: {created}'
        errors = [r for r in caplog.records if r.levelno >= logging.ERROR]
        [error] = [r for r in errors if 'not implemented' in r.getMessage()
                   and broker_type in r.getMessage().lower()]
        assert 'Broker not available' in error.getMessage()
        assert error.exc_info is None, 'a configuration error needs no traceback'
    finally:
        agent.terminate()
        _wait_until(lambda: not agent.is_active(), 5.0)


class _UnfinishedBroker(EmptyBroker):
    def start(self, options):
        raise NotImplementedError('some broker feature')


@xfail_42r
def test_another_not_implemented_error_is_still_reported_as_unexpected(caplog, monkeypatch):
    caplog.set_level(logging.INFO)
    monkeypatch.setattr(BrokerMaker, 'create_broker',
                        lambda self, kind, notifier: _UnfinishedBroker(notifier))
    agent = _Recorder('unfinished', {
        'CONCURRENCY_TYPE': 'thread',
        'broker': {'broker_name': 'b', 'b': {'broker_type': 'empty'}},
    })
    agent.start()
    try:
        assert _wait_until(lambda: not agent.is_active(), 5.0), 'the agent kept running'
        assert not agent.activated.is_set()
        [error] = [r for r in caplog.records if 'some broker feature' in r.getMessage()]
        assert 'Unexpected error starting broker' in error.getMessage()
        assert error.exc_info is not None
        assert 'Broker not available' not in caplog.text
    finally:
        agent.terminate()
        _wait_until(lambda: not agent.is_active(), 5.0)


@xfail_42r
@pytest.mark.parametrize('broker_class', [cls for _, cls in PLACEHOLDERS])
def test_a_placeholder_broker_refuses_to_start_publish_or_subscribe(broker_class):
    unavailable = placeholder_broker.BrokerNotAvailableError
    assert issubclass(unavailable, NotImplementedError)
    broker = broker_class(mock.Mock())
    with pytest.raises(unavailable, match='not implemented') as raised:
        broker.start({})
    assert "'mqtt'" in str(raised.value) and "'empty'" in str(raised.value)
    with pytest.raises(unavailable, match='not implemented'):
        broker.publish('t', b'x')
    with pytest.raises(unavailable, match='not implemented'):
        broker.subscribe('t', 'str')
    broker.stop()  # deactivation still calls stop(); it must not raise


def test_every_module_in_the_package_can_be_imported():
    failures = {}
    for info in pkgutil.walk_packages(agentflow.__path__, 'agentflow.'):
        try:
            importlib.import_module(info.name)
        except Exception as ex:  # noqa: BLE001 - report every failure
            failures[info.name] = repr(ex)
    assert not failures, failures
