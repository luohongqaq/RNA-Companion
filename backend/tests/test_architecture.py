"""三层依赖检查：组合入口在api，业务不依赖存储、框架和具体模型SDK。"""
import ast
from importlib.util import resolve_name
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SHARED_DOCUMENT_MAPPING_IMPORTS = {
    "module.memory_management.shared.repository",
    "module.memory_management.shared.repository.document_mappers",
}
BUSINESS_FORBIDDEN = {
    "api", "storage", "fastapi", "starlette", "sqlite3", "aiosqlite",
    "sqlalchemy", "qdrant_client", "openai", "anthropic", "zhipuai", "dashscope",
}
DOMAIN_FILES = {
    "document", "source", "plan", "generation", "review", "publication", "job", "command",
    "ports", "errors", "retrieval", "timeline", "representation", "embedding", "verification", "retention",
}
DOMAIN_FORBIDDEN_DEPENDENCIES = {
    "document": {"source", "plan", "generation", "review", "publication", "job", "command", "ports"},
    "source": {"generation", "review", "publication", "job", "command", "ports"},
    "plan": {"generation", "review", "publication", "job", "command", "ports"},
    "generation": {"review", "publication", "job", "command", "ports"},
    "review": {"publication", "job", "command", "ports"},
    "publication": {"job", "command", "ports"},
    "job": {"command", "ports"},
}


def imports(source, package):
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            for alias in node.names:
                yield node.lineno, alias.name
        elif isinstance(node, ast.ImportFrom):
            module = node.module or ""
            if node.level:
                module = resolve_name("." * node.level + module, package)
            yield node.lineno, module
            if module.startswith("module."):
                for alias in node.names:
                    if alias.name != "*":
                        yield node.lineno, f"{module}.{alias.name}"
        elif isinstance(node, ast.Call) and node.args:
            name = node.func
            dynamic = (isinstance(name, ast.Name) and name.id == "__import__"
                       or isinstance(name, ast.Attribute) and name.attr == "import_module")
            if dynamic and isinstance(node.args[0], ast.Constant) and isinstance(node.args[0].value, str):
                yield node.lineno, node.args[0].value


def violations(layer, source, package):
    forbidden = BUSINESS_FORBIDDEN if layer == "core" else {"api"} if layer == "storage" else set()
    return [(line, module) for line, module in imports(source, package)
            if module.split(".")[0] in forbidden
            or (layer == "core" and module.startswith("module.")
                and ".repository" in module)]


@pytest.mark.parametrize("layer", ["api", "storage"])
def test_runtime_layer_dependencies(layer):
    errors = []
    for path in sorted((ROOT / layer).rglob("*.py")):
        relative = path.relative_to(ROOT)
        package = ".".join(relative.parent.parts)
        for line, module in violations(layer, path.read_text(encoding="utf-8-sig"), package):
            errors.append(f"{relative}:{line}: {layer} must not import {module}")
    assert not errors, "\n".join(errors)


def test_function_module_layer_dependencies():
    errors = []
    for path in sorted((ROOT / "module").rglob("*.py")):
        relative = path.relative_to(ROOT)
        package = ".".join(relative.parent.parts)
        layer = "storage" if "repository" in relative.parts else "core"
        source = path.read_text(encoding="utf-8-sig")
        for line, module in violations(layer, source, package):
            errors.append(f"{relative}:{line}: {layer} must not import {module}")
        for line, module in imports(source, package):
            if module == "core" or module.startswith("core."):
                errors.append(f"{relative}:{line}: use the owning module or module.shared")
            if relative.parts[:2] == ("module", "shared") and (
                module == "module" or (module.startswith("module.") and
                module != "module.shared" and not module.startswith("module.shared."))
            ):
                errors.append(f"{relative}:{line}: shared code must not import {module}")
            if not module.startswith("module."):
                continue
            # Domain rules remain independent of processing and scheduling.
            if "domain" in relative.parts and any(
                    part in module.split(".") for part in ("processing", "scheduling")):
                errors.append(f"{relative}:{line}: domain must not import {module}")
            if "processing" in relative.parts and "scheduling" in module.split("."):
                errors.append(f"{relative}:{line}: processing must not import {module}")
    assert not errors, "\n".join(errors)


def test_shared_contracts_do_not_depend_on_execution_implementations():
    errors = []
    for path in sorted((ROOT / "module/memory_management/shared/contracts").glob("*.py")):
        source = path.read_text(encoding="utf-8-sig")
        package = ".".join(path.relative_to(ROOT).parent.parts)
        for line, module in imports(source, package):
            if (module.startswith("module.memory_management.")
                    and not module.startswith("module.memory_management.shared.contracts")):
                errors.append(f"{path.relative_to(ROOT)}:{line}: shared contracts must not import {module}")
    assert not errors, "\n".join(errors)


def test_domain_file_roles_and_dependency_direction():
    errors = []
    for path in sorted((ROOT / "module/memory_management").rglob("*.py")):
        if path.parent.name != "domain":
            continue
        if path.name == "__init__.py":
            continue
        relative = path.relative_to(ROOT)
        assert path.stem in DOMAIN_FILES, f"{relative}: use a documented domain file role"
        source = path.read_text(encoding="utf-8-sig")
        package = ".".join(relative.parent.parts)
        forbidden = DOMAIN_FORBIDDEN_DEPENDENCIES.get(path.stem, set())
        for line, module in imports(source, package):
            parts = module.split(".")
            if module.startswith("module.memory_management.") and "domain" in parts:
                index = parts.index("domain") + 1
                category = parts[index] if index < len(parts) else ""
                if category in forbidden:
                    errors.append(f"{relative}:{line}: {path.stem} must not import {category}")
        tree = ast.parse(source)
        if path.stem == "job":
            for node in tree.body:
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    errors.append(f"{relative}:{node.lineno}: put command factories on the job input class")
        if path.stem != "ports":
            for node in tree.body:
                if isinstance(node, ast.ClassDef) and any(
                    isinstance(base, ast.Name) and base.id == "Protocol" for base in node.bases
                ):
                    errors.append(f"{relative}:{node.lineno}: put external capability interfaces in ports.py")
    assert not errors, "\n".join(errors)


def test_memory_recall_capability_dependencies():
    allowed = {
        "agent": {"agent", "cache", "retrieval", "shared"},
        "cache": {"cache", "retrieval", "shared"},
        "retrieval": {"retrieval", "shared"},
        "shared": {"shared"},
    }
    errors = []
    root = ROOT / "module/memory_recall"
    for path in sorted(root.rglob("*.py")):
        relative = path.relative_to(root)
        owner = relative.parts[0]
        if owner not in allowed:
            continue
        package = ".".join(path.relative_to(ROOT).parent.parts)
        for line, module in imports(path.read_text(encoding="utf-8-sig"), package):
            if module.startswith("module.memory_recall."):
                target = module.split(".")[2]
                if target not in allowed[owner]:
                    errors.append(f"{relative}:{line}: {owner} must not import {target}")
            shared_document_mapping = (
                "repository" in relative.parts and module in SHARED_DOCUMENT_MAPPING_IMPORTS
            )
            if (module.startswith("module.memory_management.") and ".repository" in module
                    and not shared_document_mapping):
                errors.append(f"{relative}:{line}: recall must not import management repository")
    assert not errors, "\n".join(errors)


def test_user_memory_edit_module_boundaries():
    errors = []
    root = ROOT / "module/user_edit/memory_edit"
    for path in sorted(root.rglob("*.py")):
        relative = path.relative_to(root)
        package = ".".join(path.relative_to(ROOT).parent.parts)
        for line, module in imports(path.read_text(encoding="utf-8-sig"), package):
            if module.startswith("module.memory_recall"):
                errors.append(f"{relative}:{line}: user memory management must not depend on recall")
            if module.startswith("module.memory_management."):
                allowed = (".core.domain." in module
                    or module.startswith("module.memory_management.shared.contracts.")
                    or ("repository" in relative.parts and module.startswith(
                        "module.memory_management.shared.indexing.repository."))
                    or ("repository" in relative.parts and module in SHARED_DOCUMENT_MAPPING_IMPORTS))
                if not allowed:
                    errors.append(f"{relative}:{line}: depend on formal contracts or shared indexing, not {module}")
    assert not errors, "\n".join(errors)


@pytest.mark.parametrize("source", [
    "import storage.sqlite.models",
    "from storage import sqlite",
    "def call():\n    import openai",
    "import importlib\nimportlib.import_module('qdrant_client')",
    "from module.memory_management.daily.repository.sqlite import DailyProductionRepository",
    "from module.memory_management.daily import repository",
])
def test_guard_detects_forbidden_business_imports(source):
    assert violations("core", source, "module.shared.context.types")


def test_api_composition_and_storage_business_ports_are_allowed():
    assert not violations("api", "from storage.sqlite import models", "api")
    assert not violations("storage", "from module.chat.reply.core.domain import context_ports", "storage.sqlite")
    assert (1, "module.shared.context") in imports("from .. import types", "module.shared.context.config")
