"""Content-safe diagnostics shared by agents and broker backends."""

from collections.abc import Mapping
from logging import Logger
import re


_SECRET_KEYS = (
    "password",
    "passwd",
    "secret",
    "token",
    "apikey",
    "accesskey",
    "privatekey",
    "credential",
    "authorization",
)


def redact_config(value: object) -> object:
    """Return a log-only copy; never change live configuration or call object repr."""
    ancestors: set[int] = set()

    def redact(item: object, depth: int) -> object:
        if depth >= 20:
            return "<max-depth>"
        if isinstance(item, (Mapping, list, tuple)):
            identity = id(item)
            if identity in ancestors:
                return "<recursive>"
            ancestors.add(identity)
            try:
                if isinstance(item, Mapping):
                    result: dict[str, object] = {}
                    for key, child in item.items():
                        name = key if isinstance(key, str) else "<non-string-key>"
                        normalized = re.sub(r"[^a-z0-9]", "", name.lower())
                        result[name] = (
                            "***"
                            if any(part in normalized for part in _SECRET_KEYS)
                            else redact(child, depth + 1)
                        )
                    return result
                children = [redact(child, depth + 1) for child in item]
                return tuple(children) if isinstance(item, tuple) else children
            finally:
                ancestors.remove(identity)
        if item is None or type(item) in (str, int, float, bool):
            return item
        return "<object>"

    return redact(value, 0)


def log_publish(logger: Logger, topic: str, payload: object) -> None:
    """Keep publish diagnostics bounded without formatting application content."""
    length = (
        len(payload)
        if isinstance(payload, (bytes, bytearray, str))
        and type(payload) in (bytes, bytearray, str)
        else None
    )
    logger.debug("topic: %s, payload_length: %s", topic, length)
