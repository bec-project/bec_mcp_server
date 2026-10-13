"""Every Next: call a finding can suggest names a registered tool and only that tool's arguments.

Findings tell a model which tool to call next, and bec_diagnose passes them on from every tool. A
renamed tool or argument would turn that advice into a failing call, so this reads every
``Finding(...)`` in the tool modules and checks its ``next_tool`` and ``next_args`` against the
schemas the server registers. Tool names and argument keys must be literals, module constants
or local variables assigned from literals, so that they stay checkable.
"""

from __future__ import annotations

import ast
import importlib
import pkgutil
from collections.abc import Iterator
from pathlib import Path
from types import ModuleType

import anyio
import pytest
from mcp.client import Client

import bec_mcp_server.tools as tools_package
from bec_mcp_server.app import build_app
from bec_mcp_server.settings import Settings
from bec_mcp_server.source.roots import SourceRoot

_NEXT_TOOL, _NEXT_ARGS = (4, "next_tool"), (5, "next_args")


class Unresolved(Exception):
    """A next_tool or next_args expression this test cannot evaluate statically."""


def _argument(call: ast.Call, position: tuple[int, str]) -> ast.expr | None:
    index, keyword = position
    for item in call.keywords:
        if item.arg == keyword:
            return item.value
    return call.args[index] if len(call.args) > index else None


def _alternatives(
    node: ast.expr | None, module: ModuleType, local: dict[str, ast.expr]
) -> list[ast.expr]:
    """The expressions ``node`` can stand for: both arms of a conditional, a variable's value."""
    if node is None or (isinstance(node, ast.Constant) and node.value is None):
        return []
    if isinstance(node, ast.IfExp):
        return _alternatives(node.body, module, local) + _alternatives(node.orelse, module, local)
    if isinstance(node, ast.Name) and node.id in local:
        return _alternatives(local[node.id], module, {})
    return [node]


def _tool_names(node: ast.expr | None, module: ModuleType, local: dict) -> list[str]:
    names = []
    for option in _alternatives(node, module, local):
        if isinstance(option, ast.Constant) and isinstance(option.value, str):
            names.append(option.value)
        elif isinstance(option, ast.Name) and isinstance(getattr(module, option.id, None), str):
            names.append(getattr(module, option.id))
        else:
            raise Unresolved(ast.unparse(option))
    return names


def _argument_keys(node: ast.expr | None, module: ModuleType, local: dict) -> list[set[str]]:
    key_sets = []
    for option in _alternatives(node, module, local):
        if not isinstance(option, ast.Dict) or not all(
            isinstance(key, ast.Constant) and isinstance(key.value, str) for key in option.keys
        ):
            raise Unresolved(ast.unparse(option))
        key_sets.append({key.value for key in option.keys})
    return key_sets or [set()]


def _local_assignments(function: ast.AST) -> dict[str, ast.expr]:
    return {
        node.targets[0].id: node.value
        for node in ast.walk(function)
        if isinstance(node, ast.Assign)
        and len(node.targets) == 1
        and isinstance(node.targets[0], ast.Name)
    }


def suggested_calls() -> Iterator[tuple[str, str, set[str]]]:
    """(where, tool, argument keys) for every next call a Finding in a tool module can carry."""
    for info in pkgutil.iter_modules(tools_package.__path__):
        module = importlib.import_module(f"{tools_package.__name__}.{info.name}")
        tree = ast.parse(Path(module.__file__).read_text(encoding="utf-8"))
        functions = [n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)]
        for function in functions:
            local = _local_assignments(function)
            for call in ast.walk(function):
                name = (
                    getattr(call.func, "id", getattr(call.func, "attr", None))
                    if isinstance(call, ast.Call)
                    else None
                )
                if name != "Finding":
                    continue
                where = f"{info.name}.py:{call.lineno}"
                try:
                    tools = _tool_names(_argument(call, _NEXT_TOOL), module, local)
                    key_sets = _argument_keys(_argument(call, _NEXT_ARGS), module, local)
                except Unresolved as exc:
                    yield where, f"<cannot check {exc}>", set()
                    continue
                for tool in tools:
                    for keys in key_sets:
                        yield where, tool, keys


@pytest.fixture
def registered(make_connection, tmp_path) -> dict[str, set[str]]:
    """Every tool the server registers in code mode, the superset of all modes, with arguments."""
    settings = Settings(mode="code", code_roots=(SourceRoot("src", tmp_path),))
    app = build_app(settings, connection=make_connection())

    async def list_tools():
        async with Client(app.server) as client:
            return (await client.list_tools()).tools

    return {
        tool.name: set((tool.input_schema or {}).get("properties") or {})
        for tool in anyio.run(list_tools)
    }


def test_the_check_sees_the_findings_it_should():
    tools = {tool for _, tool, _ in suggested_calls()}

    assert {"bec_logs", "bec_device_diagnose", "bec_device_read", "mcp_status"} <= tools
    assert {"bec_code_traceback", "bec_code_read"} <= tools


def test_every_next_call_matches_a_registered_tool(registered):
    problems = []
    for where, tool, keys in suggested_calls():
        if tool not in registered:
            problems.append(f"{where}: Next: {tool}() is not a registered tool")
        elif unknown := keys - registered[tool]:
            problems.append(
                f"{where}: {tool} has no argument {', '.join(sorted(unknown))}; "
                f"it takes {', '.join(sorted(registered[tool])) or 'none'}"
            )

    assert not problems, "\n".join(problems)
