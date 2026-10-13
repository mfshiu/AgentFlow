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
    """Every identifier the code of `obj` uses or defines: names,
    attribute names, imported names, function and class names,
    parameter names and keyword-argument names. Strings, comments and
    docstrings are not code, so a word that only appears there is not
    reported."""
    names = set()
    for node in ast.walk(parse_source(obj)):
        if isinstance(node, ast.Name):
            names.add(node.id)
        elif isinstance(node, ast.Attribute):
            names.add(node.attr)
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            names.add(node.name)
        elif isinstance(node, ast.arg):
            names.add(node.arg)
        elif isinstance(node, ast.keyword) and node.arg is not None:
            names.add(node.arg)
        elif isinstance(node, (ast.Import, ast.ImportFrom)):
            for alias in node.names:
                names.add(alias.name.split('.')[-1])
                if alias.asname:
                    names.add(alias.asname)
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


def names_used(obj) -> Set[str]:
    """`identifiers(obj)` plus every string literal that is itself an
    identifier: a name the code may reach through getattr() or
    importlib, or a config key. Log messages and other prose are not
    identifiers, so they are left out."""
    return identifiers(obj) | {s for s in string_literals(obj) if s.isidentifier()}


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


def call_arguments(obj, method_name: str) -> List[List[str]]:
    """Source text of the arguments (positional, then keyword) of every
    call `<expr>.<method_name>(...)` in `obj`."""
    return [
        [ast.unparse(arg) for arg in node.args]
        + [ast.unparse(kw) for kw in node.keywords]
        for node in ast.walk(parse_source(obj))
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == method_name
    ]


_OS_MODULES = ('os', 'posix', 'nt')


def os_function_references(obj, functions: Iterable[str]) -> List[str]:
    """Every reference in `obj` to one of the os-module `functions`
    (e.g. `_exit`, `abort`), however it is spelled: `<m>.<function>`
    where `<m>` is bound to os / posix / nt by an import in the module
    of `obj` (`import os`, `import os as _os`), a use of a name bound by
    `from os import <function> [as alias]`, and such an import itself.
    An attribute of anything else (`self._exit`) is not a reference."""
    wanted = set(functions)
    module_tree = parse_source(inspect.getmodule(obj))
    module_names = set()
    function_names = set()
    for node in ast.walk(module_tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name in _OS_MODULES:
                    module_names.add(alias.asname or alias.name)
                elif alias.name.split('.')[0] in _OS_MODULES and not alias.asname:
                    module_names.add(alias.name.split('.')[0])   # import os.path
        elif isinstance(node, ast.ImportFrom) and node.module in _OS_MODULES:
            function_names.update(alias.asname or alias.name
                                  for alias in node.names if alias.name in wanted)
    refs = []
    for node in ast.walk(parse_source(obj)):
        if (isinstance(node, ast.Attribute) and node.attr in wanted
                and isinstance(node.value, ast.Name)
                and node.value.id in module_names):
            refs.append(ast.unparse(node))
        elif isinstance(node, ast.Name) and node.id in function_names:
            refs.append(node.id)
        elif (isinstance(node, ast.ImportFrom) and node.module in _OS_MODULES
              and any(alias.name in wanted for alias in node.names)):
            refs.append(ast.unparse(node))
    return refs


def os_exit_references(obj) -> List[str]:
    """Every reference to `os._exit` in `obj` (see os_function_references)."""
    return os_function_references(obj, ('_exit',))
