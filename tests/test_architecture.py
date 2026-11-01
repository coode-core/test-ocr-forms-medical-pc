import ast
from pathlib import Path

SRC = Path(__file__).resolve().parents[1] / "src" / "plan_cuidado"


def _imports(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    names = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module:
            names.add(node.module)
        elif isinstance(node, ast.Import):
            names.update(a.name for a in node.names)
    return names


def test_services_and_domain_never_import_adapters():
    for layer in ("services", "domain"):
        for path in (SRC / layer).glob("*.py"):
            bad = {m for m in _imports(path) if m.startswith("plan_cuidado.adapters")}
            assert not bad, f"{path.name} imports {bad}"
