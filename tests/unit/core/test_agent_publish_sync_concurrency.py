"""Tests for RFC-006 publish_sync topic_wait collision (fail-fast).

Post-RFC-006:

  - publish_sync detects an already-awaited topic_wait under a lock
    and raises TopicWaitCollisionError(RuntimeError) immediately —
    no subscribe, no publish, no wait.
  - The register + collision check is atomic under _handlers_lock.
  - The finally identity-check-and-pop is atomic under the same lock.
  - Agent.subscribe's warn-then-overwrite is unchanged (RFC-006 §7.11
    scope limitation).
  - Distinct topic_wait or omitted topic_wait continues to work.

Two aspirational xfails remain, tracking correlation-ID / multi-
handler fan-out (deferred to a future RFC).
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


def test_second_publish_sync_with_same_topic_wait_fails_fast():
    """Was xfail(strict). Post-RFC-006: passes.
    Second publish_sync with an already-awaited topic_wait raises
    TopicWaitCollisionError within milliseconds (no timeout wait)."""
    agent, broker = _make_agent()
    try:
        a_thread, _, _ = _spawn_publish_sync(agent, 'T', 'A', timeout=2.0)
        assert _wait_until(
            lambda: _count_subscribed(broker, 'T') == 1, timeout=1.0,
        )

        start = time.monotonic()
        with pytest.raises(TopicWaitCollisionError):
            agent.publish_sync('req', 'B', topic_wait='T', timeout=1.0)
        elapsed = time.monotonic() - start
        assert elapsed < 0.1, (
            f'collision should raise immediately; elapsed={elapsed:.3f}s'
        )

        a_thread.join(3.0)
    finally:
        agent.terminate()


def test_publish_sync_two_threads_second_raises_collision():
    """Two threads with the same topic_wait: first owns; second
    raises TopicWaitCollisionError. First handler is not overwritten."""
    agent, broker = _make_agent()
    try:
        a_thread, a_result, a_error = _spawn_publish_sync(
            agent, 'T', 'A-body', timeout=1.0,
        )
        assert _wait_until(
            lambda: _count_subscribed(broker, 'T') == 1, timeout=1.0,
        )
        first_handler = _handlers(agent).get('T')
        assert first_handler is not None

        # Second call raises immediately.
        with pytest.raises(TopicWaitCollisionError):
            agent.publish_sync('req', 'B-body', topic_wait='T', timeout=1.0)

        # First handler is still the same (not overwritten).
        assert _handlers(agent).get('T') is first_handler

        # Only one broker.subscribe for 'T'.
        assert _count_subscribed(broker, 'T') == 1

        # Deliver response — A completes normally.
        broker.deliver('T', TextParcel('reply-for-A').payload())
        a_thread.join(2.0)
        assert a_result[0].content == 'reply-for-A'
        assert a_error[0] is None
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
        a_thread, _, _ = _spawn_publish_sync(
            agent, 'my/topic/here', 'A', timeout=1.0,
        )
        assert _wait_until(
            lambda: _count_subscribed(broker, 'my/topic/here') == 1,
            timeout=1.0,
        )

        with pytest.raises(TopicWaitCollisionError) as exc_info:
            agent.publish_sync(
                'req', 'B', topic_wait='my/topic/here', timeout=1.0,
            )
        assert 'my/topic/here' in str(exc_info.value)

        a_thread.join(3.0)
    finally:
        agent.terminate()


def test_collision_does_not_call_broker_subscribe_for_second_caller():
    agent, broker = _make_agent()
    try:
        a_thread, _, _ = _spawn_publish_sync(agent, 'T', 'A', timeout=1.0)
        assert _wait_until(
            lambda: _count_subscribed(broker, 'T') == 1, timeout=1.0,
        )
        subscribes_before = len(broker.subscribe_calls)

        with pytest.raises(TopicWaitCollisionError):
            agent.publish_sync('req', 'B', topic_wait='T', timeout=1.0)

        # No new broker.subscribe call.
        assert len(broker.subscribe_calls) == subscribes_before

        a_thread.join(2.0)
    finally:
        agent.terminate()


def test_collision_does_not_call_broker_publish_for_second_caller():
    agent, broker = _make_agent()
    try:
        a_thread, _, _ = _spawn_publish_sync(agent, 'T', 'A', timeout=1.0)
        assert _wait_until(
            lambda: _count_subscribed(broker, 'T') == 1, timeout=1.0,
        )
        assert _wait_until(
            lambda: _count_published(broker, 'req') == 1, timeout=1.0,
        )
        publishes_before = len(broker.publish_calls)

        with pytest.raises(TopicWaitCollisionError):
            agent.publish_sync('req', 'B', topic_wait='T', timeout=1.0)

        # No new broker.publish call.
        assert len(broker.publish_calls) == publishes_before

        a_thread.join(2.0)
    finally:
        agent.terminate()


def test_collision_does_not_touch_registry_for_other_topics():
    agent, broker = _make_agent()
    try:
        # Register an unrelated handler directly.
        def unrelated(topic, pcl):
            pass
        agent.subscribe('other/topic', topic_handler=unrelated)

        a_thread, _, _ = _spawn_publish_sync(agent, 'T', 'A', timeout=1.0)
        assert _wait_until(
            lambda: _count_subscribed(broker, 'T') == 1, timeout=1.0,
        )
        handlers_snapshot_before = dict(_handlers(agent))

        with pytest.raises(TopicWaitCollisionError):
            agent.publish_sync('req', 'B', topic_wait='T', timeout=1.0)

        # Registry is unchanged (only 'T' matters; 'other/topic' still
        # bound to unrelated).
        assert _handlers(agent) == handlers_snapshot_before
        assert _handlers(agent)['other/topic'].handler is unrelated
        assert _handlers(agent)['other/topic'].owner_type is _HandlerOwnerType.NORMAL

        a_thread.join(2.0)
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
# E. Stress: N callers, same topic_wait — exactly 1 owns, N-1 collide
# ==========================================================================

@pytest.mark.parametrize('n', [3, 10, 50])
def test_N_callers_same_topic_wait_first_owns_rest_collide(n):
    """Post-RFC-006: exactly one caller acquires ownership; every
    other caller raises TopicWaitCollisionError immediately (does
    NOT time out)."""
    agent, broker = _make_agent()
    try:
        threads = []
        results = []
        errors = []
        for i in range(n):
            t, r, e = _spawn_publish_sync(
                agent, 'T', f'body-{i}', timeout=2.0,
            )
            threads.append(t)
            results.append(r)
            errors.append(e)

        # Wait until every thread has either acquired ownership
        # (subscribed) or collided (raised). At most one subscribe
        # will land on 'T'; the rest raise immediately.
        assert _wait_until(
            lambda: (
                _count_subscribed(broker, 'T') == 1
                and sum(1 for e in errors if e[0] is not None) == n - 1
            ),
            timeout=3.0,
        ), (
            f'expected 1 subscribe + {n-1} collisions; '
            f'got subs={_count_subscribed(broker, "T")}, '
            f'errors={sum(1 for e in errors if e[0] is not None)}'
        )

        # Every error is TopicWaitCollisionError; nobody timed out.
        collision_count = sum(
            1 for e in errors if isinstance(e[0], TopicWaitCollisionError)
        )
        timeout_count = sum(
            1 for e in errors if isinstance(e[0], TimeoutError)
        )
        assert collision_count == n - 1
        assert timeout_count == 0

        # The one owner receives a response.
        broker.deliver('T', TextParcel('for-owner').payload())
        for t in threads:
            t.join(3.0)

        completed = sum(1 for r in results if r[0] is not None)
        assert completed == 1
        # Owner's response content is what we delivered.
        owner_result = [r[0] for r in results if r[0] is not None][0]
        assert owner_result.content == 'for-owner'
    finally:
        agent.terminate()


# ==========================================================================
# F. Atomicity stress (register / cleanup race)
# ==========================================================================

def test_atomic_ownership_under_concurrent_race_stress():
    """Many concurrent publish_sync calls compete for ownership of
    the same topic_wait. Exactly one wins per round; there is no
    torn state (no partial registration, no orphan handler)."""
    agent, broker = _make_agent()
    try:
        trials = 10
        threads_per_trial = 8
        for trial in range(trials):
            topic = f'T-{trial}'
            broker.auto_respond_with(f'r-{trial}')

            threads = []
            errors = []
            results = []
            barrier = threading.Barrier(threads_per_trial, timeout=3.0)

            def target(r, e):
                try:
                    barrier.wait()
                    r[0] = agent.publish_sync(
                        'req', 'x', topic_wait=topic, timeout=2.0,
                    )
                except BaseException as ex:
                    e[0] = ex

            for _ in range(threads_per_trial):
                r = [None]
                e = [None]
                results.append(r)
                errors.append(e)
                th = threading.Thread(target=target, args=(r, e), daemon=True)
                threads.append(th)
                th.start()

            for th in threads:
                th.join(4.0)

            completed = sum(1 for r in results if r[0] is not None)
            collisions = sum(
                1 for e in errors if isinstance(e[0], TopicWaitCollisionError)
            )
            other = sum(
                1 for e in errors
                if e[0] is not None
                and not isinstance(e[0], TopicWaitCollisionError)
            )
            assert other == 0, (
                f'trial {trial}: unexpected errors '
                f'{[e[0] for e in errors if e[0] is not None and not isinstance(e[0], TopicWaitCollisionError)]}'
            )
            assert completed == 1, f'trial {trial}: completed={completed}'
            assert collisions == threads_per_trial - 1, (
                f'trial {trial}: collisions={collisions}'
            )
            # After every trial, no orphan handler remains.
            assert topic not in _handlers(agent)

            broker.stop_auto_responding()
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


# ==========================================================================
# G. Aspirational strict xfails — kept for future RFC
# ==========================================================================

@pytest.mark.xfail(
    reason=('R.4 follow-up: RFC-006 closed the fast-fail path — a '
            'second caller with the same topic_wait now raises '
            'TopicWaitCollisionError. True concurrent multi-caller '
            'support (both callers receive their own response) '
            'requires a correlation-ID metadata field on Parcel and '
            'is deferred to a future RFC under R-20.'),
    strict=True,
)
def test_both_callers_should_receive_own_response_with_shared_topic_wait():
    agent, broker = _make_agent()
    try:
        a_thread, a_result, a_error = _spawn_publish_sync(
            agent, 'T', 'A', timeout=1.0,
        )
        assert _wait_until(
            lambda: _count_subscribed(broker, 'T') == 1, timeout=1.0,
        )
        b_thread, b_result, b_error = _spawn_publish_sync(
            agent, 'T', 'B', timeout=1.0,
        )
        assert _wait_until(
            lambda: b_error[0] is not None or _count_subscribed(broker, 'T') == 2,
            timeout=1.0,
        )

        broker.deliver('T', TextParcel('for-A').payload())
        broker.deliver('T', TextParcel('for-B').payload())

        a_thread.join(2.0)
        b_thread.join(2.0)
        # Ideal (future RFC): both complete with own responses.
        assert a_result[0] is not None
        assert b_result[0] is not None
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
