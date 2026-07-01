from __future__ import annotations

import ast
import re
from dataclasses import dataclass

FENCE_RE = re.compile(r"```(?:python|py)?\s*(.*?)```", re.IGNORECASE | re.DOTALL)
DIFF_FENCE_RE = re.compile(r"```(?:diff|patch)?\s*(.*?)```", re.IGNORECASE | re.DOTALL)
HUNK_RE = re.compile(
    r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@(?: .*)?$"
)

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


@dataclass(frozen=True)
class ValidationCheck:
    name: str
    ok: bool
    detail: str = ""


class PatchApplyError(ValueError):
    pass


class DenylistVisitor(ast.NodeVisitor):
    def __init__(self) -> None:
        self.errors: list[str] = []
        self.module_aliases: dict[str, str] = {}
        self.denied_call_names: set[str] = set(DENIED_NAME_CALLS)

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
        if module == "telegram" and any(
            alias.name == "ParseMode" for alias in node.names
        ):
            self.errors.append(
                "Import ParseMode from telegram.constants, not from telegram."
            )
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


def extract_unified_diff(text: str) -> str:
    stripped = text.strip()
    match = DIFF_FENCE_RE.search(stripped)
    if match:
        stripped = match.group(1).strip()
    first_hunk = stripped.find("@@ ")
    if first_hunk < 0:
        raise PatchApplyError("AI did not return a unified diff.")
    header = stripped.rfind("--- ", 0, first_hunk)
    return stripped[header if header >= 0 else first_hunk :].strip()


def apply_unified_diff(source: str, raw_diff: str) -> str:
    """Apply one strict unified diff without accepting fuzzy context matches."""
    diff = extract_unified_diff(raw_diff)
    source_lines = source.splitlines()
    diff_lines = diff.splitlines()
    output: list[str] = []
    source_index = 0
    index = 0
    saw_hunk = False

    while index < len(diff_lines):
        line = diff_lines[index]
        if line.startswith(("--- ", "+++ ")):
            index += 1
            continue
        match = HUNK_RE.match(line)
        if match is None:
            raise PatchApplyError(f"Invalid unified diff line: {line[:120]}")

        saw_hunk = True
        old_start = int(match.group(1))
        old_count = int(match.group(2) or "1")
        new_count = int(match.group(4) or "1")
        target_index = max(0, old_start - 1)
        if target_index < source_index or target_index > len(source_lines):
            raise PatchApplyError("Patch hunk points outside the current source.")
        output.extend(source_lines[source_index:target_index])
        source_index = target_index
        consumed = 0
        produced = 0
        index += 1

        while index < len(diff_lines) and not diff_lines[index].startswith("@@ "):
            patch_line = diff_lines[index]
            if patch_line == r"\ No newline at end of file":
                index += 1
                continue
            if not patch_line or patch_line[0] not in {" ", "+", "-"}:
                raise PatchApplyError(
                    f"Invalid patch operation: {patch_line[:120]}"
                )
            operation, content = patch_line[0], patch_line[1:]
            if operation in {" ", "-"}:
                if source_index >= len(source_lines) or source_lines[source_index] != content:
                    raise PatchApplyError(
                        f"Patch context does not match current source near line {source_index + 1}."
                    )
                if operation == " ":
                    output.append(source_lines[source_index])
                    produced += 1
                source_index += 1
                consumed += 1
            else:
                output.append(content)
                produced += 1
            index += 1

        if consumed != old_count:
            raise PatchApplyError(
                f"Patch hunk expected {old_count} source lines but consumed {consumed}."
            )
        if produced != new_count:
            raise PatchApplyError(
                f"Patch hunk expected {new_count} output lines but produced {produced}."
            )

    if not saw_hunk:
        raise PatchApplyError("Unified diff contains no hunks.")
    output.extend(source_lines[source_index:])
    trailing_newline = "\n" if source.endswith(("\n", "\r")) else ""
    return "\n".join(output) + trailing_newline


def repair_known_code_issues(code: str) -> tuple[str, tuple[str, ...]]:
    repairs: list[str] = []
    lines = code.splitlines()
    output: list[str] = []
    import_pattern = re.compile(r"^(\s*)from telegram import (.+)$")
    for line in lines:
        match = import_pattern.match(line)
        if match is None or "(" in match.group(2):
            output.append(line)
            continue
        names = [name.strip() for name in match.group(2).split(",")]
        parse_mode_names = [
            name for name in names if name.split(" as ", 1)[0].strip() == "ParseMode"
        ]
        if not parse_mode_names:
            output.append(line)
            continue
        remaining = [name for name in names if name not in parse_mode_names]
        indent = match.group(1)
        if remaining:
            output.append(f"{indent}from telegram import {', '.join(remaining)}")
        output.append(
            f"{indent}from telegram.constants import {', '.join(parse_mode_names)}"
        )
        repairs.append("Moved ParseMode import to telegram.constants.")
    repaired = "\n".join(output)
    if code.endswith(("\n", "\r")):
        repaired += "\n"
    return repaired, tuple(repairs)


def validate_generated_code(code: str) -> ValidationResult:
    report = validate_generated_code_report(code)
    failed = next((check for check in report if not check.ok), None)
    if failed is None:
        return ValidationResult(True)
    return ValidationResult(False, failed.detail or f"{failed.name} failed.")


def validate_generated_code_report(code: str) -> list[ValidationCheck]:
    if not code.strip():
        return [ValidationCheck("Source", False, "Generated code is empty.")]

    checks: list[ValidationCheck] = []
    try:
        tree = ast.parse(code)
    except SyntaxError as exc:
        return [
            ValidationCheck(
                "Syntax",
                False,
                f"Syntax error on line {exc.lineno}: {exc.msg}",
            )
        ]
    checks.append(ValidationCheck("Syntax", True, "ast.parse passed."))

    visitor = DenylistVisitor()
    visitor.visit(tree)
    if visitor.errors:
        checks.append(ValidationCheck("Security", False, "; ".join(visitor.errors)))
        return checks
    checks.append(ValidationCheck("Security", True, "Denylist passed."))
    return checks
