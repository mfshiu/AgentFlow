"""Characterization tests for Agent._on_message auto-reply behaviour
(Risk R-05 in docs/audit/05-risk-register.md).

Post-RFC-003 (Option D):

  R-fallback-silent: only topics with a specifically registered
    handler in Agent.__topic_handlers may trigger an auto-reply.
    Fall-through to on_message dispatches the handler but never
    publishes a reply.

  R-strip-topic_return: every auto-reply parcel carries
    topic_return=None. If a handler returns a Parcel whose
    topic_return is truthy, the framework reconstructs the parcel
    with topic_return stripped rather than mutating the handler's
    object.

  R-exception-fresh: on handler exception the auto-reply is a fresh
    TextParcel(None) with .error = str(ex); the incoming parcel is
    not mutated and is not reused as the reply.
"""

import threading
import time
from typing import Dict, List

import pytest

from agentflow.core.agent import Agent
from agentflow.core.parcel import BinaryParcel, Parcel, TextParcel

from tests.fakes.fake_broker import FakeBroker, FakeWorker


# --------------------------------------------------------------------------
# Fixtures + helpers
# --------------------------------------------------------------------------

@pytest.fixture
def agent_with_fake_broker():
    a = Agent(name='test_r05', agent_config={})
    b = FakeBroker(notifier=a)
    a._broker = b
    a._agent_worker = FakeWorker()
    return a, b


def _handlers(agent):
    return agent._Agent__topic_handlers


def _wait_until(predicate, timeout: float = 1.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.002)
    return False


def _replies_to(broker, topic):
    return [p for (t, p) in broker.publish_calls if t == topic]


# ==========================================================================
# A. Normal request-response  (unchanged by RFC-003)
# ==========================================================================

def test_scenario_1_no_topic_return_no_auto_reply(agent_with_fake_broker):
    agent, broker = agent_with_fake_broker
    handler_called = threading.Event()

    def h(topic, pcl):
        handler_called.set()

    agent.subscribe('T', topic_handler=h)
    publish_count_before = len(broker.publish_calls)

    broker.deliver('T', TextParcel('hello').payload())
    assert handler_called.wait(1.0)
    time.sleep(0.05)
    assert len(broker.publish_calls) == publish_count_before


def test_scenario_2_handler_returns_None_auto_reply_wraps_None(
    agent_with_fake_broker,
):
    agent, broker = agent_with_fake_broker

    def h(topic, pcl):
        return None

    agent.subscribe('T', topic_handler=h)
    broker.deliver('T', TextParcel('req', topic_return='R').payload())

    ok = _wait_until(lambda: _replies_to(broker, 'R'))
    assert ok
    reply_pcl = Parcel.from_payload(_replies_to(broker, 'R')[0])
    assert reply_pcl.content is None
    assert reply_pcl.topic_return is None


def test_scenario_3_handler_returns_string_auto_reply_wraps_string(
    agent_with_fake_broker,
):
    agent, broker = agent_with_fake_broker

    def h(topic, pcl):
        return 'the-response'

    agent.subscribe('T', topic_handler=h)
    broker.deliver('T', TextParcel('req', topic_return='R').payload())

    ok = _wait_until(lambda: _replies_to(broker, 'R'))
    assert ok
    reply_pcl = Parcel.from_payload(_replies_to(broker, 'R')[0])
    assert reply_pcl.content == 'the-response'
    assert reply_pcl.topic_return is None


def test_scenario_4_handler_returns_Parcel_auto_reply_uses_that_Parcel(
    agent_with_fake_broker,
):
    agent, broker = agent_with_fake_broker

    def h(topic, pcl):
        return TextParcel({'k': 'v'})

    agent.subscribe('T', topic_handler=h)
    broker.deliver('T', TextParcel('req', topic_return='R').payload())

    ok = _wait_until(lambda: _replies_to(broker, 'R'))
    assert ok
    reply_pcl = Parcel.from_payload(_replies_to(broker, 'R')[0])
    assert reply_pcl.content == {'k': 'v'}
    assert reply_pcl.topic_return is None


# ==========================================================================
# RFC-003 R-strip-topic_return
# ==========================================================================

def test_scenario_5_reply_Parcel_with_topic_return_is_stripped_on_wire(
    agent_with_fake_broker,
):
    """Post-RFC-003: handler-returned Parcel with topic_return set has
    its topic_return stripped on the reply wire. Chain-terminating."""
    agent, broker = agent_with_fake_broker

    def h(topic, pcl):
        return TextParcel('reply', topic_return='handler_set_this')

    agent.subscribe('T', topic_handler=h)
    broker.deliver('T', TextParcel('req', topic_return='R').payload())

    ok = _wait_until(lambda: _replies_to(broker, 'R'))
    assert ok
    reply_pcl = Parcel.from_payload(_replies_to(broker, 'R')[0])
    assert reply_pcl.content == 'reply'
    assert reply_pcl.topic_return is None


def test_handler_returned_Parcel_object_is_not_mutated_by_framework(
    agent_with_fake_broker,
):
    """RFC-003 R-strip-topic_return uses reconstruction, not mutation.
    The Parcel object the handler returned must retain its own
    topic_return so caller-held references stay consistent."""
    agent, broker = agent_with_fake_broker
    handler_owned = TextParcel('body', topic_return='handler_set_this')

    def h(topic, pcl):
        return handler_owned

    agent.subscribe('T', topic_handler=h)
    broker.deliver('T', TextParcel('req', topic_return='R').payload())

    ok = _wait_until(lambda: _replies_to(broker, 'R'))
    assert ok
    # Reconstructed reply strips topic_return...
    reply_pcl = Parcel.from_payload(_replies_to(broker, 'R')[0])
    assert reply_pcl.topic_return is None
    # ...but the handler's original object is untouched.
    assert handler_owned.topic_return == 'handler_set_this'
    assert handler_owned.content == 'body'


def test_handler_returned_Parcel_content_survives_strip(agent_with_fake_broker):
    agent, broker = agent_with_fake_broker

    def h(topic, pcl):
        return TextParcel({'preserved': True, 'n': [1, 2, 3]},
                           topic_return='ignored')

    agent.subscribe('T', topic_handler=h)
    broker.deliver('T', TextParcel('req', topic_return='R').payload())

    ok = _wait_until(lambda: _replies_to(broker, 'R'))
    assert ok
    reply_pcl = Parcel.from_payload(_replies_to(broker, 'R')[0])
    assert reply_pcl.content == {'preserved': True, 'n': [1, 2, 3]}
    assert reply_pcl.topic_return is None


def test_handler_returned_Parcel_error_field_survives_strip(
    agent_with_fake_broker,
):
    agent, broker = agent_with_fake_broker

    def h(topic, pcl):
        p = TextParcel('body', topic_return='ignored')
        p.error = 'user-set-error'
        return p

    agent.subscribe('T', topic_handler=h)
    broker.deliver('T', TextParcel('req', topic_return='R').payload())

    ok = _wait_until(lambda: _replies_to(broker, 'R'))
    assert ok
    reply_pcl = Parcel.from_payload(_replies_to(broker, 'R')[0])
    assert reply_pcl.content == 'body'
    assert reply_pcl.topic_return is None
    assert reply_pcl.error == 'user-set-error'


def test_handler_returned_BinaryParcel_stays_BinaryParcel_after_strip(
    agent_with_fake_broker,
):
    agent, broker = agent_with_fake_broker

    def h(topic, pcl):
        return BinaryParcel(b'bytes-content', topic_return='ignored')

    agent.subscribe('T', topic_handler=h)
    broker.deliver('T', TextParcel('req', topic_return='R').payload())

    ok = _wait_until(lambda: _replies_to(broker, 'R'))
    assert ok
    payload = _replies_to(broker, 'R')[0]
    # Wire header confirms subclass preserved through reconstruction.
    assert payload.startswith(BinaryParcel.HEAD)
    reply_pcl = Parcel.from_payload(payload)
    assert isinstance(reply_pcl, BinaryParcel)
    assert reply_pcl.content == b'bytes-content'
    assert reply_pcl.topic_return is None


# ==========================================================================
# RFC-003 R-exception-fresh
# ==========================================================================

def test_exception_path_does_not_mutate_incoming_parcel(agent_with_fake_broker):
    """Post-RFC-003: on handler exception, framework must NOT mutate
    the parcel the handler received. A stored reference must see the
    original content/error/topic_return."""
    agent, broker = agent_with_fake_broker
    stored = []

    def raising_handler(topic, pcl):
        stored.append(pcl)
        raise RuntimeError('boom')

    agent.subscribe('T', topic_handler=raising_handler)
    broker.deliver('T', TextParcel('original-content', topic_return='R').payload())

    ok = _wait_until(
        lambda: bool(stored) and bool(_replies_to(broker, 'R')),
        timeout=1.0,
    )
    assert ok
    assert stored[0].content == 'original-content'
    assert stored[0].error is None
    assert stored[0].topic_return == 'R'


def test_exception_path_publishes_fresh_parcel_with_error_field(
    agent_with_fake_broker,
):
    """Post-RFC-003: the error echo is a NEW parcel carrying
    .error=str(ex), content=None, topic_return=None."""
    agent, broker = agent_with_fake_broker

    def raising_handler(topic, pcl):
        raise RuntimeError('the-diagnostic-message')

    agent.subscribe('T', topic_handler=raising_handler)
    broker.deliver('T', TextParcel('req', topic_return='R').payload())

    ok = _wait_until(lambda: _replies_to(broker, 'R'))
    assert ok
    reply_pcl = Parcel.from_payload(_replies_to(broker, 'R')[0])
    assert reply_pcl.content is None
    assert reply_pcl.topic_return is None
    assert reply_pcl.error is not None
    assert 'the-diagnostic-message' in reply_pcl.error


def test_handler_exception_error_echo_does_not_carry_topic_return(
    agent_with_fake_broker,
):
    """Was: test_handler_exception_error_echo_should_not_carry_topic_return
    (strict xfail). Post-RFC-003 this passes because of R-exception-fresh."""
    agent, broker = agent_with_fake_broker

    def raising(topic, pcl):
        raise RuntimeError('boom')

    agent.subscribe('T', topic_handler=raising)
    broker.deliver('T', TextParcel('req', topic_return='R').payload())

    ok = _wait_until(lambda: _replies_to(broker, 'R'), timeout=1.0)
    assert ok
    reply_pcl = Parcel.from_payload(_replies_to(broker, 'R')[0])
    assert reply_pcl.topic_return is None


# ==========================================================================
# RFC-003 R-fallback-silent
# ==========================================================================

def test_default_on_message_does_not_auto_reply_when_no_specific_handler(
    agent_with_fake_broker,
):
    """Was: test_default_on_message_should_not_auto_reply_when_no_specific_handler
    (strict xfail). Post-RFC-003 this passes because of R-fallback-silent.

    Subscribing a topic without a specific handler does not register it
    in __topic_handlers, so dispatch falls through to on_message. Under
    R-fallback-silent, no auto-reply is emitted even if topic_return
    is set."""
    agent, broker = agent_with_fake_broker
    agent.subscribe('T', topic_handler=None)  # no handler registered
    broker.deliver('T', TextParcel('req', topic_return='R').payload())
    time.sleep(0.05)
    assert not _replies_to(broker, 'R')


def test_scenario_7_duplicate_delivery_no_topic_return_after_publish_sync(
    agent_with_fake_broker,
):
    """Scenario 7 & 8: a duplicate/late delivery without topic_return
    silently falls through and produces no further publish."""
    agent, broker = agent_with_fake_broker
    broker.auto_respond_with('ok')
    result = agent.publish_sync('req', 'q', topic_wait='R', timeout=1.0)
    assert result.content == 'ok'
    assert 'R' not in _handlers(agent)
    broker.stop_auto_responding()

    prev = len(broker.publish_calls)
    broker.deliver('R', TextParcel('dup').payload())  # no topic_return
    time.sleep(0.05)
    assert len(broker.publish_calls) == prev


def test_scenario_7b_duplicate_delivery_with_topic_return_does_not_trigger_auto_reply(
    agent_with_fake_broker,
):
    """Post-RFC-003: after publish_sync cleanup, the return topic has
    no specific handler → R-fallback-silent → the duplicate that
    carries topic_return is silently dropped, not echoed."""
    agent, broker = agent_with_fake_broker
    broker.auto_respond_with('ok')
    agent.publish_sync('req', 'q', topic_wait='R', timeout=1.0)
    broker.stop_auto_responding()
    prev = len(broker.publish_calls)

    dup = TextParcel('dup', topic_return='UNRELATED_REPLY_TOPIC')
    broker.deliver('R', dup.payload())
    time.sleep(0.05)

    assert not _replies_to(broker, 'UNRELATED_REPLY_TOPIC')
    # No new publishes at all (R was already unsubscribed by RFC-001).
    assert len(broker.publish_calls) == prev


def test_publish_sync_late_reply_after_cleanup_is_silently_dropped(
    agent_with_fake_broker,
):
    """RFC-003 R-fallback-silent + RFC-001 cleanup: a late reply
    arriving after publish_sync cleanup dispatches to on_message
    default and produces no auto-reply, even if the late reply itself
    carries a topic_return."""
    agent, broker = agent_with_fake_broker
    with pytest.raises(TimeoutError):
        agent.publish_sync('req', 'q', topic_wait='R', timeout=0.05)
    assert 'R' not in _handlers(agent)
    prev = len(broker.publish_calls)

    broker.deliver('R', TextParcel('late', topic_return='SOMEWHERE').payload())
    time.sleep(0.05)

    assert not _replies_to(broker, 'SOMEWHERE')
    assert len(broker.publish_calls) == prev


# ==========================================================================
# C. Loop termination (previously loop-triggering scenarios)
# ==========================================================================

def test_scenario_9_handler_exception_does_not_create_reply_loop(
    agent_with_fake_broker,
):
    """Was: test_scenario_9_handler_exception_creates_reply_loop_bounded_by_broker.
    Post-RFC-003 the exception echo is a fresh parcel with
    topic_return=None; the self-echoed error message triggers no
    further auto-reply."""
    agent, broker = agent_with_fake_broker
    broker.enable_self_echo(max_dispatches=20)

    def raising_handler(topic, pcl):
        raise RuntimeError('handler always fails')

    agent.subscribe('T', topic_handler=raising_handler)
    agent.publish('T', TextParcel('start', topic_return='T'))

    time.sleep(0.15)  # allow any residual thread to complete
    # Expected: 1 initial publish + 1 error echo = 2, way below the
    # 20-dispatch bound.
    assert len(broker.publish_calls) <= 3, (
        f'loop not broken; publish_calls={len(broker.publish_calls)}'
    )


def test_scenario_4_5_handler_returns_topic_return_parcel_does_not_create_loop(
    agent_with_fake_broker,
):
    """Was: test_scenario_4_5_handler_returns_topic_return_parcel_creates_loop.
    Post-RFC-003 R-strip-topic_return breaks the echo cycle in one hop."""
    agent, broker = agent_with_fake_broker
    broker.enable_self_echo(max_dispatches=20)

    def loopy_handler(topic, pcl):
        return TextParcel('echo', topic_return='T')

    agent.subscribe('T', topic_handler=loopy_handler)
    agent.publish('T', TextParcel('start', topic_return='T'))

    time.sleep(0.15)
    assert len(broker.publish_calls) <= 3, (
        f'loop not broken; publish_calls={len(broker.publish_calls)}'
    )


def test_scenario_6_two_agent_reply_does_not_loop_via_hub_broker():
    """Was: test_scenario_6_two_agent_reply_loop_via_hub_broker.
    Post-RFC-003: each side's auto-reply has topic_return stripped, so
    the receiving agent's dispatch sees topic_return=None and does not
    auto-reply. Loop broken in one hop."""
    hub_publish_calls: List[tuple] = []
    subscriptions: Dict[str, List[Agent]] = {}
    lock = threading.Lock()
    MAX = 30

    def hub_publish(topic, payload):
        with lock:
            if len(hub_publish_calls) >= MAX:
                return
            hub_publish_calls.append((topic, payload))
        for notifier in list(subscriptions.get(topic, [])):
            try:
                notifier._on_message(topic, payload)
            except Exception:
                pass

    def make_adapter(notifier):
        class HubAdapter:
            def start(self, options): pass
            def stop(self): pass
            def publish(self, topic, payload):
                hub_publish(topic, payload)
            def subscribe(self, topic, data_type):
                subscriptions.setdefault(topic, []).append(notifier)
            def unsubscribe(self, topic):
                subs = subscriptions.get(topic, [])
                if notifier in subs:
                    subs.remove(notifier)
        return HubAdapter()

    a1 = Agent(name='A1', agent_config={})
    a1._agent_worker = FakeWorker()
    a1._broker = make_adapter(a1)

    a2 = Agent(name='A2', agent_config={})
    a2._agent_worker = FakeWorker()
    a2._broker = make_adapter(a2)

    def a2_handler(topic, pcl):
        return TextParcel('from-a2', topic_return='REQ')

    def a1_handler(topic, pcl):
        return TextParcel('from-a1', topic_return='REPLY_TO_A1')

    a2.subscribe('REQ', topic_handler=a2_handler)
    a1.subscribe('REPLY_TO_A1', topic_handler=a1_handler)

    a1.publish('REQ', TextParcel('kick', topic_return='REPLY_TO_A1'))

    time.sleep(0.15)
    # Expected chain: A1→REQ (1) → A2 auto-reply→REPLY_TO_A1 (2,
    # topic_return stripped) → A1 dispatches a1_handler but sees
    # topic_return=None so no auto-reply. Total: 2 publishes.
    assert len(hub_publish_calls) <= 3, (
        f'two-agent loop not broken; hub_publish_calls={len(hub_publish_calls)}'
    )


# ==========================================================================
# D. Fall-through with self-echo — now silent
# ==========================================================================

def test_scenario_10_fallback_on_message_does_not_auto_reply_under_self_echo(
    agent_with_fake_broker,
):
    """Was: test_scenario_10_default_on_message_auto_reply_wraps_None_and_terminates.
    Post-RFC-003: fall-through dispatches do not emit any auto-reply
    even under a self-echoing broker."""
    agent, broker = agent_with_fake_broker
    broker.enable_self_echo(max_dispatches=20)
    agent.subscribe('T', topic_handler=None)  # subscribe topic only

    agent.publish('T', TextParcel('one-shot', topic_return='T'))
    time.sleep(0.15)

    # Only the initial publish; no fall-through auto-reply.
    assert len(broker.publish_calls) == 1


# ==========================================================================
# E. publish_sync happy path is not regressed
# ==========================================================================

def test_publish_sync_happy_path_still_works_under_RFC_003(agent_with_fake_broker):
    agent, broker = agent_with_fake_broker
    broker.auto_respond_with('happy-path')
    result = agent.publish_sync('req', 'q', topic_wait='R', timeout=1.0)
    assert result.content == 'happy-path'
    assert 'R' not in _handlers(agent)  # RFC-001 cleanup preserved
