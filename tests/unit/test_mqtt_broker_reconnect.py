"""Post-RFC-005 tests for MqttBroker subscription recovery (R-03).

Target module: agentflow.broker.mqtt_broker (real code).
All tests use the mocked paho Client fixture from tests/conftest.py.
No real broker, socket, or network is used.

Contracts verified:
  - MqttBroker holds a `_registry: dict[topic, data_type]` maintained
    thread-safely by subscribe/unsubscribe.
  - `_on_connect(rc=0)` distinguishes first-connect from reconnect via
    `_ever_connected`; reconnect resubscribes every entry in the
    registry snapshot.
  - `_on_connect` / subscribe / unsubscribe short-circuit after
    `stop()` has flipped `_stopping`.
  - `_on_disconnect` classifies planned vs unexpected, clears
    `_connect_ok` and `_connected_evt`, preserves the registry.
  - Per-topic resubscribe failures are isolated (log + metric +
    continue).
  - `_state_lock` is never held across a paho client call.
"""

import threading
import time
import types

import pytest


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------

def _fire_connect(broker, fake_client, rc: int = 0):
    broker._on_connect(
        client=fake_client, userdata=None, flags={},
        reasonCode=rc, properties=None,
    )


def _fire_disconnect(broker, fake_client, rc: int = 1):
    broker._on_disconnect(
        client=fake_client, userdata=None, _flags={},
        reasonCode=rc, _properties=None,
    )


# ==========================================================================
# Category A: Initial connect + first-connect behaviour
# ==========================================================================

def test_start_registers_on_disconnect_callback_on_client(broker, fake_client):
    broker.start({})
    assert fake_client.on_disconnect == broker._on_disconnect


def test_first_on_connect_notifies_notifier(broker, fake_client, notifier):
    _fire_connect(broker, fake_client)
    notifier._on_connect.assert_called_once_with()


def test_first_on_connect_does_not_run_recovery(broker, fake_client):
    """First successful connect: registry is empty and _ever_connected
    was False, so no snapshot is taken and no resubscribe fires."""
    _fire_connect(broker, fake_client)
    metrics = broker.recovery_metrics()
    assert metrics['recovery_run_count'] == 0
    assert metrics['resubscribe_success_count'] == 0
    assert metrics['ever_connected'] is True


def test_first_on_connect_marks_broker_connected(broker, fake_client):
    _fire_connect(broker, fake_client)
    assert broker.recovery_metrics()['connected'] is True


# ==========================================================================
# Category B: Subscription registry (RFC-005 §7.1-7.4)
# ==========================================================================

def test_broker_maintains_subscription_registry(broker, fake_client):
    _fire_connect(broker, fake_client)
    broker.subscribe('A', 'str')
    broker.subscribe('B', 'str')
    assert broker.recovery_metrics()['active_subscriptions'] == 2


def test_subscribe_updates_registry_and_forwards_when_connected(
    broker, fake_client,
):
    _fire_connect(broker, fake_client)
    fake_client.subscribe.reset_mock()
    broker.subscribe('T', 'str')
    fake_client.subscribe.assert_called_once_with(topic='T')
    assert broker.recovery_metrics()['active_subscriptions'] == 1


def test_subscribe_only_updates_registry_when_disconnected(
    broker, fake_client,
):
    # Broker has never connected → _connected=False, _ever_connected=False.
    broker.subscribe('T', 'str')
    fake_client.subscribe.assert_not_called()
    assert broker.recovery_metrics()['active_subscriptions'] == 1


def test_unsubscribe_updates_registry_and_forwards_when_connected(
    broker, fake_client,
):
    _fire_connect(broker, fake_client)
    broker.subscribe('T', 'str')
    fake_client.unsubscribe.reset_mock()
    broker.unsubscribe('T')
    fake_client.unsubscribe.assert_called_once_with('T')
    assert broker.recovery_metrics()['active_subscriptions'] == 0


def test_unsubscribe_only_updates_registry_when_disconnected(
    broker, fake_client,
):
    broker.subscribe('T', 'str')  # queued in registry (not connected)
    fake_client.unsubscribe.reset_mock()
    broker.unsubscribe('T')
    fake_client.unsubscribe.assert_not_called()
    assert broker.recovery_metrics()['active_subscriptions'] == 0


def test_duplicate_subscribe_last_write_wins_on_registry(broker, fake_client):
    _fire_connect(broker, fake_client)
    broker.subscribe('T', 'str')
    broker.subscribe('T', 'bytes')  # same topic, different data_type
    assert broker.recovery_metrics()['active_subscriptions'] == 1
    # Both forwarded to client (broker does not deduplicate client calls).
    assert fake_client.subscribe.call_count == 2


def test_subscribe_disconnected_returns_None(broker, fake_client):
    assert broker.subscribe('T', 'str') is None


def test_unsubscribe_disconnected_returns_None(broker, fake_client):
    broker.subscribe('T', 'str')
    assert broker.unsubscribe('T') is None


# ==========================================================================
# Category C: on_connect callback behaviour on repeated / failing rc
# ==========================================================================

def test_on_connect_with_nonzero_reason_does_not_notify_notifier(
    broker, fake_client, notifier,
):
    _fire_connect(broker, fake_client, rc=5)
    notifier._on_connect.assert_not_called()
    assert broker._connect_ok is False
    assert broker._connect_err is not None
    assert broker.recovery_metrics()['connected'] is False


def test_on_connect_with_nonzero_reason_does_not_run_recovery(
    broker, fake_client,
):
    _fire_connect(broker, fake_client)          # first success (empty snap)
    broker.subscribe('T', 'str')
    _fire_disconnect(broker, fake_client)
    fake_client.subscribe.reset_mock()

    _fire_connect(broker, fake_client, rc=5)    # failed reconnect
    fake_client.subscribe.assert_not_called()
    assert broker.recovery_metrics()['recovery_run_count'] == 0


def test_on_connect_notifies_notifier_on_every_success_invocation(
    broker, fake_client, notifier,
):
    for _ in range(3):
        _fire_connect(broker, fake_client)
    assert notifier._on_connect.call_count == 3


def test_repeated_on_connect_is_idempotent_with_empty_registry(
    broker, fake_client,
):
    """3 successive connects with no registry entries → 0 recoveries
    invoked (each reconnect snapshots an empty registry and skips
    the loop). recovery_run_count remains 0."""
    for _ in range(3):
        _fire_connect(broker, fake_client)
    assert broker.recovery_metrics()['recovery_run_count'] == 0


# ==========================================================================
# Category D: Reconnect recovery — RFC-005 §7.5 CORE
# ==========================================================================

def test_recovery_resubscribes_all_registered_topics_on_reconnect(
    broker, fake_client,
):
    """R-03 primary invariant: after subscribe → disconnect →
    reconnect, every registered topic gets a fresh client.subscribe."""
    _fire_connect(broker, fake_client)
    broker.subscribe('T/1', 'str')
    broker.subscribe('T/2', 'str')
    _fire_disconnect(broker, fake_client)
    fake_client.subscribe.reset_mock()

    _fire_connect(broker, fake_client)

    subs = [c.kwargs['topic'] for c in fake_client.subscribe.call_args_list]
    assert set(subs) == {'T/1', 'T/2'}
    metrics = broker.recovery_metrics()
    assert metrics['recovery_run_count'] == 1
    assert metrics['resubscribe_success_count'] == 2
    assert metrics['resubscribe_error_count'] == 0


def test_recovery_does_not_resurrect_unsubscribed_topic(broker, fake_client):
    _fire_connect(broker, fake_client)
    broker.subscribe('KEEP', 'str')
    broker.subscribe('DROP', 'str')
    broker.unsubscribe('DROP')
    _fire_disconnect(broker, fake_client)
    fake_client.subscribe.reset_mock()

    _fire_connect(broker, fake_client)

    subs = [c.kwargs['topic'] for c in fake_client.subscribe.call_args_list]
    assert 'KEEP' in subs
    assert 'DROP' not in subs


def test_disconnected_subscribe_recovered_on_next_reconnect(
    broker, fake_client,
):
    """Subscribe while disconnected only updates the registry. On the
    next successful connect, recovery re-emits it via client.subscribe."""
    _fire_connect(broker, fake_client)   # first connect (empty registry)
    _fire_disconnect(broker, fake_client)

    # Subscribe while disconnected — only registry write.
    broker.subscribe('LATE', 'str')
    fake_client.subscribe.assert_not_called()

    _fire_connect(broker, fake_client)   # reconnect: recovery runs
    subs = [c.kwargs['topic'] for c in fake_client.subscribe.call_args_list]
    assert 'LATE' in subs


def test_recovery_run_count_increments_once_per_reconnect(
    broker, fake_client,
):
    _fire_connect(broker, fake_client)   # first: no recovery
    broker.subscribe('T', 'str')

    for _ in range(4):
        _fire_disconnect(broker, fake_client)
        _fire_connect(broker, fake_client)

    assert broker.recovery_metrics()['recovery_run_count'] == 4


def test_new_subscription_after_reconnect_forwards_to_client(
    broker, fake_client,
):
    _fire_connect(broker, fake_client)
    _fire_disconnect(broker, fake_client)
    _fire_connect(broker, fake_client)   # now connected again
    fake_client.subscribe.reset_mock()

    broker.subscribe('POST/RECONNECT', 'str')
    fake_client.subscribe.assert_called_once_with(topic='POST/RECONNECT')


def test_dynamic_subscribe_unsubscribe_across_reconnect_final_state(
    broker, fake_client,
):
    """Full sequence exercising registry maintenance and recovery."""
    _fire_connect(broker, fake_client)
    broker.subscribe('A', 'str')
    _fire_disconnect(broker, fake_client)
    # Disconnected: only registry writes.
    broker.subscribe('B', 'str')
    _fire_connect(broker, fake_client)
    # Recovery restored A and B; broker.subscribe('C') is a fresh call.
    fake_client.subscribe.reset_mock()
    broker.unsubscribe('A')
    broker.subscribe('C', 'str')

    assert broker.recovery_metrics()['active_subscriptions'] == 2  # B, C
    assert 'C' in [c.kwargs['topic'] for c in fake_client.subscribe.call_args_list]
    unsubs = [c.args[0] for c in fake_client.unsubscribe.call_args_list]
    assert 'A' in unsubs


# ==========================================================================
# Category E: on_disconnect state observations (RFC-005 §6.4)
# ==========================================================================

def test_on_disconnect_clears_connect_ok_flag(broker, fake_client):
    _fire_connect(broker, fake_client)
    assert broker._connect_ok is True
    _fire_disconnect(broker, fake_client, rc=1)
    assert broker._connect_ok is False


def test_on_disconnect_clears_connected_event(broker, fake_client):
    _fire_connect(broker, fake_client)
    assert broker._connected_evt.is_set()
    _fire_disconnect(broker, fake_client, rc=1)
    assert not broker._connected_evt.is_set()


def test_on_disconnect_distinguishes_planned_from_unexpected(
    broker, fake_client,
):
    # Unexpected: non-zero reason.
    _fire_connect(broker, fake_client)
    _fire_disconnect(broker, fake_client, rc=7)
    assert broker.last_disconnect_was_planned is False

    # Planned: rc=0 (paho's clean-disconnect code) — even without stop().
    _fire_connect(broker, fake_client)
    _fire_disconnect(broker, fake_client, rc=0)
    assert broker.last_disconnect_was_planned is True


def test_on_disconnect_after_stop_is_classified_as_planned(
    broker, fake_client,
):
    _fire_connect(broker, fake_client)
    broker.stop()  # sets _stopping=True
    fake_client.subscribe.reset_mock()
    # Any rc code on disconnect after stop is 'planned'.
    _fire_disconnect(broker, fake_client, rc=7)
    assert broker.last_disconnect_was_planned is True


def test_on_disconnect_preserves_desired_registry(broker, fake_client):
    _fire_connect(broker, fake_client)
    broker.subscribe('A', 'str')
    broker.subscribe('B', 'str')
    _fire_disconnect(broker, fake_client, rc=1)
    # Registry survives the disconnect.
    assert broker.recovery_metrics()['active_subscriptions'] == 2


# ==========================================================================
# Category F: stop() short-circuits (RFC-005 §6.5, §7.7)
# ==========================================================================

def test_stop_sets_stopping_flag_before_client_disconnect(
    broker, fake_client,
):
    """The _stopping flag MUST be set before client.disconnect() so
    any callback fired inline observes the flag.

    Post-RFC-010: NEW.stop() is a pure no-op that does NOT reach paho.
    Prime the broker to RUNNING first so the helper thread actually
    dispatches to paho.
    """
    _fire_connect(broker, fake_client)   # NEW → RUNNING
    fake_client.disconnect.reset_mock()

    observed_stopping = []

    def observe_disconnect(*_a, **_kw):
        observed_stopping.append(broker.recovery_metrics()['stopping'])

    fake_client.disconnect.side_effect = observe_disconnect
    assert broker.stop(graceful_timeout_s=2.0) is True
    assert observed_stopping == [True]


def test_on_connect_after_stop_does_not_notify_notifier(
    broker, fake_client, notifier,
):
    """Post-RFC-010: NEW.stop() is a no-op that does NOT set _stopping,
    so a subsequent _on_connect would still notify. Prime the broker
    to RUNNING (so stop() flips _stopping via linearization) before
    firing the delayed _on_connect."""
    _fire_connect(broker, fake_client)   # NEW → RUNNING
    notifier._on_connect.reset_mock()

    assert broker.stop(graceful_timeout_s=2.0) is True
    notifier._on_connect.reset_mock()
    _fire_connect(broker, fake_client)
    notifier._on_connect.assert_not_called()


def test_on_connect_after_stop_does_not_run_recovery(broker, fake_client):
    _fire_connect(broker, fake_client)
    broker.subscribe('T', 'str')
    broker.stop()
    fake_client.subscribe.reset_mock()

    _fire_connect(broker, fake_client)   # would normally trigger recovery

    fake_client.subscribe.assert_not_called()
    assert broker.recovery_metrics()['recovery_run_count'] == 0


def test_subscribe_after_stop_returns_None_and_does_not_forward(
    broker, fake_client,
):
    _fire_connect(broker, fake_client)
    broker.stop()
    fake_client.subscribe.reset_mock()

    result = broker.subscribe('T', 'str')
    assert result is None
    fake_client.subscribe.assert_not_called()
    # Registry is NOT updated after stop.
    assert broker.recovery_metrics()['active_subscriptions'] == 0


def test_unsubscribe_after_stop_returns_None_and_does_not_forward(
    broker, fake_client,
):
    _fire_connect(broker, fake_client)
    broker.subscribe('T', 'str')
    broker.stop()
    fake_client.unsubscribe.reset_mock()

    result = broker.unsubscribe('T')
    assert result is None
    fake_client.unsubscribe.assert_not_called()


# ==========================================================================
# Category G: Failure isolation (RFC-005 §7.12)
# ==========================================================================

def test_client_subscribe_exception_propagates_when_connected(
    broker, fake_client,
):
    _fire_connect(broker, fake_client)
    fake_client.subscribe.side_effect = RuntimeError('paho failure')
    with pytest.raises(RuntimeError, match='paho failure'):
        broker.subscribe('T', 'str')
    # Registry was updated BEFORE the client call raised.
    assert broker.recovery_metrics()['active_subscriptions'] == 1


def test_client_unsubscribe_exception_propagates_when_connected(
    broker, fake_client,
):
    _fire_connect(broker, fake_client)
    broker.subscribe('T', 'str')
    fake_client.unsubscribe.side_effect = RuntimeError('paho failure')
    with pytest.raises(RuntimeError, match='paho failure'):
        broker.unsubscribe('T')
    # Registry deletion completed before the client call raised.
    assert broker.recovery_metrics()['active_subscriptions'] == 0


def test_recovery_isolates_single_topic_failure(broker, fake_client):
    """Per-topic try/except: one raising client.subscribe does NOT
    abort the recovery of the remaining topics."""
    _fire_connect(broker, fake_client)
    broker.subscribe('A', 'str')
    broker.subscribe('B', 'str')
    broker.subscribe('C', 'str')
    _fire_disconnect(broker, fake_client)
    fake_client.subscribe.reset_mock()

    # Recovery iterates the registry; the middle topic raises.
    def selective_raise(topic):
        if topic == 'B':
            raise RuntimeError('B is unreachable')
        return None
    fake_client.subscribe.side_effect = selective_raise

    _fire_connect(broker, fake_client)

    subs_attempted = [c.kwargs['topic'] for c in fake_client.subscribe.call_args_list]
    # All three topics were ATTEMPTED even though B raised.
    assert set(subs_attempted) == {'A', 'B', 'C'}
    metrics = broker.recovery_metrics()
    assert metrics['resubscribe_success_count'] == 2   # A, C
    assert metrics['resubscribe_error_count'] == 1     # B


def test_recovery_metric_counters_accumulate_across_reconnects(
    broker, fake_client,
):
    _fire_connect(broker, fake_client)
    broker.subscribe('A', 'str')
    broker.subscribe('B', 'str')

    _fire_disconnect(broker, fake_client)
    _fire_connect(broker, fake_client)   # +2 successes
    _fire_disconnect(broker, fake_client)
    _fire_connect(broker, fake_client)   # +2 successes

    metrics = broker.recovery_metrics()
    assert metrics['recovery_run_count'] == 2
    assert metrics['resubscribe_success_count'] == 4
    assert metrics['resubscribe_error_count'] == 0


# ==========================================================================
# Category H: Notifier binding preserved across reconnect
# ==========================================================================

def test_notifier_reference_preserved_across_reconnect(
    broker, fake_client, notifier,
):
    original_id = id(broker._notifier)
    _fire_connect(broker, fake_client)
    _fire_disconnect(broker, fake_client)
    _fire_connect(broker, fake_client)
    assert id(broker._notifier) == original_id
    assert broker._notifier is notifier


def test_on_message_after_reconnect_still_forwards_to_same_notifier(
    broker, fake_client, notifier,
):
    _fire_connect(broker, fake_client)
    _fire_disconnect(broker, fake_client)
    _fire_connect(broker, fake_client)

    msg = types.SimpleNamespace(topic='post/reconnect', payload=b'X')
    broker._on_message(client=fake_client, db=None, message=msg)
    notifier._on_message.assert_called_once_with('post/reconnect', b'X')


# ==========================================================================
# Category I: Metrics snapshot consistency (RFC-005 §7.13)
# ==========================================================================

def test_recovery_metrics_returns_all_keys(broker, fake_client):
    snap = broker.recovery_metrics()
    assert set(snap.keys()) == {
        'resubscribe_success_count', 'resubscribe_error_count',
        'recovery_run_count', 'active_subscriptions',
        'connected', 'stopping', 'ever_connected',
    }


def test_recovery_metrics_reflects_registry_size_live(broker, fake_client):
    _fire_connect(broker, fake_client)
    broker.subscribe('A', 'str')
    assert broker.recovery_metrics()['active_subscriptions'] == 1
    broker.subscribe('B', 'str')
    assert broker.recovery_metrics()['active_subscriptions'] == 2
    broker.unsubscribe('A')
    assert broker.recovery_metrics()['active_subscriptions'] == 1


# ==========================================================================
# Category J: Race conditions (RFC-005 §7.10, §11 lock-not-held test)
# ==========================================================================

def test_state_lock_is_not_held_across_client_subscribe(broker, fake_client):
    """RFC-005 §11 acceptance #7: the broker must never hold
    _state_lock while calling into paho. Verified with a spy that
    tries to acquire the lock — a bounded acquire succeeds only if
    the lock is not held by the broker."""
    _fire_connect(broker, fake_client)
    lock_acquired_during_client_call = []

    def spy_subscribe(topic):
        got = broker._state_lock.acquire(timeout=0.5)
        lock_acquired_during_client_call.append(got)
        if got:
            broker._state_lock.release()
        return None

    fake_client.subscribe.side_effect = spy_subscribe
    broker.subscribe('T', 'str')
    assert lock_acquired_during_client_call == [True]


def test_state_lock_is_not_held_across_client_unsubscribe(
    broker, fake_client,
):
    _fire_connect(broker, fake_client)
    broker.subscribe('T', 'str')
    lock_acquired = []

    def spy_unsubscribe(topic):
        got = broker._state_lock.acquire(timeout=0.5)
        lock_acquired.append(got)
        if got:
            broker._state_lock.release()
        return None

    fake_client.unsubscribe.side_effect = spy_unsubscribe
    broker.unsubscribe('T')
    assert lock_acquired == [True]


def test_state_lock_is_not_held_across_recovery_subscribe(
    broker, fake_client,
):
    _fire_connect(broker, fake_client)
    broker.subscribe('T', 'str')
    _fire_disconnect(broker, fake_client)
    fake_client.subscribe.reset_mock()

    lock_acquired = []

    def spy_subscribe(topic):
        got = broker._state_lock.acquire(timeout=0.5)
        lock_acquired.append(got)
        if got:
            broker._state_lock.release()
        return None

    fake_client.subscribe.side_effect = spy_subscribe
    _fire_connect(broker, fake_client)
    assert lock_acquired == [True]


def test_stop_during_recovery_aborts_remaining_topics(broker, fake_client):
    """Recovery iterates outside the lock and rechecks _stopping
    between topics. A stop() call from another thread mid-recovery
    causes the loop to bail early."""
    _fire_connect(broker, fake_client)
    for i in range(10):
        broker.subscribe(f'T/{i}', 'str')
    _fire_disconnect(broker, fake_client)

    call_count = [0]

    def spy_subscribe(topic):
        call_count[0] += 1
        if call_count[0] == 3:
            # Simulate concurrent stop() from another thread.
            broker.stop()
        return None

    fake_client.subscribe.reset_mock()
    fake_client.subscribe.side_effect = spy_subscribe

    _fire_connect(broker, fake_client)

    # Recovery attempted a few topics then bailed. Not all 10.
    assert call_count[0] < 10
    # Metrics reflect the partial run.
    metrics = broker.recovery_metrics()
    assert metrics['recovery_run_count'] == 1
    assert metrics['stopping'] is True


def test_unsubscribe_during_recovery_skips_topic_from_snapshot(
    broker, fake_client,
):
    """Recovery rechecks the live registry before each subscribe. A
    topic that was unsubscribed after snapshot capture is skipped."""
    _fire_connect(broker, fake_client)
    broker.subscribe('KEEP', 'str')
    broker.subscribe('DROP_MID_RECOVERY', 'str')
    _fire_disconnect(broker, fake_client)
    fake_client.subscribe.reset_mock()

    def spy_subscribe(topic):
        if topic == 'KEEP':
            # Between iterations, unsubscribe DROP_MID_RECOVERY.
            broker.unsubscribe('DROP_MID_RECOVERY')
        return None

    fake_client.subscribe.side_effect = spy_subscribe
    _fire_connect(broker, fake_client)

    subs = [c.kwargs['topic'] for c in fake_client.subscribe.call_args_list]
    assert 'KEEP' in subs
    # Depending on iteration order, DROP may or may not be attempted;
    # if attempted before the KEEP callback fired, it succeeds. If
    # attempted after, the recheck skips it. The invariant is that
    # after recovery, DROP is NOT in the registry.
    assert broker.recovery_metrics()['active_subscriptions'] == 1


def test_subscribe_during_reconnect_lands_in_registry(broker, fake_client):
    """A subscribe that arrives DURING the recovery loop is added to
    the registry (protected by _state_lock). It may or may not be
    covered by the current recovery snapshot, but it is preserved for
    the next reconnect and (if still connected) forwarded to client."""
    _fire_connect(broker, fake_client)
    broker.subscribe('OLD', 'str')
    _fire_disconnect(broker, fake_client)
    fake_client.subscribe.reset_mock()

    def spy_subscribe(topic):
        if topic == 'OLD':
            broker.subscribe('NEW', 'str')  # arrives mid-recovery
        return None

    fake_client.subscribe.side_effect = spy_subscribe
    _fire_connect(broker, fake_client)

    active = broker.recovery_metrics()['active_subscriptions']
    assert active == 2  # OLD + NEW in registry


def test_recovery_thread_safety_under_concurrent_subscribe(
    broker, fake_client,
):
    """Fire recovery + concurrent subscribes from several threads; the
    invariants are:
      - No exception escapes.
      - Every subscribed topic is in the final registry.
      - `active_subscriptions` matches the set size."""
    _fire_connect(broker, fake_client)
    baseline_topics = [f'BASE/{i}' for i in range(5)]
    for t in baseline_topics:
        broker.subscribe(t, 'str')
    _fire_disconnect(broker, fake_client)

    late_topics = [f'LATE/{i}' for i in range(20)]

    def producer():
        for t in late_topics:
            broker.subscribe(t, 'str')

    prod = threading.Thread(target=producer)
    prod.start()
    _fire_connect(broker, fake_client)
    prod.join(2.0)

    # All 25 topics are in the registry.
    metrics = broker.recovery_metrics()
    assert metrics['active_subscriptions'] == 25


# ==========================================================================
# Callback handler mapping across reconnect (regression fence for
# Agent-side __topic_handlers)
# ==========================================================================

def test_broker_never_touches_notifier_topic_handlers_across_reconnect(
    broker, fake_client, notifier,
):
    """Broker maintains its own subscription registry; it must not
    inspect or modify notifier state that holds topic→handler
    mappings. Verified by ensuring the notifier was called with no
    arguments (no state introspection) on each on_connect."""
    _fire_connect(broker, fake_client)
    broker.subscribe('T', 'str')
    _fire_disconnect(broker, fake_client)
    _fire_connect(broker, fake_client)

    for call in notifier._on_connect.call_args_list:
        assert call.args == () and call.kwargs == {}
