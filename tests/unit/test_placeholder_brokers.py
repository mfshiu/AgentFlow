"""Placeholder and broken broker backends (issue #42, W-41).

`broker_type: redis` and `broker_type: ros` selected classes whose
methods only log. The agent logged "Broker started successfully", ran
on_activate and then dropped every message, so publish_sync timed out
with nothing to say why. dds_broker.py and ros_noetic_broker.py were
shipped in the wheel although neither can be imported.

Contracts verified:
  - Starting an agent on the redis or ros placeholder fails at once:
    on_activate does not run, nothing claims the broker started, the
    error says the backend is not implemented, the start is not
    retried, and the agent stops.
  - The placeholder classes refuse to start, publish or subscribe.
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
from agentflow.broker.broker_maker import BrokerMaker
from agentflow.broker.redis_broker import RedisBroker
from agentflow.broker.ros_broker import RosBroker
from agentflow.core.agent import Agent

xfail_42 = pytest.mark.xfail(strict=True, reason='issue #42: placeholder brokers')
xfail_41_42 = pytest.mark.xfail(strict=True, reason='issues #41, #42: modules that cannot be imported')

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


@xfail_42
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
        errors = [r.getMessage() for r in caplog.records if r.levelno >= logging.ERROR]
        assert any('not implemented' in m and broker_type in m.lower() for m in errors), errors
    finally:
        agent.terminate()
        _wait_until(lambda: not agent.is_active(), 5.0)


@xfail_42
@pytest.mark.parametrize('broker_class', [cls for _, cls in PLACEHOLDERS])
def test_a_placeholder_broker_refuses_to_start_publish_or_subscribe(broker_class):
    broker = broker_class(mock.Mock())
    with pytest.raises(NotImplementedError, match='not implemented'):
        broker.start({})
    with pytest.raises(NotImplementedError, match='not implemented'):
        broker.publish('t', b'x')
    with pytest.raises(NotImplementedError, match='not implemented'):
        broker.subscribe('t', 'str')
    broker.stop()  # deactivation still calls stop(); it must not raise


@xfail_41_42
def test_every_module_in_the_package_can_be_imported():
    failures = {}
    for info in pkgutil.walk_packages(agentflow.__path__, 'agentflow.'):
        try:
            importlib.import_module(info.name)
        except Exception as ex:  # noqa: BLE001 - report every failure
            failures[info.name] = repr(ex)
    assert not failures, failures
