"""Error replies (issue #68, W-42).

When a handler raised, the automatic reply carried `error = str(ex)`:
the exception message, with whatever internals it held (connection
strings, hosts, credentials), went back over the broker to the
requester and to any client subscribed to the reply topic. And
publish_sync returned that Parcel like any other, so a caller reading
only `.content` took a failure for a success whose content was None.

Contracts verified (two agents on an in-memory hub):
  - publish_sync(..., raise_on_error=True) raises RemoteError (with
    the topic, the error and the reply) for a reply carrying a
    non-empty error; by default it still returns the Parcel.
    RemoteError pickles (e.g. across a process pool), and its message
    bounds and escapes the remote error text.
  - The automatic error reply names the exception type only; the
    message stays in the responder's log. errors.expose_details=True
    restores the message. A handler's own Parcel.error is forwarded
    as is.
  - A malformed errors section fails at construction.
"""

import logging
import pickle

import pytest

from agentflow.core import agent as agent_module
from agentflow.core.agent import Agent
from agentflow.core.parcel import Parcel
from tests.fakes.fake_broker import FakeBroker, FakeWorker

xfail_68r = pytest.mark.xfail(strict=True, reason='issue #68 review: fixed in the next commit')

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


def test_raise_on_error_raises_remote_error(hub):
    _failing_service(hub)
    client = hub('cli')
    with pytest.raises(agent_module.RemoteError) as raised:
        client.publish_sync('db/query', {'sql': 'select 1'}, timeout=3, raise_on_error=True)
    assert raised.value.topic == 'db/query'
    assert 'ConnectionError' in raised.value.error
    assert raised.value.reply.error == raised.value.error
    assert raised.value.reply.correlation_id is None    # RFC-014: cleared before raising


def _service_replying_error(make, error):
    service = make('svc')

    def handler(topic, pcl):
        failed = Parcel.from_content('partial')
        failed.error = error
        return failed
    service.subscribe('db/query', topic_handler=handler)


@pytest.mark.parametrize('error', [pytest.param('', marks=xfail_68r), None])
def test_an_empty_error_is_not_raised(hub, error):
    _service_replying_error(hub, error)
    reply = hub('cli').publish_sync('db/query', 'q', timeout=3, raise_on_error=True)
    assert reply.content == 'partial'


@xfail_68r
def test_remote_error_pickles():
    reply = Parcel.from_content(None)
    reply.error = 'ConnectionError'
    error = agent_module.RemoteError('db/query', 'ConnectionError', reply)
    clone = pickle.loads(pickle.dumps(error))
    assert (clone.topic, clone.error, clone.reply.error, str(clone)) == (
        'db/query', 'ConnectionError', 'ConnectionError', str(error))


@xfail_68r
def test_remote_error_message_bounds_and_escapes_the_remote_text(hub):
    forged = 'x\n2026-10-13 00:00:00,000 CRITICAL forged line ' + 'y' * 100_000
    _service_replying_error(hub, forged)
    with pytest.raises(agent_module.RemoteError) as raised:
        hub('cli').publish_sync('db/query', 'q', timeout=3, raise_on_error=True)
    assert '\n' not in str(raised.value) and len(str(raised.value)) < 1000
    assert raised.value.error == forged                   # the full value is kept


def test_by_default_the_error_parcel_is_returned(hub):
    _failing_service(hub)
    reply = hub('cli').publish_sync('db/query', {'sql': 'select 1'}, timeout=3)
    assert reply.content is None and reply.error


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


@pytest.mark.parametrize('errors, exception', [
    ('yes', TypeError),
    ({'expose_details': 'yes'}, TypeError),
    ({'expose': True}, ValueError),
])
def test_a_malformed_errors_section_fails_at_construction(errors, exception):
    with pytest.raises(exception, match='errors'):
        Agent('a', {'errors': errors})
