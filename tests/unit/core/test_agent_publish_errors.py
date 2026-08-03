"""Characterization tests for Agent.publish / publish_sync / _publish_or_raise
error handling (Risk R-13 in docs/audit/05-risk-register.md).

Post-RFC-002:

  - Agent.publish keeps its fire-and-forget contract: catches every
    Exception, returns None, logs via logger.exception.
  - Agent._publish_or_raise is the strict internal variant: wraps data
    as a Parcel, forwards to the broker, and lets broker exceptions
    propagate. Raises RuntimeError when no broker is attached.
  - publish_sync uses _publish_or_raise, so it fast-fails on publish
    failure with the ORIGINAL exception object (not TimeoutError).
    R-02 cleanup runs on every exit path.
"""

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
    a = Agent(name='test_r13', agent_config={})
    b = FakeBroker(notifier=a)
    a._broker = b
    a._agent_worker = FakeWorker()
    return a, b


@pytest.fixture
def agent_without_broker():
    a = Agent(name='test_r13_no_broker', agent_config={})
    a._broker = None
    a._agent_worker = FakeWorker()
    return a


def _handlers(agent):
    return agent._Agent__topic_handlers


# ==========================================================================
# 1. Agent.publish success contract  (unchanged by RFC-002)
# ==========================================================================

def test_publish_returns_none_on_success(agent_with_fake_broker):
    agent, _broker = agent_with_fake_broker
    assert agent.publish('topic', 'payload') is None


def test_publish_delegates_to_broker_publish(agent_with_fake_broker):
    agent, broker = agent_with_fake_broker
    agent.publish('t', 'body')
    assert len(broker.publish_calls) == 1
    topic, payload = broker.publish_calls[0]
    assert topic == 't'
    round_trip = Parcel.from_payload(payload)
    assert round_trip.content == 'body'


def test_publish_wraps_non_parcel_data_via_from_content(agent_with_fake_broker):
    agent, broker = agent_with_fake_broker
    agent.publish('t', {'k': 'v', 'n': 3})
    _topic, payload = broker.publish_calls[-1]
    round_trip = Parcel.from_payload(payload)
    assert round_trip.content == {'k': 'v', 'n': 3}


def test_publish_passes_through_existing_parcel_unchanged(agent_with_fake_broker):
    agent, broker = agent_with_fake_broker
    pcl = TextParcel('preset')
    pcl.topic_return = 'ret/preset'
    agent.publish('t', pcl)
    _topic, payload = broker.publish_calls[-1]
    round_trip = Parcel.from_payload(payload)
    assert round_trip.content == 'preset'
    assert round_trip.topic_return == 'ret/preset'


# ==========================================================================
# 2. Agent.publish fire-and-forget compatibility  (unchanged by RFC-002)
# ==========================================================================

@pytest.mark.parametrize('exc', [
    RuntimeError('broker down'),
    ConnectionError('connection reset'),
    TimeoutError('broker slow'),
    OSError('generic os error'),
])
def test_publish_swallows_broker_exception_and_returns_none(
    agent_with_fake_broker, exc,
):
    """Fire-and-forget: Agent.publish catches every Exception subclass
    (agent.py) and only logs. Caller receives None with no signal.
    This contract is preserved by RFC-002."""
    agent, broker = agent_with_fake_broker
    broker.publish_exception = exc
    assert agent.publish('t', 'x') is None


def test_publish_records_attempt_before_broker_raises(agent_with_fake_broker):
    agent, broker = agent_with_fake_broker
    broker.publish_exception = ConnectionError('gone')
    agent.publish('t', 'x')
    assert len(broker.publish_calls) == 1
    assert broker.publish_calls[0][0] == 't'


def test_publish_still_attempted_after_previous_publish_raised(
    agent_with_fake_broker,
):
    agent, broker = agent_with_fake_broker
    broker.publish_exception = RuntimeError('irrelevant')
    agent.publish('t/A', 'a')
    agent.publish('t/B', 'b')
    topics = [t for (t, _) in broker.publish_calls]
    assert topics == ['t/A', 't/B']


@pytest.mark.parametrize('exc', [
    RuntimeError('r'),
    ConnectionError('c'),
    TimeoutError('t'),
    OSError('o'),
])
def test_publish_never_reraises_broker_exception(agent_with_fake_broker, exc):
    """Fire-and-forget: no propagation. Any raise fails this test."""
    agent, broker = agent_with_fake_broker
    broker.publish_exception = exc
    agent.publish('t', 'x')


def test_publish_does_not_catch_BaseException_subclasses(agent_with_fake_broker):
    """Agent.publish catches only Exception. BaseException subclasses
    (KeyboardInterrupt, SystemExit) DO propagate — behaviour preserved."""
    agent, broker = agent_with_fake_broker
    broker.publish_exception = KeyboardInterrupt()
    with pytest.raises(KeyboardInterrupt):
        agent.publish('t', 'x')


# ==========================================================================
# 3. publish_sync fast-fail semantics  (RFC-002 core)
# ==========================================================================

@pytest.mark.parametrize('exc', [
    RuntimeError('broker down'),
    ConnectionError('reset'),
    TimeoutError('slow'),
    OSError('io'),
])
def test_publish_sync_propagates_broker_exception_unchanged(
    agent_with_fake_broker, exc,
):
    """Post-RFC-002: publish_sync propagates the broker's original
    exception object — same type, same message, same identity."""
    agent, broker = agent_with_fake_broker
    broker.publish_exception = exc
    with pytest.raises(type(exc)) as exc_info:
        agent.publish_sync('req', 'q', topic_wait='ret/r13/prop', timeout=1.0)
    assert exc_info.value is exc


def test_publish_sync_fast_fails_when_broker_publish_raises(
    agent_with_fake_broker,
):
    """Post-RFC-002: publish_sync no longer waits the full timeout when
    publish has already failed. Elapsed should be well under 50 ms
    against a mocked broker that raises synchronously."""
    agent, broker = agent_with_fake_broker
    broker.publish_exception = RuntimeError('immediate')
    start = time.monotonic()
    with pytest.raises(RuntimeError):
        agent.publish_sync('req', 'q', topic_wait='ret/r13/ff', timeout=1.0)
    elapsed = time.monotonic() - start
    assert elapsed < 0.05, f'elapsed={elapsed:.3f}s should be fast'


def test_publish_sync_preserves_broker_exception_message(agent_with_fake_broker):
    """The broker's diagnostic message reaches the caller verbatim."""
    agent, broker = agent_with_fake_broker
    broker.publish_exception = RuntimeError('secret-diagnostic-message')
    with pytest.raises(RuntimeError) as exc_info:
        agent.publish_sync('req', 'q', topic_wait='ret/r13/msg', timeout=0.05)
    assert 'secret-diagnostic-message' in str(exc_info.value)


def test_publish_sync_raises_original_broker_exception_object(
    agent_with_fake_broker,
):
    """The raised exception is the SAME object the broker raised — no
    wrapping, no chaining, no substitution."""
    agent, broker = agent_with_fake_broker
    original = RuntimeError('root')
    broker.publish_exception = original
    with pytest.raises(RuntimeError) as exc_info:
        agent.publish_sync('req', 'q', topic_wait='ret/r13/orig', timeout=0.05)
    assert exc_info.value is original


def test_publish_sync_TimeoutError_still_used_for_true_timeout(
    agent_with_fake_broker,
):
    """publish_sync's own TimeoutError is unchanged when the broker
    accepted the publish but no response arrived within timeout."""
    agent, _broker = agent_with_fake_broker
    # No publish_exception set; publish succeeds, no response arrives.
    with pytest.raises(TimeoutError) as exc_info:
        agent.publish_sync('req', 'q', topic_wait='ret/r13/real-timeout', timeout=0.05)
    assert 'ret/r13/real-timeout' in str(exc_info.value)


# --------------------------------------------------------------------------
# 3b. R-02 cleanup remains intact under the new fast-fail path
# --------------------------------------------------------------------------

def test_publish_sync_cleans_up_handler_when_broker_publish_raises(
    agent_with_fake_broker,
):
    agent, broker = agent_with_fake_broker
    broker.publish_exception = RuntimeError('boom')
    with pytest.raises(RuntimeError):
        agent.publish_sync('req', 'q', topic_wait='ret/r13/h', timeout=0.05)
    assert 'ret/r13/h' not in _handlers(agent)


def test_publish_sync_calls_broker_unsubscribe_when_broker_publish_raises(
    agent_with_fake_broker,
):
    agent, broker = agent_with_fake_broker
    broker.publish_exception = RuntimeError('boom')
    with pytest.raises(RuntimeError):
        agent.publish_sync('req', 'q', topic_wait='ret/r13/u', timeout=0.05)
    assert 'ret/r13/u' in broker.unsubscribe_calls


def test_publish_sync_subscribe_already_recorded_before_publish_raised(
    agent_with_fake_broker,
):
    """Subscribe happens first (agent.py); on publish failure the
    subscribe is still recorded — this is why R-02 cleanup matters."""
    agent, broker = agent_with_fake_broker
    broker.publish_exception = RuntimeError('boom')
    with pytest.raises(RuntimeError):
        agent.publish_sync('req', 'q', topic_wait='ret/r13/order', timeout=0.05)
    subs = [t for (t, _dt) in broker.subscribe_calls]
    assert 'ret/r13/order' in subs


# ==========================================================================
# 4. Agent.publish when broker is None  (unchanged by RFC-002)
# ==========================================================================

def test_publish_returns_none_when_broker_is_none(agent_without_broker):
    """Fire-and-forget: with no broker, publish swallows the
    RuntimeError from _publish_or_raise and returns None."""
    assert agent_without_broker.publish('t', 'x') is None


def test_publish_does_not_raise_when_broker_is_none(agent_without_broker):
    # Any propagated exception fails this test.
    agent_without_broker.publish('t', 'x')


def test_publish_wraps_data_into_parcel_even_when_broker_is_none(
    agent_without_broker,
):
    agent_without_broker.publish('t', {'complex': ['data', 42, None]})


def test_publish_with_none_broker_does_not_touch_topic_handlers(
    agent_without_broker,
):
    before = dict(_handlers(agent_without_broker))
    agent_without_broker.publish('t', 'x')
    assert _handlers(agent_without_broker) == before


# ==========================================================================
# 5. publish_sync when broker is None  (RFC-002: fast-fail RuntimeError)
# ==========================================================================

def test_publish_sync_raises_RuntimeError_when_broker_is_none(agent_without_broker):
    """Post-RFC-002: _publish_or_raise raises RuntimeError immediately
    when no broker is attached; publish_sync no longer waits for the
    full timeout in this scenario."""
    with pytest.raises(RuntimeError, match='no broker attached'):
        agent_without_broker.publish_sync(
            'req', 'q', topic_wait='ret/r13/none', timeout=0.05,
        )


def test_publish_sync_fails_fast_when_broker_is_none(agent_without_broker):
    start = time.monotonic()
    with pytest.raises(RuntimeError):
        agent_without_broker.publish_sync(
            'req', 'q', topic_wait='ret/r13/none-fast', timeout=1.0,
        )
    elapsed = time.monotonic() - start
    assert elapsed < 0.05, f'elapsed={elapsed:.3f}s should be fast'


def test_publish_sync_cleans_up_handler_when_broker_is_none(agent_without_broker):
    """R-02 finally cleanup runs; Agent.unsubscribe's `if self._broker`
    guard skips the (non-existent) broker call."""
    with pytest.raises(RuntimeError):
        agent_without_broker.publish_sync(
            'req', 'q', topic_wait='ret/r13/none-h', timeout=0.05,
        )
    assert 'ret/r13/none-h' not in _handlers(agent_without_broker)


def test_publish_sync_with_none_broker_does_not_crash_on_cleanup(
    agent_without_broker,
):
    for i in range(3):
        with pytest.raises(RuntimeError):
            agent_without_broker.publish_sync(
                'req', 'q', topic_wait=f'ret/r13/none-safe/{i}', timeout=0.02,
            )
    assert not any(
        k.startswith('ret/r13/none-safe/') for k in _handlers(agent_without_broker)
    )


# ==========================================================================
# 7. Return value only in log for Agent.publish (fire-and-forget preserved)
# ==========================================================================

def test_publish_return_value_is_none_regardless_of_broker_outcome(
    agent_with_fake_broker,
):
    """Fire-and-forget: Agent.publish still returns None whether the
    broker accepted, rejected, or was uncontactable."""
    agent, broker = agent_with_fake_broker
    r_success = agent.publish('t', 'x')
    broker.publish_exception = RuntimeError('bad')
    r_failure = agent.publish('t', 'x')
    assert r_success is None
    assert r_failure is None


def test_publish_return_value_cannot_distinguish_success_from_failure(
    agent_with_fake_broker,
):
    """Fire-and-forget: Agent.publish's return value is uniform across
    all outcomes. Callers who need to distinguish must use the strict
    variant _publish_or_raise."""
    agent, broker = agent_with_fake_broker
    outcomes = []
    outcomes.append(agent.publish('t', 'ok'))
    for exc in (RuntimeError('a'), ConnectionError('b'), TimeoutError('c')):
        broker.publish_exception = exc
        outcomes.append(agent.publish('t', 'x'))
    broker.publish_exception = None
    outcomes.append(agent.publish('t', 'ok-again'))
    assert outcomes == [None, None, None, None, None]


# ==========================================================================
# 8. Agent._publish_or_raise  (RFC-002 new internal method)
# ==========================================================================

def test_publish_or_raise_returns_none_on_success(agent_with_fake_broker):
    agent, _broker = agent_with_fake_broker
    assert agent._publish_or_raise('t', 'body') is None


def test_publish_or_raise_delegates_to_broker_publish(agent_with_fake_broker):
    agent, broker = agent_with_fake_broker
    agent._publish_or_raise('t', 'body')
    assert len(broker.publish_calls) == 1
    topic, payload = broker.publish_calls[0]
    assert topic == 't'
    round_trip = Parcel.from_payload(payload)
    assert round_trip.content == 'body'


def test_publish_or_raise_wraps_non_parcel_data_via_from_content(
    agent_with_fake_broker,
):
    agent, broker = agent_with_fake_broker
    agent._publish_or_raise('t', {'k': 'v', 'n': 3})
    _topic, payload = broker.publish_calls[-1]
    round_trip = Parcel.from_payload(payload)
    assert round_trip.content == {'k': 'v', 'n': 3}


def test_publish_or_raise_passes_through_existing_parcel_unchanged(
    agent_with_fake_broker,
):
    agent, broker = agent_with_fake_broker
    pcl = TextParcel('preset')
    pcl.topic_return = 'ret/preset'
    agent._publish_or_raise('t', pcl)
    _topic, payload = broker.publish_calls[-1]
    round_trip = Parcel.from_payload(payload)
    assert round_trip.content == 'preset'
    assert round_trip.topic_return == 'ret/preset'


@pytest.mark.parametrize('exc', [
    RuntimeError('broker down'),
    ConnectionError('reset'),
    TimeoutError('slow'),
    OSError('io'),
])
def test_publish_or_raise_reraises_broker_exception(agent_with_fake_broker, exc):
    """Strict semantics: broker exceptions propagate unchanged."""
    agent, broker = agent_with_fake_broker
    broker.publish_exception = exc
    with pytest.raises(type(exc)) as exc_info:
        agent._publish_or_raise('t', 'x')
    assert exc_info.value is exc


def test_publish_or_raise_raises_RuntimeError_when_broker_is_none(
    agent_without_broker,
):
    with pytest.raises(RuntimeError, match='no broker attached'):
        agent_without_broker._publish_or_raise('t', 'x')


def test_publish_or_raise_lets_caller_distinguish_success_from_failure(
    agent_with_fake_broker,
):
    """The escape hatch introduced by RFC-002 — callers who need to
    distinguish success from failure call _publish_or_raise instead of
    Agent.publish. This is the concrete resolution of the R-13
    'callers cannot distinguish outcomes' concern."""
    agent, broker = agent_with_fake_broker
    assert agent._publish_or_raise('t', 'ok') is None
    broker.publish_exception = RuntimeError('broken')
    with pytest.raises(RuntimeError, match='broken'):
        agent._publish_or_raise('t', 'x')
