import logging
import os
import warnings


# Issue #70: 2025.5.24 defined these two helpers here. They were removed
# without a deprecation period; these aliases warn and will go in a
# later release.

def get_logger() -> logging.Logger:
    """Deprecated: AgentFlow's modules log to
    `logging.getLogger(os.getenv('LOGGER_NAME'))`, the root logger when
    LOGGER_NAME is unset (2025.5.24 defaulted to 'agentflow'). This
    returns that logger."""
    warnings.warn(
        "agentflow.get_logger() is deprecated; AgentFlow logs to "
        "logging.getLogger(os.getenv('LOGGER_NAME')), use that",
        DeprecationWarning, stacklevel=2)
    return logging.getLogger(os.getenv('LOGGER_NAME'))


def ensure_size(text, max_length: int = 300):
    """Deprecated: use agentflow.core.parcel.ensure_size, which also
    turns None into ''. As in 2025.5.24, falsy input is returned
    unchanged."""
    warnings.warn(
        "agentflow.ensure_size() is deprecated; use agentflow.core.parcel.ensure_size",
        DeprecationWarning, stacklevel=2)
    if not text:
        return text
    from agentflow.core.parcel import ensure_size as _ensure_size
    return _ensure_size(text, max_length)
