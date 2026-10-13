"""Deprecated aliases for APIs removed since 2025.5.24 (issue #70, W-61).

FlowDepot pins mas_agentflow==2025.5.24. Since then
`agentflow.get_logger()` and `agentflow.ensure_size()` disappeared
(src/agentflow/__init__.py is empty), and the `on_begining` hook was
renamed `on_activating`, so a subclass overriding it is silently never
called. None of this warned.

Contracts verified:
  - agentflow.get_logger() warns (DeprecationWarning) and returns the
    logger AgentFlow's modules log to.
  - agentflow.ensure_size() warns and truncates as before; falsy input
    is returned unchanged, as in 2025.5.24.
  - A subclass that overrides on_begining has it called once at
    activation, after on_activating and before on_activate, with a
    DeprecationWarning and a WARNING log once per class.
  - A subclass that does not override it gets no warning.
"""

import logging
import os
import threading
import time
import warnings

import pytest

import agentflow
from agentflow.core.agent import Agent

xfail_70 = pytest.mark.xfail(strict=True, reason='issue #70: deprecated aliases')


def _config():
    return {
        'CONCURRENCY_TYPE': 'thread',
        'broker': {'broker_name': 'b', 'b': {'broker_type': 'empty'}},
    }


def _wait_until(predicate, timeout_s):
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.02)
    return predicate()


def _run(agent):
    """Start the agent until on_activate has run, then stop it."""
    agent.start()
    try:
        assert agent.activated.wait(5.0), 'on_activate never ran'
    finally:
        agent.terminate()
        _wait_until(lambda: not agent.is_active(), 5.0)


class _Legacy(Agent):
    def __init__(self, name):
        super().__init__(name, _config())
        self.calls = []
        self.activated = threading.Event()

    def on_activating(self):
        self.calls.append('on_activating')

    def on_begining(self):
        self.calls.append('on_begining')

    def on_activate(self):
        self.calls.append('on_activate')
        self.activated.set()


class _Current(Agent):
    def __init__(self, name):
        super().__init__(name, _config())
        self.activated = threading.Event()

    def on_activate(self):
        self.activated.set()


@xfail_70
def test_get_logger_warns_and_returns_the_framework_logger():
    with pytest.warns(DeprecationWarning, match='get_logger'):
        log = agentflow.get_logger()
    assert isinstance(log, logging.Logger)
    assert log is logging.getLogger(os.getenv('LOGGER_NAME'))


@xfail_70
def test_ensure_size_warns_and_truncates_as_before():
    with pytest.warns(DeprecationWarning, match='ensure_size'):
        assert agentflow.ensure_size('x' * 400) == 'x' * 298 + '..'
    with pytest.warns(DeprecationWarning):
        assert agentflow.ensure_size('short') == 'short'
    with pytest.warns(DeprecationWarning):
        assert agentflow.ensure_size('abcdef', 4) == 'ab..'
    with pytest.warns(DeprecationWarning):
        assert agentflow.ensure_size(None) is None
    with pytest.warns(DeprecationWarning):
        assert agentflow.ensure_size('') == ''


@xfail_70
def test_an_overridden_on_begining_runs_once_between_on_activating_and_on_activate(caplog):
    caplog.set_level(logging.WARNING)

    class Legacy(_Legacy):
        pass

    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter('always')
        first = Legacy('legacy_first')
        _run(first)
        second = Legacy('legacy_second')
        _run(second)

    assert first.calls == ['on_activating', 'on_begining', 'on_activate']
    assert second.calls == ['on_activating', 'on_begining', 'on_activate']
    deprecations = [w for w in caught if issubclass(w.category, DeprecationWarning)
                    and 'on_begining' in str(w.message)]
    assert deprecations, [str(w.message) for w in caught]
    logged = [r for r in caplog.records
              if r.levelno == logging.WARNING and 'on_begining' in r.getMessage()]
    assert len(logged) == 1, [r.getMessage() for r in logged]
    assert 'on_activating' in logged[0].getMessage()


def test_an_agent_that_does_not_override_on_begining_gets_no_warning(caplog):
    caplog.set_level(logging.WARNING)
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter('always')
        _run(_Current('current'))
    assert not [w for w in caught if 'on_begining' in str(w.message)]
    assert 'on_begining' not in caplog.text
