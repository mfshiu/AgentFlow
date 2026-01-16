from abc import ABC, abstractmethod
import json
import pickle
from typing import Any, Dict, List, Union

VERSION = 3


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
    def from_payload(payload: bytes) -> "Parcel":
        if BinaryParcel.is_payload(payload):
            return BinaryParcel.from_payload(payload)
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
    def from_payload(payload: bytes) -> "BinaryParcel":
        if not isinstance(payload, (bytes, bytearray)):
            raise TypeError("payload must be bytes for BinaryParcel")
        raw = bytes(payload)[len(BinaryParcel.HEAD):]
        managed = pickle.loads(raw)
        pcl = BinaryParcel()
        pcl._set_managed_data(managed)
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
    def from_payload(payload: bytes) -> "TextParcel":
        if not isinstance(payload, (bytes, bytearray)):
            raise TypeError("payload must be bytes for TextParcel")
        raw = bytes(payload)[len(TextParcel.HEAD):]
        data = json.loads(raw.decode("utf-8", "replace"))
        pcl = TextParcel()
        pcl._set_managed_data(data)
        return pcl

    @staticmethod
    def is_payload(payload: bytes) -> bool:
        return bool(payload) and isinstance(payload, (bytes, bytearray)) and bytes(payload).startswith(TextParcel.HEAD)

    def payload(self) -> bytes:
        data = json.dumps(self._get_managed_data(), ensure_ascii=False).encode("utf-8")
        return TextParcel.HEAD + data
