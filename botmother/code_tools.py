from __future__ import annotations

import ast
import re
from dataclasses import dataclass

FENCE_RE = re.compile(r"```(?:python|py)?\s*(.*?)```", re.IGNORECASE | re.DOTALL)

DENIED_IMPORT_ROOTS = {
    "ctypes",
    "importlib",
    "multiprocessing",
    "socket",
    "subprocess",
}

DENIED_MODULE_CALLS = {
    ("os", "remove"),
    ("os", "removedirs"),
    ("os", "rename"),
    ("os", "replace"),
    ("os", "rmdir"),
    ("os", "system"),
    ("os", "unlink"),
    ("shutil", "move"),
    ("shutil", "rmtree"),
}

DENIED_NAME_CALLS = {"eval", "exec", "compile", "__import__"}


@dataclass(frozen=True)
class ValidationResult:
    ok: bool
    error: str | None = None


class DenylistVisitor(ast.NodeVisitor):
    def __init__(self) -> None:
        self.errors: list[str] = []
        self.module_aliases: dict[str, str] = {}
        self.denied_call_names: set[str] = set(DENIED_NAME_CALLS)
        self.has_global_error_handler = False
        self.has_command_menu_registration = False

    def visit_Import(self, node: ast.Import) -> None:
        for alias in node.names:
            root = alias.name.split(".", 1)[0]
            local_name = alias.asname or root
            self.module_aliases[local_name] = root
            if root in DENIED_IMPORT_ROOTS:
                self.errors.append(f"Denied import: {alias.name}")
        self.generic_visit(node)

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        module = node.module or ""
        root = module.split(".", 1)[0]
        if root in DENIED_IMPORT_ROOTS:
            self.errors.append(f"Denied import: {module}")
        for alias in node.names:
            local_name = alias.asname or alias.name
            if (root, alias.name) in DENIED_MODULE_CALLS:
                self.denied_call_names.add(local_name)
                self.errors.append(f"Denied import: from {root} import {alias.name}")
        self.generic_visit(node)

    def visit_Call(self, node: ast.Call) -> None:
        if isinstance(node.func, ast.Name) and node.func.id in self.denied_call_names:
            self.errors.append(f"Denied call: {node.func.id}()")

        if isinstance(node.func, ast.Attribute) and isinstance(
            node.func.value, ast.Name
        ):
            module_root = self.module_aliases.get(
                node.func.value.id, node.func.value.id
            )
            call = (module_root, node.func.attr)
            if call in DENIED_MODULE_CALLS:
                self.errors.append(f"Denied call: {module_root}.{node.func.attr}()")

        if isinstance(node.func, ast.Name) and node.func.id == "getattr":
            self.errors.append("Denied call: getattr()")

        if (
            isinstance(node.func, ast.Attribute)
            and node.func.attr == "add_error_handler"
        ):
            self.has_global_error_handler = True

        if isinstance(node.func, ast.Attribute) and node.func.attr == "set_my_commands":
            self.has_command_menu_registration = True

        for keyword in node.keywords:
            if keyword.arg == "parse_mode" and isinstance(keyword.value, ast.Constant):
                if str(keyword.value.value).lower() in {
                    "markdown",
                    "parsemode.markdown",
                }:
                    self.errors.append(
                        "Use HTML or MarkdownV2 with escaping, not legacy Markdown parse mode."
                    )

        self.generic_visit(node)

    def visit_Attribute(self, node: ast.Attribute) -> None:
        if node.attr == "MARKDOWN":
            self.errors.append(
                "Use ParseMode.HTML or ParseMode.MARKDOWN_V2, not legacy ParseMode.MARKDOWN."
            )
        self.generic_visit(node)


def extract_python_code(text: str) -> str:
    stripped = text.strip()
    match = FENCE_RE.search(stripped)
    if match:
        return match.group(1).strip()
    if stripped.startswith("```"):
        lines = stripped.splitlines()
        if lines and lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        return "\n".join(lines).strip()
    return stripped


def validate_generated_code(code: str) -> ValidationResult:
    if not code.strip():
        return ValidationResult(False, "Generated code is empty.")
    try:
        tree = ast.parse(code)
    except SyntaxError as exc:
        return ValidationResult(False, f"Syntax error on line {exc.lineno}: {exc.msg}")

    visitor = DenylistVisitor()
    visitor.visit(tree)
    if visitor.errors:
        return ValidationResult(False, "; ".join(visitor.errors))
    if not visitor.has_global_error_handler:
        return ValidationResult(
            False,
            "Missing global error handler: call application.add_error_handler(...).",
        )
    if not visitor.has_command_menu_registration:
        return ValidationResult(
            False,
            "Missing bot command menu registration: call application.bot.set_my_commands(...).",
        )
    return ValidationResult(True)
