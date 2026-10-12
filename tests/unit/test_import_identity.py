"""Test modules load under a single name (issue #76, W-76).

An `__init__.py` at the repository root made pytest treat the whole
repository as a package named after its directory (`AgentFlow`): test
modules loaded as `AgentFlow.tests...`, the repository's parent
directory went first on sys.path, and `from tests.fakes import ...`
loaded the same files a second time under `tests...`, splitting any
module-level state in two. And test_process_worker_lifecycle.py
rewrote PYTHONPATH when imported, for the rest of the session.

Contracts verified:
  - Test modules are named `tests....`; nothing is loaded as
    `AgentFlow....`.
  - The repository's parent directory is not on sys.path.
  - Importing a test module leaves PYTHONPATH alone.
"""

import importlib
import os
import pathlib
import sys

import pytest

REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]


def test_test_modules_load_under_the_tests_package_only():
    assert __name__ == 'tests.unit.test_import_identity'
    assert not [name for name in sys.modules if name.split('.')[0] == REPO_ROOT.name]


def test_the_repositorys_parent_directory_is_not_on_sys_path():
    assert REPO_ROOT.parent not in [pathlib.Path(p or '.').resolve() for p in sys.path]


def test_importing_a_test_module_leaves_pythonpath_alone(monkeypatch):
    monkeypatch.setenv('PYTHONPATH', 'src')
    name = 'tests.unit.core.test_process_worker_lifecycle'
    monkeypatch.delitem(sys.modules, name, raising=False)   # restored afterwards
    importlib.import_module(name)
    assert os.environ['PYTHONPATH'] == 'src'
