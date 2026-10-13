"""Deprecated aliases for APIs removed since 2025.5.24 (issue #70, W-61).

FlowDepot pins mas_agentflow==2025.5.24. Since then
`agentflow.get_logger()` and `agentflow.ensure_size()` disappeared
(src/agentflow/__init__.py was emptied), and the `on_begining` hook was
renamed `on_activating`, so a subclass overriding it is silently never
called. None of this warned.

Contracts verified:
  - agentflow.get_logger() warns (DeprecationWarning, at the caller)
    and returns the logger AgentFlow's modules log to, even when
    LOGGER_NAME changed after they were imported.
  - agentflow.ensure_size() warns at the caller and truncates as
    before; falsy input is returned unchanged, as in 2025.5.24.
  - A subclass that overrides on_begining has it called once at
    activation, after on_activating and before on_activate, with a
    DeprecationWarning pointing at the override and a WARNING log, once
    per class and process. In a spawned child (module `__mp_main__`)
    the default filter for `__main__` still shows the warning.
  - A class that defines on_activating itself next to on_begining
    (written for both versions) has only on_activating called.
  - Under an `error` filter every agent of such a class fails alike.
  - An unhashable class (metaclass with __eq__) still activates.
  - A subclass that does not override on_begining gets no warning.
"""

import logging
import os
import threading
import time
import warnings

import pytest

import agentflow
import agentflow.core.agent as agent_module
from agentflow.core.agent import Agent
from agentflow.core.agent_worker import WorkerState

xfail_70r = pytest.mark.xfail(strict=True, reason='issue #70 review')


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
        _stop(agent)


def _stop(agent):
    agent.terminate()
    _wait_until(lambda: not agent.is_active(), 5.0)


class _Recording(Agent):
    def __init__(self, name):
        super().__init__(name, _config())
        self.calls = []
        self.activated = threading.Event()

    def on_activating(self):
        self.calls.append('on_activating')

    def on_activate(self):
        self.calls.append('on_activate')
        self.activated.set()


class _Legacy(_Recording):
    """A 2025.5.24 subclass: it knows on_begining only."""

    def on_begining(self):
        self.calls.append('on_begining')


class _Current(Agent):
    def __init__(self, name):
        super().__init__(name, _config())
        self.activated = threading.Event()

    def on_activate(self):
        self.activated.set()


def _on_begining_warnings(caught):
    return [w for w in caught if issubclass(w.category, DeprecationWarning)
            and 'on_begining' in str(w.message)]


@xfail_70r
def test_get_logger_warns_and_returns_the_framework_logger(monkeypatch):
    monkeypatch.setenv('LOGGER_NAME', 'renamed_after_import')
    with pytest.warns(DeprecationWarning, match='get_logger') as record:
        log = agentflow.get_logger()
    assert log is agent_module.logger
    assert record[0].filename == __file__


def test_ensure_size_warns_and_truncates_as_before():
    with pytest.warns(DeprecationWarning, match='ensure_size') as record:
        assert agentflow.ensure_size('x' * 400) == 'x' * 298 + '..'
    assert record[0].filename == __file__
    with pytest.warns(DeprecationWarning):
        assert agentflow.ensure_size('short') == 'short'
    with pytest.warns(DeprecationWarning):
        assert agentflow.ensure_size('abcdef', 4) == 'ab..'
    with pytest.warns(DeprecationWarning):
        assert agentflow.ensure_size(None) is None
    with pytest.warns(DeprecationWarning):
        assert agentflow.ensure_size('') == ''


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
    deprecations = _on_begining_warnings(caught)
    assert len(deprecations) == 1, [str(w.message) for w in caught]
    assert deprecations[0].filename == __file__
    assert deprecations[0].lineno == _Legacy.on_begining.__code__.co_firstlineno
    logged = [r for r in caplog.records
              if r.levelno == logging.WARNING and 'on_begining' in r.getMessage()]
    assert len(logged) == 1, [r.getMessage() for r in logged]
    assert 'on_activating' in logged[0].getMessage()


@xfail_70r
def test_the_warning_passes_the_main_filter_in_a_spawned_child():
    class Legacy(_Recording):
        def on_begining(self):
            self.calls.append('on_begining')

    Legacy.__module__ = '__mp_main__'   # how spawn names the main script
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter('ignore')
        warnings.filterwarnings('default', category=DeprecationWarning, module='__main__')
        _run(Legacy('legacy_child'))
    assert _on_begining_warnings(caught)


class _Alias(_Recording):
    def on_activating(self):
        self.calls.append('on_activating')

    on_begining = on_activating


class _Delegating(_Recording):
    def on_activating(self):
        self.calls.append('on_activating')

    def on_begining(self):
        self.on_activating()


@xfail_70r
@pytest.mark.parametrize('cls', [_Alias, _Delegating])
def test_a_class_written_for_both_versions_runs_on_activating_once(cls, caplog):
    caplog.set_level(logging.WARNING)
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter('always')
        agent = cls('both_versions')
        _run(agent)
    assert agent.calls == ['on_activating', 'on_activate']
    assert not _on_begining_warnings(caught)
    assert 'on_begining' not in caplog.text


@xfail_70r
def test_under_an_error_filter_every_agent_of_the_class_fails_alike():
    class Legacy(_Legacy):
        pass

    outcomes = []
    with warnings.catch_warnings():
        warnings.simplefilter('error', DeprecationWarning)
        for name in ('strict_first', 'strict_second'):
            agent = Legacy(name)
            agent.start()
            try:
                assert _wait_until(lambda: not agent.is_active(), 5.0)
                worker = agent._agent_worker
                outcomes.append((worker.state, type(worker.last_exception),
                                 agent.activated.is_set()))
            finally:
                _stop(agent)
    assert outcomes == [(WorkerState.FAILED, DeprecationWarning, False)] * 2


class _HashlessMeta(type(Agent)):
    def __eq__(cls, other):
        return cls is other

    __hash__ = None  # type: ignore[assignment]


@xfail_70r
def test_an_unhashable_class_still_activates():
    class Legacy(_Legacy, metaclass=_HashlessMeta):
        pass

    with warnings.catch_warnings():
        warnings.simplefilter('ignore')
        agent = Legacy('hashless')
        _run(agent)
    assert agent.calls == ['on_activating', 'on_begining', 'on_activate']


def test_an_agent_that_does_not_override_on_begining_gets_no_warning(caplog):
    caplog.set_level(logging.WARNING)
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter('always')
        _run(_Current('current'))
    assert not _on_begining_warnings(caught)
    assert 'on_begining' not in caplog.text
