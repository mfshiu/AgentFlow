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

xfail_77 = pytest.mark.xfail(strict=True, reason='issue #77: Python versions disagree')

REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]


def _setup_keywords():
    tree = ast.parse((REPO_ROOT / 'setup.py').read_text(encoding='utf-8'))
    [call] = [node for node in ast.walk(tree)
              if isinstance(node, ast.Call) and getattr(node.func, 'id', None) == 'setup']
    return {kw.arg: ast.literal_eval(kw.value) for kw in call.keywords
            if kw.arg in ('python_requires', 'classifiers')}


def _ci_matrix_versions():
    ci = (REPO_ROOT / '.github' / 'workflows' / 'ci.yml').read_text(encoding='utf-8')
    [matrix] = re.findall(r'^\s*python-version:\s*\[([^\]]*)\]', ci, re.MULTILINE)
    return sorted(re.findall(r'"(\d+\.\d+)"', matrix), key=lambda v: tuple(map(int, v.split('.'))))


def _smoke_test_version():
    ci = (REPO_ROOT / '.github' / 'workflows' / 'ci.yml').read_text(encoding='utf-8')
    package_job = ci[ci.index('\n  package:'):]
    return re.search(r'python-version:\s*"(\d+\.\d+)"', package_job).group(1)


def test_the_python_floor_is_the_lowest_ci_version():
    floor = _ci_matrix_versions()[0]
    assert _setup_keywords()['python_requires'] == f'>={floor}'
    assert _smoke_test_version() == floor


@xfail_77
def test_the_classifiers_name_the_ci_tested_versions():
    classifiers = _setup_keywords().get('classifiers', [])
    versions = sorted(c.rsplit(' :: ', 1)[1] for c in classifiers
                      if re.fullmatch(r'Programming Language :: Python :: \d+\.\d+', c))
    assert versions == _ci_matrix_versions()
    assert 'Programming Language :: Python :: 3 :: Only' in classifiers
