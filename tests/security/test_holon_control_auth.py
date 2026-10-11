"""Holon control messages must come from who they claim to be
(issue #13, W-31).

Any client able to publish to the broker could:
  - stop a child with a `terminate` that names no parent, or a parent
    that never registered it;
  - grow a parent's `_children` without bound with forged
    `register_child` messages, each of which was answered;
  - make a service publish, under its own identity, into a holon
    control topic by naming that topic as a request's `topic_return`;
  - predict publish_sync reply topics, which came from the seedable
    `random` module.

Contracts verified:
  - `terminate` is honoured only from a registered parent. One that
    overtakes its parent's `register_parent` (the dispatcher does not
    order messages) runs once that parent registers; such deferred
    terminates are few and expire. `holon.trust_unregistered_parents`
    restores the old behaviour. With a control secret, an authenticated
    terminate needs no registration.
  - `register_parent` counts only on the child's own channel
    `{agent_id}.to_child.{parent_name}`, where parents send it.
  - A terminating parent first lets running registration hooks finish,
    so those children get `register_parent` before its broker stops.
  - `holon.max_children` caps `_children`: registrations beyond it are
    refused without an answer. Unlimited by default (members never
    expire yet, #6).
  - With `holon.control_secret`, the control subjects (terminate,
    register_child, register_parent, parent_online) carry an
    HMAC-SHA256 bound to the topic and a timestamp, and unsigned,
    tampered, replayed-elsewhere or stale ones are dropped. Application
    subjects are unaffected. Control data is signed as the receiver
    sees it after decoding; data that cannot be signed is not sent.
    The secret never reaches the log.
  - Auto-replies never publish into a holon control topic.
  - Reply topics come from `secrets`.
  - A malformed `holon` config section fails at construction.
"""

import logging
import random
import threading
import time
from unittest import mock

import pytest

from agentflow.core.agent import Agent
from agentflow.core.parcel import BinaryParcel, Parcel, TextParcel
from tests.fakes.fake_broker import FakeBroker, FakeWorker
from tests.fakes.fake_bus import FakeBus

pytestmark = pytest.mark.security

SECRET = 'holon-test-secret-0123456789'
KEY = SECRET.encode()
OTHER_SECRET = 'another-holon-secret-987654321'
SETTLE_S = 0.3

xfail_review = pytest.mark.xfail(strict=True, reason='issue #13: gaps found in review')


def _holon_auth():
    from agentflow.core import holon_auth   # added by the issue #13 fix
    return holon_auth


def _content(subject, parent_id=None, parent_name='root', **extra):
    content = {'subject': subject, 'data': None, **extra}
    if parent_id is not None:
        content['parent_id'] = parent_id
    if parent_name is not None:
        content['parent_name'] = parent_name
    return content


def _msg(subject, parent_id=None, parent_name='root', **extra):
    return TextParcel(_content(subject, parent_id, parent_name, **extra))


def _register_child_msg(child_id):
    return TextParcel({'child_id': child_id, 'child_name': 'x.root',
                       'subject': 'register_child', 'data': None})


def _unicast_topic(child):
    return f'{child.agent_id}.to_child.{child.parent_name}'


def _settle(seconds=SETTLE_S):
    FakeBus.wait_until(lambda: False, timeout=seconds)


# ==========================================================================
# A. terminate only from registered parents
# ==========================================================================

def test_terminate_without_parent_id_is_ignored():
    child = Agent('leaf.root', {})
    with mock.patch.object(child, '_terminate') as term:
        child._handle_parents('to_child.root', TextParcel({'subject': 'terminate'}))
    term.assert_not_called()


def test_terminate_from_an_unregistered_parent_is_ignored():
    child = Agent('leaf.root', {})
    with mock.patch.object(child, '_terminate') as term:
        child._handle_parents('to_child.root', _msg('terminate', 'stranger'))
    term.assert_not_called()
    assert child._parents == {}


def test_terminate_from_a_registered_parent_is_honoured():
    child = Agent('leaf.root', {})
    child._handle_parents(_unicast_topic(child), _msg('register_parent', 'P'))
    with mock.patch.object(child, '_terminate') as term:
        child._handle_parents('to_child.root', _msg('terminate', 'P'))
    term.assert_called_once()


def test_terminate_that_overtook_register_parent_runs_once_registered():
    child = Agent('leaf.root', {})
    seen = []
    child.on_parents_message = lambda topic, parent: seen.append(parent.get('subject'))
    with mock.patch.object(child, '_terminate') as term:
        child._handle_parents('to_child.root', _msg('terminate', 'P'))
        assert term.call_count == 0
        child._handle_parents(_unicast_topic(child), _msg('register_parent', 'P'))
        term.assert_called_once()
        child._handle_parents(_unicast_topic(child), _msg('register_parent', 'P'))
        term.assert_called_once()           # released once, not per registration
    assert seen.count('terminate') == 1


def test_deferred_terminate_expires(monkeypatch):
    import agentflow.core.agent as agent_module
    monkeypatch.setattr(agent_module, '_PENDING_TERMINATE_TTL_S', -1.0)
    child = Agent('leaf.root', {})
    with mock.patch.object(child, '_terminate') as term:
        child._handle_parents('to_child.root', _msg('terminate', 'P'))
        child._handle_parents(_unicast_topic(child), _msg('register_parent', 'P'))
    term.assert_not_called()


def test_deferred_terminates_are_bounded():
    child = Agent('leaf.root', {})
    with mock.patch.object(child, '_terminate'):
        for i in range(100):
            child._handle_parents('to_child.root', _msg('terminate', f'stranger-{i}'))
    assert len(child._pending_terminates) <= 8


def test_trust_unregistered_parents_restores_the_old_behaviour():
    child = Agent('leaf.root', {'holon': {'trust_unregistered_parents': True}})
    with mock.patch.object(child, '_terminate') as term:
        child._handle_parents('to_child.root', _msg('terminate', 'stranger'))
    term.assert_called_once()


def test_register_parent_counts_only_on_our_own_channel():
    """Otherwise anyone could register as our parent on a broadcast
    topic, without knowing any agent id, and then terminate us."""
    child = Agent('leaf.root', {})
    for topic in ('to_child.root', 'to_child.leaf.root', 'someone-else.to_child.root'):
        child._handle_parents(topic, _msg('register_parent', 'intruder'))
    assert child._parents == {}
    with mock.patch.object(child, '_terminate') as term:
        child._handle_parents('to_child.root', _msg('terminate', 'intruder'))
    term.assert_not_called()


class SlowRegistrationParent(Agent):
    def __init__(self, name):
        super().__init__(name=name, agent_config={})
        self._Agent__terminate_event = threading.Event()

    def on_register_child(self, child_id, child_info):
        time.sleep(1.5)

    def terminated(self, timeout=0.0):
        return self._Agent__terminate_event.wait(timeout)


@xfail_review
def test_parent_terminating_during_a_slow_registration_still_stops_that_child(bus):
    """A parent answers register_child only after on_register_child
    returns (#6), and its broker stops about 1 s after _terminate. A
    child whose registration hook was still running held the terminate
    back for a register_parent that never came."""
    parent = SlowRegistrationParent('a')
    bus.attach(parent)
    bus.agents.append(parent)
    bus.connect(parent)
    child = _agent(bus, 'b.a', {})
    assert bus.wait_until(lambda: child.agent_id in parent._children)

    def stop_parent_link():
        if parent.terminated(10):
            bus.remove(parent)          # its broker stops with it
    threading.Thread(target=stop_parent_link, daemon=True).start()
    parent._terminate()

    assert child.terminated(10), 'the child kept running after its parent terminated'


# ==========================================================================
# B. holon.max_children
# ==========================================================================

def test_registrations_beyond_max_children_are_refused_without_reply():
    root = Agent('root', {'holon': {'max_children': 10}})
    registered, forwarded = [], []
    root.on_register_child = lambda child_id, info: registered.append(child_id)
    root.on_children_message = lambda topic, info: forwarded.append(info['child_id'])
    with mock.patch.object(root, '_notify_child') as reply:
        for i in range(1000):
            root._handle_children('to_parent.root', _register_child_msg(f'c{i}'))
    first_ten = [f'c{i}' for i in range(10)]
    assert list(root._children) == first_ten
    assert [c.args[0] for c in reply.call_args_list] == first_ten
    assert registered == first_ten
    assert forwarded == first_ten


def test_known_children_can_reregister_at_the_cap():
    root = Agent('root', {'holon': {'max_children': 2}})
    with mock.patch.object(root, '_notify_child') as reply:
        for child_id in ('c0', 'c1', 'c2', 'c0'):
            root._handle_children('to_parent.root', _register_child_msg(child_id))
    assert set(root._children) == {'c0', 'c1'}
    assert [c.args[0] for c in reply.call_args_list] == ['c0', 'c1', 'c0']


def test_children_are_unlimited_by_default():
    root = Agent('root', {})
    with mock.patch.object(root, '_notify_child'):
        for i in range(200):
            root._handle_children('to_parent.root', _register_child_msg(f'c{i}'))
    assert len(root._children) == 200


# ==========================================================================
# C. holon config validation
# ==========================================================================

@pytest.mark.parametrize('holon, error', [
    ('not a dict', TypeError),
    ({'bogus': 1}, ValueError),
    ({'max_children': 0}, ValueError),
    ({'max_children': -1}, ValueError),
    ({'max_children': True}, TypeError),
    ({'max_children': '10'}, TypeError),
    ({'trust_unregistered_parents': 'yes'}, TypeError),
    ({'control_secret': 'too-short'}, ValueError),
    ({'control_secret': 1234567890123456789}, TypeError),
    ({'control_secret': SECRET, 'control_max_age': 0}, ValueError),
    ({'control_secret': SECRET, 'control_max_age': float('inf')}, ValueError),
    ({'control_secret': SECRET, 'control_max_age': '300'}, TypeError),
    ({'control_secret': SECRET, 'control_max_age': True}, TypeError),
    pytest.param({'control_secret': SECRET, 'control_max_age': 10**400}, ValueError,
                 marks=xfail_review),
])
def test_malformed_holon_config_fails_at_construction(holon, error):
    with pytest.raises(error):
        Agent('a', {'holon': holon})


@pytest.mark.parametrize('holon', [
    None,
    {},
    {'control_secret': SECRET},
    {'control_secret': KEY, 'control_max_age': 30, 'max_children': 100,
     'trust_unregistered_parents': False},
])
def test_valid_holon_config_is_accepted(holon):
    Agent('a', {'holon': holon})


# ==========================================================================
# D. holon.control_secret
# ==========================================================================

def test_signature_binds_content_topic_key_and_time():
    ha = _holon_auth()
    msg = _content('terminate', 'P')
    signed = ha.sign(KEY, 'to_child.root', msg, now=1000.0)
    assert 'auth' not in msg                                # the input is not modified

    def verify(message, topic='to_child.root', key=KEY, now=1000.0):
        return ha.verify(key, topic, message, max_age=300, now=now)

    assert verify(signed) is None
    assert verify({**signed, 'subject': 'parent_online'}) is not None
    assert verify({**signed, 'parent_id': 'Q'}) is not None
    assert verify({**signed, 'target_children': ['x']}) is not None
    assert verify(signed, topic='abc.to_child.root') is not None
    assert verify(signed, key=OTHER_SECRET.encode()) is not None
    assert verify(msg) is not None
    assert verify(signed, now=1000.0 + 301) is not None
    assert verify(signed, now=1000.0 - 301) is not None
    assert verify(signed, now=1000.0 + 299) is None


def test_signature_survives_parcel_encoding():
    ha = _holon_auth()
    signed = ha.sign(KEY, 'to_child.root',
                     _content('terminate', 'P', target_children=['a', 'b']))
    decoded = Parcel.from_payload(TextParcel(signed).payload()).content
    assert ha.verify(KEY, 'to_child.root', decoded, max_age=300) is None


@pytest.mark.parametrize('data', [
    pytest.param({2: 'x', 10: 'y'}, marks=xfail_review),
    pytest.param({1: 'a', 'b': 2}, marks=xfail_review),
    (1, (2, 3)),
    {'k': [1, {3: None}]},
])
def test_signed_control_data_survives_both_parcel_encodings(data):
    ha = _holon_auth()
    signed = ha.sign(KEY, 'to_child.root', _content('terminate', 'P', data=data))
    for parcel in (TextParcel(signed), BinaryParcel(signed)):
        decoded = Parcel.from_payload(parcel.payload()).content
        assert ha.verify(KEY, 'to_child.root', decoded, max_age=300) is None, type(parcel)


@pytest.mark.parametrize('auth', [
    'not a dict',
    {'v': 2, 'ts': 1000000, 'mac': '0' * 64},
    {'v': 1, 'ts': True, 'mac': '0' * 64},
    {'v': 1, 'ts': 1000000.5, 'mac': '0' * 64},
    {'v': 1, 'ts': 1000000, 'mac': 'zz' * 32},
    {'v': 1, 'ts': 1000000, 'mac': 'é' * 64},
    {'v': 1, 'ts': 1000000, 'mac': None},
    pytest.param({'v': 1, 'ts': 10**400, 'mac': '0' * 64}, marks=xfail_review),
    pytest.param({'v': 1, 'ts': -10**400, 'mac': '0' * 64}, marks=xfail_review),
])
def test_malformed_auth_is_rejected_without_raising(auth):
    ha = _holon_auth()
    message = {**_content('terminate', 'P'), 'auth': auth}
    assert ha.verify(KEY, 'to_child.root', message, max_age=300, now=1000.0) is not None


def test_content_that_cannot_be_encoded_canonically_is_rejected():
    ha = _holon_auth()
    deep = []
    for _ in range(100000):
        deep = [deep]
    for data in ({1: 'a', 'b': 2}, float('nan'), deep):
        message = {**_content('terminate', 'P'), 'data': data,
                   'auth': {'v': 1, 'ts': 1000000, 'mac': '0' * 64}}
        assert ha.verify(KEY, 'to_child.root', message, max_age=300, now=1000.0) is not None


def test_with_a_secret_unsigned_terminate_is_ignored_even_from_a_registered_parent():
    ha = _holon_auth()
    child = Agent('leaf.root', {'holon': {'control_secret': SECRET}})
    topic = _unicast_topic(child)
    child._handle_parents(topic, TextParcel(ha.sign(KEY, topic, _content('register_parent', 'P'))))
    assert 'P' in child._parents
    with mock.patch.object(child, '_terminate') as term:
        child._handle_parents('to_child.root', _msg('terminate', 'P'))
        term.assert_not_called()
        forged = ha.sign(OTHER_SECRET.encode(), 'to_child.root', _content('terminate', 'P'))
        child._handle_parents('to_child.root', TextParcel(forged))
        term.assert_not_called()
        signed = ha.sign(KEY, 'to_child.root', _content('terminate', 'P'))
        child._handle_parents('to_child.root', TextParcel(signed))
        term.assert_called_once()


def test_with_a_secret_unsigned_membership_messages_are_ignored():
    config = {'holon': {'control_secret': SECRET}}
    root = Agent('root', config)
    with mock.patch.object(root, '_notify_child') as reply:
        root._handle_children('to_parent.root', _register_child_msg('c0'))
    assert root._children == {}
    reply.assert_not_called()

    child = Agent('leaf.root', config)
    child._handle_parents(_unicast_topic(child), _msg('register_parent', 'P'))
    assert child._parents == {}
    with mock.patch.object(child, 'publish') as publish:
        child._handle_parents('to_child.root', _msg('parent_online', 'P'))
    publish.assert_not_called()


@xfail_review
def test_with_a_secret_an_authenticated_terminate_needs_no_registration():
    """The signature already proves the sender holds the holon's
    secret, so a terminate is not held back for a register_parent that
    may never come."""
    ha = _holon_auth()
    child = Agent('leaf.root', {'holon': {'control_secret': SECRET}})
    signed = ha.sign(KEY, 'to_child.root', _content('terminate', 'P'))
    with mock.patch.object(child, '_terminate') as term:
        child._handle_parents('to_child.root', TextParcel(signed))
    term.assert_called_once()


@xfail_review
def test_with_a_secret_unsignable_control_data_is_dropped_without_raising():
    root = Agent('root', {'holon': {'control_secret': SECRET}})
    root._children['c0'] = {}
    with mock.patch.object(root, 'publish') as publish:
        root._notify_children('terminate', data=float('nan'))
    publish.assert_not_called()


def test_the_control_secret_is_never_logged(caplog):
    caplog.set_level(logging.DEBUG)
    for secret in (SECRET, KEY):
        agent = Agent('leaf.root', {'holon': {'control_secret': secret}})
        with mock.patch.object(agent, '_get_worker'):
            agent.start()
    assert 'self.config' in caplog.text         # the config is logged, ...
    assert SECRET not in caplog.text            # ... but not the secret


def test_with_a_secret_application_messages_are_not_authenticated():
    child = Agent('leaf.root', {'holon': {'control_secret': SECRET}})
    seen = []
    child.on_parents_message = lambda topic, parent: seen.append(parent.get('subject'))
    child._handle_parents('to_child.root', _msg('hello', 'P'))
    assert seen == ['hello']


class HolonAgent(Agent):
    def __init__(self, name, config):
        super().__init__(name=name, agent_config=config)
        self._Agent__terminate_event = threading.Event()

    def terminated(self, timeout=0.0):
        return self._Agent__terminate_event.wait(timeout)


@pytest.fixture
def bus():
    b = FakeBus(max_deliveries=500)
    b.agents = []
    yield b
    for a in b.agents:
        a.terminate()


def _agent(bus, name, config):
    a = HolonAgent(name, config)
    bus.attach(a)
    bus.agents.append(a)
    bus.connect(a)
    return a


_CONTROL_SUBJECTS = {'terminate', 'register_child', 'register_parent', 'parent_online'}


def test_holon_sharing_a_secret_converges_and_terminates(bus):
    config = {'holon': {'control_secret': SECRET}}
    root, mid, leaf = (_agent(bus, n, config) for n in ('a', 'b.a', 'c.b.a'))
    assert bus.wait_until(lambda: set(root._children) == {mid.agent_id}
                          and set(mid._children) == {leaf.agent_id}
                          and root.agent_id in mid._parents
                          and mid.agent_id in leaf._parents)

    root._terminate()

    for a in (root, mid, leaf):
        assert a.terminated(5), f'{a.name} did not stop'
    control = [c for c in (Parcel.from_payload(p).content for _, _, p in bus.delivered)
               if isinstance(c, dict) and c.get('subject') in _CONTROL_SUBJECTS]
    assert control and all('auth' in c for c in control)


def test_agents_with_another_secret_do_not_join_the_holon(bus):
    root = _agent(bus, 'a', {'holon': {'control_secret': SECRET}})
    outsider = _agent(bus, 'b.a', {'holon': {'control_secret': OTHER_SECRET}})
    _settle()
    assert root._children == {}
    assert outsider._parents == {}


# ==========================================================================
# E. auto-replies and reply topics
# ==========================================================================

@pytest.fixture
def echo_service():
    agent = Agent('svc', {})
    broker = FakeBroker(notifier=agent)
    agent._broker = broker
    agent._agent_worker = FakeWorker()
    handled = threading.Event()

    def echo(topic, pcl):
        handled.set()
        return pcl.content

    agent.subscribe('echo', topic_handler=echo)
    return agent, broker, handled


def _request(broker, topic_return):
    broker.deliver('echo', TextParcel(_content('terminate', 'P'), topic_return=topic_return).payload())


@pytest.mark.parametrize('topic_return', [
    'to_child.vp',
    'to_parent.vp',
    '0123456789abcdef0123456789abcdef.to_child.vp',
    '0123456789abcdef0123456789abcdef.to_parent.vp',
])
def test_auto_reply_never_publishes_into_a_holon_control_topic(echo_service, topic_return):
    agent, broker, handled = echo_service
    _request(broker, topic_return)
    assert handled.wait(2)
    _settle()
    assert topic_return not in [t for t, _ in broker.publish_calls]


@pytest.mark.parametrize('topic_return', [
    'reply.abc',
    'abcd-0123456789/to_child.vp',          # publish_sync's reply topic for a request to to_child.vp
    'my.to_child.vp',
])
def test_auto_reply_to_other_topics_is_unchanged(echo_service, topic_return):
    agent, broker, handled = echo_service
    _request(broker, topic_return)
    assert FakeBus.wait_until(lambda: topic_return in [t for t, _ in broker.publish_calls], timeout=2)


def test_reply_topics_do_not_come_from_the_seedable_prng():
    agent = Agent('svc', {})
    generate = agent._Agent__generate_return_topic
    state = random.getstate()
    try:
        random.seed(13)
        first = generate('req')
        random.seed(13)
        second = generate('req')
    finally:
        random.setstate(state)
    assert first != second
