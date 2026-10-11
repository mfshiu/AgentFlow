"""The framework must import on hosts without tkinter (issue #47, R-16).

Minimal Docker images and headless servers often ship Python without
Tk. agent.py used to do `from tkinter import N` (never used), so on such
hosts `import agentflow.core.agent` failed with ModuleNotFoundError.

The import runs in a fresh interpreter with a meta-path finder that
blocks tkinter, so the module cache of this test session is untouched.
"""

import os
import pathlib
import subprocess
import sys
import textwrap

import pytest

_SRC = pathlib.Path(__file__).resolve().parents[2] / 'src'

_PROBE = textwrap.dedent("""
    import importlib.abc, sys

    class BlockTk(importlib.abc.MetaPathFinder):
        def find_spec(self, name, path, target=None):
            if name in ('tkinter', '_tkinter') or name.startswith('tkinter.'):
                raise ModuleNotFoundError(f"No module named {name!r}")
            return None

    sys.meta_path.insert(0, BlockTk())

    from agentflow.core.agent import Agent
    from agentflow.core.agent_worker import ProcessWorker, ThreadWorker
    from agentflow.broker.empty_broker import EmptyBroker
    from agentflow.broker.mqtt_broker import MqttBroker
    assert 'tkinter' not in sys.modules
    print('import without tkinter: OK')
""")


@pytest.mark.xfail(strict=True, reason='#47: agent.py still imports tkinter')
def test_framework_imports_without_tkinter():
    env = dict(os.environ)
    env['PYTHONPATH'] = os.pathsep.join(
        p for p in (str(_SRC), env.get('PYTHONPATH', '')) if p)
    result = subprocess.run(
        [sys.executable, '-c', _PROBE],
        capture_output=True, text=True, timeout=60, env=env,
    )
    assert result.returncode == 0, result.stderr
    assert 'import without tkinter: OK' in result.stdout
