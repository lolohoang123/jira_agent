#!/usr/bin/env python3
"""Deterministic guardrail checker for the jira_agent repo (CLAUDE.md "Non-negotiable rules").

Parses source with ``ast`` (no imports, no execution) and prints a compact digest:
    [ERROR] G1 path:line  message
Exit code 1 when any ERROR is found, 0 otherwise (WARN never fails).

Usage: python check_guardrails.py [repo_root] [--json]
"""
from __future__ import annotations

import argparse
import ast
import json
import re
import sys
from dataclasses import asdict, dataclass
from pathlib import Path

SOURCE_DIRS = ("agent", "approval", "functions", "handlers", "jira", "slack", "util")
# The only places allowed to define or call the Jira worklog write.
WRITE_DEFINITION = "jira/jira_service.py"
WRITE_CALLER = "approval/approval_service.py"
SENSITIVE_NAME = re.compile(
    r"(token|secret|password|description|raw_body|signature|authorization|credential)", re.IGNORECASE)
LOG_METHODS = {"debug", "info", "warning", "warn", "error", "exception", "critical", "log"}
ALLOWED_RUNTIME_DEPS = {"boto3"}
FORBIDDEN_HTTP_LIBS = {"requests", "httpx", "aiohttp", "urllib3"}


@dataclass
class Finding:
    rule: str
    level: str  # ERROR | WARN
    path: str
    line: int
    message: str


class Checker:
    def __init__(self, root: Path):
        self.root = root
        self.findings: list[Finding] = []
        self.sources: dict[str, str] = {}
        for folder in SOURCE_DIRS:
            for file in sorted((root / folder).rglob("*.py")) if (root / folder).exists() else []:
                rel = file.relative_to(root).as_posix()
                self.sources[rel] = file.read_text(encoding="utf-8", errors="replace")

    def add(self, rule, level, path, line, message):
        self.findings.append(Finding(rule, level, path, line, message))

    # ------------------------------------------------------------------ rules
    def run(self) -> list[Finding]:
        for path, src in self.sources.items():
            try:
                tree = ast.parse(src, filename=path)
            except SyntaxError as exc:
                self.add("G0", "ERROR", path, exc.lineno or 0, f"Syntax error: {exc.msg}")
                continue
            self.g1_single_write_path(path, tree)
            self.g2_no_dynamic_dispatch(path, tree)
            self.g3_no_sensitive_logging(path, tree)
            self.g6_dynamodb_wire_format(path, tree, src)
            self.g7_http_timeouts(path, tree)
        self.g4_entry_authentication()
        self.g5_owner_bound_claim()
        self.g8_dependencies()
        return sorted(self.findings, key=lambda f: (f.level != "ERROR", f.rule, f.path, f.line))

    def g1_single_write_path(self, path, tree):
        """All worklog writes go through ApprovalService (after a PENDING -> EXECUTING claim)."""
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and _call_name(node) in {"create_worklog", "create_jira_worklog"}:
                if path != WRITE_CALLER:
                    self.add("G1", "ERROR", path, node.lineno,
                             f"`{_call_name(node)}()` called outside {WRITE_CALLER}: a Jira write that "
                             "bypasses the approval claim")
            if isinstance(node, ast.FunctionDef) and node.name in {"create_worklog", "create_jira_worklog"}:
                if path not in {WRITE_DEFINITION}:
                    self.add("G1", "ERROR", path, node.lineno,
                             f"`{node.name}` defined outside {WRITE_DEFINITION}: second write path")
            if isinstance(node, ast.Call) and _call_name(node) == "request" and _kwarg_str(node, "method") in {
                    "POST", "PUT", "DELETE"} and path not in {WRITE_DEFINITION, "jira/jira_client.py"}:
                url = _first_str(node)
                if url is None or "/worklog" in url:
                    self.add("G1", "ERROR", path, node.lineno,
                             "Jira write request issued outside jira_service.create_worklog")

    def g2_no_dynamic_dispatch(self, path, tree):
        """The model never chooses which function runs."""
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                name = _call_name(node)
                if name in {"eval", "exec", "__import__"} or (name == "import_module"):
                    self.add("G2", "ERROR", path, node.lineno, f"`{name}()` enables dynamic code execution")
                if name == "getattr" and path.startswith("agent/") and len(node.args) >= 2 \
                        and not isinstance(node.args[1], ast.Constant):
                    self.add("G2", "ERROR", path, node.lineno,
                             "getattr with a non-literal name in agent/: dispatch by model-supplied name")
            if isinstance(node, ast.Subscript) and isinstance(node.value, ast.Call) \
                    and _call_name(node.value) in {"globals", "locals", "vars"}:
                self.add("G2", "ERROR", path, node.lineno, "Lookup in globals()/locals(): dynamic dispatch")
        if path == "agent/tool_registry.py" and "ALLOWED_INTENTS" not in self.sources[path]:
            self.add("G2", "ERROR", path, 1, "ALLOWED_INTENTS allow-list is missing")

    def g3_no_sensitive_logging(self, path, tree):
        """Never log OAuth tokens, Slack request bodies or worklog descriptions."""
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            if isinstance(func, ast.Name) and func.id == "print":
                self.add("G3", "ERROR", path, node.lineno,
                         "print() in Lambda code goes to CloudWatch unfiltered; use logger with safe fields")
                continue
            if isinstance(func, ast.Attribute) and func.attr in LOG_METHODS and _looks_like_logger(func.value):
                for arg in list(node.args[1:]) + [kw.value for kw in node.keywords]:
                    for name in _names_in(arg):
                        if SENSITIVE_NAME.search(name):
                            self.add("G3", "ERROR", path, node.lineno,
                                     f"Logging `{name}` may expose tokens, request bodies or descriptions")
                if node.args and isinstance(node.args[0], ast.JoinedStr):
                    for name in _names_in(node.args[0]):
                        if SENSITIVE_NAME.search(name):
                            self.add("G3", "ERROR", path, node.lineno,
                                     f"f-string log message interpolates `{name}`")

    def g4_entry_authentication(self):
        """Slack signature on every Slack request; async self-invocations need ASYNC_TASK_SECRET."""
        entry = self.sources.get("functions/read_jira_lambda.py", "")
        security = self.sources.get("slack/slack_security.py", "")
        checks = [
            (entry, "functions/read_jira_lambda.py", "verify_slack_signature(", "Slack signature is not verified"),
            (entry, "functions/read_jira_lambda.py", "ASYNC_TASK_SECRET", "async tasks are not authenticated"),
            (entry, "functions/read_jira_lambda.py", "compare_digest", "task secret not compared in constant time"),
            (security, "slack/slack_security.py", "compare_digest", "signature not compared in constant time"),
        ]
        for src, path, needle, message in checks:
            if needle not in src:
                self.add("G4", "ERROR", path, 1, message)
        # Exact number: "> 3000" must not pass as "> 300".
        if not re.search(r">\s*300(?!\d)", security):
            self.add("G4", "ERROR", "slack/slack_security.py", 1, "replay window (300 s) check missing or widened")
        for path, src in self.sources.items():
            for node in ast.walk(ast.parse(src)):
                if isinstance(node, ast.Dict):
                    keys = {k.value for k in node.keys if isinstance(k, ast.Constant)}
                    if "_async_task" in keys and "_task_secret" not in keys:
                        self.add("G4", "ERROR", path, node.lineno,
                                 "async task payload built without _task_secret (403 at the entry point)")

    def g5_owner_bound_claim(self):
        """Only the creator may approve/reject; the claim is a conditional PENDING -> EXECUTING update."""
        path = "approval/approval_repository.py"
        src = self.sources.get(path, "")
        for needle, message in [
            ("slack_user_id = :owner", "claim/reject not bound to the approval owner"),
            ("#s = :pending", "claim/reject does not require status PENDING"),
            ("expires_at > :now", "claim/reject does not check expiry"),
            ("#s = :executing", "finish does not require status EXECUTING"),
        ]:
            if needle not in src:
                self.add("G5", "ERROR", path, 1, message)

    def g6_dynamodb_wire_format(self, path, tree, src):
        """A boto3 *resource* client serializes values itself; pre-serialized values get wrapped twice."""
        if "meta.client" not in src:
            return
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and _call_name(node) in {"TypeSerializer", "serialize"}:
                self.add("G6", "ERROR", path, node.lineno,
                         "Pre-serializing values for a resource client (`meta.client`): they are serialized "
                         "again ('operand type: M'). Pass plain Python values")
            if isinstance(node, ast.Dict):
                for key in node.keys:
                    if isinstance(key, ast.Constant) and key.value in {"S", "N", "M", "BOOL", "NULL", "L"} \
                            and len(node.keys) == 1:
                        self.add("G6", "ERROR", path, node.lineno,
                                 f"Typed attribute value {{'{key.value}': ...}} passed via a resource client")

    def g7_http_timeouts(self, path, tree):
        """External calls must have a timeout (Slack must get an ACK within 3 s)."""
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and _call_name(node) == "urlopen" \
                    and not any(kw.arg == "timeout" for kw in node.keywords):
                self.add("G7", "ERROR", path, node.lineno, "urlopen() without timeout")

    def g8_dependencies(self):
        """Runtime dependencies: stdlib + boto3 only."""
        req = self.root / "requirements.txt"
        if req.exists():
            for number, raw in enumerate(req.read_text(encoding="utf-8").splitlines(), 1):
                line = raw.split("#", 1)[0].strip()
                name = re.split(r"[<>=!~\[; ]", line, maxsplit=1)[0].lower() if line else ""
                if name and name not in ALLOWED_RUNTIME_DEPS and name != "botocore":
                    self.add("G8", "WARN", "requirements.txt", number, f"Runtime dependency `{name}` (stdlib + boto3 only)")
        for path, src in self.sources.items():
            for node in ast.walk(ast.parse(src)):
                mods = []
                if isinstance(node, ast.Import):
                    mods = [a.name.split(".")[0] for a in node.names]
                elif isinstance(node, ast.ImportFrom) and node.module:
                    mods = [node.module.split(".")[0]]
                for mod in mods:
                    if mod in FORBIDDEN_HTTP_LIBS:
                        self.add("G8", "ERROR", path, node.lineno, f"`{mod}` import: HTTP calls use the standard library")
                    if mod.startswith("_backup"):
                        self.add("G8", "ERROR", path, node.lineno, "Import from a _backup_* snapshot")


# ---------------------------------------------------------------------- helpers
def _call_name(node: ast.Call) -> str:
    func = node.func
    if isinstance(func, ast.Name):
        return func.id
    if isinstance(func, ast.Attribute):
        return func.attr
    return ""


def _kwarg_str(node: ast.Call, name: str):
    for kw in node.keywords:
        if kw.arg == name and isinstance(kw.value, ast.Constant) and isinstance(kw.value.value, str):
            return kw.value.value.upper()
    return None


def _first_str(node: ast.Call):
    if node.args and isinstance(node.args[0], ast.Constant) and isinstance(node.args[0].value, str):
        return node.args[0].value
    if node.args and isinstance(node.args[0], ast.JoinedStr):
        return "".join(v.value for v in node.args[0].values if isinstance(v, ast.Constant))
    return None


def _looks_like_logger(value: ast.AST) -> bool:
    return (isinstance(value, ast.Name) and value.id in {"logger", "logging", "log", "LOGGER"}) or \
        (isinstance(value, ast.Attribute) and value.attr in {"logger", "log"})


def _names_in(node: ast.AST) -> list[str]:
    names = []
    for sub in ast.walk(node):
        if isinstance(sub, ast.Name):
            names.append(sub.id)
        elif isinstance(sub, ast.Attribute):
            names.append(sub.attr)
        elif isinstance(sub, ast.Subscript) and isinstance(sub.slice, ast.Constant) \
                and isinstance(sub.slice.value, str):
            names.append(sub.slice.value)
    return names


def main() -> int:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    parser = argparse.ArgumentParser(description="Check jira_agent against its non-negotiable rules.")
    parser.add_argument("root", nargs="?", default=".", help="Repository root (default: current directory)")
    parser.add_argument("--json", action="store_true", help="Emit JSON instead of the text digest")
    args = parser.parse_args()
    root = Path(args.root).resolve()
    checker = Checker(root)
    if not checker.sources:
        print(f"No source files found under {root} ({', '.join(SOURCE_DIRS)})", file=sys.stderr)
        return 2
    findings = checker.run()
    errors = [f for f in findings if f.level == "ERROR"]
    if args.json:
        print(json.dumps([asdict(f) for f in findings], indent=2))
    else:
        print(f"# jira-agent guardrails: {len(checker.sources)} files, "
              f"{len(errors)} error(s), {len(findings) - len(errors)} warning(s)")
        for f in findings:
            print(f"- [{f.level}] {f.rule} {f.path}:{f.line}  {f.message}")
        if not findings:
            print("- [OK] All guardrails G1-G8 pass.")
    return 1 if errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
