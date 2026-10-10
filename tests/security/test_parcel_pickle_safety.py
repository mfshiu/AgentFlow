"""R-01 / issue #1: BinaryParcel payloads must never execute code.

BinaryParcel.from_payload used to hand wire bytes straight to
pickle.loads, so any client able to publish to a subscribed topic could
run arbitrary code inside the receiving agent. The decoder now uses a
restricted unpickler: no module global may be resolved except a small
set of safe builtin constructors (plus explicit, per-agent opt-ins).

The "gadget" below is deliberately harmless: its __reduce__ only calls
_record() in this module, which appends to a list. The tests assert
that _record() is never reached.

Contracts verified:
  - Payloads that reference any non-allowlisted global (functions,
    classes, modules) are rejected with ParcelDecodeError, a TypeError
    subclass, before anything is called — directly and via
    Agent._on_message.
  - Plain data (dict / list / tuple / set / frozenset / str / bytes /
    bytearray / int / float / complex / bool / None) still round-trips
    for pickle protocols 3-5, including payloads produced by the
    downstream-pinned AgentFlow release (protocol 4).
  - Agent config `codec.pickle_allowed_globals` opts in extra globals;
    `codec.accept_pickle: False` refuses BinaryParcel payloads entirely.
  - Malformed bodies (non-dict) raise ParcelDecodeError.
"""

import datetime
import pickle
import threading

import pytest

from agentflow.core import parcel as parcel_mod
from agentflow.core.agent import Agent
from agentflow.core.parcel import BinaryParcel, Parcel, TextParcel
from tests.fakes.fake_broker import FakeBroker, FakeWorker

pytestmark = pytest.mark.security


# --------------------------------------------------------------------------
# Harmless gadget + helpers
# --------------------------------------------------------------------------

_GADGET_CALLS = []


def _record(*args):
    _GADGET_CALLS.append(args)


class _Gadget:
    def __reduce__(self):
        return (_record, ('gadget executed',))


@pytest.fixture(autouse=True)
def _clear_gadget_calls():
    _GADGET_CALLS.clear()
    yield
    _GADGET_CALLS.clear()


def _managed(content):
    return {'version': 3, 'content': content, 'topic_return': None, 'error': None}


def _binary_payload(obj, protocol=pickle.HIGHEST_PROTOCOL):
    return BinaryParcel.HEAD + pickle.dumps(obj, protocol=protocol)


def _global_ref_payload(module, name):
    """{'content': <module.name>} as a protocol-2 pickle that only LOADS
    the global (GLOBAL opcode) and never calls it."""
    body = (b'\x80\x02}X\x07\x00\x00\x00content'
            + b'c' + f'{module}\n{name}\n'.encode() + b's.')
    return BinaryParcel.HEAD + body


def _agent(config=None):
    a = Agent(name='pickle_safety', agent_config=config or {})
    b = FakeBroker(notifier=a)
    a._broker = b
    a._agent_worker = FakeWorker()
    return a, b


def _subscribe_recorder(agent, topic='svc/in'):
    got = []
    done = threading.Event()

    def handler(t, pcl):
        got.append(pcl)
        done.set()

    agent.subscribe(topic, topic_handler=handler)
    return got, done


# --------------------------------------------------------------------------
# Rejection of code-executing payloads
# --------------------------------------------------------------------------

def test_gadget_payload_is_rejected_without_executing():
    payload = _binary_payload(_managed(_Gadget()))
    with pytest.raises(TypeError):
        Parcel.from_payload(payload)
    assert _GADGET_CALLS == []


def test_gadget_payload_rejection_uses_parcel_decode_error():
    payload = _binary_payload(_managed(_Gadget()))
    with pytest.raises(parcel_mod.ParcelDecodeError):
        BinaryParcel.from_payload(payload)
    assert _GADGET_CALLS == []


def test_parcel_decode_error_is_a_type_error():
    # Parcel.from_payload has always raised TypeError for invalid
    # payloads; existing `except TypeError` callers keep working.
    assert issubclass(parcel_mod.ParcelDecodeError, TypeError)


@pytest.mark.parametrize('module,name', [
    ('os', 'system'),
    ('subprocess', 'Popen'),
    ('builtins', 'eval'),
    ('builtins', 'exec'),
    ('builtins', '__import__'),
    ('builtins', 'getattr'),
    ('__builtin__', 'eval'),        # protocol <= 2 spelling
    ('_codecs', 'encode'),          # protocol 2 bytes; not allowlisted
])
def test_payload_referencing_unsafe_global_is_rejected(module, name):
    with pytest.raises(parcel_mod.ParcelDecodeError):
        Parcel.from_payload(_global_ref_payload(module, name))


class _MutableConfig:
    """A Python-defined (mutable) class an operator might allowlist."""
    flag = 'original'


def _build_on_global_payload(module, name, attr, value):
    """Protocol-2 pickle: GLOBAL module.name, then BUILD with state
    (None, {attr: value}). Unrestricted, BUILD does
    setattr(<the class itself>, attr, value), process-wide."""
    def s(text):
        b = text.encode()
        return b'X' + len(b).to_bytes(4, 'little') + b
    body = (b'\x80\x02c' + f'{module}\n{name}\n'.encode()
            + b'N}' + s(attr) + s(value) + b's\x86b.')
    return BinaryParcel.HEAD + body


def test_build_on_allowlisted_python_class_cannot_mutate_it():
    allowed = {(__name__, '_MutableConfig')}
    payload = _build_on_global_payload(__name__, '_MutableConfig', 'flag', 'tampered')
    with pytest.raises(parcel_mod.ParcelDecodeError):
        Parcel.from_payload(payload, pickle_allowed_globals=allowed)
    assert _MutableConfig.flag == 'original'


@pytest.mark.parametrize('module,name', [
    (__name__, '_MutableConfig'),   # Python-defined class (mutable)
    (__name__, '_record'),          # Python function (mutable __defaults__ etc.)
    ('os', 'path'),                 # a module object
])
def test_allowlisted_mutable_globals_are_still_refused(module, name):
    with pytest.raises(parcel_mod.ParcelDecodeError):
        Parcel.from_payload(_global_ref_payload(module, name),
                            pickle_allowed_globals={(module, name)})
    assert _GADGET_CALLS == []


def test_bytearray_cannot_be_built_from_an_int():
    # {'content': bytearray(50_000_000)} via GLOBAL builtins.bytearray +
    # REDUCE: a few bytes of payload that would otherwise allocate 50 MB
    # (or 50 GB). Wrapped in a dict so only the guard can reject it.
    payload = (BinaryParcel.HEAD + b'\x80\x02}X\x07\x00\x00\x00content'
               + b'cbuiltins\nbytearray\n(I50000000\ntRs.')
    with pytest.raises(parcel_mod.ParcelDecodeError):
        Parcel.from_payload(payload)
    # Legit bytearray content (rebuilt from bytes) still decodes.
    pcl = Parcel.from_payload(_binary_payload(_managed(bytearray(b'ok')), protocol=4))
    assert pcl.content == bytearray(b'ok')


@pytest.mark.parametrize('protocol', [0, 1, 2])
def test_protocol_0_to_2_builtin_spelling_is_normalised(protocol):
    # Protocols <= 2 spell the module '__builtin__'. No bytes here:
    # protocol <= 2 bytes need _codecs.encode, which is refused.
    content = {'s': {1, 2}, 'fs': frozenset({3}), 'c': 1 + 2j, 'txt': 'hi'}
    payload = _binary_payload(_managed(content), protocol=protocol)
    assert b'__builtin__' in payload
    assert Parcel.from_payload(payload).content == content


def test_builtin_compat_name_mapping_is_still_refused():
    # _compat_pickle maps ('__builtin__', 'reduce') to functools.reduce;
    # normalising the module name must not make it resolvable.
    with pytest.raises(parcel_mod.ParcelDecodeError):
        Parcel.from_payload(_global_ref_payload('__builtin__', 'reduce'))


def test_persistent_id_payload_is_rejected():
    payload = BinaryParcel.HEAD + b'\x80\x02X\x01\x00\x00\x00aQ.'   # BINPERSID
    with pytest.raises(TypeError):
        Parcel.from_payload(payload)


def test_gadget_delivered_to_agent_is_not_executed_and_not_dispatched():
    agent, broker = _agent()
    got, done = _subscribe_recorder(agent)
    try:
        with pytest.raises(TypeError):
            broker.deliver('svc/in', _binary_payload(_managed(_Gadget())))
        assert not done.wait(0.3)
        assert got == []
        assert _GADGET_CALLS == []
    finally:
        agent.terminate()


# --------------------------------------------------------------------------
# Plain data keeps working (compatibility)
# --------------------------------------------------------------------------

PLAIN_CONTENT = {
    'content': b'\x00\x01binary',
    'mime_type': 'image/png',
    'text': '中文 ok',
    'n': 42, 'big': 2 ** 100, 'f': 1.5, 'c': 1 + 2j, 'flag': True, 'none': None,
    'list': [1, 'a', b'b'], 'tuple': (1, 2), 'set': {1, 2}, 'fset': frozenset({3}),
    'ba': bytearray(b'xy'), 'nested': {'deep': [{'k': b'v'}]},
}


@pytest.mark.parametrize('protocol', [3, 4, 5])
def test_plain_data_round_trips_for_protocols_3_to_5(protocol):
    pcl = Parcel.from_payload(_binary_payload(_managed(PLAIN_CONTENT), protocol))
    assert isinstance(pcl, BinaryParcel)
    assert pcl.content == PLAIN_CONTENT


def test_payload_from_current_sender_round_trips():
    pcl = BinaryParcel({'content': b'audio-bytes'}, topic_return='r/1')
    out = Parcel.from_payload(pcl.payload())
    assert out.content == {'content': b'audio-bytes'}
    assert out.topic_return == 'r/1'


def test_agent_still_receives_binary_parcels_by_default():
    agent, broker = _agent()
    got, done = _subscribe_recorder(agent)
    try:
        broker.deliver('svc/in', BinaryParcel({'content': b'img', 'mime_type': 'image/png'}).payload())
        assert done.wait(3)
        assert got[0].content == {'content': b'img', 'mime_type': 'image/png'}
    finally:
        agent.terminate()


def test_protocol_2_bytes_are_rejected_by_default():
    # Documented limitation: protocol <= 2 encodes bytes through
    # _codecs.encode, which is not allowlisted. AgentFlow itself never
    # sends protocol 2 (it uses HIGHEST_PROTOCOL; older releases used
    # the default, protocol 4).
    with pytest.raises(parcel_mod.ParcelDecodeError):
        Parcel.from_payload(_binary_payload(_managed({'content': b'x'}), protocol=2))


# --------------------------------------------------------------------------
# Opt-ins and opt-outs
# --------------------------------------------------------------------------

def test_datetime_is_rejected_unless_allowlisted():
    payload = _binary_payload(_managed({'at': datetime.datetime(2026, 1, 1)}))
    with pytest.raises(parcel_mod.ParcelDecodeError):
        Parcel.from_payload(payload)
    pcl = Parcel.from_payload(
        payload, pickle_allowed_globals={('datetime', 'datetime')})
    assert pcl.content == {'at': datetime.datetime(2026, 1, 1)}


def test_parse_pickle_globals_accepts_dotted_and_colon_forms():
    assert parcel_mod.parse_pickle_globals(
        ['datetime.datetime', 'collections:OrderedDict', 'a.b.c:Outer.Inner']
    ) == frozenset({('datetime', 'datetime'), ('collections', 'OrderedDict'),
                    ('a.b.c', 'Outer.Inner')})


@pytest.mark.parametrize('entry', ['nodot', '', '.name', 'module.', ':x', 'm:', 123, None])
def test_parse_pickle_globals_rejects_malformed_entries(entry):
    with pytest.raises(ValueError):
        parcel_mod.parse_pickle_globals([entry])


def test_agent_codec_config_allowlists_extra_globals():
    agent, broker = _agent({'codec': {'pickle_allowed_globals': ['datetime.datetime']}})
    got, done = _subscribe_recorder(agent)
    try:
        broker.deliver('svc/in', _binary_payload(_managed({'at': datetime.datetime(2026, 1, 1)})))
        assert done.wait(3)
        assert got[0].content == {'at': datetime.datetime(2026, 1, 1)}
    finally:
        agent.terminate()


@pytest.mark.parametrize('codec,error', [
    ({'pickle_allowed_globals': ['nodot']}, ValueError),
    ({'pickle_allowed_globals': 'datetime.datetime'}, TypeError),  # not a list
    ({'accept_pickle': 'false'}, TypeError),    # a string must not fail open
    ({'accept_pickle': 0}, TypeError),
    ({'accept_pickles': False}, ValueError),    # misspelled key
    ('not a dict', TypeError),
])
def test_agent_with_malformed_codec_config_fails_at_construction(codec, error):
    with pytest.raises(error):
        Agent(name='bad_codec', agent_config={'codec': codec})


def test_agent_can_refuse_pickle_payloads_entirely():
    agent, broker = _agent({'codec': {'accept_pickle': False}})
    got, done = _subscribe_recorder(agent)
    try:
        with pytest.raises(parcel_mod.ParcelDecodeError):
            broker.deliver('svc/in', BinaryParcel({'content': b'x'}).payload())
        assert not done.wait(0.3)
        broker.deliver('svc/in', TextParcel({'ok': 1}).payload())
        assert done.wait(3)
        assert got[0].content == {'ok': 1}
    finally:
        agent.terminate()


def test_parcel_from_payload_accept_pickle_false():
    with pytest.raises(parcel_mod.ParcelDecodeError):
        Parcel.from_payload(BinaryParcel({'a': 1}).payload(), accept_pickle=False)
    assert Parcel.from_payload(TextParcel({'a': 1}).payload(),
                               accept_pickle=False).content == {'a': 1}


# --------------------------------------------------------------------------
# Malformed bodies
# --------------------------------------------------------------------------

@pytest.mark.parametrize('payload', [
    BinaryParcel.HEAD + pickle.dumps([1, 2, 3]),
    BinaryParcel.HEAD + pickle.dumps('just a string'),
    BinaryParcel.HEAD + b'not a pickle',
    BinaryParcel.HEAD,
    TextParcel.HEAD + b'[1, 2, 3]',
    TextParcel.HEAD + b'"just a string"',
    TextParcel.HEAD + b'{not json',
])
def test_malformed_body_raises_parcel_decode_error(payload):
    with pytest.raises(parcel_mod.ParcelDecodeError):
        Parcel.from_payload(payload)
