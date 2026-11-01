"""Build a source release zip with a prebuilt frontend.

Produces ``dist/atr-release-<UTC date>.zip``. In a clean git checkout the file
list comes from ``git archive HEAD``; a dirty worktree or a non-git directory is
packed by walking the worktree. ``frontend/dist`` and the installers are always
overlaid so the package runs without Node.
"""
from __future__ import annotations

import argparse
import hashlib
import io
import json
import os
import subprocess
import sys
import zipfile
from datetime import datetime, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
INSTALLERS = ("install.ps1", "install.sh")
EXECUTABLE_FILES = {"install.sh", "scripts/atr"}
TOP_EXCLUDED = {
    ".git", ".venv", "venv", "data", "orbital", "output", "node_modules", "dist",
    "test-results", ".pytest_cache", ".ruff_cache", ".mypy_cache", ".uv-cache",
    "uploads", "videos", "htmlcov", ".superpowers", ".idea", ".vscode",
}
PART_EXCLUDED = {
    "__pycache__", ".pytest_cache", ".ruff_cache", ".mypy_cache", ".git",
    ".venv", "venv", "node_modules",
}
SUFFIX_EXCLUDED = (
    ".pyc", ".pyo", ".sqlite3", ".sqlite3-wal", ".sqlite3-shm",
    ".tsbuildinfo", ".log", ".webm",
)


class ReleaseError(RuntimeError):
    pass


def _run_git(root: Path, *args: str, text: bool = True):
    return subprocess.run(
        ["git", "-C", str(root), *args],
        capture_output=True, text=text, timeout=120,
    )


def git_head(root: Path) -> str | None:
    """Return HEAD only when root itself is the git toplevel."""
    try:
        completed = _run_git(root, "rev-parse", "--show-toplevel", "HEAD")
    except (OSError, subprocess.TimeoutExpired):
        return None
    if completed.returncode != 0:
        return None
    lines = completed.stdout.strip().splitlines()
    value = lines[-1].strip() if lines else ""
    if len(lines) < 2 or len(value) != 40 or any(c not in "0123456789abcdef" for c in value):
        return None
    try:
        return value if Path(lines[0]).resolve() == root.resolve() else None
    except OSError:
        return None


def git_is_dirty(root: Path) -> bool:
    try:
        completed = _run_git(root, "status", "--porcelain", "--untracked-files=no")
    except (OSError, subprocess.TimeoutExpired):
        return False
    return completed.returncode != 0 or bool(completed.stdout.strip())


def _excluded_name(name: str) -> bool:
    return name in TOP_EXCLUDED or name.startswith(".pytest-tmp-")


def _excluded_dir(rel: Path) -> bool:
    parts = rel.parts
    if not parts:
        return False
    if len(parts) == 1 and _excluded_name(parts[0]):
        return True
    return any(part in PART_EXCLUDED for part in parts)


def _include_file(rel: Path) -> bool:
    parts = rel.parts
    if not parts or _excluded_name(parts[0]):
        return False
    if any(part in PART_EXCLUDED for part in parts[:-1]):
        return False
    name = rel.name
    if name == ".env" or (name.startswith(".env.") and name != ".env.example"):
        return False
    if rel.as_posix().endswith(SUFFIX_EXCLUDED):
        return False
    if parts[0] == "targets":
        return rel.as_posix() == "targets/.gitkeep"
    return True


def collect_worktree_files(root: Path) -> dict[str, bytes]:
    """Collect package files by walking a worktree (no git required)."""
    entries: dict[str, bytes] = {}
    root = root.resolve()
    for directory, dirnames, filenames in os.walk(root):
        base = Path(directory)
        rel_dir = base.relative_to(root)
        dirnames[:] = sorted(
            name for name in dirnames if not _excluded_dir(rel_dir / name)
        )
        for name in sorted(filenames):
            path = base / name
            rel = path.relative_to(root)
            if path.is_symlink() or not path.is_file() or not _include_file(rel):
                continue
            entries[rel.as_posix()] = path.read_bytes()
    return entries


def collect_git_archive(root: Path) -> dict[str, bytes]:
    completed = _run_git(root, "archive", "--format=zip", "HEAD", text=False)
    if completed.returncode != 0:
        detail = completed.stderr.decode("utf-8", "replace").strip()
        raise ReleaseError(f"git archive 失败：{detail or completed.returncode}")
    with zipfile.ZipFile(io.BytesIO(completed.stdout)) as archive:
        return {
            name: archive.read(name)
            for name in archive.namelist()
            if not name.endswith("/") and _include_file(Path(name))
        }


def collect_dist_files(root: Path) -> dict[str, bytes]:
    dist = root / "frontend" / "dist"
    entries: dict[str, bytes] = {}
    for path in sorted(dist.rglob("*")):
        if path.is_symlink() or not path.is_file():
            continue
        entries[path.relative_to(root).as_posix()] = path.read_bytes()
    return entries


def read_build_manifest(root: Path) -> dict | None:
    path = root / "frontend" / "dist" / "build.json"
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return value if isinstance(value, dict) else None


def check_dist(root: Path, allow_stale_dist: bool = False, head: str | None = None) -> None:
    index = root / "frontend" / "dist" / "index.html"
    if not index.is_file():
        raise ReleaseError(
            "frontend/dist/index.html 不存在。请先在 frontend 目录执行 npm ci && npm run build，"
            "或使用已内置 dist 的源码目录。"
        )
    manifest = read_build_manifest(root)
    if manifest is None:
        print("警告：frontend/dist/build.json 缺失或无效，无法校验前端是否与当前提交一致。")
        return
    if head is None:
        head = git_head(root)
    commit = str(manifest.get("commit") or "")
    if not head:
        return
    if not commit:
        print("警告：build.json 未记录 commit，跳过前端新鲜度比对。")
        return
    if commit != head:
        if allow_stale_dist:
            print(f"警告：前端 commit {commit[:12]} 与 HEAD {head[:12]} 不一致，已按 --allow-stale-dist 继续。")
            return
        raise ReleaseError(
            f"frontend/dist 与当前 HEAD 不一致（build.json commit={commit[:12]}，HEAD={head[:12]}）。"
            "请先在 frontend 执行 npm run build；确需旧前端时加 --allow-stale-dist。"
        )


def build_release(
    root: Path = REPO,
    output: Path | None = None,
    allow_stale_dist: bool = False,
    day: str | None = None,
    use_git: bool | None = None,
) -> dict:
    root = root.resolve()
    check_dist(root, allow_stale_dist=allow_stale_dist)
    missing = [name for name in INSTALLERS if not (root / name).is_file()]
    if missing:
        raise ReleaseError(f"缺少安装脚本：{', '.join(missing)}")

    head = git_head(root)
    if use_git is None:
        use_git = bool(head) and not git_is_dirty(root)
        if head and not use_git:
            print("提示：工作树有未提交改动，改用工作树内容打包（不使用 git archive HEAD）。")
    entries = collect_git_archive(root) if use_git else collect_worktree_files(root)
    entries.update(collect_dist_files(root))
    for name in INSTALLERS:
        entries[name] = (root / name).read_bytes()

    names = sorted(entries)
    for name in names:
        if not _include_file(Path(name)):
            raise ReleaseError(f"发布包包含不允许的文件：{name}")

    stamp = day or datetime.now(timezone.utc).strftime("%Y%m%d")
    output = output or root / "dist" / f"atr-release-{stamp}.zip"
    output.parent.mkdir(parents=True, exist_ok=True)
    zip_time = datetime.now().timetuple()[:6]
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=6) as archive:
        for name in names:
            if name in EXECUTABLE_FILES:
                info = zipfile.ZipInfo(name, date_time=zip_time)
                info.create_system = 3  # Unix, so extractors honor external_attr
                info.compress_type = zipfile.ZIP_DEFLATED
                info.external_attr = (0o755 << 16) | 0o100000
                archive.writestr(info, entries[name])
            else:
                archive.writestr(name, entries[name])

    digest = hashlib.sha256(output.read_bytes()).hexdigest()
    return {
        "path": str(output),
        "sha256": digest,
        "files": len(names),
        "bytes": output.stat().st_size,
        "source": "git-archive" if use_git else "worktree",
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="构建内置前端产物的 ATR 发布包")
    parser.add_argument("--allow-stale-dist", action="store_true", help="允许 build.json 与 HEAD 不一致")
    parser.add_argument("--output", type=Path, help="输出 zip 路径（默认 dist/atr-release-<UTC 日期>.zip）")
    args = parser.parse_args(argv)
    try:
        result = build_release(output=args.output, allow_stale_dist=args.allow_stale_dist)
    except (ReleaseError, OSError) as exc:
        print(f"构建失败：{exc}", file=sys.stderr)
        return 1
    print(f"发布包：{result['path']}")
    print(f"SHA-256：{result['sha256']}")
    print(f"文件数：{result['files']}")
    print(f"字节数：{result['bytes']}")
    print(f"来源：{result['source']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
