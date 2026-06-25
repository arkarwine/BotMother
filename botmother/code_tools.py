from __future__ import annotations

import ast
import builtins
import re
import subprocess
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

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
BUILTIN_NAMES = set(dir(builtins)) | {
    "__annotations__",
    "__debug__",
    "__doc__",
    "__file__",
    "__name__",
    "__package__",
    "__spec__",
}
NON_CALLABLE_KINDS = {"constant", "list", "tuple", "dict", "set"}
SYNC_METHODS_OFTEN_MISTAKEN_AS_ASYNC = {
    "add_error_handler",
    "add_handler",
    "run_polling",
}
MYPY_TIMEOUT_SECONDS = 20


@dataclass(frozen=True)
class ValidationResult:
    ok: bool
    error: str | None = None


@dataclass
class StaticScope:
    parent: "StaticScope | None" = None
    defined: set[str] = field(default_factory=set)
    local_assigned: set[str] = field(default_factory=set)
    global_names: set[str] = field(default_factory=set)
    value_kinds: dict[str, str] = field(default_factory=dict)

    def root(self) -> "StaticScope":
        scope = self
        while scope.parent is not None:
            scope = scope.parent
        return scope

    def has(self, name: str) -> bool:
        if name in BUILTIN_NAMES:
            return True
        if name in self.defined:
            return True
        if self.parent is not None:
            return self.parent.has(name)
        return False

    def kind(self, name: str) -> str | None:
        if name in self.value_kinds:
            return self.value_kinds[name]
        if self.parent is not None:
            return self.parent.kind(name)
        return None

    def define(self, name: str, kind: str = "unknown") -> None:
        target = self.root() if name in self.global_names else self
        target.defined.add(name)
        target.value_kinds[name] = kind


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


class StaticTypeVisitor(ast.NodeVisitor):
    def __init__(self) -> None:
        self.errors: list[str] = []
        self.scope = StaticScope()
        self.module_aliases: dict[str, str] = {}

    def check(self, tree: ast.Module) -> list[str]:
        for name in self._collect_module_bindings(tree):
            self.scope.define(name)
        self.visit(tree)
        return self.errors

    def _error(self, node: ast.AST, message: str) -> None:
        line = getattr(node, "lineno", None)
        prefix = f"line {line}: " if line else ""
        self.errors.append(f"Static type check: {prefix}{message}")

    def _collect_module_bindings(self, tree: ast.Module) -> set[str]:
        bindings: set[str] = set()
        for statement in tree.body:
            if isinstance(statement, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                bindings.add(statement.name)
            elif isinstance(statement, ast.Import):
                for alias in statement.names:
                    bindings.add(alias.asname or alias.name.split(".", 1)[0])
            elif isinstance(statement, ast.ImportFrom):
                for alias in statement.names:
                    if alias.name != "*":
                        bindings.add(alias.asname or alias.name)
            elif isinstance(statement, (ast.Assign, ast.AnnAssign, ast.AugAssign)):
                for name in self._target_names_from_statement(statement):
                    bindings.add(name)
        return bindings

    def _collect_local_assignments(self, statements: list[ast.stmt]) -> set[str]:
        assigned: set[str] = set()

        def walk(node: ast.AST) -> None:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Lambda)):
                return
            if isinstance(node, (ast.Assign, ast.AnnAssign, ast.AugAssign, ast.For, ast.AsyncFor, ast.With, ast.AsyncWith, ast.ExceptHandler)):
                assigned.update(self._target_names_from_statement(node))
            elif isinstance(node, ast.NamedExpr):
                assigned.update(self._target_names(node.target))
            elif isinstance(node, (ast.Import, ast.ImportFrom)):
                for alias in node.names:
                    if alias.name != "*":
                        assigned.add(alias.asname or alias.name.split(".", 1)[0])
            for child in ast.iter_child_nodes(node):
                walk(child)

        for statement in statements:
            walk(statement)
        return assigned

    def _target_names_from_statement(self, node: ast.AST) -> set[str]:
        targets: list[ast.AST] = []
        if isinstance(node, ast.Assign):
            targets.extend(node.targets)
        elif isinstance(node, (ast.AnnAssign, ast.AugAssign, ast.NamedExpr)):
            targets.append(node.target)
        elif isinstance(node, (ast.For, ast.AsyncFor)):
            targets.append(node.target)
        elif isinstance(node, (ast.With, ast.AsyncWith)):
            targets.extend(item.optional_vars for item in node.items if item.optional_vars is not None)
        elif isinstance(node, ast.ExceptHandler) and node.name:
            return {node.name}
        return {name for target in targets for name in self._target_names(target)}

    def _target_names(self, target: ast.AST) -> set[str]:
        if isinstance(target, ast.Name):
            return {target.id}
        if isinstance(target, (ast.Tuple, ast.List)):
            return {name for item in target.elts for name in self._target_names(item)}
        return set()

    def _value_kind(self, value: ast.AST) -> str:
        if isinstance(value, ast.Constant):
            return "constant"
        if isinstance(value, ast.List):
            return "list"
        if isinstance(value, ast.Tuple):
            return "tuple"
        if isinstance(value, ast.Dict):
            return "dict"
        if isinstance(value, ast.Set):
            return "set"
        if isinstance(value, (ast.Lambda, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            return "callable"
        return "unknown"

    def _define_target(self, target: ast.AST, kind: str = "unknown") -> None:
        for name in self._target_names(target):
            self.scope.define(name, kind)

    def _push_scope(self, local_assigned: set[str] | None = None) -> StaticScope:
        previous = self.scope
        self.scope = StaticScope(parent=previous, local_assigned=local_assigned or set())
        return previous

    def _pop_scope(self, previous: StaticScope) -> None:
        self.scope = previous

    def visit_Import(self, node: ast.Import) -> None:
        for alias in node.names:
            root = alias.name.split(".", 1)[0]
            local_name = alias.asname or root
            self.module_aliases[local_name] = root
            self.scope.define(local_name, "module")

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        for alias in node.names:
            if alias.name == "*":
                continue
            self.scope.define(alias.asname or alias.name)

    def visit_Global(self, node: ast.Global) -> None:
        self.scope.global_names.update(node.names)

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        self.scope.define(node.name, "sync_func")
        self._visit_function_body(node)

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
        self.scope.define(node.name, "async_func")
        self._visit_function_body(node)

    def _visit_function_body(self, node: ast.FunctionDef | ast.AsyncFunctionDef) -> None:
        for decorator in node.decorator_list:
            self.visit(decorator)
        for default in [*node.args.defaults, *node.args.kw_defaults]:
            if default is not None:
                self.visit(default)
        if node.returns is not None:
            self.visit(node.returns)

        previous = self._push_scope(self._collect_local_assignments(node.body))
        for arg in [
            *node.args.posonlyargs,
            *node.args.args,
            *node.args.kwonlyargs,
        ]:
            self.scope.define(arg.arg)
            if arg.annotation is not None:
                self.visit(arg.annotation)
        if node.args.vararg is not None:
            self.scope.define(node.args.vararg.arg)
        if node.args.kwarg is not None:
            self.scope.define(node.args.kwarg.arg)
        for statement in node.body:
            self.visit(statement)
        self._pop_scope(previous)

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        self.scope.define(node.name, "class")
        for base in node.bases:
            self.visit(base)
        for keyword in node.keywords:
            self.visit(keyword.value)
        previous = self._push_scope()
        for statement in node.body:
            self.visit(statement)
        self._pop_scope(previous)

    def visit_Assign(self, node: ast.Assign) -> None:
        self.visit(node.value)
        kind = self._value_kind(node.value)
        for target in node.targets:
            self._define_target(target, kind)

    def visit_AnnAssign(self, node: ast.AnnAssign) -> None:
        self.visit(node.annotation)
        if node.value is not None:
            self.visit(node.value)
            kind = self._value_kind(node.value)
        else:
            kind = "unknown"
        self._define_target(node.target, kind)

    def visit_AugAssign(self, node: ast.AugAssign) -> None:
        self.visit(node.target)
        self.visit(node.value)
        self._define_target(node.target)

    def visit_For(self, node: ast.For) -> None:
        self.visit(node.iter)
        self._define_target(node.target)
        for statement in [*node.body, *node.orelse]:
            self.visit(statement)

    def visit_AsyncFor(self, node: ast.AsyncFor) -> None:
        self.visit_For(node)

    def visit_With(self, node: ast.With) -> None:
        for item in node.items:
            self.visit(item.context_expr)
            if item.optional_vars is not None:
                self._define_target(item.optional_vars)
        for statement in node.body:
            self.visit(statement)

    def visit_AsyncWith(self, node: ast.AsyncWith) -> None:
        self.visit_With(node)

    def visit_ExceptHandler(self, node: ast.ExceptHandler) -> None:
        if node.type is not None:
            self.visit(node.type)
        if node.name:
            self.scope.define(node.name)
        for statement in node.body:
            self.visit(statement)

    def visit_Name(self, node: ast.Name) -> None:
        if not isinstance(node.ctx, ast.Load):
            return
        if node.id in self.scope.local_assigned and node.id not in self.scope.defined:
            self._error(node, f"name '{node.id}' is used before local assignment.")
            return
        if not self.scope.has(node.id):
            self._error(node, f"name '{node.id}' is not defined.")

    def visit_Call(self, node: ast.Call) -> None:
        if isinstance(node.func, ast.Name):
            kind = self.scope.kind(node.func.id)
            if kind in NON_CALLABLE_KINDS:
                self._error(node, f"name '{node.func.id}' is a {kind}, not callable.")
        elif isinstance(node.func, (ast.Constant, ast.List, ast.Tuple, ast.Dict, ast.Set)):
            self._error(node, "literal value is not callable.")

        if (
            isinstance(node.func, ast.Attribute)
            and node.func.attr == "run"
            and isinstance(node.func.value, ast.Name)
            and self.module_aliases.get(node.func.value.id, node.func.value.id) == "asyncio"
            and node.args
            and isinstance(node.args[0], ast.Name)
            and self.scope.kind(node.args[0].id) == "async_func"
        ):
            self._error(
                node,
                f"asyncio.run expected a coroutine object; call {node.args[0].id}() instead of passing the function.",
            )

        self.generic_visit(node)

    def visit_Await(self, node: ast.Await) -> None:
        value = node.value
        if isinstance(value, ast.Name) and self.scope.kind(value.id) == "async_func":
            self._error(node, f"await expected a coroutine object; call {value.id}() first.")
        elif isinstance(value, (ast.Constant, ast.List, ast.Tuple, ast.Dict, ast.Set)):
            self._error(node, "await expected an awaitable value.")
        elif isinstance(value, ast.Call):
            if isinstance(value.func, ast.Name) and self.scope.kind(value.func.id) == "sync_func":
                self._error(node, f"cannot await sync function '{value.func.id}()'.")
            elif (
                isinstance(value.func, ast.Attribute)
                and value.func.attr in SYNC_METHODS_OFTEN_MISTAKEN_AS_ASYNC
            ):
                self._error(node, f"cannot await sync method '{value.func.attr}()'.")
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


def run_mypy_static_check(code: str) -> ValidationResult:
    with tempfile.TemporaryDirectory(prefix="botmother-mypy-") as tmp:
        tmp_path = Path(tmp)
        source_path = tmp_path / "bot.py"
        cache_path = tmp_path / ".mypy_cache"
        source_path.write_text(code, encoding="utf-8")
        command = [
            sys.executable,
            "-m",
            "mypy",
            "--no-error-summary",
            "--show-error-codes",
            "--ignore-missing-imports",
            "--follow-imports=skip",
            "--check-untyped-defs",
            "--no-incremental",
            f"--cache-dir={cache_path}",
            str(source_path),
        ]
        try:
            completed = subprocess.run(
                command,
                cwd=tmp,
                capture_output=True,
                text=True,
                timeout=MYPY_TIMEOUT_SECONDS,
                check=False,
            )
        except subprocess.TimeoutExpired:
            return ValidationResult(False, "Mypy static type check timed out.")

    output = "\n".join(
        part.strip()
        for part in (completed.stdout, completed.stderr)
        if part and part.strip()
    )
    if completed.returncode == 0:
        return ValidationResult(True)
    if "No module named mypy" in output:
        return ValidationResult(True)
    if completed.returncode == 1:
        cleaned = _clean_mypy_output(output)
        return ValidationResult(False, f"Mypy static type check failed: {cleaned}")
    return ValidationResult(False, f"Mypy static type check could not run: {output or completed.returncode}")


def _clean_mypy_output(output: str, limit: int = 5) -> str:
    lines = []
    for line in output.splitlines():
        text = line.strip()
        if not text:
            continue
        text = re.sub(r"^.*bot\.py:", "bot.py:", text)
        lines.append(text)
        if len(lines) >= limit:
            break
    return "; ".join(lines) if lines else "unknown mypy error"


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
    static_errors = StaticTypeVisitor().check(tree)
    if static_errors:
        return ValidationResult(False, "; ".join(static_errors[:5]))
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
    mypy_result = run_mypy_static_check(code)
    if not mypy_result.ok:
        return mypy_result
    return ValidationResult(True)
