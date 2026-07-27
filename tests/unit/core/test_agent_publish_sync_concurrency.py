"""Characterization tests for concurrent publish_sync with shared
topic_wait — the RFC-001 §10.R.4 hazard that the identity-guard
cleanup could not fully solve.

Findings verified below:

  - Agent.subscribe (agent.py) unconditionally overwrites
    __topic_handlers[topic] on a second call with the same topic.
    Only a WARNING is logged; the second call succeeds silently.
  - When two publish_sync calls share topic_wait, only the second
    caller's handle_response closure is registered. The first
    caller can never wake and always times out.
  - A response delivered to that shared topic is routed to whichever
    handler is currently registered — with no correlation between
    the response and the caller who "generated" it. Responses may
    reach the wrong caller.
  - RFC-001's identity guard in publish_sync's finally block
    prevents the FIRST caller's cleanup from evicting the SECOND
    caller's handler in the observable common ordering (see notes
    on narrow-race caveat in the module footer).
  - After the second caller completes and unsubscribes, any later
    delivery on the same topic falls through to on_message default
    (RFC-003 R-fallback-silent) and is silently dropped.
  - The race is entirely eliminated when callers use distinct
    topic_wait values, or omit topic_wait so the framework
    auto-generates a unique per-call return topic.

Strict xfails at the bottom describe aspirational contracts for a
future RFC — collision detection, isolated correlation keys, or
explicit multi-waiter support.
"""

import threading
import time
from typing import List

import pytest

from agentflow.core.agent import Agent
from agentflow.core.parcel import Parcel, TextParcel

from tests.fakes.fake_broker import FakeBroker, FakeWorker


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------

def _handlers(agent):
    return agent._Agent__topic_handlers


def _make_agent():
    a = Agent(name='test_dupkey', agent_config={
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
    once the call returns."""
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
# A. Overwrite confirmation (deterministic)
# ==========================================================================

def test_second_subscribe_overwrites_first_handler_in_registry():
    """The plainest possible demonstration: no threads, no
    publish_sync — just two sequential subscribe calls to the same
    topic. The second silently overwrites the first."""
    agent, _broker = _make_agent()
    try:
        def h_A(topic, pcl):
            pass

        def h_B(topic, pcl):
            pass

        agent.subscribe('T', topic_handler=h_A)
        assert _handlers(agent)['T'] is h_A

        agent.subscribe('T', topic_handler=h_B)   # silent overwrite
        assert _handlers(agent)['T'] is h_B
    finally:
        agent.terminate()


def test_publish_sync_two_threads_leave_only_second_handler_registered():
    """Two publish_sync callers with the same topic_wait, sequenced
    via broker.subscribe_calls polling. After both threads' subscribe
    steps complete, __topic_handlers holds only B's closure."""
    agent, broker = _make_agent()
    try:
        a_thread, _, _ = _spawn_publish_sync(
            agent, 'T', 'A-body', timeout=0.2,
        )
        assert _wait_until(
            lambda: _count_subscribed(broker, 'T') == 1, timeout=1.0,
        )
        handler_after_A = _handlers(agent).get('T')
        assert handler_after_A is not None

        b_thread, _, _ = _spawn_publish_sync(
            agent, 'T', 'B-body', timeout=0.2,
        )
        assert _wait_until(
            lambda: _count_subscribed(broker, 'T') == 2, timeout=1.0,
        )
        handler_after_B = _handlers(agent).get('T')
        assert handler_after_B is not None

        # Distinct closures: B overwrote A.
        assert handler_after_A is not handler_after_B

        # Both broker subscribes were forwarded even though only one
        # handler is "live" in the registry (broker-side is R-03
        # concern, characterized separately in
        # test_mqtt_broker_reconnect.py; here we simply document the
        # observation).
        assert _count_subscribed(broker, 'T') == 2

        a_thread.join(2.0)
        b_thread.join(2.0)
    finally:
        agent.terminate()


# ==========================================================================
# B. Response routing (whoever is registered wins)
# ==========================================================================

def test_only_second_caller_can_receive_delivered_response():
    """One response is delivered on the shared topic. Only the
    currently-registered handler (B) fires; A never wakes."""
    agent, broker = _make_agent()
    try:
        a_thread, a_result, a_error = _spawn_publish_sync(
            agent, 'T', 'A-body', timeout=0.5,
        )
        assert _wait_until(
            lambda: _count_subscribed(broker, 'T') == 1, timeout=1.0,
        )

        b_thread, b_result, b_error = _spawn_publish_sync(
            agent, 'T', 'B-body', timeout=1.5,
        )
        assert _wait_until(
            lambda: _count_subscribed(broker, 'T') == 2, timeout=1.0,
        )
        assert _wait_until(
            lambda: _count_published(broker, 'req') == 2, timeout=1.0,
        )

        broker.deliver('T', TextParcel('the-only-response').payload())

        b_thread.join(2.0)
        a_thread.join(2.0)

        assert b_result[0] is not None
        assert b_result[0].content == 'the-only-response'
        assert b_error[0] is None
        assert a_result[0] is None
        assert isinstance(a_error[0], TimeoutError)
    finally:
        agent.terminate()


def test_response_semantically_for_first_caller_reaches_second_caller():
    """The framework has no correlation between a delivered response
    and the caller who "generated" it. A response labeled as A's
    reply is consumed by B (currently registered). B has no way to
    detect the semantic mismatch."""
    agent, broker = _make_agent()
    try:
        a_thread, a_result, a_error = _spawn_publish_sync(
            agent, 'T', 'A-request', timeout=0.5,
        )
        assert _wait_until(
            lambda: _count_subscribed(broker, 'T') == 1, timeout=1.0,
        )

        b_thread, b_result, b_error = _spawn_publish_sync(
            agent, 'T', 'B-request', timeout=1.5,
        )
        assert _wait_until(
            lambda: _count_subscribed(broker, 'T') == 2, timeout=1.0,
        )
        assert _wait_until(
            lambda: _count_published(broker, 'req') == 2, timeout=1.0,
        )

        # This payload was semantically A's reply, but goes to whoever
        # is registered — which is B.
        broker.deliver('T', TextParcel('reply-that-was-meant-for-A').payload())

        b_thread.join(2.0)
        a_thread.join(2.0)

        assert b_result[0].content == 'reply-that-was-meant-for-A'
        assert isinstance(a_error[0], TimeoutError)
    finally:
        agent.terminate()


def test_second_delivered_response_dropped_by_is_set_guard():
    """publish_sync's handle_response has an is_set() guard
    (agent.py). After B's data_event is set, a second delivery to
    the same topic still invokes the handler (or its fallback), but
    the is_set() guard drops the payload — A cannot piggy-back."""
    agent, broker = _make_agent()
    try:
        a_thread, a_result, a_error = _spawn_publish_sync(
            agent, 'T', 'A-body', timeout=0.5,
        )
        assert _wait_until(
            lambda: _count_subscribed(broker, 'T') == 1, timeout=1.0,
        )

        b_thread, b_result, b_error = _spawn_publish_sync(
            agent, 'T', 'B-body', timeout=1.5,
        )
        assert _wait_until(
            lambda: _count_subscribed(broker, 'T') == 2, timeout=1.0,
        )
        assert _wait_until(
            lambda: _count_published(broker, 'req') == 2, timeout=1.0,
        )

        broker.deliver('T', TextParcel('response-1').payload())
        b_thread.join(2.0)
        assert b_result[0].content == 'response-1'

        # After B's finally cleaned up, deliver another response.
        # No handler is now registered → RFC-003 R-fallback-silent →
        # on_message default → dropped. A still cannot wake.
        broker.deliver('T', TextParcel('response-2').payload())

        a_thread.join(2.0)
        assert isinstance(a_error[0], TimeoutError)
    finally:
        agent.terminate()


# ==========================================================================
# C. Cleanup race — RFC-001 identity guard verification
# ==========================================================================

def test_first_caller_cleanup_does_not_evict_second_callers_handler():
    """A subscribes, B overwrites. A times out first, enters finally.
    RFC-001 identity guard: A's dict.get('T') returns B's handler,
    not A's own handle_response closure. Identity check fails; A
    skips unsubscribe. B's handler stays registered and B can
    still receive its response."""
    agent, broker = _make_agent()
    try:
        # A: very short timeout so it times out before B.
        a_thread, a_result, a_error = _spawn_publish_sync(
            agent, 'T', 'A-body', timeout=0.05,
        )
        assert _wait_until(
            lambda: _count_subscribed(broker, 'T') == 1, timeout=1.0,
        )

        # B: long timeout.
        b_thread, b_result, b_error = _spawn_publish_sync(
            agent, 'T', 'B-body', timeout=2.0,
        )
        assert _wait_until(
            lambda: _count_subscribed(broker, 'T') == 2, timeout=1.0,
        )

        # Wait for A to time out and its finally to run.
        a_thread.join(2.0)
        assert isinstance(a_error[0], TimeoutError)

        # B's handler must still be registered.
        assert 'T' in _handlers(agent)

        # Deliver — B completes.
        broker.deliver('T', TextParcel('B-response').payload())
        b_thread.join(2.0)
        assert b_result[0].content == 'B-response'
        assert b_error[0] is None
    finally:
        agent.terminate()


def test_second_caller_cleanup_removes_handler_no_double_unsubscribe():
    """When B completes and cleans up, only one broker.unsubscribe
    call is issued for 'T'. A's later finally observes the empty
    registry and skips its own unsubscribe."""
    agent, broker = _make_agent()
    try:
        a_thread, a_result, a_error = _spawn_publish_sync(
            agent, 'T', 'A-body', timeout=0.5,
        )
        assert _wait_until(
            lambda: _count_subscribed(broker, 'T') == 1, timeout=1.0,
        )

        b_thread, b_result, b_error = _spawn_publish_sync(
            agent, 'T', 'B-body', timeout=1.5,
        )
        assert _wait_until(
            lambda: _count_subscribed(broker, 'T') == 2, timeout=1.0,
        )
        assert _wait_until(
            lambda: _count_published(broker, 'req') == 2, timeout=1.0,
        )

        broker.deliver('T', TextParcel('R').payload())
        b_thread.join(2.0)
        a_thread.join(2.0)

        unsubs_for_T = [u for u in broker.unsubscribe_calls if u == 'T']
        assert len(unsubs_for_T) == 1
        assert 'T' not in _handlers(agent)
    finally:
        agent.terminate()


# ==========================================================================
# D. Failure interactions
# ==========================================================================

def test_late_response_after_second_caller_completes_is_silently_dropped():
    """After B has completed and unsubscribed, a further delivery on
    'T' has no specific handler → RFC-003 R-fallback-silent → the
    delivery is dropped. A still cannot wake and times out. No
    auto-reply is generated (verified via publish_calls stability)."""
    agent, broker = _make_agent()
    try:
        a_thread, a_result, a_error = _spawn_publish_sync(
            agent, 'T', 'A-body', timeout=0.5,
        )
        assert _wait_until(
            lambda: _count_subscribed(broker, 'T') == 1, timeout=1.0,
        )

        b_thread, b_result, b_error = _spawn_publish_sync(
            agent, 'T', 'B-body', timeout=1.5,
        )
        assert _wait_until(
            lambda: _count_subscribed(broker, 'T') == 2, timeout=1.0,
        )
        assert _wait_until(
            lambda: _count_published(broker, 'req') == 2, timeout=1.0,
        )

        broker.deliver('T', TextParcel('R1').payload())
        b_thread.join(2.0)

        # Late deliver — no handler, no auto-reply.
        publish_calls_before = len(broker.publish_calls)
        broker.deliver('T', TextParcel('late-for-A').payload())
        time.sleep(0.05)   # let dispatcher drain
        assert len(broker.publish_calls) == publish_calls_before

        a_thread.join(2.0)
        assert isinstance(a_error[0], TimeoutError)
    finally:
        agent.terminate()


def test_agent_terminate_during_concurrent_publish_sync_does_not_hang_waiters():
    """agent.terminate() runs while two publish_sync waiters are
    blocked on event.wait. The waiters' own event.wait(timeout)
    deadlines fire independently of dispatcher state; both raise
    TimeoutError without hanging.

    Note: the dispatcher is created lazily on the first _on_message
    call, so a terminate that happens before any response delivery is
    effectively a no-op on the dispatcher — this test therefore does
    not rely on any dispatcher-stop semantic. Its focus is that
    publish_sync's own timeout is authoritative."""
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

        # No delivery. Both waiters time out on their own deadlines.
        a_thread.join(2.0)
        b_thread.join(2.0)
        assert isinstance(a_error[0], TimeoutError)
        assert isinstance(b_error[0], TimeoutError)
    finally:
        agent.terminate()  # idempotent


# ==========================================================================
# E. Distinct topic_wait — control: no key collision → no problem
# ==========================================================================

def test_distinct_topic_wait_both_callers_receive_own_response():
    """When topic_wait differs, there is no shared registry key,
    so both callers register their own handlers and both complete."""
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
    """When topic_wait is not specified, publish_sync auto-generates
    a unique return topic per call (agent.__generate_return_topic).
    Two concurrent callers cannot collide."""
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

        assert a_result[0] is not None
        assert b_result[0] is not None
        assert a_result[0].content == 'ok'
        assert b_result[0].content == 'ok'

        # At least two distinct return topics were generated.
        return_topics = {t for t, _ in broker.subscribe_calls}
        assert len(return_topics) >= 2
    finally:
        agent.terminate()


# ==========================================================================
# F. Stress: N callers, same topic_wait, at most one completes
# ==========================================================================

@pytest.mark.parametrize('n', [3, 10, 50])
def test_N_callers_same_topic_wait_at_most_one_completes(n):
    agent, broker = _make_agent()
    try:
        threads = []
        results = []
        errors = []
        for i in range(n):
            t, r, e = _spawn_publish_sync(
                agent, 'T', f'body-{i}', timeout=0.5,
            )
            threads.append(t)
            results.append(r)
            errors.append(e)

        assert _wait_until(
            lambda: _count_subscribed(broker, 'T') == n, timeout=3.0,
        )
        assert _wait_until(
            lambda: _count_published(broker, 'req') == n, timeout=3.0,
        )

        # ONE response — only the currently-registered (last-writer)
        # handler receives it.
        broker.deliver('T', TextParcel('the-only-response').payload())

        for t in threads:
            t.join(2.0)

        completed = sum(1 for r in results if r[0] is not None)
        timeouts = sum(
            1 for e in errors if isinstance(e[0], TimeoutError)
        )
        other_errors = sum(
            1 for e in errors
            if e[0] is not None and not isinstance(e[0], TimeoutError)
        )

        assert other_errors == 0, (
            f'unexpected errors (N={n}): '
            f'{[e[0] for e in errors if e[0] is not None and not isinstance(e[0], TimeoutError)]}'
        )
        assert completed == 1, (
            f'expected exactly 1 completion; got {completed} (N={n})'
        )
        assert timeouts == n - 1, (
            f'expected {n-1} timeouts; got {timeouts} (N={n})'
        )
    finally:
        agent.terminate()


# ==========================================================================
# G. Aspirational strict xfails — ideal contracts for a future RFC
# ==========================================================================

@pytest.mark.xfail(
    reason=('R.4 (RFC-001 §10): Agent.subscribe silently overwrites '
            'the handler for an already-subscribed topic. Ideal '
            'behaviour: a second publish_sync whose topic_wait is '
            'already actively awaited should fail fast (e.g. raise '
            'a CollisionError) rather than clobbering the first '
            'waiter and forcing it to time out.'),
    strict=True,
)
def test_second_publish_sync_with_same_topic_wait_should_fail_fast():
    agent, broker = _make_agent()
    try:
        a_thread, _, _ = _spawn_publish_sync(agent, 'T', 'A', timeout=2.0)
        assert _wait_until(
            lambda: _count_subscribed(broker, 'T') == 1, timeout=1.0,
        )

        start = time.monotonic()
        with pytest.raises(Exception):
            agent.publish_sync('req', 'B', topic_wait='T', timeout=1.0)
        elapsed = time.monotonic() - start
        # Ideal: fast rejection, not a full-timeout wait.
        assert elapsed < 0.1, f'elapsed={elapsed:.3f}s should be << 1.0s'

        a_thread.join(3.0)
    finally:
        agent.terminate()


@pytest.mark.xfail(
    reason=('R.4: When two callers share topic_wait the framework has '
            'no correlation key beyond the topic itself, so the two '
            'responses cannot be routed to the two waiters. Ideal '
            'behaviour: each publish_sync gets an isolated correlation '
            'key (e.g. via an internal id or a Parcel metadata field) '
            'so both callers can complete concurrently.'),
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
            lambda: _count_subscribed(broker, 'T') == 2, timeout=1.0,
        )
        assert _wait_until(
            lambda: _count_published(broker, 'req') == 2, timeout=1.0,
        )

        broker.deliver('T', TextParcel('for-A').payload())
        broker.deliver('T', TextParcel('for-B').payload())

        a_thread.join(2.0)
        b_thread.join(2.0)
        # Ideal: both callers received a response.
        assert a_result[0] is not None
        assert b_result[0] is not None
    finally:
        agent.terminate()


@pytest.mark.xfail(
    reason=('R.4: The framework maps one handler per topic. Ideal '
            'behaviour: allow multiple handlers per topic (e.g. a '
            'handler list) so multiple waiters — such as concurrent '
            'publish_sync callers or independent subscribers on the '
            'same topic — can all be notified.'),
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
        # Ideal: BOTH handlers fire.
        assert _wait_until(
            lambda: received_A and received_B, timeout=1.0,
        )
    finally:
        agent.terminate()


# ==========================================================================
# Module footer note (not a test)
# ==========================================================================
#
# Narrow race NOT covered by a dedicated test (documented for the RFC):
#
#   RFC-001's identity guard uses non-atomic check-then-pop:
#
#     if self.__topic_handlers.get(topic) is handle_response:
#         self.unsubscribe(topic)         # → dict.pop + broker.unsubscribe
#
#   Between the .get() read and the .pop() write, another thread can
#   overwrite __topic_handlers[topic]. If A's finally reads A_handler,
#   B then overwrites to B_handler, and A then pops, A silently evicts
#   B's handler. The window is very narrow (a few bytecodes under the
#   GIL) and requires an intermediate context switch; a deterministic
#   reproduction would need monkey-patching Agent internals. A future
#   fix would wrap the check-and-pop under a per-agent handler lock;
#   noted here as scope for a follow-up RFC.
