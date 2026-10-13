import logging
import warnings


# Issue #70: 2025.5.24 defined these two helpers here. They were removed
# without a deprecation period; these aliases warn and will go in a
# later release.

def get_logger() -> logging.Logger:
    """Deprecated: returns the logger AgentFlow's modules log to,
    `logging.getLogger(os.getenv('LOGGER_NAME'))` as read when they were
    imported. With LOGGER_NAME unset that is the root logger (2025.5.24
    defaulted to 'agentflow'), so changing its level or handlers affects
    every library. It has no `verbose()` method: 2025.5.24 patched one
    onto logging.Logger for the whole process, and this does not."""
    warnings.warn(
        "agentflow.get_logger() is deprecated; AgentFlow logs to "
        "logging.getLogger(os.getenv('LOGGER_NAME')), use that",
        DeprecationWarning, stacklevel=2)
    from agentflow.core import agent
    return agent.logger


def ensure_size(text, max_length: int = 300):
    """Deprecated: use agentflow.core.parcel.ensure_size, which also
    turns None into ''. As in 2025.5.24, falsy input is returned
    unchanged; other input is converted with str() first, where
    2025.5.24 returned or rejected non-str input as it was."""
    warnings.warn(
        "agentflow.ensure_size() is deprecated; use agentflow.core.parcel.ensure_size",
        DeprecationWarning, stacklevel=2)
    if not text:
        return text
    from agentflow.core.parcel import ensure_size as _ensure_size
    return _ensure_size(text, max_length)
