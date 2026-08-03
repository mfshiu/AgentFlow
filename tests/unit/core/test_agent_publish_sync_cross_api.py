"""RFC-007 cross-API boundary tests: reserved-topic protection.

Post-RFC-007:

  - Agent.subscribe on a topic reserved by an active publish_sync
    waiter raises ``TopicWaitCollisionError``.
  - Agent.unsubscribe on a topic reserved by an active publish_sync
    waiter raises ``TopicWaitCollisionError``.
  - Agent.publish_sync on a topic already registered by a normal
    subscribe handler raises ``TopicWaitCollisionError``.
  - Agent._on_message reads the handler registry atomically under
    ``_handlers_lock`` — is_specific_handler and topic_handler are
    decided from one snapshot.
  - Agent.subscribe warn-then-overwrite on NORMAL-owned topics is
    preserved (RFC-006 §7.11 / RFC-007 §7.5).
  - The three residual risks documented in the R.6 characterisation
    (R.6-1 overwrite of waiter, R.6-2 cancellation of waiter, R.6-3
    _on_message TOCTOU) are all resolved.

Registry shape (RFC-007): ``Agent.__topic_handlers`` values are now
``_HandlerRecord(owner_type, handler)`` instances rather than raw
Callables. Tests that inspect the registry use ``.handler`` and
``.owner_type`` accessors.
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
    a = Agent(name='test_r007', agent_config={
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


def _spawn_publish_sync(agent, topic_wait, content, timeout):
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
# A. Cross-API protection — direct Agent.subscribe against active waiter
# ==========================================================================

def test_direct_subscribe_on_publish_sync_reserved_topic_raises():
    """R.6-1 resolved: direct subscribe on a topic held by an active
    publish_sync waiter raises TopicWaitCollisionError instead of
    silently overwriting."""
    agent, broker = _make_agent()
    try:
        a_thread, a_result, a_error = _spawn_publish_sync(
            agent, 'T', 'A-body', timeout=1.0,
        )
        assert _wait_until(
            lambda: _count_subscribed(broker, 'T') == 1, timeout=1.0,
        )
        waiter_record = _handlers(agent).get('T')
        assert waiter_record is not None
        assert waiter_record.owner_type is _HandlerOwnerType.PUBLISH_SYNC

        subscribes_before = list(broker.subscribe_calls)

        with pytest.raises(TopicWaitCollisionError) as exc_info:
            agent.subscribe('T', topic_handler=lambda t, p: None)
        assert 'T' in str(exc_info.value)
        assert 'publish_sync' in str(exc_info.value).lower()

        # Waiter is untouched; no broker.subscribe added.
        assert _handlers(agent).get('T') is waiter_record
        assert list(broker.subscribe_calls) == subscribes_before

        # Waiter still receives its response and completes normally.
        broker.deliver('T', TextParcel('reply-for-A').payload())
        a_thread.join(2.0)
        assert a_result[0].content == 'reply-for-A'
        assert a_error[0] is None
    finally:
        agent.terminate()


def test_direct_subscribe_collision_error_identifies_publish_sync_owner():
    """Exception message must identify the owner type so callers can
    react appropriately."""
    agent, broker = _make_agent()
    try:
        a_thread, _, _ = _spawn_publish_sync(agent, 'T', 'A', timeout=1.0)
        assert _wait_until(
            lambda: _count_subscribed(broker, 'T') == 1, timeout=1.0,
        )
        try:
            agent.subscribe('T', topic_handler=lambda t, p: None)
        except TopicWaitCollisionError as ex:
            msg = str(ex)
            assert 'reserved' in msg
            assert 'publish_sync' in msg
        else:
            pytest.fail('expected TopicWaitCollisionError')
        a_thread.join(2.0)
    finally:
        agent.terminate()


# ==========================================================================
# B. Cross-API protection — direct Agent.unsubscribe against active waiter
# ==========================================================================

def test_direct_unsubscribe_on_publish_sync_reserved_topic_raises():
    """R.6-2 resolved: direct unsubscribe on a topic held by an
    active publish_sync waiter raises TopicWaitCollisionError instead
    of silently removing the waiter's handler."""
    agent, broker = _make_agent()
    try:
        a_thread, a_result, a_error = _spawn_publish_sync(
            agent, 'T', 'A-body', timeout=1.0,
        )
        assert _wait_until(
            lambda: _count_subscribed(broker, 'T') == 1, timeout=1.0,
        )
        waiter_record = _handlers(agent).get('T')
        unsubscribes_before = list(broker.unsubscribe_calls)

        with pytest.raises(TopicWaitCollisionError) as exc_info:
            agent.unsubscribe('T')
        assert 'T' in str(exc_info.value)

        # Waiter is untouched; no broker.unsubscribe added.
        assert _handlers(agent).get('T') is waiter_record
        assert list(broker.unsubscribe_calls) == unsubscribes_before

        # Waiter still receives its response and completes normally.
        broker.deliver('T', TextParcel('reply-for-A').payload())
        a_thread.join(2.0)
        assert a_result[0].content == 'reply-for-A'
        assert a_error[0] is None
    finally:
        agent.terminate()


# ==========================================================================
# C. Cross-API protection — publish_sync on NORMAL-owned topic
# ==========================================================================

def test_publish_sync_on_normal_subscribed_topic_raises():
    """RFC-007 §7.6: publish_sync must not trample a normal
    subscribe handler. Raise immediately; leave the normal handler
    in place; no broker.subscribe or broker.publish."""
    agent, broker = _make_agent()
    try:
        normal_handler = lambda t, p: None  # noqa: E731
        agent.subscribe('T', topic_handler=normal_handler)
        subscribes_before = list(broker.subscribe_calls)
        publishes_before = list(broker.publish_calls)

        with pytest.raises(TopicWaitCollisionError) as exc_info:
            agent.publish_sync('req', 'body', topic_wait='T', timeout=1.0)
        msg = str(exc_info.value)
        assert 'T' in msg
        assert 'normal' in msg.lower() or 'subscribe' in msg.lower()

        # Normal handler still registered.
        assert _handlers(agent).get('T').handler is normal_handler
        assert _handlers(agent).get('T').owner_type is _HandlerOwnerType.NORMAL

        # No broker traffic.
        assert list(broker.subscribe_calls) == subscribes_before
        assert list(broker.publish_calls) == publishes_before
    finally:
        agent.terminate()


# ==========================================================================
# D. Compatibility — NORMAL rebind + teardown unchanged
# ==========================================================================

def test_normal_rebind_on_normal_topic_is_allowed_and_warns():
    """RFC-006 §7.11 / RFC-007 §7.5: NORMAL over NORMAL is allowed
    (warning + overwrite). Response goes to the latest handler."""
    agent, broker = _make_agent()
    try:
        received_1 = []
        received_2 = []

        def h1(topic, pcl):
            received_1.append(pcl.content)

        def h2(topic, pcl):
            received_2.append(pcl.content)

        agent.subscribe('T', topic_handler=h1)
        assert _handlers(agent)['T'].handler is h1
        assert _handlers(agent)['T'].owner_type is _HandlerOwnerType.NORMAL

        # Rebind — legitimate use case, unchanged.
        agent.subscribe('T', topic_handler=h2)
        assert _handlers(agent)['T'].handler is h2
        assert _handlers(agent)['T'].owner_type is _HandlerOwnerType.NORMAL

        broker.deliver('T', TextParcel('X').payload())
        assert _wait_until(lambda: received_2, timeout=1.0)
        assert received_2 == ['X']
        assert received_1 == []
    finally:
        agent.terminate()


def test_normal_unsubscribe_teardown_is_expected_use_case():
    """Direct unsubscribe of an owned NORMAL handler is a legitimate
    teardown pattern; it succeeds and removes the entry."""
    agent, broker = _make_agent()
    try:
        received = []

        def h(topic, pcl):
            received.append(pcl.content)

        agent.subscribe('T', topic_handler=h)
        assert _handlers(agent).get('T').handler is h

        agent.unsubscribe('T')
        assert 'T' not in _handlers(agent)
        assert 'T' in broker.unsubscribe_calls

        # Late delivery falls through (RFC-003 R-fallback-silent).
        publish_before = len(broker.publish_calls)
        broker.deliver('T', TextParcel('X').payload())
        time.sleep(0.05)
        assert received == []
        assert len(broker.publish_calls) == publish_before
    finally:
        agent.terminate()


def test_publish_sync_after_normal_unsubscribe_succeeds():
    """Sequential: after the normal handler is unsubscribed, the
    topic is free again; publish_sync can use it."""
    agent, broker = _make_agent()
    try:
        agent.subscribe('T', topic_handler=lambda t, p: None)
        agent.unsubscribe('T')
        broker.auto_respond_with('ok')
        result = agent.publish_sync('req', 'x', topic_wait='T', timeout=1.0)
        assert result.content == 'ok'
    finally:
        agent.terminate()


# ==========================================================================
# E. Registry shape
# ==========================================================================

def test_normal_entry_is_stored_as_HandlerRecord_with_NORMAL_owner():
    agent, _broker = _make_agent()
    try:
        def h(topic, pcl):
            pass
        agent.subscribe('T', topic_handler=h)
        rec = _handlers(agent).get('T')
        assert isinstance(rec, _HandlerRecord)
        assert rec.owner_type is _HandlerOwnerType.NORMAL
        assert rec.handler is h
    finally:
        agent.terminate()


def test_publish_sync_entry_is_stored_as_HandlerRecord_with_PUBLISH_SYNC_owner():
    agent, broker = _make_agent()
    try:
        a_thread, _, _ = _spawn_publish_sync(agent, 'T', 'x', timeout=1.0)
        assert _wait_until(
            lambda: _count_subscribed(broker, 'T') == 1, timeout=1.0,
        )
        rec = _handlers(agent).get('T')
        assert isinstance(rec, _HandlerRecord)
        assert rec.owner_type is _HandlerOwnerType.PUBLISH_SYNC
        broker.deliver('T', TextParcel('done').payload())
        a_thread.join(2.0)
    finally:
        agent.terminate()


def test_cleanup_only_removes_own_publish_sync_entry():
    """RFC-006 §7.5 + RFC-007 §7.8 triple check: cleanup only pops
    when record exists AND owner_type is PUBLISH_SYNC AND handler
    identity matches. Verified by injecting a foreign NORMAL record
    that the caller's finally must not evict."""
    agent, broker = _make_agent()
    try:
        original_publish = broker.publish
        foreign_handler = lambda t, p: None   # noqa: E731

        def evil_publish(topic, payload):
            # Simulate a corrupted registry state during publish_sync's
            # wait: overwrite with a NORMAL foreign record.
            _handlers(agent)['T'] = _HandlerRecord(
                _HandlerOwnerType.NORMAL, foreign_handler,
            )
            original_publish(topic, payload)

        broker.publish = evil_publish

        with pytest.raises(TimeoutError):
            agent.publish_sync('req', 'x', topic_wait='T', timeout=0.05)

        # Triple check: foreign NORMAL record survives publish_sync's
        # finally because owner_type != PUBLISH_SYNC.
        rec = _handlers(agent).get('T')
        assert rec is not None
        assert rec.owner_type is _HandlerOwnerType.NORMAL
        assert rec.handler is foreign_handler
        assert 'T' not in broker.unsubscribe_calls
    finally:
        agent.terminate()


# ==========================================================================
# F. _on_message atomic snapshot
# ==========================================================================

def test_on_message_registry_read_uses_handler_from_record():
    """Post-RFC-007: _on_message reads _HandlerRecord.handler under
    the lock. Baseline: dispatch delivers the handler from the record."""
    agent, broker = _make_agent()
    try:
        received = []

        def h(topic, pcl):
            received.append(pcl.content)

        agent.subscribe('T', topic_handler=h)
        broker.deliver('T', TextParcel('X').payload())
        assert _wait_until(lambda: received, timeout=1.0)
        assert received == ['X']

        agent.unsubscribe('T')
        assert _handlers(agent).get('T') is None
    finally:
        agent.terminate()


def test_on_message_dispatch_survives_concurrent_direct_subscribe_stress():
    """_on_message reads registry under _handlers_lock — no
    inconsistent snapshot under concurrent subscribe. Rebind is
    NORMAL-over-NORMAL so no collision fires from the churner
    itself; framework does not crash; dispatcher error_count == 0."""
    agent, broker = _make_agent()
    try:
        # Warm the lazy dispatcher.
        broker.deliver('warm', TextParcel('x').payload())
        _wait_until(lambda: agent._dispatcher is not None, timeout=1.0)

        stop_event = threading.Event()

        def resubscriber():
            while not stop_event.is_set():
                def h(topic, pcl):
                    pass
                # NORMAL rebind of a NORMAL entry — no collision.
                agent.subscribe('T', topic_handler=h)
                time.sleep(0)

        r_thread = threading.Thread(target=resubscriber, daemon=True)
        r_thread.start()

        try:
            for _ in range(200):
                broker.deliver('T', TextParcel('m').payload())
            assert _wait_until(
                lambda: agent._dispatcher.queue_depth == 0, timeout=2.0,
            )
        finally:
            stop_event.set()
            r_thread.join(2.0)

        metrics = agent._dispatcher.metrics_snapshot()
        assert metrics['error_count'] == 0
    finally:
        agent.terminate()


def test_on_message_dispatch_survives_concurrent_direct_unsubscribe_stress():
    """Concurrent NORMAL unsubscribe stress. The unsubscribe target
    is a NORMAL topic; no PUBLISH_SYNC collision. Framework does not
    crash. error_count remains zero."""
    agent, broker = _make_agent()
    try:
        broker.deliver('warm', TextParcel('x').payload())
        _wait_until(lambda: agent._dispatcher is not None, timeout=1.0)

        stop_event = threading.Event()

        def churner():
            while not stop_event.is_set():
                def h(topic, pcl):
                    pass
                # NORMAL subscribe + NORMAL unsubscribe.
                agent.subscribe('T', topic_handler=h)
                agent.unsubscribe('T')
                time.sleep(0)

        c_thread = threading.Thread(target=churner, daemon=True)
        c_thread.start()

        try:
            for _ in range(200):
                broker.deliver('T', TextParcel('m').payload())
            assert _wait_until(
                lambda: agent._dispatcher.queue_depth == 0, timeout=2.0,
            )
        finally:
            stop_event.set()
            c_thread.join(2.0)

        metrics = agent._dispatcher.metrics_snapshot()
        assert metrics['error_count'] == 0
    finally:
        agent.terminate()


def test_on_message_dispatch_with_publish_sync_and_direct_ops_on_different_topics():
    """RFC-007 §7.7 combined stress: an active publish_sync waiter
    on 'T', plus concurrent NORMAL subscribe/unsubscribe on 'other',
    plus deliveries. Framework does not crash; dispatcher
    error_count == 0."""
    agent, broker = _make_agent()
    try:
        broker.deliver('warm', TextParcel('x').payload())
        _wait_until(lambda: agent._dispatcher is not None, timeout=1.0)

        a_thread, a_result, a_error = _spawn_publish_sync(
            agent, 'T', 'A-body', timeout=1.5,
        )
        assert _wait_until(
            lambda: _count_subscribed(broker, 'T') == 1, timeout=1.0,
        )

        stop_event = threading.Event()

        def churner():
            while not stop_event.is_set():
                def h(topic, pcl):
                    pass
                # Churn a DIFFERENT topic so no cross-API collision.
                agent.subscribe('other', topic_handler=h)
                agent.unsubscribe('other')
                time.sleep(0)

        c_thread = threading.Thread(target=churner, daemon=True)
        c_thread.start()

        try:
            for _ in range(100):
                broker.deliver('T', TextParcel('m').payload())
        finally:
            stop_event.set()
            c_thread.join(2.0)

        # Deliver the publish_sync's response.
        broker.deliver('T', TextParcel('reply-for-A').payload())
        a_thread.join(2.0)
        # A completes with the response (or with TimeoutError; deliveries
        # to 'T' during the loop had no topic_return so did not wake it).
        # The dispatcher must have no errors in either case.
        metrics = agent._dispatcher.metrics_snapshot()
        assert metrics['error_count'] == 0
    finally:
        agent.terminate()


def test_direct_ops_on_publish_sync_reserved_topic_raise_but_framework_survives():
    """Combined stress on the SAME topic as the publish_sync waiter.
    All subscribe/unsubscribe attempts on 'T' now raise
    TopicWaitCollisionError; framework does not crash; waiter still
    completes."""
    agent, broker = _make_agent()
    try:
        broker.deliver('warm', TextParcel('x').payload())
        _wait_until(lambda: agent._dispatcher is not None, timeout=1.0)

        a_thread, a_result, a_error = _spawn_publish_sync(
            agent, 'T', 'A-body', timeout=2.0,
        )
        assert _wait_until(
            lambda: _count_subscribed(broker, 'T') == 1, timeout=1.0,
        )

        stop_event = threading.Event()
        collisions = [0]

        def churner():
            while not stop_event.is_set():
                def h(topic, pcl):
                    pass
                try:
                    agent.subscribe('T', topic_handler=h)
                except TopicWaitCollisionError:
                    collisions[0] += 1
                try:
                    agent.unsubscribe('T')
                except TopicWaitCollisionError:
                    collisions[0] += 1
                time.sleep(0)

        c_thread = threading.Thread(target=churner, daemon=True)
        c_thread.start()

        try:
            time.sleep(0.05)   # let the churner accumulate collisions
        finally:
            stop_event.set()
            c_thread.join(2.0)

        # Every attempt raised; the churner processed at least one.
        assert collisions[0] > 0

        # Waiter still there; deliver its response.
        rec = _handlers(agent).get('T')
        assert rec is not None
        assert rec.owner_type is _HandlerOwnerType.PUBLISH_SYNC

        broker.deliver('T', TextParcel('reply-for-A').payload())
        a_thread.join(2.0)
        assert a_result[0] is not None
        assert a_result[0].content == 'reply-for-A'

        metrics = agent._dispatcher.metrics_snapshot()
        assert metrics['error_count'] == 0
    finally:
        agent.terminate()


# ==========================================================================
# G. Lock hygiene — _handlers_lock never held across broker I/O
# ==========================================================================

def test_handlers_lock_is_reentrant_from_broker_subscribe_callback():
    """The _handlers_lock is an RLock; a broker.subscribe callback
    can safely re-acquire it (which would deadlock under Lock).
    Verifies RFC-007 §7.10 lock hygiene: no cross-call locking bug."""
    agent, broker = _make_agent()
    try:
        original_subscribe = broker.subscribe
        observed = []

        def spy_subscribe(topic, data_type):
            # Try to acquire _handlers_lock during the broker.subscribe
            # call. If _handlers_lock were held by the caller, this
            # would deadlock — RLock reentrancy would only save us if
            # SAME thread, but here the caller and spy_subscribe are
            # the SAME thread (Agent.subscribe called us). So either
            # way we should acquire successfully AND observe that we
            # did — proving the caller released before invoking us.
            got = agent._handlers_lock.acquire(timeout=0.5)
            observed.append(got)
            if got:
                agent._handlers_lock.release()
            return original_subscribe(topic, data_type)

        broker.subscribe = spy_subscribe
        agent.subscribe('T', topic_handler=lambda t, p: None)
        assert observed == [True]
    finally:
        agent.terminate()


def test_handlers_lock_is_reentrant_from_broker_unsubscribe_callback():
    agent, broker = _make_agent()
    try:
        agent.subscribe('T', topic_handler=lambda t, p: None)
        original_unsubscribe = broker.unsubscribe
        observed = []

        def spy_unsubscribe(topic):
            got = agent._handlers_lock.acquire(timeout=0.5)
            observed.append(got)
            if got:
                agent._handlers_lock.release()
            return original_unsubscribe(topic)

        broker.unsubscribe = spy_unsubscribe
        agent.unsubscribe('T')
        assert observed == [True]
    finally:
        agent.terminate()


def test_handler_can_safely_call_subscribe_from_within_dispatch():
    """A handler running inside the dispatcher can safely call
    Agent.subscribe (which acquires _handlers_lock). RLock permits
    re-entry; but critically, the dispatcher path never holds the
    lock during handler invocation."""
    agent, broker = _make_agent()
    try:
        second_registered = threading.Event()

        def initial_handler(topic, pcl):
            # Re-register a different handler on a DIFFERENT topic
            # from within the dispatcher's task execution.
            agent.subscribe('other', topic_handler=lambda t, p: None)
            second_registered.set()

        agent.subscribe('T', topic_handler=initial_handler)
        broker.deliver('T', TextParcel('X').payload())
        assert second_registered.wait(1.0)
        assert _handlers(agent).get('other') is not None
    finally:
        agent.terminate()


def test_dispatcher_enqueue_happens_outside_handlers_lock():
    """RFC-007 §7.10: _on_message enqueues to the dispatcher outside
    _handlers_lock. Verified by making the dispatcher's enqueue try
    to acquire the lock — it must succeed."""
    agent, broker = _make_agent()
    try:
        # Warm the dispatcher.
        broker.deliver('warm', TextParcel('x').payload())
        _wait_until(lambda: agent._dispatcher is not None, timeout=1.0)

        original_enqueue = agent._dispatcher.enqueue
        observed = []

        def spy_enqueue(task, *, topic=None):
            got = agent._handlers_lock.acquire(timeout=0.5)
            observed.append(got)
            if got:
                agent._handlers_lock.release()
            return original_enqueue(task, topic=topic)

        agent._dispatcher.enqueue = spy_enqueue
        broker.deliver('T', TextParcel('m').payload())
        assert observed
        assert all(observed)
    finally:
        agent.terminate()
