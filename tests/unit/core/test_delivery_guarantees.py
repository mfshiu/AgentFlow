"""Delivery guarantees: QoS, message ids, deduplication and reported
losses (issue #4, W-11).

Everything travelled at QoS 0 and the envelope had no message id, so a
message could neither survive a dropped connection nor be recognised
when it arrived twice. Three loss points were silent: Agent.publish
swallowed broker errors and returned None, and a request dropped by a
full dispatcher left its requester waiting for the whole timeout.

Contracts verified:
  - Holon control messages and publish_sync requests and replies
    travel at QoS 1, and the topics they arrive on are subscribed at
    QoS 1. Application publish / subscribe default to QoS 0 and take a
    `qos` keyword; an invalid QoS is refused up front.
  - Every publish stamps a fresh message id on what it sends, without
    modifying the caller's Parcel. The envelope carries it only when
    set; a malformed one decodes as None.
  - An agent handles a message id once: a redelivery (QoS 1 resends
    after a reconnect) is dropped. The window of remembered ids is
    bounded. Messages without an id are never deduplicated.
  - Agent.publish returns True when the broker accepted the message
    and False when it did not.
  - A request the dispatcher drops (queue full or stopping) is answered
    at once with an error reply carrying its correlation id, so a
    publish_sync caller fails fast.
"""

import re
import threading
import time

import pytest

from agentflow.core.agent import Agent
from agentflow.core.parcel import BinaryParcel, Parcel, TextParcel
from tests.fakes.fake_broker import FakeBroker, FakeWorker
from tests.fakes.fake_bus import FakeBus

xfail_4 = pytest.mark.xfail(strict=True, reason='issue #4: QoS 0 only, no message id')

CONTROL_SUBJECTS = {'terminate', 'register_child', 'register_parent', 'parent_online'}
SETTLE_S = 0.3


def _wait_until(predicate, timeout=2.0):
    return FakeBus.wait_until(predicate, timeout=timeout)


def _settle():
    FakeBus.wait_until(lambda: False, timeout=SETTLE_S)


def _message_id(parcel):
    return getattr(parcel, 'message_id', None)      # absent before #4


def _with_message_id(content, message_id, **kwargs):
    parcel = TextParcel(content, **kwargs)
    parcel.message_id = message_id
    return parcel.payload()


def _fake_broker_agent(name='agent', config=None):
    agent = Agent(name, config or {'dispatch': {'shutdown_timeout_s': 1.0}})
    broker = FakeBroker(notifier=agent)
    agent._broker = broker
    agent._agent_worker = FakeWorker()
    return agent, broker


@pytest.fixture
def agent_and_broker():
    agent, broker = _fake_broker_agent()
    yield agent, broker
    agent.terminate()


def _published(broker, topic):
    return [Parcel.from_payload(p) for t, p in broker.publish_calls if t == topic]


class BusAgent(Agent):
    def __init__(self, name, config=None):
        super().__init__(name=name, agent_config=config or {})
        self._Agent__terminate_event = threading.Event()

    def terminated(self, timeout=0.0):
        return self._Agent__terminate_event.wait(timeout)


@pytest.fixture
def bus():
    b = FakeBus(max_deliveries=2000)
    b.agents = []
    yield b
    for a in b.agents:
        a.terminate()


def _agent(bus, name, config=None):
    a = BusAgent(name, config)
    bus.attach(a)
    bus.agents.append(a)
    bus.connect(a)
    return a


def _sent(agent):
    """(topic, decoded parcel, qos) for everything `agent` published."""
    broker = agent._broker
    return [(t, Parcel.from_payload(p), q) for (t, p), q in zip(broker.published, broker.published_qos)]


# ==========================================================================
# A. QoS
# ==========================================================================

def test_holon_control_messages_travel_at_qos_1(bus):
    root, child = _agent(bus, 'a'), _agent(bus, 'b.a')
    assert bus.wait_until(lambda: child.agent_id in root._children
                          and root.agent_id in child._parents)
    root._terminate()
    assert child.terminated(5)

    control = [(pcl.content['subject'], qos) for a in (root, child) for _, pcl, qos in _sent(a)
               if isinstance(pcl.content, dict) and pcl.content.get('subject') in CONTROL_SUBJECTS]
    assert {subject for subject, _ in control} == CONTROL_SUBJECTS
    assert all(qos == 1 for _, qos in control), control
    for a in (root, child):
        holon = {t: q for t, q in a._broker.subscribe_qos.items() if 'to_child.' in t or 'to_parent.' in t}
        assert holon and all(q == 1 for q in holon.values()), holon


def test_publish_sync_requests_and_replies_travel_at_qos_1(bus):
    service, client = _agent(bus, 'svc-agent'), _agent(bus, 'client')
    service.subscribe('svc', topic_handler=lambda topic, pcl: 'pong')

    assert client.publish_sync('svc', 'ping', timeout=3).content == 'pong'

    [(_, request, request_qos)] = [s for s in _sent(client) if s[0] == 'svc']
    replies = [qos for t, _, qos in _sent(service) if t == request.topic_return]
    assert (request_qos, replies) == (1, [1])
    assert client._broker.subscribe_qos[request.topic_return] == 1
    assert _message_id(request)


def test_application_publish_and_subscribe_default_to_qos_0(agent_and_broker):
    agent, broker = agent_and_broker
    agent.publish('t', 'x')
    agent.subscribe('s', topic_handler=lambda topic, pcl: None)
    assert broker.publish_qos == [0]
    assert broker.subscribe_qos['s'] == 0


def test_application_publish_and_subscribe_take_a_qos(agent_and_broker):
    agent, broker = agent_and_broker
    agent.publish('t', 'x', qos=1)
    agent.subscribe('s', topic_handler=lambda topic, pcl: None, qos=2)
    assert broker.publish_qos == [1]
    assert broker.subscribe_qos['s'] == 2


@pytest.mark.parametrize('qos', [-1, 3, True, '1', None])
def test_invalid_qos_is_refused_up_front(agent_and_broker, qos):
    agent, broker = agent_and_broker
    with pytest.raises(ValueError, match='QoS must be 0, 1 or 2'):
        agent.publish('t', 'x', qos=qos)
    with pytest.raises(ValueError, match='QoS must be 0, 1 or 2'):
        agent.subscribe('s', topic_handler=lambda topic, pcl: None, qos=qos)
    assert broker.publish_calls == [] and 's' not in broker.subscribe_qos


# ==========================================================================
# B. Message ids
# ==========================================================================

def test_every_publish_stamps_a_fresh_message_id(agent_and_broker):
    agent, broker = agent_and_broker
    caller_parcel = TextParcel('x')
    agent.publish('t', caller_parcel)
    agent.publish('t', caller_parcel)
    agent.publish('t', 'plain content')
    ids = [_message_id(p) for p in _published(broker, 't')]
    assert len(set(ids)) == 3, ids
    assert all(isinstance(i, str) and re.fullmatch(r'[0-9a-f]{32}', i) for i in ids), ids
    assert caller_parcel.message_id is None             # the caller's Parcel is not modified


@pytest.mark.parametrize('parcel', [TextParcel({'q': 1}, topic_return='r'), BinaryParcel(b'raw')])
def test_message_id_round_trips_in_both_encodings(parcel):
    parcel.message_id = 'm-1'
    decoded = Parcel.from_payload(parcel.payload())
    assert decoded.message_id == 'm-1'
    assert (decoded.content, decoded.topic_return) == (parcel.content, parcel.topic_return)


def test_new_and_legacy_parcels_have_no_message_id():
    assert TextParcel('x').message_id is None
    legacy = b'text/json|{"version": 3, "content": "x", "topic_return": null, "error": null}'
    assert Parcel.from_payload(legacy).message_id is None


@pytest.mark.parametrize('value', ['5', '["m"]', '{"a": 1}', '"' + 'a' * 129 + '"'])
def test_malformed_message_id_decodes_as_none(value):
    payload = ('text/json|{"version": 3, "content": 1, "topic_return": null, '
               f'"error": null, "message_id": {value}}}').encode()
    assert Parcel.from_payload(payload).message_id is None


# ==========================================================================
# C. Deduplication
# ==========================================================================

def _collect(agent, topic):
    seen = []
    agent.subscribe(topic, topic_handler=lambda t, pcl: seen.append(pcl.content))
    return seen


def test_a_redelivered_message_is_handled_once(agent_and_broker):
    agent, broker = agent_and_broker
    seen = _collect(agent, 't')
    for payload in (_with_message_id('a', 'm-1'), _with_message_id('a', 'm-1'),
                    _with_message_id('b', 'm-2'), _with_message_id('a', 'm-1')):
        broker.deliver('t', payload)
    assert _wait_until(lambda: len(seen) >= 2)
    _settle()
    assert sorted(seen) == ['a', 'b']


def test_messages_without_a_message_id_are_never_deduplicated(agent_and_broker):
    agent, broker = agent_and_broker
    seen = _collect(agent, 't')
    for _ in range(2):
        broker.deliver('t', TextParcel('a').payload())
    assert _wait_until(lambda: len(seen) == 2)


def test_the_deduplication_window_is_bounded():
    import agentflow.core.agent as agent_module
    window = agent_module._DEDUP_WINDOW
    agent, broker = _fake_broker_agent(config={'dispatch': {'queue_capacity': 2 * window,
                                                            'shutdown_timeout_s': 1.0}})
    try:
        for i in range(window + 100):
            broker.deliver('t', _with_message_id(i, f'm-{i}'))
        assert len(agent._recent_message_ids) == window
    finally:
        agent.terminate()


# ==========================================================================
# D. Reported losses
# ==========================================================================

@xfail_4
def test_publish_reports_whether_the_broker_accepted_the_message(agent_and_broker):
    agent, broker = agent_and_broker
    assert agent.publish('t', 'x') is True
    broker.publish_exception = RuntimeError('broker down')
    assert agent.publish('t', 'x') is False


def _request(content, topic_return, correlation_id):
    parcel = TextParcel(content, topic_return=topic_return)
    parcel.correlation_id = correlation_id
    return parcel.payload()


@xfail_4
def test_a_request_dropped_by_a_full_dispatcher_gets_an_error_reply():
    agent, broker = _fake_broker_agent(config={'dispatch': {
        'workers': 1, 'queue_capacity': 1, 'shutdown_timeout_s': 1.0}})
    gate, busy = threading.Event(), threading.Event()

    def handler(topic, pcl):
        busy.set()
        gate.wait(5)
        return f'done {pcl.content}'
    agent.subscribe('svc', topic_handler=handler)
    try:
        broker.deliver('svc', _request('q0', 'reply/0', 'c-0'))
        assert busy.wait(2)                                      # the worker is busy
        broker.deliver('svc', _request('q1', 'reply/1', 'c-1'))  # queued
        broker.deliver('svc', _request('q2', 'reply/2', 'c-2'))  # dropped

        [error] = _published(broker, 'reply/2')
        assert error.error and 'overloaded' in error.error
        assert (error.content, error.correlation_id) == (None, 'c-2')

        gate.set()
        assert _wait_until(lambda: _published(broker, 'reply/0') and _published(broker, 'reply/1'))
    finally:
        gate.set()
        agent.terminate()


def test_a_dropped_message_without_topic_return_gets_no_reply():
    agent, broker = _fake_broker_agent(config={'dispatch': {
        'workers': 1, 'queue_capacity': 1, 'shutdown_timeout_s': 1.0}})
    gate, busy = threading.Event(), threading.Event()

    def handler(topic, pcl):
        busy.set()
        gate.wait(5)
    agent.subscribe('svc', topic_handler=handler)
    try:
        broker.deliver('svc', TextParcel('m0').payload())
        assert busy.wait(2)
        for i in (1, 2):
            broker.deliver('svc', TextParcel(f'm{i}').payload())
        assert broker.publish_calls == []
    finally:
        gate.set()
        agent.terminate()


@xfail_4
def test_a_requester_fails_fast_when_the_service_is_overloaded(bus):
    service = _agent(bus, 'svc-agent', {'dispatch': {
        'workers': 1, 'queue_capacity': 1, 'shutdown_timeout_s': 1.0}})
    client = _agent(bus, 'client')
    gate, busy = threading.Event(), threading.Event()

    def handler(topic, pcl):
        busy.set()
        gate.wait(5)
        return 'done'
    service.subscribe('svc', topic_handler=handler)
    background = [threading.Thread(target=client.publish_sync, args=('svc', i),
                                   kwargs={'timeout': 6}, daemon=True) for i in range(2)]
    try:
        background[0].start()
        assert busy.wait(2)
        background[1].start()
        assert _wait_until(lambda: len([t for t, *_ in _sent(client) if t == 'svc']) == 2)

        started = time.monotonic()
        reply = client.publish_sync('svc', 'third', timeout=5)
        assert time.monotonic() - started < 2.0
        assert reply.error and 'overloaded' in reply.error
    finally:
        gate.set()
        for t in background:
            t.join(6)
