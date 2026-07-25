"""Characterization tests for Agent.publish_sync (agent.py:321-349).

These tests document the CURRENT behaviour of publish_sync, including
its known leaks (Risk R-02 in docs/audit/05-risk-register.md). Any
assertion in this file that pins a leak is paired with an @xfail
counterpart that describes the aspirational cleaned-up behaviour. When
a future change fixes the leak, the strict xfail will convert to an
XPASS and force the pinning assertion (and this file) to be revisited.

Constraints:
  - No real MQTT / socket / subprocess / ProcessWorker.
  - Every test's total wall time should stay well under 2 seconds.
  - `Agent._on_message` spawns a short-lived per-message thread
    (agent.py:562); that is prod-code behaviour, not a test artefact.
"""

import re
import threading
import time

import pytest

from agentflow.core.agent import Agent
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
# 3: no response → TimeoutError
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
    # Lower bound: cannot short-circuit before requested timeout.
    # Upper bound: generous headroom for CI jitter.
    assert 0.03 < elapsed < 1.0, f'timeout elapsed={elapsed:.3f}s'


# --------------------------------------------------------------------------
# 4: publish exception behaviour  (also witnesses Risk R-13)
# --------------------------------------------------------------------------

def test_publish_sync_still_times_out_when_publish_raises(agent_with_fake_broker):
    """Characterization: Agent.publish (agent.py:312-313) catches every
    Exception and only logs. publish_sync therefore never sees the
    broker failure and eventually raises TimeoutError, NOT the original
    exception. This is Risk R-13."""
    agent, broker = agent_with_fake_broker
    broker.publish_exception = RuntimeError('broker down')
    with pytest.raises(TimeoutError):
        agent.publish_sync('req', 'q', topic_wait='ret/4', timeout=0.05)


def test_publish_sync_records_publish_attempt_even_when_publish_raises(
    agent_with_fake_broker,
):
    agent, broker = agent_with_fake_broker
    broker.publish_exception = RuntimeError('broker down')
    with pytest.raises(TimeoutError):
        agent.publish_sync('req', 'q', topic_wait='ret/4b', timeout=0.05)
    published_topics = [t for (t, _p) in broker.publish_calls]
    assert 'req' in published_topics


def test_publish_sync_subscribes_return_topic_even_when_publish_raises(
    agent_with_fake_broker,
):
    """Subscribe happens before publish (agent.py:343-344); a publish
    failure does not undo the subscribe. This compounds Risk R-02."""
    agent, broker = agent_with_fake_broker
    broker.publish_exception = RuntimeError('broker down')
    with pytest.raises(TimeoutError):
        agent.publish_sync('req', 'q', topic_wait='ret/4c', timeout=0.05)
    subscribed = [t for (t, _dt) in broker.subscribe_calls]
    assert 'ret/4c' in subscribed


# --------------------------------------------------------------------------
# 5: duplicate response
# --------------------------------------------------------------------------

def test_publish_sync_returns_first_response_and_duplicate_still_dispatches(
    agent_with_fake_broker,
):
    agent, broker = agent_with_fake_broker
    broker.auto_respond_with('first')
    first = agent.publish_sync('req', 'q', topic_wait='ret/5', timeout=1.0)
    assert first.content == 'first'

    assert 'ret/5' in _handlers(agent), (
        'handler must still be registered after success (R-02)'
    )
    original = _handlers(agent)['ret/5']
    second_seen = threading.Event()

    def spy(topic, pcl):
        try:
            return original(topic, pcl)
        finally:
            second_seen.set()

    _handlers(agent)['ret/5'] = spy
    broker.deliver('ret/5', TextParcel('second').payload())
    assert second_seen.wait(1.0), (
        'duplicate response should still reach leaked handler'
    )


# --------------------------------------------------------------------------
# 6: late response after timeout
# --------------------------------------------------------------------------

def test_late_response_after_timeout_still_dispatches_to_leaked_handler(
    agent_with_fake_broker,
):
    agent, broker = agent_with_fake_broker
    with pytest.raises(TimeoutError):
        agent.publish_sync('req', 'q', topic_wait='ret/6', timeout=0.05)

    assert 'ret/6' in _handlers(agent), (
        'handler must still be registered after timeout (R-02)'
    )
    original = _handlers(agent)['ret/6']
    late_seen = threading.Event()

    def spy(topic, pcl):
        try:
            return original(topic, pcl)
        finally:
            late_seen.set()

    _handlers(agent)['ret/6'] = spy
    broker.deliver('ret/6', TextParcel('too-late').payload())
    assert late_seen.wait(1.0), (
        'late response should still reach leaked handler'
    )


# --------------------------------------------------------------------------
# 7: 100 consecutive successes
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
# 8: 100 consecutive timeouts
# --------------------------------------------------------------------------

def test_100_consecutive_publish_sync_all_timeout(agent_with_fake_broker):
    agent, _broker = agent_with_fake_broker
    for i in range(100):
        with pytest.raises(TimeoutError):
            agent.publish_sync(
                'req', f'q-{i}', topic_wait=f'ret/8/{i}', timeout=0.01,
            )


# --------------------------------------------------------------------------
# 9: __topic_handlers count after success  (Risk R-02)
# --------------------------------------------------------------------------

def test_topic_handlers_grows_by_one_per_successful_publish_sync(
    agent_with_fake_broker,
):
    """CHARACTERIZATION: publish_sync never cleans __topic_handlers.
    Pins the observed leak so a future cleanup causes this test to
    fail — at which point the xfail below should be flipped."""
    agent, broker = agent_with_fake_broker
    broker.auto_respond_with('x')
    before = len(_handlers(agent))
    N = 10
    for i in range(N):
        agent.publish_sync('req', 'q', topic_wait=f'ret/9/{i}', timeout=1.0)
    assert len(_handlers(agent)) - before == N


@pytest.mark.xfail(
    reason=('R-02: publish_sync does not clean up __topic_handlers '
            'after success (agent.py:321-349 has no delete path)'),
    strict=True,
)
def test_topic_handlers_should_be_cleaned_after_successful_publish_sync(
    agent_with_fake_broker,
):
    agent, broker = agent_with_fake_broker
    broker.auto_respond_with('x')
    before = len(_handlers(agent))
    for i in range(10):
        agent.publish_sync('req', 'q', topic_wait=f'ret/9x/{i}', timeout=1.0)
    assert len(_handlers(agent)) == before


# --------------------------------------------------------------------------
# 10: __topic_handlers count after timeout  (Risk R-02)
# --------------------------------------------------------------------------

def test_topic_handlers_grows_by_one_per_timed_out_publish_sync(
    agent_with_fake_broker,
):
    agent, _broker = agent_with_fake_broker
    before = len(_handlers(agent))
    N = 10
    for i in range(N):
        with pytest.raises(TimeoutError):
            agent.publish_sync(
                'req', 'q', topic_wait=f'ret/10/{i}', timeout=0.01,
            )
    assert len(_handlers(agent)) - before == N


@pytest.mark.xfail(
    reason=('R-02: publish_sync does not clean up __topic_handlers '
            'after timeout (agent.py:346-349 raises without cleanup)'),
    strict=True,
)
def test_topic_handlers_should_be_cleaned_after_timed_out_publish_sync(
    agent_with_fake_broker,
):
    agent, _broker = agent_with_fake_broker
    before = len(_handlers(agent))
    for i in range(10):
        with pytest.raises(TimeoutError):
            agent.publish_sync(
                'req', 'q', topic_wait=f'ret/10x/{i}', timeout=0.01,
            )
    assert len(_handlers(agent)) == before


# --------------------------------------------------------------------------
# 11: broker.subscribe_calls after success  (Risk R-02)
# --------------------------------------------------------------------------

def test_broker_subscribe_calls_grow_by_one_per_successful_publish_sync(
    agent_with_fake_broker,
):
    agent, broker = agent_with_fake_broker
    broker.auto_respond_with('x')
    before = len(broker.subscribe_calls)
    N = 10
    for i in range(N):
        agent.publish_sync('req', 'q', topic_wait=f'ret/11/{i}', timeout=1.0)
    assert len(broker.subscribe_calls) - before == N
    # Positive witness: Agent's current API has no unsubscribe path.
    assert broker.unsubscribe_calls == []


@pytest.mark.xfail(
    reason=('R-02: Agent has no unsubscribe path; broker subscriptions '
            'accumulate one per publish_sync call and are never released'),
    strict=True,
)
def test_broker_should_unsubscribe_after_successful_publish_sync(
    agent_with_fake_broker,
):
    agent, broker = agent_with_fake_broker
    broker.auto_respond_with('x')
    for i in range(10):
        agent.publish_sync('req', 'q', topic_wait=f'ret/11x/{i}', timeout=1.0)
    assert len(broker.unsubscribe_calls) == 10


# --------------------------------------------------------------------------
# 12: broker.subscribe_calls after timeout  (Risk R-02)
# --------------------------------------------------------------------------

def test_broker_subscribe_calls_grow_by_one_per_timed_out_publish_sync(
    agent_with_fake_broker,
):
    agent, broker = agent_with_fake_broker
    before = len(broker.subscribe_calls)
    N = 10
    for i in range(N):
        with pytest.raises(TimeoutError):
            agent.publish_sync(
                'req', 'q', topic_wait=f'ret/12/{i}', timeout=0.01,
            )
    assert len(broker.subscribe_calls) - before == N
    assert broker.unsubscribe_calls == []


@pytest.mark.xfail(
    reason=('R-02: subscriptions from timed-out publish_sync are '
            'never released'),
    strict=True,
)
def test_broker_should_unsubscribe_after_timed_out_publish_sync(
    agent_with_fake_broker,
):
    agent, broker = agent_with_fake_broker
    for i in range(10):
        with pytest.raises(TimeoutError):
            agent.publish_sync(
                'req', 'q', topic_wait=f'ret/12x/{i}', timeout=0.01,
            )
    assert len(broker.unsubscribe_calls) == 10


# --------------------------------------------------------------------------
# 13: return topic uniqueness
# --------------------------------------------------------------------------

def test_generated_return_topics_are_unique_across_100_calls():
    """Directly exercise Agent.__generate_return_topic (name-mangled).
    10 base-36 chars → ~40 bits of entropy; 100 samples give a
    collision probability on the order of 1e-11 — treated as zero."""
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
    """Characterization: agent.py:325-326 keeps the parcel's own
    topic_return when set and no topic_wait is given."""
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
    """Characterization: agent.py:325-327. When both are set, the
    parcel's own topic_return wins and topic_wait is only logged as a
    warning."""
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
