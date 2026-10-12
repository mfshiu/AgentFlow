"""Payload size limits and decoding off the receiving thread (issue
#34, W-36).

Agent._on_message decoded every payload completely (JSON or pickle) on
the broker's network thread, with no size limit, before deciding to
queue or drop it. A large or slow payload stalled network I/O and
keepalive. A message the full queue was about to drop was decoded
anyway. The dispatcher queue was bounded by count only, so memory was
not bounded at all.

Contracts verified:
  - A payload larger than codec.max_payload_bytes (default 16 MiB) is
    dropped without being decoded, and counted in decode_failure_count.
  - Decoding runs on a dispatcher thread, not on the receiving thread.
  - A message that the full queue drops is not decoded (small requests
    are still answered "overloaded", issue #4).
  - dispatch.max_queued_bytes bounds the bytes of queued, and of held
    (follow-up of #17), messages.
  - Malformed limits fail at construction.
"""

import threading

import pytest

import agentflow.core.agent as agent_module
from agentflow.core.agent import Agent
from agentflow.core.parcel import Parcel, TextParcel
from tests.fakes.fake_broker import FakeBroker, FakeWorker
from tests.fakes.fake_bus import FakeBus

xfail_34 = pytest.mark.xfail(strict=True, reason='issue #34: no size limit; decoding on the receiving thread')

MiB = 1024 * 1024
SETTLE_S = 0.3


def _wait_until(predicate, timeout=3.0):
    return FakeBus.wait_until(predicate, timeout=timeout)


def _settle():
    FakeBus.wait_until(lambda: False, timeout=SETTLE_S)


def _agent(config=None):
    cfg = {'dispatch': {'shutdown_timeout_s': 1.0}}
    for section, values in (config or {}).items():
        cfg.setdefault(section, {}).update(values)
    agent = Agent('receiver', cfg)
    broker = FakeBroker(notifier=agent)
    agent._broker = broker
    agent._agent_worker = FakeWorker()
    return agent, broker


@pytest.fixture
def decoded(monkeypatch):
    """The (payload size, thread id) of every Parcel.from_payload call."""
    calls = []
    original = Parcel.from_payload

    def spy(payload, **kwargs):
        calls.append((len(payload), threading.get_ident()))
        return original(payload, **kwargs)
    monkeypatch.setattr(Parcel, 'from_payload', staticmethod(spy))
    return calls


def _sizes(decoded):
    return [size for size, _ in decoded]


def _blocked_worker(agent, broker, topic='busy'):
    """Occupy a single dispatcher worker until the returned event is set."""
    gate, busy = threading.Event(), threading.Event()

    def block(t, pcl):
        busy.set()
        gate.wait(5)
    agent.subscribe(topic, topic_handler=block)
    broker.deliver(topic, TextParcel('block').payload())
    assert busy.wait(2)
    return gate


@xfail_34
def test_an_oversized_payload_is_dropped_before_decoding(decoded):
    agent, broker = _agent({'codec': {'max_payload_bytes': 1000}})
    seen = []
    agent.subscribe('t', topic_handler=lambda topic, pcl: seen.append(pcl.content))
    try:
        big = TextParcel('x' * 2000).payload()
        broker.deliver('t', big)
        broker.deliver('t', TextParcel('small').payload())
        assert _wait_until(lambda: seen == ['small'])
        assert len(big) not in _sizes(decoded)
        assert agent.decode_failure_count == 1
    finally:
        agent.terminate()


@xfail_34
def test_the_default_limit_is_16_mib(decoded):
    agent, broker = _agent()
    try:
        at_limit = TextParcel.HEAD + b' ' * (16 * MiB - len(TextParcel.HEAD))
        over_limit = at_limit + b' '
        broker.deliver('t', over_limit)
        broker.deliver('t', at_limit)
        assert _wait_until(lambda: agent.decode_failure_count == 2)
        assert _sizes(decoded) == [16 * MiB]        # only the one at the limit was decoded
    finally:
        agent.terminate()


@xfail_34
def test_decoding_runs_off_the_receiving_thread(decoded):
    agent, broker = _agent()
    seen = []
    agent.subscribe('t', topic_handler=lambda topic, pcl: seen.append(pcl.content))
    try:
        broker.deliver('t', TextParcel('m').payload())
        assert _wait_until(lambda: seen == ['m'])
        assert decoded and all(thread != threading.get_ident() for _, thread in decoded)
    finally:
        agent.terminate()


@xfail_34
def test_a_message_the_full_queue_drops_is_not_decoded(decoded):
    agent, broker = _agent({'dispatch': {'workers': 1, 'queue_capacity': 1}})
    gate = _blocked_worker(agent, broker)
    try:
        broker.deliver('t', TextParcel('queued').payload())             # fills the queue
        large = TextParcel('x' * 100_000, topic_return='reply/1').payload()
        broker.deliver('t', large)                                      # dropped
        assert agent._get_dispatcher().dropped_message_count == 1
        assert len(large) not in _sizes(decoded)
        assert [t for t, _ in broker.publish_calls if t == 'reply/1'] == []   # too large to answer
    finally:
        gate.set()
        agent.terminate()


@xfail_34
def test_the_queue_is_bounded_in_bytes():
    agent, broker = _agent({'dispatch': {'workers': 1, 'max_queued_bytes': 10_000}})
    gate = _blocked_worker(agent, broker)
    agent.subscribe('svc', topic_handler=lambda topic, pcl: 'done')
    try:
        for i in range(3):
            request = TextParcel('x' * 4000, topic_return=f'reply/{i}')
            request.correlation_id = f'c-{i}'
            broker.deliver('svc', request.payload())
        # The third does not fit in 10 000 bytes: it is answered "overloaded".
        overloaded = [Parcel.from_payload(p) for t, p in broker.publish_calls if t == 'reply/2']
        assert len(overloaded) == 1 and 'overloaded' in overloaded[0].error
        assert overloaded[0].correlation_id == 'c-2'

        gate.set()
        assert _wait_until(lambda: {'reply/0', 'reply/1'} <= {t for t, _ in broker.publish_calls})
        assert _wait_until(lambda: agent._queued_bytes == 0)
    finally:
        gate.set()
        agent.terminate()


@xfail_34
def test_held_messages_are_bounded_in_bytes():
    agent, broker = _agent({'dispatch': {'max_queued_bytes': 10_000}})
    try:
        agent._Agent__await_hook('on_activate')
        for _ in range(3):
            broker.deliver('stray', TextParcel('x' * 4000).payload())
        assert len(agent._early_messages) == 2
        agent._Agent__hook_done('on_activate')
    finally:
        agent.terminate()


@xfail_34
@pytest.mark.parametrize('config, error, match', [
    ({'codec': {'max_payload_bytes': 0}}, ValueError, "max_payload_bytes' must be"),
    ({'codec': {'max_payload_bytes': -1}}, ValueError, "max_payload_bytes' must be"),
    ({'codec': {'max_payload_bytes': True}}, TypeError, "max_payload_bytes' must be"),
    ({'codec': {'max_payload_bytes': '1'}}, TypeError, "max_payload_bytes' must be"),
    ({'dispatch': {'max_queued_bytes': 0}}, ValueError, "max_queued_bytes' must be"),
    ({'dispatch': {'max_queued_bytes': 1.5}}, TypeError, "max_queued_bytes' must be"),
])
def test_malformed_limits_fail_at_construction(config, error, match):
    with pytest.raises(error, match=match):
        Agent('a', config)
