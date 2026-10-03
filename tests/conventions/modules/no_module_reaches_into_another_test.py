"""Rule: code outside a module imports only its public surface (`scout.modules.<m>`), never its
underscore-prefixed internals. Reads source; runs nothing."""

from __future__ import annotations

import ast
from pathlib import Path

SRC = Path(__file__).resolve().parents[3] / "src" / "scout"


def _owner(path: Path) -> str | None:
    parts = path.relative_to(SRC).parts
    return parts[1] if parts[0] == "modules" and len(parts) > 2 else None


def test_no_module_imports_another_modules_internals() -> None:
    violations = []
    for path in SRC.rglob("*.py"):
        owner = _owner(path)
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            names = (
                [node.module or ""]
                if isinstance(node, ast.ImportFrom)
                else [a.name for a in node.names]
                if isinstance(node, ast.Import)
                else []
            )
            for name in names:
                parts = name.split(".")
                if parts[:2] != ["scout", "modules"] or len(parts) < 4:
                    continue
                target, inner = parts[2], parts[3]
                if inner.startswith("_") and target != owner:
                    violations.append(f"{path.relative_to(SRC)} imports {name}")
    assert violations == []


def test_every_module_has_a_readme_and_a_public_surface() -> None:
    for module in (SRC / "modules").iterdir():
        if module.is_dir() and not module.name.startswith("__"):
            assert (module / "README.md").is_file(), module.name
            init = (module / "__init__.py").read_text(encoding="utf-8")
            assert "__all__" in init, module.name


def test_no_junk_drawer_directories() -> None:
    banned = {"helpers", "util", "utils", "common", "shared", "misc"}
    assert not [p for p in SRC.rglob("*") if p.is_dir() and p.name in banned]
