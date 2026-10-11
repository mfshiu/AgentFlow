"""publish_sync must take only the reply to its own request (issue #36,
W-38).

The Parcel envelope had no correlation id, so publish_sync matched
replies by topic alone and took the first parcel to arrive on its reply
topic: a late reply to an earlier call that used the same topic_wait,
or any other parcel published there.

Contracts verified (RFC-014):
  - The envelope carries an optional `correlation_id`. Parcels without
    one encode exactly as before, so older receivers see no change; a
    malformed one decodes as None.
  - publish_sync stamps every request with a fresh, unpredictable
    correlation id, without modifying the caller's Parcel.
  - An auto-reply echoes the request's correlation id, without
    modifying the handler's Parcel; replies to uncorrelated requests
    are unchanged.
  - publish_sync ignores a reply that carries another correlation id
    and still accepts one without any (a service older than RFC-014).
    The reply it returns has no correlation id, so forwarding it by
    hand to another requester works as before.
  - Auto-replies keep a handler's Parcel subclass and its extra state.
  - Parcels built without Parcel.__init__ still encode.
"""

import json
import pickle
import re
import threading
import time

import pytest

from agentflow.core.agent import Agent
from agentflow.core.parcel import BinaryParcel, Parcel, TextParcel
from tests.fakes.fake_broker import FakeBroker, FakeWorker
from tests.fakes.fake_bus import FakeBus



def _wait_until(predicate, timeout=2.0):
    return FakeBus.wait_until(predicate, timeout=timeout)


@pytest.fixture
def agent_and_broker():
    agent = Agent('cli', {'dispatch': {'shutdown_timeout_s': 1.0}})
    broker = FakeBroker(notifier=agent)
    agent._broker = broker
    agent._agent_worker = FakeWorker()
    yield agent, broker
    agent.terminate()


def _published(broker, topic):
    return [Parcel.from_payload(p) for t, p in broker.publish_calls if t == topic]


def _correlation_id(parcel):
    return getattr(parcel, 'correlation_id', None)    # absent before #36


def _correlated(content, correlation_id, cls=TextParcel):
    parcel = cls(content)
    parcel.correlation_id = correlation_id
    return parcel


# ==========================================================================
# A. Envelope
# ==========================================================================

def test_parcels_without_a_correlation_id_encode_exactly_as_before():
    assert TextParcel('x').payload() == \
        b'text/json|{"version": 3, "content": "x", "topic_return": null, "error": null}'
    assert TextParcel({'a': 1}, topic_return='r').payload() == \
        b'text/json|{"version": 3, "content": {"a": 1}, "topic_return": "r", "error": null}'
    body = pickle.loads(BinaryParcel(b'x').payload().split(b'|', 1)[1])    # our own bytes
    assert body == {'version': 3, 'content': b'x', 'topic_return': None, 'error': None}


def test_new_and_legacy_parcels_have_no_correlation_id():
    assert TextParcel('x').correlation_id is None
    legacy = b'text/json|{"version": 3, "content": "x", "topic_return": null, "error": null}'
    assert Parcel.from_payload(legacy).correlation_id is None


@pytest.mark.parametrize('parcel', [TextParcel({'q': 1}, topic_return='r'), BinaryParcel(b'raw')])
def test_correlation_id_round_trips_in_both_encodings(parcel):
    parcel.correlation_id = 'c-1'
    decoded = Parcel.from_payload(parcel.payload())
    assert decoded.correlation_id == 'c-1'
    assert (decoded.content, decoded.topic_return) == (parcel.content, parcel.topic_return)


def test_correlation_id_is_read_from_the_envelope():
    payload = b'text/json|{"version": 3, "content": 1, "topic_return": null, "error": null, "correlation_id": "c-9"}'
    assert Parcel.from_payload(payload).correlation_id == 'c-9'


@pytest.mark.parametrize('value', ['5', '["c"]', '{"a": 1}', 'true'])
def test_malformed_correlation_id_decodes_as_none(value):
    payload = ('text/json|{"version": 3, "content": 1, "topic_return": null, '
               f'"error": null, "correlation_id": {value}}}').encode()
    assert Parcel.from_payload(payload).correlation_id is None


# ==========================================================================
# B. Auto-reply (service side)
# ==========================================================================

def _serve(agent, handler):
    handled = threading.Event()

    def wrapped(topic, pcl):
        try:
            return handler(topic, pcl)
        finally:
            handled.set()
    agent.subscribe('svc', topic_handler=wrapped)
    return handled


@pytest.mark.parametrize('kind', ['content', 'parcel', 'error'])
def test_auto_reply_echoes_the_request_correlation_id(agent_and_broker, kind):
    agent, broker = agent_and_broker
    returned = _correlated('pong-parcel', 'handler-own')

    def handler(topic, pcl):
        if kind == 'error':
            raise ValueError('boom')
        return returned if kind == 'parcel' else 'pong'
    _serve(agent, handler)

    request = _correlated('ping', 'c-1')
    request.topic_return = 'reply/1'
    broker.deliver('svc', request.payload())

    assert _wait_until(lambda: _published(broker, 'reply/1'))
    reply = _published(broker, 'reply/1')[0]
    assert reply.correlation_id == 'c-1'
    assert returned.correlation_id == 'handler-own'     # the handler's Parcel is not modified
    if kind == 'error':
        assert reply.error == 'boom'


def test_auto_reply_to_an_uncorrelated_request_is_unchanged(agent_and_broker):
    agent, broker = agent_and_broker
    _serve(agent, lambda topic, pcl: 'pong')
    broker.deliver('svc', TextParcel('ping', topic_return='reply/1').payload())
    assert _wait_until(lambda: [p for t, p in broker.publish_calls if t == 'reply/1'])
    assert [p for t, p in broker.publish_calls if t == 'reply/1'] == [TextParcel('pong').payload()]


# ==========================================================================
# C. publish_sync (requester side)
# ==========================================================================

def test_publish_sync_stamps_each_request_with_a_fresh_correlation_id(agent_and_broker):
    agent, broker = agent_and_broker
    broker.auto_respond_with('pong')
    caller_parcel = TextParcel('ping')
    agent.publish_sync('svc', caller_parcel, timeout=2)
    agent.publish_sync('svc', 'ping', topic_wait='T', timeout=2)
    ids = [p.correlation_id for p in _published(broker, 'svc')]
    assert len(ids) == 2 and ids[0] != ids[1]
    assert all(isinstance(i, str) and re.fullmatch(r'[0-9a-f]{32}', i) for i in ids), ids
    assert caller_parcel.correlation_id is None         # the caller's Parcel is not modified


def _spawn_publish_sync(agent, topic_wait, timeout):
    outcome = {}

    def run():
        try:
            outcome['reply'] = agent.publish_sync('svc', 'ping', topic_wait=topic_wait, timeout=timeout)
        except BaseException as ex:     # noqa: BLE001 - reported to the test
            outcome['error'] = ex
    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    return thread, outcome


def test_publish_sync_ignores_a_late_reply_to_an_earlier_call(agent_and_broker):
    agent, broker = agent_and_broker
    with pytest.raises(TimeoutError):
        agent.publish_sync('svc', 'ping', topic_wait='T', timeout=0.2)
    earlier = _published(broker, 'svc')[0]

    thread, outcome = _spawn_publish_sync(agent, 'T', timeout=3)
    assert _wait_until(lambda: len(_published(broker, 'svc')) == 2)
    current = _published(broker, 'svc')[1]

    broker.deliver('T', _correlated('late reply to the earlier call', _correlation_id(earlier)).payload())
    time.sleep(0.2)
    broker.deliver('T', _correlated('reply to this call', _correlation_id(current)).payload())
    thread.join(5)

    assert 'error' not in outcome, outcome
    assert outcome['reply'].content == 'reply to this call'


def test_publish_sync_ignores_a_reply_to_another_request(agent_and_broker):
    agent, broker = agent_and_broker
    thread, outcome = _spawn_publish_sync(agent, 'T', timeout=3)
    assert _wait_until(lambda: _published(broker, 'svc'))
    request = _published(broker, 'svc')[0]

    broker.deliver('T', _correlated('not for us', 'someone-else').payload())
    time.sleep(0.2)
    broker.deliver('T', _correlated('for us', _correlation_id(request)).payload())
    thread.join(5)

    assert outcome['reply'].content == 'for us'


def test_publish_sync_accepts_an_uncorrelated_reply_from_an_older_service(agent_and_broker):
    agent, broker = agent_and_broker
    broker.auto_respond_with('pong')        # replies without a correlation id
    assert agent.publish_sync('svc', 'ping', timeout=2).content == 'pong'


# ==========================================================================
# D. End to end on a FakeBus
# ==========================================================================

class Service(Agent):
    def __init__(self):
        super().__init__('svc-agent', {})

    def handle(self, topic, pcl):
        return {'echo': pcl.content}


def test_round_trip_between_two_agents_is_correlated():
    bus = FakeBus()
    service, client = Service(), Agent('client', {})
    for a in (service, client):
        bus.attach(a)
        bus.connect(a)
    service.subscribe('svc', topic_handler=service.handle)
    try:
        reply = client.publish_sync('svc', 'ping', timeout=3)
        assert reply.content == {'echo': 'ping'}
        request = [Parcel.from_payload(p) for t, p in client._broker.published if t == 'svc'][0]
        sent = [Parcel.from_payload(p) for t, p in service._broker.published if t == request.topic_return]
        assert request.correlation_id and sent[0].correlation_id == request.correlation_id
    finally:
        for a in (service, client):
            a.terminate()


# ==========================================================================
# E. Gaps found in review
# ==========================================================================

class TaggedParcel(TextParcel):
    """A user Parcel whose constructor is not (content) and whose extra
    state goes into the envelope."""
    def __init__(self, content, tag):
        super().__init__(content)
        self.tag = tag

    def _get_managed_data(self):
        managed = super()._get_managed_data()
        managed['tag'] = self.tag
        return managed


def test_auto_reply_keeps_a_handlers_parcel_subclass_intact(agent_and_broker):
    agent, broker = agent_and_broker
    returned = TaggedParcel('pong', tag='t1')
    _serve(agent, lambda topic, pcl: returned)
    request = _correlated('ping', 'c-1')
    request.topic_return = 'reply/1'
    broker.deliver('svc', request.payload())

    assert _wait_until(lambda: [p for t, p in broker.publish_calls if t == 'reply/1'])
    raw = [p for t, p in broker.publish_calls if t == 'reply/1'][0]
    body = json.loads(raw.split(b'|', 1)[1])
    assert (body['content'], body['tag'], body['correlation_id']) == ('pong', 't1', 'c-1')
    assert returned.correlation_id is None          # the handler's Parcel is not modified


class Gateway(Agent):
    """Answers in on_message, where there is no auto-reply (RFC-003),
    by forwarding the reply of a downstream publish_sync by hand."""
    def __init__(self):
        super().__init__('gateway', {})

    def on_message(self, topic, pcl):
        reply = self.publish_sync('svc', pcl.content, timeout=2)
        self.publish(pcl.topic_return, reply)


def test_a_reply_forwarded_by_hand_reaches_the_original_requester():
    bus = FakeBus()
    service, gateway, client = Service(), Gateway(), Agent('client', {})
    for a in (service, gateway, client):
        bus.attach(a)
        bus.connect(a)
    service.subscribe('svc', topic_handler=service.handle)
    gateway.subscribe('gw')
    try:
        assert client.publish_sync('gw', 'ping', timeout=3).content == {'echo': 'ping'}
    finally:
        for a in (service, gateway, client):
            a.terminate()


def test_parcels_built_without_parcel_init_still_encode():
    """E.g. a subclass that does not call super().__init__, or a Parcel
    pickled by code older than RFC-014."""
    parcel = TextParcel.__new__(TextParcel)
    parcel.version, parcel.content, parcel.topic_return, parcel.error = 3, 'x', None, None
    assert parcel.payload() == TextParcel('x').payload()
    assert 'correlation_id' not in str(parcel)
