"""Error replies (issue #68, W-42).

When a handler raised, the automatic reply carried `error = str(ex)`:
the exception message, with whatever internals it held (connection
strings, hosts, credentials), went back over the broker to the
requester and to any client subscribed to the reply topic. And
publish_sync returned that Parcel like any other, so a caller reading
only `.content` took a failure for a success whose content was None.

Contracts verified (two agents on an in-memory hub):
  - publish_sync(..., raise_on_error=True) raises RemoteError (with
    the topic, the error and the reply) for a reply carrying an error;
    by default it still returns the Parcel.
  - The automatic error reply names the exception type only; the
    message stays in the responder's log. errors.expose_details=True
    restores the message. A handler's own Parcel.error is forwarded
    as is.
  - A malformed errors section fails at construction.
"""

import logging

import pytest

from agentflow.core import agent as agent_module
from agentflow.core.agent import Agent
from agentflow.core.parcel import Parcel
from tests.fakes.fake_broker import FakeBroker, FakeWorker

xfail_68 = pytest.mark.xfail(strict=True, reason='issue #68: error replies')

SECRET = 'postgresql://app:S3cr3t@10.0.0.5/prod'


class _Hub(FakeBroker):
    """In-memory transport: a publish reaches every agent of the hub
    subscribed to its topic."""

    def __init__(self, notifier, agents):
        super().__init__(notifier=notifier)
        self._agents = agents

    def publish(self, topic, payload, *, qos=0, retain=False):
        super().publish(topic, payload, qos=qos, retain=retain)
        for agent in list(self._agents):
            if topic in agent._broker.subscribed_topics:
                agent._broker.deliver(topic, payload)


@pytest.fixture
def hub():
    agents = []

    def make(name, config=None):
        agent = Agent(name, {'dispatch': {'shutdown_timeout_s': 1.0}, **(config or {})})
        agent._broker = _Hub(agent, agents)
        agent._agent_worker = FakeWorker()
        agents.append(agent)
        return agent
    yield make
    for agent in agents:
        agent.terminate()


def _failing_service(make, config=None):
    service = make('svc', config)

    def handler(topic, pcl):
        raise ConnectionError(f'connect failed: {SECRET}')
    service.subscribe('db/query', topic_handler=handler)
    return service


def _wire_replies(service):
    return [p for t, p in service._broker.publish_calls if t != 'db/query']


@xfail_68
def test_raise_on_error_raises_remote_error(hub):
    _failing_service(hub)
    client = hub('cli')
    with pytest.raises(agent_module.RemoteError) as raised:
        client.publish_sync('db/query', {'sql': 'select 1'}, timeout=3, raise_on_error=True)
    assert raised.value.topic == 'db/query'
    assert 'ConnectionError' in raised.value.error
    assert raised.value.reply.error == raised.value.error


def test_by_default_the_error_parcel_is_returned(hub):
    _failing_service(hub)
    reply = hub('cli').publish_sync('db/query', {'sql': 'select 1'}, timeout=3)
    assert reply.content is None and reply.error


@xfail_68
def test_the_error_reply_withholds_the_exception_message(hub, caplog):
    caplog.set_level(logging.ERROR)
    service = _failing_service(hub)
    reply = hub('cli').publish_sync('db/query', {'sql': 'select 1'}, timeout=3)
    assert SECRET not in reply.error and 'ConnectionError' in reply.error
    assert all(SECRET.encode() not in payload for payload in _wire_replies(service))
    assert any(SECRET in r.getMessage() or (r.exc_info and SECRET in str(r.exc_info[1]))
               for r in caplog.records)                     # still in the responder's log


def test_expose_details_restores_the_message(hub):
    _failing_service(hub, {'errors': {'expose_details': True}})
    reply = hub('cli').publish_sync('db/query', {'sql': 'select 1'}, timeout=3)
    assert reply.error == f'connect failed: {SECRET}'


def test_a_handlers_own_error_is_forwarded_as_is(hub):
    service = hub('svc')

    def handler(topic, pcl):
        failed = Parcel.from_content(None)
        failed.error = 'quota exceeded'
        return failed
    service.subscribe('db/query', topic_handler=handler)
    assert hub('cli').publish_sync('db/query', 'q', timeout=3).error == 'quota exceeded'


@xfail_68
@pytest.mark.parametrize('errors, exception', [
    ('yes', TypeError),
    ({'expose_details': 'yes'}, TypeError),
    ({'expose': True}, ValueError),
])
def test_a_malformed_errors_section_fails_at_construction(errors, exception):
    with pytest.raises(exception, match='errors'):
        Agent('a', {'errors': errors})
