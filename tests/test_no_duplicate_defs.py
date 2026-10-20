"""A module that defines the same function twice keeps only the last one, silently.

server.py once carried two ``_owe_device_pollers`` from a clean auto-merge; the later one dropped
the earlier one's wider-quiesce guard. This fails on any repeated top-level function or class, or
repeated method in a class, across the package (property accessors and overloads excepted)."""

import ast
from collections import Counter
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parent.parent / "src" / "tt_device_mcp"
_DEFS = (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)


def _is_accessor_or_overload(node) -> bool:
    for dec in getattr(node, "decorator_list", []):
        if isinstance(dec, ast.Attribute) and dec.attr in ("setter", "getter", "deleter"):
            return True
        if isinstance(dec, (ast.Name, ast.Attribute)) and getattr(dec, "id", getattr(dec, "attr", "")) == "overload":
            return True
    return False


def _repeats(body) -> list[str]:
    names = Counter(n.name for n in body if isinstance(n, _DEFS) and not _is_accessor_or_overload(n))
    return sorted(name for name, count in names.items() if count > 1)


def _modules():
    return sorted(SRC.rglob("*.py"))


@pytest.mark.parametrize("path", _modules(), ids=lambda p: str(p.relative_to(SRC)))
def test_no_module_defines_the_same_name_twice(path):
    tree = ast.parse(path.read_text(), filename=str(path))
    found = [f"{name} (module)" for name in _repeats(tree.body)]
    for cls in (n for n in ast.walk(tree) if isinstance(n, ast.ClassDef)):
        found += [f"{cls.name}.{name}" for name in _repeats(cls.body)]
    assert not found, f"{path.name} defines these more than once: {', '.join(found)}"


def test_the_guard_catches_a_repeated_def():
    tree = ast.parse("def f():\n    pass\n\n\ndef f():\n    return 1\n")
    assert _repeats(tree.body) == ["f"]
