"""The supported Python versions agree everywhere (issue #77, W-77).

setup.py required Python >= 3.11, yet nothing verified that floor, no
classifier said which versions are supported, and old commit messages
and a test comment still spoke of a Python 3.9 baseline. The versions
CI tests are the source of truth.

Contracts verified:
  - The python_requires floor is the lowest version in the CI test
    matrix, and the package smoke test installs on that version.
  - The classifiers name exactly the CI-tested versions, and Python 3
    only.
"""

import ast
import pathlib
import re

import pytest
from packaging.specifiers import SpecifierSet

REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]
VERSION = r'''["']?(\d+\.\d+)["']?'''


def _key(version):
    return tuple(map(int, version.split('.')))


def _setup_keywords():
    tree = ast.parse((REPO_ROOT / 'setup.py').read_text(encoding='utf-8'))
    calls = [node for node in ast.walk(tree) if isinstance(node, ast.Call)
             and (getattr(node.func, 'id', None) == 'setup'
                  or getattr(node.func, 'attr', None) == 'setup')]
    assert len(calls) == 1, 'setup.py should make exactly one setup() call'
    keywords = {kw.arg: kw.value for kw in calls[0].keywords}
    try:
        return {name: ast.literal_eval(keywords[name])
                for name in ('python_requires', 'classifiers') if name in keywords}
    except ValueError:
        pytest.fail('setup.py: python_requires and classifiers should be literals')


def _ci_job(name):
    """The text of one top-level job in the CI workflow."""
    ci = (REPO_ROOT / '.github' / 'workflows' / 'ci.yml').read_text(encoding='utf-8')
    ci = ci.replace('\r\n', '\n')
    match = re.search(rf'^  {name}:\n(.*?)(?=^  \S|\Z)', ci, re.MULTILINE | re.DOTALL)
    assert match, f'ci.yml has no {name!r} job'
    return match.group(1)


def _ci_matrix_versions():
    """The Python versions the `test` job's matrix runs."""
    job = _ci_job('test')
    assert 'include:' not in job, 'read the matrix include entries too'
    lists = re.findall(r'^\s*python-version:\s*\[([^\]]*)\]', job, re.MULTILINE)
    assert len(lists) == 1, "expected one python-version list in the test job's matrix"
    versions = re.findall(VERSION, lists[0])
    assert versions, 'the test matrix lists no Python version'
    return sorted(versions, key=_key)


def _smoke_test_version():
    """The Python version of the `package` job (build and install)."""
    versions = re.findall(rf'python-version:\s*{VERSION}\s*$', _ci_job('package'), re.MULTILINE)
    assert len(versions) == 1, 'the package job should set one literal python-version'
    return versions[0]


def test_the_python_floor_is_the_lowest_ci_version():
    floor = _ci_matrix_versions()[0]
    requires = SpecifierSet(_setup_keywords()['python_requires'])
    major, minor = _key(floor)
    assert f'{floor}.0' in requires, f'python_requires {requires} excludes {floor}'
    assert f'{major}.{minor - 1}.99' not in requires, (
        f'python_requires {requires} admits versions below the CI floor {floor}')
    assert _smoke_test_version() == floor, 'the package job should test the floor'


def test_the_classifiers_name_the_ci_tested_versions():
    classifiers = _setup_keywords().get('classifiers', [])
    versions = {c.rsplit(' :: ', 1)[1] for c in classifiers
                if re.fullmatch(r'Programming Language :: Python :: \d+\.\d+', c)}
    assert versions == set(_ci_matrix_versions())
    assert 'Programming Language :: Python :: 3 :: Only' in classifiers
