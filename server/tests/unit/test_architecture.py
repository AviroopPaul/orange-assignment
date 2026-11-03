"""House rules, enforced. import-linter covers module layers; these cover file-level rules."""

import ast
from pathlib import Path

import pytest

APP = Path(__file__).resolve().parents[2] / "app"


def imported_modules(path: Path) -> set[str]:
    modules: set[str] = set()
    for node in ast.walk(ast.parse(path.read_text())):
        if isinstance(node, ast.Import):
            modules.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            modules.add(node.module)
    return modules


ROUTERS = sorted(APP.glob("domains/*/router.py"))
ALL_MODULES = sorted(APP.rglob("*.py"))


def relative(path: Path) -> str:
    return str(path.relative_to(APP))


def test_every_router_is_found() -> None:
    assert {path.parent.name for path in ROUTERS} == {
        "bookings",
        "chat",
        "notifications",
        "system",
        "tracing",
    }


@pytest.mark.parametrize("router", ROUTERS, ids=relative)
def test_routers_do_not_touch_the_database(router: Path) -> None:
    for module in imported_modules(router):
        assert not module.startswith("sqlalchemy"), module
        assert not module.endswith((".models", ".repository")), module


@pytest.mark.parametrize("path", ALL_MODULES, ids=relative)
def test_only_the_agent_domain_and_composition_points_import_adk(path: Path) -> None:
    allowed = {"container.py", "migrate.py"}
    if relative(path).startswith("domains/agent/") or relative(path) in allowed:
        return
    for module in imported_modules(path):
        assert not module.startswith("google.adk"), f"{relative(path)} imports {module}"


def test_worker_never_loads_adk_at_import_time() -> None:
    """ADK is heavy. The container imports it inside a function, so workers stay small."""
    tree = ast.parse((APP / "container.py").read_text())
    top_level = [node for node in tree.body if isinstance(node, ast.Import | ast.ImportFrom)]
    for node in top_level:
        module = getattr(node, "module", None) or ""
        assert not module.startswith(("google.adk", "app.domains.agent")), module


@pytest.mark.parametrize("path", sorted(APP.glob("domains/*/repository.py")), ids=relative)
def test_repositories_never_commit(path: Path) -> None:
    source = path.read_text()
    assert ".commit(" not in source
    assert "session_scope" not in source
