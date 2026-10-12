"""Structural checks for design constraints that no test can observe.

Issue #74: tests used to assert on the literal text returned by
`inspect.getsource`, so reformatting, rewording a comment or renaming a
local variable failed them although behaviour was unchanged. Prefer a
behavioural test. Use these helpers only for a constraint that cannot
be observed at runtime (e.g. "core never calls os._exit"). They work on
the AST, so whitespace, comments, docstrings and local variable names
do not matter.
"""

import ast
import inspect
import textwrap
from typing import Any, Iterable, List, Set


def parse_source(obj) -> ast.AST:
    """AST of the source of a module, class or function."""
    return ast.parse(textwrap.dedent(inspect.getsource(obj)))


def daemon_settings(obj) -> List[Any]:
    """Every value `obj` gives a `daemon` flag, in source order: the
    `daemon=` keyword of any call (`Thread(...)`, `Process(...)`) and
    any assignment to an attribute named `daemon`. A literal is
    returned as its value; anything else as its source text, so it
    never equals a bool."""
    found = []
    for node in ast.walk(parse_source(obj)):
        values = []
        if isinstance(node, ast.Call):
            values = [kw.value for kw in node.keywords if kw.arg == 'daemon']
        elif isinstance(node, (ast.Assign, ast.AnnAssign)):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            if node.value is not None and any(
                isinstance(t, ast.Attribute) and t.attr == 'daemon' for t in targets
            ):
                values = [node.value]
        for value in values:
            found.append((node.lineno, node.col_offset, value))
    found.sort(key=lambda item: item[:2])
    return [
        value.value if isinstance(value, ast.Constant) else ast.unparse(value)
        for _, _, value in found
    ]


def identifiers(obj) -> Set[str]:
    """Every identifier the code of `obj` uses: names, attribute names
    and imported names. Strings, comments and docstrings are not
    code, so a word that only appears there is not reported."""
    names = set()
    for node in ast.walk(parse_source(obj)):
        if isinstance(node, ast.Name):
            names.add(node.id)
        elif isinstance(node, ast.Attribute):
            names.add(node.attr)
        elif isinstance(node, (ast.Import, ast.ImportFrom)):
            names.update(alias.name.split('.')[-1] for alias in node.names)
    return names


def string_literals(obj) -> Set[str]:
    """Every string literal in the code of `obj` (e.g. a config key),
    docstrings excluded."""
    tree = parse_source(obj)
    docstrings = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef,
                             ast.FunctionDef, ast.AsyncFunctionDef)):
            first = node.body[0] if node.body else None
            if (isinstance(first, ast.Expr) and isinstance(first.value, ast.Constant)
                    and isinstance(first.value.value, str)):
                docstrings.add(id(first.value))
    return {
        node.value for node in ast.walk(tree)
        if isinstance(node, ast.Constant) and isinstance(node.value, str)
        and id(node) not in docstrings
    }


def imported_modules(obj) -> Set[str]:
    """Dotted names of the modules `obj` imports anywhere in its body,
    including imports local to a function. `from a import b` reports
    both `a` and `a.b`."""
    modules = set()
    for node in ast.walk(parse_source(obj)):
        if isinstance(node, ast.Import):
            modules.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            modules.add(node.module)
            modules.update(f'{node.module}.{alias.name}' for alias in node.names)
    return modules


def method_calls(obj, method_names: Iterable[str]) -> List[str]:
    """Source text of every call `<expr>.<name>(...)` in `obj` whose
    method name is one of `method_names`."""
    wanted = set(method_names)
    return [
        ast.unparse(node.func)
        for node in ast.walk(parse_source(obj))
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr in wanted
    ]


def os_exit_references(obj) -> List[str]:
    """Every reference to `os._exit` in `obj`, however it is spelled:
    any `<expr>._exit` attribute (`os._exit`, an aliased module), or
    `_exit` imported from os / posix / nt."""
    refs = []
    for node in ast.walk(parse_source(obj)):
        if isinstance(node, ast.Attribute) and node.attr == '_exit':
            refs.append(ast.unparse(node))
        elif (isinstance(node, ast.ImportFrom)
              and node.module in ('os', 'posix', 'nt')
              and any(alias.name == '_exit' for alias in node.names)):
            refs.append(f'from {node.module} import _exit')
    return refs
