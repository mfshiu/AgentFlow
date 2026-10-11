from abc import ABC, abstractmethod
import io
import json
import pickle
import types
from typing import Any, Dict, FrozenSet, Iterable, List, Optional, Tuple, Union

VERSION = 3


class ParcelDecodeError(TypeError):
    """A payload could not be decoded into a Parcel: malformed, or
    refused by the decoding policy (issue #1). Subclass of TypeError,
    which Parcel.from_payload has always raised for invalid payloads."""


# Globals a BinaryParcel payload may reference by default (issue #1):
# only the builtin constructors pickle needs for plain data (bytearray
# below protocol 5, set / frozenset below protocol 4, complex). Any
# other global — functions, classes, modules — is refused, so decoding
# wire bytes can never execute code.
DEFAULT_PICKLE_ALLOWED_GLOBALS: FrozenSet[Tuple[str, str]] = frozenset({
    ('builtins', 'bytearray'),
    ('builtins', 'complex'),
    ('builtins', 'frozenset'),
    ('builtins', 'set'),
})


def parse_pickle_globals(entries: Iterable[Any]) -> FrozenSet[Tuple[str, str]]:
    """Parse 'module.name' or 'module:qualname' strings into
    (module, name) pairs for `pickle_allowed_globals`. Use the colon
    form for nested names (e.g. 'pkg.mod:Outer.Inner'). Raises
    ValueError on a malformed entry. Only immutable types and builtin
    functions are honoured at decode time (e.g. datetime.datetime,
    decimal.Decimal); Python-defined classes are refused."""
    parsed = set()
    for entry in entries or ():
        if not isinstance(entry, str):
            raise ValueError(f"pickle_allowed_globals entry must be a str: {entry!r}")
        if ':' in entry:
            module, _, name = entry.partition(':')
        else:
            module, _, name = entry.rpartition('.')
        if not module or not name:
            raise ValueError(
                f"pickle_allowed_globals entry must look like 'module.name' "
                f"or 'module:qualname': {entry!r}"
            )
        parsed.add((module, name))
    return frozenset(parsed)


_PY_TPFLAGS_IMMUTABLETYPE = 1 << 8     # CPython >= 3.10


def _is_immutable_global(obj: Any) -> bool:
    """True for globals a BUILD opcode cannot modify: immutable (static
    or C) types and builtin functions. Python-defined classes (Enum,
    dataclass, fractions.Fraction, ...), Python functions and modules
    are mutable: BUILD would setattr on them, process-wide."""
    if isinstance(obj, type):
        return bool(obj.__flags__ & _PY_TPFLAGS_IMMUTABLETYPE)
    return isinstance(obj, types.BuiltinFunctionType)


class _BytearrayFromBytes:
    """Stands in for builtins.bytearray during unpickling. Pickle only
    ever rebuilds a bytearray from bytes, whereas bytearray(<int>) would
    let a few payload bytes allocate gigabytes. `__slots__ = ()` keeps
    the instance itself immune to BUILD."""
    __slots__ = ()

    def __call__(self, *args):
        if len(args) != 1 or not isinstance(args[0], bytes):
            raise pickle.UnpicklingError("bytearray may only be rebuilt from bytes")
        return bytearray(args[0])


_BYTEARRAY_FROM_BYTES = _BytearrayFromBytes()


class _RestrictedUnpickler(pickle.Unpickler):
    """Unpickler that resolves only allowlisted, immutable globals
    (issue #1). Every callable pickle can invoke (REDUCE, INST, OBJ,
    NEWOBJ, EXT*) is obtained through find_class, so refusing it here
    closes the code-execution path; refusing mutable globals keeps
    BUILD from rewriting a class or function process-wide. Persistent
    IDs stay unsupported (the base class raises UnpicklingError)."""

    def __init__(self, file, allowed: FrozenSet[Tuple[str, str]]):
        super().__init__(file)
        self._allowed = allowed

    def find_class(self, module, name):
        key = ('builtins' if module == '__builtin__' else module, name)
        if key not in self._allowed:
            raise pickle.UnpicklingError(
                f"global '{module}.{name}' is not allowed in a BinaryParcel "
                f"payload (see codec.pickle_allowed_globals)"
            )
        obj = super().find_class(module, name)
        if not _is_immutable_global(obj):
            raise pickle.UnpicklingError(
                f"global '{module}.{name}' is a mutable {type(obj).__name__}; "
                f"only immutable types and builtin functions can be allowed"
            )
        if obj is bytearray:
            return _BYTEARRAY_FROM_BYTES
        return obj


def ensure_size(text: Any, max_length: int = 300) -> str:
    """
    Ensure a text size is <= max_length. If exceeds, return a truncated version ending with '..'.
    Always coerces input to string safely.
    """
    if text is None:
        return ""
    s = str(text)
    if len(s) <= max_length:
        return s
    return s[: max_length - 2] + ".."


def _require_dict(managed: Any, kind: str) -> Dict[str, Any]:
    if not isinstance(managed, dict):
        raise ParcelDecodeError(
            f"invalid {kind} payload: body must be a dict, "
            f"got {type(managed).__name__}"
        )
    return managed


class Parcel(ABC):
    def __init__(self, content: Any = None, topic_return: Any = None):
        self.version: int = VERSION
        self.content: Any = content
        self.topic_return: Any = topic_return
        self.error: Any = None

    def __str__(self) -> str:
        def _convert_content(content: Any) -> Union[str, Dict[str, Any], List[Any]]:
            """Convert parcel content recursively to a JSON-serializable, readable structure."""
            # bytes -> utf-8 text (replace undecodable), keep length info on failure
            if isinstance(content, (bytes, bytearray)):
                try:
                    return ensure_size(bytes(content).decode("utf-8", "replace"))
                except Exception as e:  # very defensive; decode with 'replace' should not fail
                    return f"len(<binary data>): {len(content)}, error: {e}"

            # dict -> recurse values
            if isinstance(content, dict):
                return {str(k): _convert_content(v) for k, v in content.items()}

            # list/tuple/set -> list of converted
            if isinstance(content, (list, tuple, set)):
                return [_convert_content(v) for v in content]

            # everything else -> string
            try:
                return ensure_size(str(content))
            except Exception:
                return "<unstringifiable>"

        body = {
            "version": self.version,
            "content": _convert_content(self.content),
            "topic_return": self.topic_return,
            "error": self.error,
        }

        # Ensure JSON serializable (final safety net)
        try:
            return json.dumps(body, indent=4, ensure_ascii=False)
        except TypeError:
            def _force(v: Any):
                if isinstance(v, (str, int, float, bool)) or v is None:
                    return v
                if isinstance(v, dict):
                    return {str(k): _force(val) for k, val in v.items()}
                if isinstance(v, list):
                    return [_force(i) for i in v]
                return str(v)

            safe_body = {k: _force(v) for k, v in body.items()}
            return json.dumps(safe_body, indent=4, ensure_ascii=False)

    @staticmethod
    def from_content(content: Any) -> "Parcel":
        is_binary = isinstance(content, (bytes, bytearray))
        if is_binary:
            return BinaryParcel(content)
        return TextParcel(content)

    @staticmethod
    def from_payload(
        payload: bytes,
        *,
        accept_pickle: bool = True,
        pickle_allowed_globals: Optional[Iterable[Tuple[str, str]]] = None,
    ) -> "Parcel":
        """Decode a wire payload. BinaryParcel (pickle) payloads are
        decoded with a restricted unpickler; `pickle_allowed_globals`
        adds (module, name) pairs to DEFAULT_PICKLE_ALLOWED_GLOBALS and
        `accept_pickle=False` refuses them entirely. Raises
        ParcelDecodeError (a TypeError) for refused or malformed
        payloads."""
        if BinaryParcel.is_payload(payload):
            return BinaryParcel.from_payload(
                payload, accept_pickle=accept_pickle,
                pickle_allowed_globals=pickle_allowed_globals)
        if TextParcel.is_payload(payload):
            return TextParcel.from_payload(payload)
        raise TypeError("Not valid Parcel payload.")

    @staticmethod
    def is_payload(payload: bytes) -> bool:
        return BinaryParcel.is_payload(payload) or TextParcel.is_payload(payload)

    def _get_managed_data(self) -> Dict[str, Any]:
        return {
            "version": VERSION,
            "content": self.content,
            "topic_return": self.topic_return,
            "error": self.error,
        }

    def _set_managed_data(self, managed_data: Dict[str, Any]) -> None:
        self.version = managed_data.get("version", VERSION)
        self.content = managed_data.get("content")
        self.topic_return = managed_data.get("topic_return")
        self.error = managed_data.get("error")

    @abstractmethod
    def payload(self) -> bytes:
        """Return the serialized bytes payload."""
        raise NotImplementedError

    # Subscript operations for dict-like content
    def __getitem__(self, key: Any) -> Any:
        return self.get(key)

    def __setitem__(self, key: Any, value: Any) -> None:
        self.set(key, value)

    def get(self, key: Any, default: Any = None) -> Any:
        if isinstance(self.content, dict):
            return self.content.get(key, default)
        raise TypeError("self.content is not a dictionary. Get operation is not allowed.")

    def set(self, key: Any, value: Any) -> None:
        if self.content is None:
            self.content = {}
        if isinstance(self.content, dict):
            self.content[key] = value
        else:
            raise TypeError("self.content is not a dictionary. Set operation is not allowed.")


class BinaryParcel(Parcel):
    HEAD = b"application/pickle|"

    def __init__(self, content: Any = None, topic_return: Any = None):
        super().__init__(content, topic_return)

    @staticmethod
    def from_payload(
        payload: bytes,
        *,
        accept_pickle: bool = True,
        pickle_allowed_globals: Optional[Iterable[Tuple[str, str]]] = None,
    ) -> "BinaryParcel":
        if not isinstance(payload, (bytes, bytearray)):
            raise TypeError("payload must be bytes for BinaryParcel")
        if not accept_pickle:
            raise ParcelDecodeError(
                "BinaryParcel (pickle) payloads are refused "
                "(codec.accept_pickle is False)"
            )
        raw = bytes(payload)[len(BinaryParcel.HEAD):]
        allowed = DEFAULT_PICKLE_ALLOWED_GLOBALS
        if pickle_allowed_globals:
            allowed = allowed | frozenset(pickle_allowed_globals)
        try:
            managed = _RestrictedUnpickler(io.BytesIO(raw), allowed).load()
        except Exception as ex:
            raise ParcelDecodeError(f"invalid BinaryParcel payload: {ex}") from ex
        pcl = BinaryParcel()
        pcl._set_managed_data(_require_dict(managed, "BinaryParcel"))
        return pcl

    @staticmethod
    def is_payload(payload: bytes) -> bool:
        return bool(payload) and isinstance(payload, (bytes, bytearray)) and bytes(payload).startswith(BinaryParcel.HEAD)

    def payload(self) -> bytes:
        return BinaryParcel.HEAD + pickle.dumps(self._get_managed_data(), protocol=pickle.HIGHEST_PROTOCOL)


class TextParcel(Parcel):
    HEAD = b"text/json|"

    def __init__(self, content: Any = None, topic_return: Any = None):
        super().__init__(content, topic_return)

    @staticmethod
    def from_payload(
        payload: bytes,
        *,
        accept_pickle: bool = True,
        pickle_allowed_globals: Optional[Iterable[Tuple[str, str]]] = None,
    ) -> "TextParcel":
        # The keyword options match Parcel.from_payload; JSON payloads
        # never unpickle, so they do not apply here.
        if not isinstance(payload, (bytes, bytearray)):
            raise TypeError("payload must be bytes for TextParcel")
        raw = bytes(payload)[len(TextParcel.HEAD):]
        try:
            data = json.loads(raw.decode("utf-8", "replace"))
        except ValueError as ex:
            raise ParcelDecodeError(f"invalid TextParcel payload: {ex}") from ex
        pcl = TextParcel()
        pcl._set_managed_data(_require_dict(data, "TextParcel"))
        return pcl

    @staticmethod
    def is_payload(payload: bytes) -> bool:
        return bool(payload) and isinstance(payload, (bytes, bytearray)) and bytes(payload).startswith(TextParcel.HEAD)

    def payload(self) -> bytes:
        data = json.dumps(self._get_managed_data(), ensure_ascii=False).encode("utf-8")
        return TextParcel.HEAD + data
