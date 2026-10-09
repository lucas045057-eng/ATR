from __future__ import annotations

import ast
import hashlib
import os
import re
import time
from pathlib import Path

ROUTES = re.compile(r"(?:app|router)\.(get|post|put|delete|patch)\s*\(\s*['\"`]([^'\"`]+)")
ENV_REF = re.compile(r"(?:process\.env\.([A-Z0-9_]+)|os\.environ(?:\.get)?\s*\(?['\"]([A-Z0-9_]+))")


def describe_source(relative: str, text: str) -> dict:
    """Shared V1/V2 parser. AST inspection is local and never imports uploaded modules."""
    info = {"imports": [], "routes": [], "symbols": [], "environment_keys": []}
    info["client_calls"] = []
    if relative.endswith(".py"):
        try:
            tree = ast.parse(text, filename=relative)
            for count, item in enumerate(ast.walk(tree)):
                if count > 100000:
                    raise ValueError("AST node limit")
                if isinstance(item, ast.Import):
                    info["imports"].extend(alias.name for alias in item.names)
                elif isinstance(item, ast.ImportFrom):
                    info["imports"].append("." * item.level + (item.module or ""))
                elif (
                    isinstance(item, ast.Call)
                    and isinstance(item.func, ast.Attribute)
                    and isinstance(item.func.value, ast.Name)
                ):
                    if item.func.value.id in {
                        "pymysql",
                        "psycopg",
                        "psycopg2",
                        "redis",
                    } and item.func.attr in {"connect", "Redis", "from_url"}:
                        info["client_calls"].append(
                            {
                                "client": item.func.value.id,
                                "method": item.func.attr,
                                "line": item.lineno,
                            }
                        )
                elif isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                    info["symbols"].append(
                        {"name": item.name, "start": item.lineno, "end": item.end_lineno}
                    )
                    for decorator in getattr(item, "decorator_list", []):
                        if isinstance(decorator, ast.Call) and isinstance(
                            decorator.func, ast.Attribute
                        ):
                            if (
                                decorator.func.attr
                                in {"get", "post", "put", "delete", "patch", "route"}
                                and decorator.args
                            ):
                                arg = decorator.args[0]
                                if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
                                    info["routes"].append(
                                        {
                                            "method": decorator.func.attr.upper(),
                                            "path": arg.value[:240],
                                            "symbol": item.name,
                                            "line": item.lineno,
                                        }
                                    )
        except (SyntaxError, ValueError, RecursionError):
            info["parse_state"] = "partial_parser_error"
    else:
        info["imports"] = [
            a or b for a, b in re.findall(r"require\(['\"]([^'\"]+)|from\s+['\"]([^'\"]+)", text)
        ]
        info["routes"] = [{"method": m.upper(), "path": p[:240]} for m, p in ROUTES.findall(text)]
        for lineno, line in enumerate(text.splitlines(), 1):
            match = re.search(
                r"(?:function|class)\s+(\w+)|(?:export\s+)?(?:const|let)\s+(\w+)\s*=", line
            )
            if match:
                info["symbols"].append(
                    {"name": match.group(1) or match.group(2), "start": lineno, "end": lineno}
                )
    info["imports"] = sorted(set(info["imports"]))[:200]
    info["routes"] = info["routes"][:100]
    info["symbols"] = info["symbols"][:300]
    keys = {a or b for a, b in ENV_REF.findall(text)}
    keys.update(re.findall(r"os\.(?:getenv|environ\.get)\(['\"]([A-Z0-9_]+)", text))
    keys.update(re.findall(r"os\.environ\[['\"]([A-Z0-9_]+)", text))
    info["environment_keys"] = sorted(keys)[:100]
    return info


def _source_paths(root, stats):
    """Prune ignored directories before entering them; bound even irrelevant entries."""
    ignored = {".git", "node_modules", ".venv", "venv", "dist", "build", "__pycache__"}
    stack = [(root, 0)]
    deadline, entries_seen = time.monotonic() + 10, 0
    while stack:
        directory, depth = stack.pop()
        try:
            with os.scandir(directory) as entries:
                for entry in entries:
                    entries_seen += 1
                    if entries_seen > 20000 or time.monotonic() > deadline:
                        stats["traversal_limit_hit"] = True
                        return
                    if entry.name in ignored or entry.is_symlink():
                        continue
                    if entry.is_dir(follow_symlinks=False):
                        if depth < 16:
                            stack.append((Path(entry.path), depth + 1))
                        else:
                            stats["traversal_limit_hit"] = True
                    elif entry.is_file(follow_symlinks=False):
                        yield Path(entry.path)
        except OSError:
            continue


def inspect_source(root: Path, max_files: int, max_bytes: int) -> tuple[list[dict], dict[str, int]]:
    root = root.resolve()
    stats = {"traversal_limit_hit": False}
    rows: list[dict] = []
    total_bytes = 0
    visited = 0
    for path in _source_paths(root, stats):
        if not path.resolve().is_relative_to(root):
            continue
        if path.suffix.lower() not in {
            ".py",
            ".js",
            ".mjs",
            ".cjs",
            ".ts",
            ".yaml",
            ".yml",
            ".toml",
            ".json",
        }:
            continue
        visited += 1
        if visited > max_files or total_bytes >= max_bytes:
            break
        try:
            if path.is_symlink():
                continue
            with path.open("rb") as stream:
                content = stream.read(512001)
        except OSError:
            continue
        if len(content) > 512_000 or total_bytes + len(content) > max_bytes:
            continue
        total_bytes += len(content)
        text = content.decode("utf-8", errors="replace")
        relative = path.relative_to(root).as_posix()
        info: dict = {
            "path": relative,
            "bytes": len(content),
            "content_sha256": hashlib.sha256(content).hexdigest(),
            "routes": [],
            "imports": [],
            "environment_keys": [],
        }
        if path.suffix.lower() == ".py":
            try:
                tree = ast.parse(text, filename=relative)
                for node in ast.walk(tree):
                    if isinstance(node, ast.Import):
                        info["imports"].extend(alias.name.split(".")[0] for alias in node.names)
                    elif isinstance(node, ast.ImportFrom) and node.module:
                        info["imports"].append(node.module.split(".")[0])
                    elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                        if any(
                            isinstance(d, ast.Call)
                            and isinstance(d.func, ast.Attribute)
                            and d.func.attr in {"get", "post", "put", "delete", "patch"}
                            for d in node.decorator_list
                        ):
                            info["routes"].append(node.name)
            except SyntaxError:
                info["parse_state"] = "partial_syntax_error"
        elif path.suffix.lower() in {".js", ".mjs", ".cjs", ".ts"}:
            info["routes"] = [
                f"{method.upper()} {route}" for method, route in ROUTES.findall(text)[:100]
            ]
            imports = re.findall(r"require\(['\"]([^'\"]+)|from\s+['\"]([^'\"]+)", text)
            info["imports"] = sorted({a or b for a, b in imports})
        info["environment_keys"] = sorted({a or b for a, b in ENV_REF.findall(text)})[:100]
        info["imports"] = sorted(set(x for x in info["imports"] if isinstance(x, str)))[:100]
        rows.append(info)
    return rows, {
        "files_scanned": len(rows),
        "bytes_scanned": total_bytes,
        "file_limit_hit": visited > max_files,
        "byte_limit_hit": total_bytes >= max_bytes,
        **stats,
    }
