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

import os
import pathlib
import runpy
import sys

REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]
TESTS_DIR = REPO_ROOT / 'tests'


def test_test_modules_load_under_the_tests_package_only():
    assert __name__ == 'tests.unit.test_import_identity'
    misnamed = []
    for name, module in list(sys.modules.items()):
        path = getattr(module, '__file__', None)
        if path and pathlib.Path(path).resolve().is_relative_to(TESTS_DIR):
            if name != 'tests' and not name.startswith('tests.'):
                misnamed.append(name)
    assert misnamed == []


def test_the_repositorys_parent_directory_is_not_on_sys_path():
    # The working directory is left out: `python -m pytest` run from the
    # parent puts it there, which is not what this guards against.
    cwd = pathlib.Path.cwd().resolve()
    entries = [pathlib.Path(p).resolve() for p in sys.path if p]
    assert REPO_ROOT.parent not in [p for p in entries if p != cwd]


def test_running_a_test_module_leaves_pythonpath_alone(monkeypatch):
    monkeypatch.setenv('PYTHONPATH', 'src')
    # Executed, not imported: no second copy of the module stays loaded.
    module = TESTS_DIR / 'unit' / 'core' / 'test_process_worker_lifecycle.py'
    runpy.run_path(str(module), run_name='_issue76_probe')
    assert os.environ['PYTHONPATH'] == 'src'
