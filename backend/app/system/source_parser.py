from __future__ import annotations

import ast
import hashlib
import re
from pathlib import Path


ROUTES = re.compile(r"(?:app|router)\.(get|post|put|delete|patch)\s*\(\s*['\"`]([^'\"`]+)")
ENV_REF = re.compile(r"(?:process\.env\.([A-Z0-9_]+)|os\.environ(?:\.get)?\s*\(?['\"]([A-Z0-9_]+))")


def inspect_source(root: Path, max_files: int, max_bytes: int) -> tuple[list[dict], dict[str, int]]:
    root = root.resolve()
    ignored = {".git", "node_modules", ".venv", "venv", "dist", "build", "__pycache__"}
    rows: list[dict] = []
    total_bytes = 0
    visited = 0
    for path in root.rglob("*"):
        if any(part in ignored for part in path.parts) or not path.is_file():
            continue
        if not path.resolve().is_relative_to(root):
            continue
        if path.suffix.lower() not in {".py", ".js", ".mjs", ".cjs", ".ts", ".yaml", ".yml", ".toml", ".json"}:
            continue
        visited += 1
        if visited > max_files or total_bytes >= max_bytes:
            break
        try:
            content = path.read_bytes()
        except OSError:
            continue
        if len(content) > 512_000 or total_bytes + len(content) > max_bytes:
            continue
        total_bytes += len(content)
        text = content.decode("utf-8", errors="replace")
        relative = path.relative_to(root).as_posix()
        info: dict = {"path": relative, "bytes": len(content), "content_sha256": hashlib.sha256(content).hexdigest(),
                      "routes": [], "imports": [], "environment_keys": []}
        if path.suffix.lower() == ".py":
            try:
                tree = ast.parse(text, filename=relative)
                for node in ast.walk(tree):
                    if isinstance(node, ast.Import):
                        info["imports"].extend(alias.name.split(".")[0] for alias in node.names)
                    elif isinstance(node, ast.ImportFrom) and node.module:
                        info["imports"].append(node.module.split(".")[0])
                    elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                        if any(isinstance(d, ast.Call) and isinstance(d.func, ast.Attribute) and d.func.attr in {"get", "post", "put", "delete", "patch"} for d in node.decorator_list):
                            info["routes"].append(node.name)
            except SyntaxError:
                info["parse_state"] = "partial_syntax_error"
        elif path.suffix.lower() in {".js", ".mjs", ".cjs", ".ts"}:
            info["routes"] = [f"{method.upper()} {route}" for method, route in ROUTES.findall(text)[:100]]
            imports = re.findall(r"require\(['\"]([^'\"]+)|from\s+['\"]([^'\"]+)", text)
            info["imports"] = sorted({a or b for a, b in imports})
        info["environment_keys"] = sorted({a or b for a, b in ENV_REF.findall(text)})[:100]
        info["imports"] = sorted(set(x for x in info["imports"] if isinstance(x, str)))[:100]
        rows.append(info)
    return rows, {"files_scanned": len(rows), "bytes_scanned": total_bytes, "file_limit_hit": visited > max_files,
                  "byte_limit_hit": total_bytes >= max_bytes}
