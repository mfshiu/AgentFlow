"""Tests for publish_sync topic_wait sharing and collisions.

RFC-006 made a second concurrent publish_sync on an already-awaited
topic_wait fail fast, because replies could not be told apart.
RFC-014 §8 (issue #36) lifts that: replies carry the request's
correlation id, so concurrent calls share the topic_wait.

  - Every caller on a shared topic_wait gets the reply that carries
    its own correlation id, in whatever order the replies arrive.
  - The topic has one registry entry and one broker subscription while
    any caller waits. The last caller to leave removes both.
  - A reply without a correlation id (a responder older than RFC-014)
    is taken when exactly one caller waits. With several waiting it
    cannot be attributed, so nobody takes it (RFC-006 Option D, FIFO,
    stays rejected).
  - A caller arriving while the last one unsubscribes does not end up
    waiting on an unsubscribed topic.
  - publish_sync on a topic owned by a normal subscribe handler still
    raises TopicWaitCollisionError at once: no subscribe, no publish.
  - Agent.subscribe's warn-then-overwrite is unchanged (RFC-006 §7.11).
  - Distinct topic_wait or omitted topic_wait continues to work.

One aspirational xfail remains: multi-handler fan-out for subscribe.
"""

import threading
import time
from typing import List

import pytest

from agentflow.core.agent import (
    Agent,
    TopicWaitCollisionError,
    _HandlerOwnerType,
    _HandlerRecord,
)
from agentflow.core.parcel import Parcel, TextParcel

from tests.fakes.fake_broker import FakeBroker, FakeWorker


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------

def _handlers(agent):
    return agent._Agent__topic_handlers


def _make_agent():
    a = Agent(name='test_r006', agent_config={
        'dispatch': {'shutdown_timeout_s': 1.0},
    })
    b = FakeBroker(notifier=a)
    a._broker = b
    a._agent_worker = FakeWorker()
    return a, b


def _wait_until(predicate, timeout: float = 1.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.002)
    return False


def _count_subscribed(broker, topic: str) -> int:
    return sum(1 for t, _ in broker.subscribe_calls if t == topic)


def _count_published(broker, topic: str) -> int:
    return sum(1 for t, _ in broker.publish_calls if t == topic)


def _requests(broker):
    """The requests publish_sync has published on 'req', decoded."""
    return [Parcel.from_payload(p) for t, p in broker.publish_calls if t == 'req']


def _reply_to(broker, request, content):
    """Answer `request` as an RFC-014 responder: echo its correlation id."""
    reply = TextParcel(content)
    reply.correlation_id = getattr(request, 'correlation_id', None)
    broker.deliver(request.topic_return, reply.payload())



def _spawn_publish_sync(agent, topic_wait, content, timeout):
    """Start publish_sync on a daemon background thread. Returns
    (thread, result, error) where result[0] and error[0] are set
    when the call returns."""
    result = [None]
    error = [None]

    def target():
        try:
            result[0] = agent.publish_sync(
                'req', content, topic_wait=topic_wait, timeout=timeout,
            )
        except BaseException as ex:
            error[0] = ex

    t = threading.Thread(target=target, daemon=True)
    t.start()
    return t, result, error


# ==========================================================================
# A. Overwrite behaviour  (Agent.subscribe unchanged; publish_sync flipped)
# ==========================================================================

def test_second_subscribe_overwrites_first_handler_in_registry():
    """RFC-006 §7.11: Agent.subscribe's warn-then-overwrite semantic
    is unchanged. This test pins that guarantee."""
    agent, _broker = _make_agent()
    try:
        def h_A(topic, pcl):
            pass

        def h_B(topic, pcl):
            pass

        agent.subscribe('T', topic_handler=h_A)
        assert _handlers(agent)['T'].handler is h_A
        assert _handlers(agent)['T'].owner_type is _HandlerOwnerType.NORMAL

        agent.subscribe('T', topic_handler=h_B)   # unchanged: silent overwrite (NORMAL rebind)
        assert _handlers(agent)['T'].handler is h_B
        assert _handlers(agent)['T'].owner_type is _HandlerOwnerType.NORMAL
    finally:
        agent.terminate()


def test_shared_topic_wait_keeps_one_registry_entry_and_one_subscription():
    """RFC-014 §8 (was RFC-006 fail-fast): a second publish_sync on an
    already-awaited topic_wait joins the first instead of raising."""
    agent, broker = _make_agent()
    try:
        spawned = [_spawn_publish_sync(agent, 'T', body, timeout=3.0) for body in ('A', 'B')]
        assert _wait_until(lambda: len(_requests(broker)) == 2, timeout=1.0)
        record = _handlers(agent).get('T')
        assert record is not None
        assert record.owner_type is _HandlerOwnerType.PUBLISH_SYNC
        assert _count_subscribed(broker, 'T') == 1

        for request in _requests(broker):
            _reply_to(broker, request, 'r')
        for thread, _, error in spawned:
            thread.join(3.0)
            assert error[0] is None, error[0]
        assert 'T' not in _handlers(agent)
        assert broker.unsubscribe_calls.count('T') == 1
    finally:
        agent.terminate()


def test_an_uncorrelated_reply_is_not_guessed_between_several_waiters():
    """A responder older than RFC-014 sends no correlation id. With one
    caller waiting the reply is taken as before; with several it cannot
    be attributed, so nobody takes it rather than risk handing one
    caller another's answer (RFC-006 Option D stays rejected)."""
    agent, broker = _make_agent()
    try:
        spawned = [_spawn_publish_sync(agent, 'T', body, timeout=1.0) for body in ('A', 'B')]
        assert _wait_until(lambda: len(_requests(broker)) == 2, timeout=1.0)
        broker.deliver('T', TextParcel('whose?').payload())
        for thread, result, error in spawned:
            thread.join(3.0)
            assert result[0] is None and isinstance(error[0], TimeoutError), (result[0], error[0])
    finally:
        agent.terminate()


@pytest.mark.xfail(strict=True, reason='issue #36: an uncorrelated reply goes to whoever is left waiting')
def test_an_uncorrelated_reply_is_not_handed_to_whoever_is_left_waiting():
    """Once calls have overlapped on a topic, a reply without a
    correlation id stays unattributable even after all but one have
    left: it may answer a call that already timed out. Before RFC-014
    §8 the second call failed fast instead of receiving it."""
    agent, broker = _make_agent()
    try:
        a_thread, a_result, a_error = _spawn_publish_sync(agent, 'T', 'A', timeout=0.5)
        assert _wait_until(lambda: len(_requests(broker)) == 1, timeout=1.0)
        b_thread, b_result, b_error = _spawn_publish_sync(agent, 'T', 'B', timeout=2.0)
        assert _wait_until(lambda: len(_requests(broker)) == 2, timeout=0.4),             'B did not join while A was waiting'
        a_thread.join(2.0)
        assert isinstance(a_error[0], TimeoutError)

        broker.deliver('T', TextParcel('late answer to A').payload())
        b_thread.join(3.0)
        assert b_result[0] is None and isinstance(b_error[0], TimeoutError),             (b_result[0], b_error[0])
    finally:
        agent.terminate()


# ==========================================================================
# B. Exception type + message + isolation
# ==========================================================================

def test_TopicWaitCollisionError_is_RuntimeError_subclass():
    assert issubclass(TopicWaitCollisionError, RuntimeError)


def test_collision_error_message_contains_offending_topic():
    agent, broker = _make_agent()
    try:
        agent.subscribe('my/topic/here', topic_handler=lambda t, p: None)
        with pytest.raises(TopicWaitCollisionError) as exc_info:
            agent.publish_sync(
                'req', 'B', topic_wait='my/topic/here', timeout=1.0,
            )
        assert 'my/topic/here' in str(exc_info.value)
    finally:
        agent.terminate()


def test_collision_does_not_call_broker_subscribe():
    """A topic owned by a normal subscribe handler is refused before
    any broker call."""
    agent, broker = _make_agent()
    try:
        agent.subscribe('T', topic_handler=lambda t, p: None)
        subscribes_before = len(broker.subscribe_calls)

        with pytest.raises(TopicWaitCollisionError):
            agent.publish_sync('req', 'B', topic_wait='T', timeout=1.0)

        assert len(broker.subscribe_calls) == subscribes_before
    finally:
        agent.terminate()


def test_collision_does_not_call_broker_publish():
    agent, broker = _make_agent()
    try:
        agent.subscribe('T', topic_handler=lambda t, p: None)
        publishes_before = len(broker.publish_calls)

        with pytest.raises(TopicWaitCollisionError):
            agent.publish_sync('req', 'B', topic_wait='T', timeout=1.0)

        assert len(broker.publish_calls) == publishes_before
    finally:
        agent.terminate()


def test_collision_does_not_touch_registry_for_other_topics():
    agent, broker = _make_agent()
    try:
        def unrelated(topic, pcl):
            pass

        def owner(topic, pcl):
            pass
        agent.subscribe('other/topic', topic_handler=unrelated)
        agent.subscribe('T', topic_handler=owner)
        handlers_snapshot_before = dict(_handlers(agent))

        with pytest.raises(TopicWaitCollisionError):
            agent.publish_sync('req', 'B', topic_wait='T', timeout=1.0)

        assert _handlers(agent) == handlers_snapshot_before
        assert _handlers(agent)['other/topic'].handler is unrelated
        assert _handlers(agent)['T'].handler is owner
        assert _handlers(agent)['T'].owner_type is _HandlerOwnerType.NORMAL
    finally:
        agent.terminate()


def test_collision_after_first_caller_completes_is_not_raised():
    """Sequential (A finishes, B starts): B is not a collision;
    B succeeds normally."""
    agent, broker = _make_agent()
    try:
        broker.auto_respond_with('resp')
        result_A = agent.publish_sync(
            'req', 'A', topic_wait='T', timeout=1.0,
        )
        assert result_A.content == 'resp'
        # After A's cleanup, 'T' is not in the registry.
        assert 'T' not in _handlers(agent)

        # B can now use the same topic_wait without collision.
        result_B = agent.publish_sync(
            'req', 'B', topic_wait='T', timeout=1.0,
        )
        assert result_B.content == 'resp'
    finally:
        agent.terminate()


# ==========================================================================
# C. Failure path cleanup
# ==========================================================================

def test_publish_sync_cleans_up_when_broker_subscribe_raises():
    """RFC-006 §7.6: if broker.subscribe raises after the registry
    write, the finally still runs; the handler is popped; broker.
    unsubscribe is attempted."""
    agent, broker = _make_agent()
    try:
        broker._original_subscribe = broker.subscribe

        def raising_subscribe(topic, data_type):
            broker._original_subscribe(topic, data_type)
            raise RuntimeError('broker.subscribe failed')

        broker.subscribe = raising_subscribe

        with pytest.raises(RuntimeError, match='broker.subscribe failed'):
            agent.publish_sync('req', 'A', topic_wait='T', timeout=1.0)

        # Registry cleaned up.
        assert 'T' not in _handlers(agent)
        # broker.unsubscribe was attempted (to compensate).
        assert 'T' in broker.unsubscribe_calls
    finally:
        agent.terminate()


def test_publish_sync_cleans_up_when_broker_publish_raises():
    """Regression check for RFC-002 fast-fail under the RFC-006
    refactor: publish failure still propagates and cleanup still
    runs atomically."""
    agent, broker = _make_agent()
    try:
        broker.publish_exception = RuntimeError('broker.publish failed')
        with pytest.raises(RuntimeError, match='broker.publish failed'):
            agent.publish_sync('req', 'A', topic_wait='T', timeout=1.0)
        assert 'T' not in _handlers(agent)
        assert 'T' in broker.unsubscribe_calls
    finally:
        agent.terminate()


def test_publish_sync_cleans_up_on_timeout():
    """Regression check for RFC-001 cleanup on timeout under the
    RFC-006 refactor."""
    agent, broker = _make_agent()
    try:
        with pytest.raises(TimeoutError):
            agent.publish_sync('req', 'A', topic_wait='T', timeout=0.05)
        assert 'T' not in _handlers(agent)
        assert 'T' in broker.unsubscribe_calls
    finally:
        agent.terminate()


def test_late_response_after_first_caller_completes_is_silently_dropped():
    """After A completes and cleans up, a late delivery on the same
    topic has no registered handler → RFC-003 R-fallback-silent →
    dropped. No auto-reply, no state change."""
    agent, broker = _make_agent()
    try:
        broker.auto_respond_with('resp')
        agent.publish_sync('req', 'A', topic_wait='T', timeout=1.0)
        broker.stop_auto_responding()

        publishes_before = len(broker.publish_calls)
        broker.deliver('T', TextParcel('late').payload())
        time.sleep(0.05)
        assert len(broker.publish_calls) == publishes_before
        assert 'T' not in _handlers(agent)
    finally:
        agent.terminate()


# ==========================================================================
# D. Distinct topic_wait control (no collision, both callers work)
# ==========================================================================

def test_distinct_topic_wait_both_callers_receive_own_response():
    agent, broker = _make_agent()
    try:
        a_thread, a_result, a_error = _spawn_publish_sync(
            agent, 'T_A', 'A-body', timeout=1.0,
        )
        b_thread, b_result, b_error = _spawn_publish_sync(
            agent, 'T_B', 'B-body', timeout=1.0,
        )
        assert _wait_until(
            lambda: _count_subscribed(broker, 'T_A') == 1, timeout=1.0,
        )
        assert _wait_until(
            lambda: _count_subscribed(broker, 'T_B') == 1, timeout=1.0,
        )
        assert _wait_until(
            lambda: _count_published(broker, 'req') == 2, timeout=1.0,
        )

        broker.deliver('T_A', TextParcel('reply-A').payload())
        broker.deliver('T_B', TextParcel('reply-B').payload())

        a_thread.join(2.0)
        b_thread.join(2.0)

        assert a_result[0].content == 'reply-A'
        assert b_result[0].content == 'reply-B'
        assert a_error[0] is None
        assert b_error[0] is None
    finally:
        agent.terminate()


def test_omitted_topic_wait_auto_generates_unique_correlation():
    agent, broker = _make_agent()
    try:
        broker.auto_respond_with('ok')

        a_result = [None]
        b_result = [None]
        a_error = [None]
        b_error = [None]

        def run_A():
            try:
                a_result[0] = agent.publish_sync('req', 'A-body', timeout=2.0)
            except BaseException as ex:
                a_error[0] = ex

        def run_B():
            try:
                b_result[0] = agent.publish_sync('req', 'B-body', timeout=2.0)
            except BaseException as ex:
                b_error[0] = ex

        a_thread = threading.Thread(target=run_A, daemon=True)
        b_thread = threading.Thread(target=run_B, daemon=True)
        a_thread.start()
        b_thread.start()
        a_thread.join(3.0)
        b_thread.join(3.0)

        assert a_result[0] is not None and a_result[0].content == 'ok'
        assert b_result[0] is not None and b_result[0].content == 'ok'
        # Distinct auto-generated return topics.
        return_topics = {t for t, _ in broker.subscribe_calls}
        assert len(return_topics) >= 2
    finally:
        agent.terminate()


def test_agent_terminate_during_concurrent_publish_sync_does_not_hang_waiters():
    agent, broker = _make_agent()
    try:
        a_thread, a_result, a_error = _spawn_publish_sync(
            agent, 'T', 'A-body', timeout=0.3,
        )
        b_thread, b_result, b_error = _spawn_publish_sync(
            agent, 'U', 'B-body', timeout=0.3,
        )
        assert _wait_until(
            lambda: _count_subscribed(broker, 'T') == 1, timeout=1.0,
        )
        assert _wait_until(
            lambda: _count_subscribed(broker, 'U') == 1, timeout=1.0,
        )
        agent.terminate()
        a_thread.join(2.0)
        b_thread.join(2.0)
        assert isinstance(a_error[0], TimeoutError)
        assert isinstance(b_error[0], TimeoutError)
    finally:
        agent.terminate()


# ==========================================================================
# E. Stress: N callers share one topic_wait
# ==========================================================================

@pytest.mark.parametrize('n', [3, 10, 50])
def test_N_callers_share_one_topic_wait(n):
    """n concurrent callers on one topic_wait: one subscription, and
    each gets its own reply although the replies arrive in reverse."""
    agent, broker = _make_agent()
    try:
        spawned = [
            _spawn_publish_sync(agent, 'T', f'body-{i}', timeout=5.0)
            for i in range(n)
        ]
        assert _wait_until(lambda: len(_requests(broker)) == n, timeout=3.0), \
            f'{len(_requests(broker))} of {n} requests published'
        assert _count_subscribed(broker, 'T') == 1

        for request in reversed(_requests(broker)):
            _reply_to(broker, request, f'reply-{request.content}')
        for i, (thread, result, error) in enumerate(spawned):
            thread.join(5.0)
            assert error[0] is None, error[0]
            assert result[0].content == f'reply-body-{i}'
        assert 'T' not in _handlers(agent)
        assert broker.unsubscribe_calls.count('T') == 1
    finally:
        agent.terminate()


# ==========================================================================
# F. Atomicity stress (register / cleanup race)
# ==========================================================================

def test_concurrent_callers_on_one_topic_wait_race_stress():
    """Rounds of 8 concurrent publish_sync calls on one topic_wait,
    answered inline from broker.publish by an RFC-014 responder, so
    callers join and leave the topic in every interleaving. Every
    caller gets its own reply, and no registry entry or subscription is
    left behind."""
    agent, broker = _make_agent()
    original_publish = broker.publish
    try:
        def respond(pub_topic, payload):
            original_publish(pub_topic, payload)
            request = Parcel.from_payload(payload)
            if request.topic_return:
                _reply_to(broker, request, f'reply-{request.content}')
        broker.publish = respond

        callers = 8
        for trial in range(10):
            topic = f'T-{trial}'
            barrier = threading.Barrier(callers, timeout=3.0)
            outcomes: List[dict] = [{} for _ in range(callers)]

            def target(i):
                try:
                    barrier.wait()
                    outcomes[i]['reply'] = agent.publish_sync(
                        'req', f'{trial}-{i}', topic_wait=topic, timeout=3.0,
                    )
                except BaseException as ex:
                    outcomes[i]['error'] = ex

            threads = [threading.Thread(target=target, args=(i,), daemon=True)
                       for i in range(callers)]
            for th in threads:
                th.start()
            for th in threads:
                th.join(5.0)

            errors = [o['error'] for o in outcomes if 'error' in o]
            assert not errors, f'trial {trial}: {errors}'
            assert [o['reply'].content for o in outcomes] == \
                [f'reply-{trial}-{i}' for i in range(callers)]
            assert topic not in _handlers(agent)
            subscribes = _count_subscribed(broker, topic)
            assert subscribes >= 1
            assert broker.unsubscribe_calls.count(topic) == subscribes
    finally:
        agent.terminate()


def test_check_and_pop_atomicity_prevents_torn_state():
    """After many rounds of sequential ownership transfers, the
    registry never accumulates orphan entries and never carries
    stale broker unsubscribe imbalance."""
    agent, broker = _make_agent()
    try:
        broker.auto_respond_with('r')
        for i in range(50):
            topic = f'seq-{i}'
            agent.publish_sync('req', 'x', topic_wait=topic, timeout=1.0)
            assert topic not in _handlers(agent)
            # Each round: one subscribe + one unsubscribe on that topic.
            assert _count_subscribed(broker, topic) == 1
            assert broker.unsubscribe_calls.count(topic) == 1
    finally:
        agent.terminate()


class _SlowUnsubscribeBroker(FakeBroker):
    """Holds every unsubscribe until `release` is set."""

    def __init__(self, notifier):
        super().__init__(notifier)
        self.unsubscribing = threading.Event()
        self.release = threading.Event()

    def unsubscribe(self, topic):
        self.unsubscribing.set()
        self.release.wait(3.0)
        super().unsubscribe(topic)


def test_a_caller_joining_while_the_last_one_unsubscribes_stays_subscribed():
    """The last caller to leave a topic_wait unsubscribes it after
    releasing the registry lock. A caller arriving meanwhile must not
    subscribe before that unsubscribe lands, or it would wait on a
    topic the broker no longer delivers. (This race predates RFC-014
    for sequential reuse; sharing makes it common.)"""
    agent = Agent(name='test_r006', agent_config={
        'dispatch': {'shutdown_timeout_s': 1.0},
    })
    broker = _SlowUnsubscribeBroker(notifier=agent)
    agent._broker = broker
    agent._agent_worker = FakeWorker()
    try:
        broker.auto_respond_with('for-A')
        a_thread, a_result, _ = _spawn_publish_sync(agent, 'T', 'A', timeout=2.0)
        assert broker.unsubscribing.wait(2.0)       # A answered and is leaving
        broker.stop_auto_responding()

        b_thread, b_result, _ = _spawn_publish_sync(agent, 'T', 'B', timeout=3.0)
        time.sleep(0.3)                             # B arrives during A's unsubscribe
        broker.release.set()
        a_thread.join(3.0)
        assert a_result[0].content == 'for-A'

        assert _wait_until(lambda: 'T' in broker.subscribed_topics, timeout=2.0), \
            'B is waiting on a topic that is no longer subscribed'
        assert _wait_until(lambda: len(_requests(broker)) == 2, timeout=2.0)
        _reply_to(broker, _requests(broker)[1], 'for-B')
        b_thread.join(3.0)
        assert b_result[0].content == 'for-B'
    finally:
        broker.release.set()
        agent.terminate()


# ==========================================================================
# G. Shared topic_wait (RFC-014 §8), and an aspirational strict xfail
# ==========================================================================

def test_both_callers_should_receive_own_response_with_shared_topic_wait():
    """RFC-014 §8: each caller gets the reply carrying its own
    correlation id, whatever order the replies arrive in."""
    agent, broker = _make_agent()
    try:
        a_thread, a_result, a_error = _spawn_publish_sync(
            agent, 'T', 'A', timeout=3.0,
        )
        b_thread, b_result, b_error = _spawn_publish_sync(
            agent, 'T', 'B', timeout=3.0,
        )
        assert _wait_until(
            lambda: len(_requests(broker)) == 2 or a_error[0] or b_error[0],
            timeout=1.0,
        )

        for request in reversed(_requests(broker)):
            _reply_to(broker, request, f'for-{request.content}')

        a_thread.join(3.0)
        b_thread.join(3.0)
        assert (a_error[0], b_error[0]) == (None, None)
        assert (a_result[0].content, b_result[0].content) == ('for-A', 'for-B')
    finally:
        agent.terminate()


@pytest.mark.xfail(
    reason=('R.4 follow-up: RFC-006 addresses the publish_sync '
            'collision hazard by fail-fast, but the underlying '
            'framework still maps one handler per topic in '
            '__topic_handlers. Multi-handler fan-out is a broader '
            'framework change deferred to a future RFC.'),
    strict=True,
)
def test_framework_should_support_multiple_handlers_per_topic():
    agent, broker = _make_agent()
    try:
        received_A = []
        received_B = []

        agent.subscribe(
            'T', topic_handler=lambda t, p: received_A.append(p.content),
        )
        agent.subscribe(
            'T', topic_handler=lambda t, p: received_B.append(p.content),
        )

        broker.deliver('T', TextParcel('X').payload())
        assert _wait_until(
            lambda: received_A and received_B, timeout=1.0,
        )
    finally:
        agent.terminate()
