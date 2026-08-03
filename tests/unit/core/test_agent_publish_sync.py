"""Characterization tests for Agent.publish_sync (agent.py:321-353).

Post R-02 fix: publish_sync wraps its wait/publish in try/finally and
unsubscribes on every exit path (success, timeout, publish exception).
Handler dispatch tolerates duplicates via an is_set() guard and the
finally cleanup uses an identity guard to protect foreign handlers.
"""

import re
import threading
import time

import pytest

from agentflow.core.agent import (
    Agent,
    _HandlerOwnerType,
    _HandlerRecord,
)
from agentflow.core.parcel import Parcel, TextParcel

from tests.fakes.fake_broker import FakeBroker, FakeWorker


# --------------------------------------------------------------------------
# Fixtures
# --------------------------------------------------------------------------

@pytest.fixture
def agent_with_fake_broker():
    """Fresh Agent + FakeBroker + FakeWorker; function-scope for isolation."""
    a = Agent(name='test_agent', agent_config={})
    b = FakeBroker(notifier=a)
    a._broker = b
    a._agent_worker = FakeWorker()
    return a, b


def _handlers(agent):
    """Access private Agent.__topic_handlers via name mangling."""
    return agent._Agent__topic_handlers


_RETURN_TOPIC_PATTERN = re.compile(r'^[0-9a-f]{4}-[0-9a-z]{10}/.+$')


# --------------------------------------------------------------------------
# 1-2: successful response
# --------------------------------------------------------------------------

def test_publish_sync_returns_parcel_when_response_arrives(agent_with_fake_broker):
    agent, broker = agent_with_fake_broker
    broker.auto_respond_with('pong')
    result = agent.publish_sync('ping', 'hello', topic_wait='ret/1', timeout=1.0)
    assert isinstance(result, Parcel)


def test_publish_sync_returns_response_content_verbatim(agent_with_fake_broker):
    agent, broker = agent_with_fake_broker
    payload = {'greeting': 'hi', 'answer': 42}
    broker.auto_respond_with(payload)
    result = agent.publish_sync('req', 'q', topic_wait='ret/2', timeout=1.0)
    assert result.content == payload


# --------------------------------------------------------------------------
# 3: no response -> TimeoutError
# --------------------------------------------------------------------------

def test_publish_sync_raises_TimeoutError_when_no_response(agent_with_fake_broker):
    agent, _broker = agent_with_fake_broker
    with pytest.raises(TimeoutError) as exc_info:
        agent.publish_sync('req', 'q', topic_wait='ret/3', timeout=0.05)
    assert 'ret/3' in str(exc_info.value)


def test_publish_sync_timeout_wait_duration_is_bounded(agent_with_fake_broker):
    agent, _broker = agent_with_fake_broker
    start = time.monotonic()
    with pytest.raises(TimeoutError):
        agent.publish_sync('req', 'q', topic_wait='ret/3b', timeout=0.05)
    elapsed = time.monotonic() - start
    assert 0.03 < elapsed < 1.0, f'timeout elapsed={elapsed:.3f}s'


# --------------------------------------------------------------------------
# 4: publish exception behaviour  (also witnesses Risk R-13)
# --------------------------------------------------------------------------

def test_publish_sync_propagates_broker_publish_exception(agent_with_fake_broker):
    """Post-RFC-002: broker.publish exceptions propagate as their original
    type through publish_sync. R-13 exception-masking is resolved; R-02
    cleanup still runs on this path."""
    agent, broker = agent_with_fake_broker
    broker.publish_exception = RuntimeError('broker down')
    with pytest.raises(RuntimeError, match='broker down'):
        agent.publish_sync('req', 'q', topic_wait='ret/4', timeout=0.05)


def test_publish_sync_records_publish_attempt_even_when_publish_raises(
    agent_with_fake_broker,
):
    agent, broker = agent_with_fake_broker
    broker.publish_exception = RuntimeError('broker down')
    with pytest.raises(RuntimeError):
        agent.publish_sync('req', 'q', topic_wait='ret/4b', timeout=0.05)
    published_topics = [t for (t, _p) in broker.publish_calls]
    assert 'req' in published_topics


def test_publish_sync_subscribes_and_then_unsubscribes_when_publish_raises(
    agent_with_fake_broker,
):
    """Subscribe happens before publish (agent.py:343-344); on publish
    failure the finally block must still unsubscribe the return topic."""
    agent, broker = agent_with_fake_broker
    broker.publish_exception = RuntimeError('broker down')
    with pytest.raises(RuntimeError):
        agent.publish_sync('req', 'q', topic_wait='ret/4c', timeout=0.05)
    subscribed = [t for (t, _dt) in broker.subscribe_calls]
    assert 'ret/4c' in subscribed
    assert 'ret/4c' in broker.unsubscribe_calls
    assert 'ret/4c' not in _handlers(agent)


# --------------------------------------------------------------------------
# 5: duplicate response (post-fix: does NOT reach the sync handler)
# --------------------------------------------------------------------------

def test_first_response_returned_and_duplicate_falls_through_to_on_message(
    agent_with_fake_broker,
):
    """After a successful publish_sync, the handler is cleaned up. A
    duplicate delivery must fall through to Agent.on_message (default
    no-op), NOT re-invoke the completed sync handler."""
    agent, broker = agent_with_fake_broker
    broker.auto_respond_with('first')
    first = agent.publish_sync('req', 'q', topic_wait='ret/5', timeout=1.0)
    assert first.content == 'first'

    # R-02 fix invariant: handler is removed after success.
    assert 'ret/5' not in _handlers(agent)
    assert 'ret/5' in broker.unsubscribe_calls

    # Route the fall-through to on_message to a spy.
    fallback_seen = threading.Event()
    fallback_calls = []

    def track_fallback(topic, pcl):
        fallback_calls.append((topic, pcl.content))
        fallback_seen.set()

    agent.on_message = track_fallback

    broker.deliver('ret/5', TextParcel('second').payload())
    assert fallback_seen.wait(1.0), (
        'duplicate response should reach on_message via fall-through'
    )
    assert fallback_calls == [('ret/5', 'second')]


# --------------------------------------------------------------------------
# 6: late response after timeout (post-fix: does NOT reach the sync handler)
# --------------------------------------------------------------------------

def test_late_response_after_timeout_falls_through_to_on_message(
    agent_with_fake_broker,
):
    agent, broker = agent_with_fake_broker
    with pytest.raises(TimeoutError):
        agent.publish_sync('req', 'q', topic_wait='ret/6', timeout=0.05)

    # R-02 fix invariant: handler is removed after timeout.
    assert 'ret/6' not in _handlers(agent)
    assert 'ret/6' in broker.unsubscribe_calls

    fallback_seen = threading.Event()
    fallback_calls = []

    def track_fallback(topic, pcl):
        fallback_calls.append((topic, pcl.content))
        fallback_seen.set()

    agent.on_message = track_fallback

    broker.deliver('ret/6', TextParcel('too-late').payload())
    assert fallback_seen.wait(1.0), (
        'late response should reach on_message via fall-through'
    )
    assert fallback_calls == [('ret/6', 'too-late')]


# --------------------------------------------------------------------------
# 7: 100 consecutive successes  (post-fix: no accumulation)
# --------------------------------------------------------------------------

def test_100_consecutive_publish_sync_all_return_correct_response(
    agent_with_fake_broker,
):
    agent, broker = agent_with_fake_broker
    for i in range(100):
        broker.auto_respond_with(f'reply-{i}')
        result = agent.publish_sync(
            'req', f'q-{i}', topic_wait=f'ret/7/{i}', timeout=1.0,
        )
        assert result.content == f'reply-{i}'


# --------------------------------------------------------------------------
# 8: 100 consecutive timeouts  (post-fix: no accumulation)
# --------------------------------------------------------------------------

def test_100_consecutive_publish_sync_all_timeout(agent_with_fake_broker):
    agent, _broker = agent_with_fake_broker
    for i in range(100):
        with pytest.raises(TimeoutError):
            agent.publish_sync(
                'req', f'q-{i}', topic_wait=f'ret/8/{i}', timeout=0.01,
            )


# --------------------------------------------------------------------------
# 9: __topic_handlers cleaned after success  (R-02 fix invariant)
# --------------------------------------------------------------------------

def test_topic_handlers_cleaned_after_successful_publish_sync(
    agent_with_fake_broker,
):
    agent, broker = agent_with_fake_broker
    broker.auto_respond_with('x')
    before = len(_handlers(agent))
    for i in range(10):
        agent.publish_sync('req', 'q', topic_wait=f'ret/9/{i}', timeout=1.0)
    assert len(_handlers(agent)) == before


# --------------------------------------------------------------------------
# 10: __topic_handlers cleaned after timeout  (R-02 fix invariant)
# --------------------------------------------------------------------------

def test_topic_handlers_cleaned_after_timed_out_publish_sync(
    agent_with_fake_broker,
):
    agent, _broker = agent_with_fake_broker
    before = len(_handlers(agent))
    for i in range(10):
        with pytest.raises(TimeoutError):
            agent.publish_sync(
                'req', 'q', topic_wait=f'ret/10/{i}', timeout=0.01,
            )
    assert len(_handlers(agent)) == before


# --------------------------------------------------------------------------
# 11: broker.subscribe grows +N, broker.unsubscribe grows +N (success)
# --------------------------------------------------------------------------

def test_broker_subscribe_and_unsubscribe_grow_together_on_success(
    agent_with_fake_broker,
):
    agent, broker = agent_with_fake_broker
    broker.auto_respond_with('x')
    subs_before = len(broker.subscribe_calls)
    unsubs_before = len(broker.unsubscribe_calls)
    N = 10
    for i in range(N):
        agent.publish_sync('req', 'q', topic_wait=f'ret/11/{i}', timeout=1.0)
    assert len(broker.subscribe_calls) - subs_before == N
    assert len(broker.unsubscribe_calls) - unsubs_before == N


def test_broker_unsubscribes_specific_topic_after_success(
    agent_with_fake_broker,
):
    agent, broker = agent_with_fake_broker
    broker.auto_respond_with('x')
    agent.publish_sync('req', 'q', topic_wait='ret/11-spec', timeout=1.0)
    assert 'ret/11-spec' in broker.unsubscribe_calls


# --------------------------------------------------------------------------
# 12: broker.subscribe grows +N, broker.unsubscribe grows +N (timeout)
# --------------------------------------------------------------------------

def test_broker_subscribe_and_unsubscribe_grow_together_on_timeout(
    agent_with_fake_broker,
):
    agent, broker = agent_with_fake_broker
    subs_before = len(broker.subscribe_calls)
    unsubs_before = len(broker.unsubscribe_calls)
    N = 10
    for i in range(N):
        with pytest.raises(TimeoutError):
            agent.publish_sync(
                'req', 'q', topic_wait=f'ret/12/{i}', timeout=0.01,
            )
    assert len(broker.subscribe_calls) - subs_before == N
    assert len(broker.unsubscribe_calls) - unsubs_before == N


def test_broker_unsubscribes_specific_topic_after_timeout(
    agent_with_fake_broker,
):
    agent, broker = agent_with_fake_broker
    with pytest.raises(TimeoutError):
        agent.publish_sync('req', 'q', topic_wait='ret/12-spec', timeout=0.01)
    assert 'ret/12-spec' in broker.unsubscribe_calls


# --------------------------------------------------------------------------
# 13: return topic uniqueness
# --------------------------------------------------------------------------

def test_generated_return_topics_are_unique_across_100_calls():
    """Directly exercise Agent.__generate_return_topic (name-mangled).
    10 base-36 chars ~ 40 bits of entropy; 100 samples give a collision
    probability on the order of 1e-11."""
    agent = Agent(name='rt', agent_config={})
    topics = {
        agent._Agent__generate_return_topic('req') for _ in range(100)
    }
    assert len(topics) == 100


def test_generated_return_topic_matches_expected_format():
    agent = Agent(name='fmt', agent_config={})
    topic = agent._Agent__generate_return_topic('req')
    assert _RETURN_TOPIC_PATTERN.match(topic), topic


def test_publish_sync_without_topic_wait_generates_matching_return_topic(
    agent_with_fake_broker,
):
    agent, broker = agent_with_fake_broker
    broker.auto_respond_with('x')
    agent.publish_sync('req', 'q', timeout=1.0)
    sub_topic = broker.subscribe_calls[-1][0]
    assert _RETURN_TOPIC_PATTERN.match(sub_topic), sub_topic


# --------------------------------------------------------------------------
# 14: topic_wait override
# --------------------------------------------------------------------------

def test_publish_sync_uses_topic_wait_when_provided(agent_with_fake_broker):
    agent, broker = agent_with_fake_broker
    broker.auto_respond_with('x')
    agent.publish_sync(
        'req', 'q', topic_wait='caller/chose/this', timeout=1.0,
    )
    subscribed = [t for (t, _dt) in broker.subscribe_calls]
    assert 'caller/chose/this' in subscribed


def test_published_parcel_topic_return_equals_topic_wait_when_provided(
    agent_with_fake_broker,
):
    agent, broker = agent_with_fake_broker
    broker.auto_respond_with('x')
    agent.publish_sync(
        'req', 'q', topic_wait='caller/chose/this/2', timeout=1.0,
    )
    published = broker.last_published_parcel()
    assert published is not None
    assert published.topic_return == 'caller/chose/this/2'


# --------------------------------------------------------------------------
# 15: pre-set topic_return on Parcel is preserved
# --------------------------------------------------------------------------

def test_publish_sync_preserves_existing_topic_return_on_parcel(
    agent_with_fake_broker,
):
    agent, broker = agent_with_fake_broker
    broker.auto_respond_with('x')
    preset = TextParcel('data')
    preset.topic_return = 'preset/ret/15'
    agent.publish_sync('req', preset, timeout=1.0)
    subscribed = [t for (t, _dt) in broker.subscribe_calls]
    assert 'preset/ret/15' in subscribed
    published = broker.last_published_parcel()
    assert published.topic_return == 'preset/ret/15'


def test_publish_sync_preserves_existing_topic_return_even_when_topic_wait_supplied(
    agent_with_fake_broker,
):
    agent, broker = agent_with_fake_broker
    broker.auto_respond_with('x')
    preset = TextParcel('data')
    preset.topic_return = 'preset/ret/15b'
    agent.publish_sync(
        'req', preset, topic_wait='ignored/topic_wait', timeout=1.0,
    )
    subscribed = [t for (t, _dt) in broker.subscribe_calls]
    assert 'preset/ret/15b' in subscribed
    assert 'ignored/topic_wait' not in subscribed


# --------------------------------------------------------------------------
# New: concurrency & identity guard  (RFC-001 §11)
# --------------------------------------------------------------------------

def test_concurrent_publish_sync_with_distinct_topic_wait_all_clean_up(
    agent_with_fake_broker,
):
    agent, broker = agent_with_fake_broker
    broker.auto_respond_with('ok')
    handlers_before = len(_handlers(agent))
    unsubs_before = len(broker.unsubscribe_calls)

    N = 5
    results = [None] * N
    errors = [None] * N

    def worker(i):
        try:
            results[i] = agent.publish_sync(
                'req', 'q', topic_wait=f'ret/conc/{i}', timeout=2.0,
            )
        except BaseException as ex:
            errors[i] = ex

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(N)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(3.0)

    for i in range(N):
        assert errors[i] is None, f'worker {i} raised: {errors[i]!r}'
        assert results[i] is not None
        assert results[i].content == 'ok'
    assert len(_handlers(agent)) == handlers_before
    assert len(broker.unsubscribe_calls) - unsubs_before == N


def test_identity_guard_preserves_foreign_handler_on_same_topic(
    agent_with_fake_broker,
):
    """If a foreign handler races onto the same topic between
    publish_sync's subscribe and its finally, the identity guard must
    prevent finally from evicting that foreign handler."""
    agent, broker = agent_with_fake_broker
    topic = 'ret/identity/foreign'
    foreign_handler = lambda t, p: None  # noqa: E731 (test sentinel)

    original_publish = broker.publish

    def evil_publish(topic_arg, payload):
        # Simulate a corrupted registry state: a NORMAL foreign handler
        # is installed between publish_sync's subscribe and its finally.
        # RFC-007: registry values are _HandlerRecord.
        _handlers(agent)[topic] = _HandlerRecord(
            _HandlerOwnerType.NORMAL, foreign_handler,
        )
        # Do NOT deliver a response; let publish_sync time out.
        original_publish(topic_arg, payload)

    broker.publish = evil_publish

    with pytest.raises(TimeoutError):
        agent.publish_sync('req', 'q', topic_wait=topic, timeout=0.05)

    # Identity guard (RFC-006 + RFC-007 triple check): finally sees a
    # NORMAL foreign record and leaves it alone.
    assert _handlers(agent).get(topic).handler is foreign_handler
    assert _handlers(agent).get(topic).owner_type is _HandlerOwnerType.NORMAL
    # Because we skipped cleanup, no unsubscribe was called for this topic.
    assert topic not in broker.unsubscribe_calls


def test_agent_unsubscribe_is_idempotent_and_broker_agnostic():
    """Agent.unsubscribe on an unknown topic must not raise, and must
    work when _broker is None."""
    agent = Agent(name='u', agent_config={})
    # Before broker is attached: no crash.
    agent.unsubscribe('never/subscribed')
    # After attaching a fake broker: passes through.
    broker = FakeBroker(notifier=agent)
    agent._broker = broker
    agent.unsubscribe('also/unknown')
    assert broker.unsubscribe_calls == ['also/unknown']
    # Second call still safe.
    agent.unsubscribe('also/unknown')
    assert broker.unsubscribe_calls == ['also/unknown', 'also/unknown']
