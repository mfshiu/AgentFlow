"""Characterization tests for Agent.publish / publish_sync error handling
(Risk R-13 in docs/audit/05-risk-register.md).

These tests document CURRENT behaviour:

  - Agent.publish (agent.py:305-314) catches every Exception raised by
    the broker and only logs; the caller receives None regardless.
  - publish_sync (agent.py:321-353) therefore sees no error signal from
    a failed publish and blocks on event.wait until the full timeout.
  - With self._broker = None, publish is a no-op that only logs.
  - publish_sync with a None broker also times out because nothing can
    deliver a response.

Aspirational strict xfails at the bottom describe fast-fail behaviour
without prescribing HOW the failure should surface (exception, sentinel
return, Result type). The user of this repository decides that in a
future RFC.
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
    a._broker = None  # explicit for clarity
    a._agent_worker = FakeWorker()
    return a


def _handlers(agent):
    return agent._Agent__topic_handlers


# ==========================================================================
# 1. Agent.publish success contract
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
# 2. Agent.publish when broker.publish raises
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
    """CHARACTERIZATION: Agent.publish catches every Exception subclass
    (agent.py:312-313) and only logs. Caller receives None with no signal."""
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
    """A prior failed publish must not stop subsequent publish attempts."""
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
    """No pytest.raises: any propagation would fail this test."""
    agent, broker = agent_with_fake_broker
    broker.publish_exception = exc
    agent.publish('t', 'x')


def test_publish_does_not_catch_BaseException_subclasses(agent_with_fake_broker):
    """CHARACTERIZATION: agent.py:312 catches only Exception. Subclasses
    of BaseException (e.g. KeyboardInterrupt, SystemExit) DO propagate."""
    agent, broker = agent_with_fake_broker
    broker.publish_exception = KeyboardInterrupt()
    with pytest.raises(KeyboardInterrupt):
        agent.publish('t', 'x')


# ==========================================================================
# 3. publish_sync when broker.publish raises
# ==========================================================================

@pytest.mark.parametrize('exc', [
    RuntimeError('broker down'),
    ConnectionError('reset'),
    TimeoutError('slow'),
    OSError('io'),
])
def test_publish_sync_masks_broker_exception_as_TimeoutError(
    agent_with_fake_broker, exc,
):
    """CHARACTERIZATION: because Agent.publish swallows every Exception,
    publish_sync sees no error signal and blocks on event.wait until the
    timeout; then raises its OWN TimeoutError with its OWN message."""
    agent, broker = agent_with_fake_broker
    broker.publish_exception = exc
    with pytest.raises(TimeoutError) as exc_info:
        agent.publish_sync('req', 'q', topic_wait='ret/r13/mask', timeout=0.05)
    # The raised TimeoutError is publish_sync's own, not the broker's.
    assert 'ret/r13/mask' in str(exc_info.value)


def test_publish_sync_waits_full_timeout_when_broker_publish_raises(
    agent_with_fake_broker,
):
    """CHARACTERIZATION: R-13's core observable — publish failure is
    immediate, but publish_sync still waits the entire timeout window."""
    agent, broker = agent_with_fake_broker
    broker.publish_exception = RuntimeError('immediate failure')
    start = time.monotonic()
    with pytest.raises(TimeoutError):
        agent.publish_sync('req', 'q', topic_wait='ret/r13/slow', timeout=0.1)
    elapsed = time.monotonic() - start
    assert 0.08 < elapsed < 1.0, f'elapsed={elapsed:.3f}s'


def test_publish_sync_cleans_up_handler_when_broker_publish_raises(
    agent_with_fake_broker,
):
    """RFC-001 cleanup runs on the publish-exception path too."""
    agent, broker = agent_with_fake_broker
    broker.publish_exception = RuntimeError('boom')
    with pytest.raises(TimeoutError):
        agent.publish_sync('req', 'q', topic_wait='ret/r13/h', timeout=0.05)
    assert 'ret/r13/h' not in _handlers(agent)


def test_publish_sync_calls_broker_unsubscribe_when_broker_publish_raises(
    agent_with_fake_broker,
):
    agent, broker = agent_with_fake_broker
    broker.publish_exception = RuntimeError('boom')
    with pytest.raises(TimeoutError):
        agent.publish_sync('req', 'q', topic_wait='ret/r13/u', timeout=0.05)
    assert 'ret/r13/u' in broker.unsubscribe_calls


def test_publish_sync_subscribe_already_recorded_before_publish_raised(
    agent_with_fake_broker,
):
    """Subscribe happens before publish (agent.py:343-344); on publish
    failure the subscribe is still recorded — this is why R-02's cleanup
    is essential on the publish-exception path."""
    agent, broker = agent_with_fake_broker
    broker.publish_exception = RuntimeError('boom')
    with pytest.raises(TimeoutError):
        agent.publish_sync('req', 'q', topic_wait='ret/r13/order', timeout=0.05)
    subs = [t for (t, _dt) in broker.subscribe_calls]
    assert 'ret/r13/order' in subs


def test_publish_sync_error_message_does_not_reveal_root_cause(
    agent_with_fake_broker,
):
    """CHARACTERIZATION: The TimeoutError raised by publish_sync mentions
    only the return topic; the broker's original exception message is
    absent. Caller can only recover it from logs."""
    agent, broker = agent_with_fake_broker
    broker.publish_exception = RuntimeError('secret-diagnostic-message')
    with pytest.raises(TimeoutError) as exc_info:
        agent.publish_sync('req', 'q', topic_wait='ret/r13/msg', timeout=0.05)
    assert 'secret-diagnostic-message' not in str(exc_info.value)


def test_publish_sync_TimeoutError_has_no_cause_chain_from_broker_exception(
    agent_with_fake_broker,
):
    """CHARACTERIZATION: __cause__ and __context__ are both None on the
    raised TimeoutError — the broker's original exception is not chained."""
    agent, broker = agent_with_fake_broker
    broker.publish_exception = RuntimeError('root')
    with pytest.raises(TimeoutError) as exc_info:
        agent.publish_sync('req', 'q', topic_wait='ret/r13/chain', timeout=0.05)
    assert exc_info.value.__cause__ is None
    assert exc_info.value.__context__ is None


# ==========================================================================
# 4. Agent.publish when broker is None
# ==========================================================================

def test_publish_returns_none_when_broker_is_none(agent_without_broker):
    """CHARACTERIZATION: agent.py:309-311 logs 'Cannot publish' and
    returns None; no exception, no signal to the caller."""
    assert agent_without_broker.publish('t', 'x') is None


def test_publish_does_not_raise_when_broker_is_none(agent_without_broker):
    # Any propagated exception fails this test.
    agent_without_broker.publish('t', 'x')


def test_publish_wraps_data_into_parcel_even_when_broker_is_none(
    agent_without_broker,
):
    """agent.py:306 always wraps first; verify no crash on complex data
    even without a broker."""
    agent_without_broker.publish('t', {'complex': ['data', 42, None]})


def test_publish_with_none_broker_does_not_touch_topic_handlers(
    agent_without_broker,
):
    """publish() does not register handlers; only subscribe() does."""
    before = dict(_handlers(agent_without_broker))
    agent_without_broker.publish('t', 'x')
    assert _handlers(agent_without_broker) == before


# ==========================================================================
# 5. publish_sync when broker is None
# ==========================================================================

def test_publish_sync_raises_TimeoutError_when_broker_is_none(agent_without_broker):
    """CHARACTERIZATION: with no broker, subscribe records the handler
    but broker.subscribe is skipped; publish is a no-op. Nothing can
    ever deliver a response, so publish_sync times out."""
    with pytest.raises(TimeoutError):
        agent_without_broker.publish_sync(
            'req', 'q', topic_wait='ret/r13/none', timeout=0.05,
        )


def test_publish_sync_cleans_up_handler_when_broker_is_none(agent_without_broker):
    """RFC-001 finally cleanup runs; Agent.unsubscribe's `if self._broker`
    guard skips the (non-existent) broker call."""
    with pytest.raises(TimeoutError):
        agent_without_broker.publish_sync(
            'req', 'q', topic_wait='ret/r13/none-h', timeout=0.05,
        )
    assert 'ret/r13/none-h' not in _handlers(agent_without_broker)


def test_publish_sync_with_none_broker_does_not_crash_on_cleanup(
    agent_without_broker,
):
    """The cleanup path must be safe when _broker is None (idempotent
    Agent.unsubscribe)."""
    for i in range(3):
        with pytest.raises(TimeoutError):
            agent_without_broker.publish_sync(
                'req', 'q', topic_wait=f'ret/r13/none-safe/{i}', timeout=0.02,
            )
    assert not any(
        k.startswith('ret/r13/none-safe/') for k in _handlers(agent_without_broker)
    )


# ==========================================================================
# 7. Error is only in log, not in return value  (R-13 core observable)
# ==========================================================================

def test_publish_return_value_is_none_regardless_of_broker_outcome(
    agent_with_fake_broker,
):
    agent, broker = agent_with_fake_broker
    r_success = agent.publish('t', 'x')
    broker.publish_exception = RuntimeError('bad')
    r_failure = agent.publish('t', 'x')
    assert r_success is None
    assert r_failure is None


def test_publish_return_value_cannot_distinguish_success_from_failure(
    agent_with_fake_broker,
):
    """CHARACTERIZATION: this is the R-13 core observable in a single
    assertion. Successful publish and every failed publish are all
    indistinguishable by return value alone."""
    agent, broker = agent_with_fake_broker
    outcomes = []
    outcomes.append(agent.publish('t', 'ok'))  # broker OK
    for exc in (RuntimeError('a'), ConnectionError('b'), TimeoutError('c')):
        broker.publish_exception = exc
        outcomes.append(agent.publish('t', 'x'))
    broker.publish_exception = None
    outcomes.append(agent.publish('t', 'ok-again'))
    assert outcomes == [None, None, None, None, None]


# ==========================================================================
# Aspirational strict xfails: fast-fail contract (R-13 ideal behaviour)
# ==========================================================================
# These describe the desired observable outcome without prescribing HOW
# the failure surfaces (exception, sentinel return, Result type, etc.).

@pytest.mark.xfail(
    reason=('R-13: publish_sync currently waits the full timeout even '
            'when broker.publish has already failed. Fast-fail is the '
            'target behaviour; the signalling mechanism (raise, sentinel '
            'return, Result type, callback) is deliberately not fixed '
            'here — that is a design decision for a future RFC.'),
    strict=True,
)
def test_publish_sync_should_fail_fast_when_broker_publish_raises(
    agent_with_fake_broker,
):
    agent, broker = agent_with_fake_broker
    broker.publish_exception = RuntimeError('immediate')
    start = time.monotonic()
    try:
        agent.publish_sync('req', 'q', topic_wait='ret/r13/fast', timeout=1.0)
    except Exception:
        pass
    elapsed = time.monotonic() - start
    # Under fast-fail, elapsed should be well under the requested timeout.
    assert elapsed < 0.1, f'elapsed={elapsed:.3f}s should be << 1.0s timeout'


@pytest.mark.xfail(
    reason=('R-13: publish_sync currently raises TimeoutError with no '
            'reference to the underlying broker failure. Ideal behaviour '
            'would either propagate the original exception, chain it via '
            '`raise ... from ex`, or expose it through another API. This '
            'test only requires that the caller be able to obtain the '
            'root cause somehow.'),
    strict=True,
)
def test_publish_sync_should_expose_broker_exception_to_caller(
    agent_with_fake_broker,
):
    agent, broker = agent_with_fake_broker
    original = RuntimeError('root-cause-diagnostic')
    broker.publish_exception = original
    try:
        agent.publish_sync('req', 'q', topic_wait='ret/r13/chain', timeout=0.05)
    except BaseException as ex:
        chain = []
        cur = ex
        while cur is not None:
            chain.append(cur)
            cur = getattr(cur, '__cause__', None) or getattr(cur, '__context__', None)
        combined_message = ' | '.join(str(e) for e in chain)
        assert 'root-cause-diagnostic' in combined_message
    else:
        pytest.fail('publish_sync returned normally; expected some exception')


@pytest.mark.xfail(
    reason=('R-13: Agent.publish returns None for every outcome (success, '
            'broker exception, missing broker). Ideal behaviour would let '
            'the caller distinguish success from at least one failure '
            'mode by observable state. Mechanism deliberately unspecified.'),
    strict=True,
)
def test_publish_return_value_should_differ_between_success_and_failure(
    agent_with_fake_broker,
):
    agent, broker = agent_with_fake_broker
    success = agent.publish('t', 'ok')
    broker.publish_exception = RuntimeError('broken')
    failure = agent.publish('t', 'x')
    assert success != failure
