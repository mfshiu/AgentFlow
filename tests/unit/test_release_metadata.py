"""License, version and changelog (issue #50, W-58).

LICENSE.md and CHANGES.md were empty, so the code was all rights
reserved although the paper calls it open source, and the wheel
shipped an empty license file. The version (2025.8.22.1933) had not
moved since January although RFC-001 to RFC-015 changed public
behaviour, and nothing said what changed.

Contracts verified:
  - LICENSE.md holds the MIT license and names its holder; setup.py
    declares the license as `MIT`.
  - CHANGES.md has a section for the version setup.py declares.
"""

import ast
import pathlib
import re

import pytest

xfail_50 = pytest.mark.xfail(strict=True, reason='issue #50: license, version, changelog')

REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]


def _setup_keyword(name):
    tree = ast.parse((REPO_ROOT / 'setup.py').read_text(encoding='utf-8'))
    [call] = [node for node in ast.walk(tree) if isinstance(node, ast.Call)
              and getattr(node.func, 'id', getattr(node.func, 'attr', None)) == 'setup']
    values = [ast.literal_eval(kw.value) for kw in call.keywords if kw.arg == name]
    return values[0] if values else None


@xfail_50
def test_the_license_is_mit_and_names_its_holder():
    text = (REPO_ROOT / 'LICENSE.md').read_text(encoding='utf-8')
    assert text.lstrip().startswith('MIT License')
    assert re.search(r'Copyright \(c\) \d{4}(-\d{4})? Ming Fang Shiu', text)
    assert 'Permission is hereby granted, free of charge' in text


@xfail_50
def test_setup_declares_the_mit_license():
    assert _setup_keyword('license') == 'MIT'


@xfail_50
def test_the_changelog_describes_the_declared_version():
    version = _setup_keyword('version')
    changes = (REPO_ROOT / 'CHANGES.md').read_text(encoding='utf-8')
    assert re.search(rf'^## \[?{re.escape(version)}\]?', changes, re.MULTILINE), (
        f'CHANGES.md has no section for version {version}')
