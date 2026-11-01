"""Operator CLI for the Adaptive Troubleshooting Runtime.

Run as ``python -m app.cli`` (the wrapper scripts under ``scripts/`` set
``PYTHONPATH``). Packaging is intentionally not changed: the repository uses
``package = false``, so no console entry point is generated.

The project root is resolved in this order: ``ATR_ROOT`` (test/automation
override), the current directory when it looks like an ATR checkout, then the
checkout that contains this module. ``atr serve`` changes into that root so the
default relative paths (``./data``, ``./demo``, ``.env``) resolve the same way
as a direct uvicorn start.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
import re
import secrets
import shutil
import socket
import sqlite3
import subprocess
import sys
import tempfile
from functools import lru_cache
from pathlib import Path

import uvicorn

from app import __version__

PROJECT_ROOT = Path(__file__).resolve().parents[2]
TOKEN_PATTERN = re.compile(r"(?m)^COLLECTOR_TOKEN=.*$")
BACKUP_NO_CLEANUP = {
    "Fresh destination and regular original paths required",
    "Backup destination cannot overwrite runtime or artifact files",
}
BACKUP_MESSAGES = {
    "Wait for active work or end waiting investigations before backup":
        "仍有排队、运行中或等待确认的任务，请先结束这些任务再备份",
    "Active investigation prevents consistent backup":
        "仍有进行中的调查，无法生成一致性备份",
    "Fresh destination and regular original paths required":
        "备份目标必须是不存在的新目录，且原始数据库与 artifacts 必须是普通路径",
    "Backup destination cannot overwrite runtime or artifact files":
        "备份目标不能覆盖运行时或资料目录",
    "Exact application SHA required":
        "无法确定应用版本 SHA（当前目录不是 git 仓库）",
    "Exact application SHA or version required":
        "无法确定应用版本（既不是 git 提交，也不是语义化版本）",
    "Valid application version required":
        "应用版本格式无效",
    "Unknown application SHA kind":
        "应用版本来源类型无效",
    "Runtime database not found":
        "找不到数据库文件，请检查 DATABASE_PATH",
    "Artifacts root not found":
        "找不到资料目录，请检查 ARTIFACT_ROOT",
}


def _project_root() -> Path:
    override = os.environ.get("ATR_ROOT")
    if override:
        return Path(override).expanduser().resolve()
    cwd = Path.cwd().resolve()
    if (cwd / ".env.example").is_file() or (cwd / "backend" / "app" / "main.py").is_file():
        return cwd
    return PROJECT_ROOT


def _settings_for(root: Path):
    from app.settings import Settings

    env_file = root / ".env"
    return Settings(_env_file=env_file if env_file.is_file() else None)


def _resolve(root: Path, path: Path) -> Path:
    return path if path.is_absolute() else (root / path)


def _wrapper(command: str) -> str:
    if os.name == "nt" and not os.environ.get("MSYSTEM"):
        return f".\\scripts\\atr.ps1 {command}"
    return f"./scripts/atr {command}"


def _validated_settings(root: Path):
    settings = _settings_for(root)
    settings.health_url_allowlist()
    roots = [item.strip() for item in settings.repository_roots.split(",") if item.strip()]
    if not roots:
        raise ValueError("REPOSITORY_ROOTS 未配置任何目录")
    for item in roots:
        if not _resolve(root, Path(item)).is_dir():
            raise ValueError(f"REPOSITORY_ROOTS 指向的目录不存在：{item}")
    return settings


def _random_token() -> str:
    return secrets.token_urlsafe(32)


def _render_env(text: str) -> str:
    token = _random_token()
    if TOKEN_PATTERN.search(text):
        return TOKEN_PATTERN.sub(f"COLLECTOR_TOKEN={token}", text, count=1)
    return text.rstrip("\n") + f"\nCOLLECTOR_TOKEN={token}\n"


def _data_path(root: Path) -> Path:
    try:
        database_path = _settings_for(root).database_path
    except Exception:
        database_path = Path("./data/runtime.sqlite3")
    return _resolve(root, database_path)


def _ensure_data_dirs(root: Path) -> None:
    _data_path(root).parent.mkdir(parents=True, exist_ok=True)
    try:
        artifact_root = _settings_for(root).artifact_root
    except Exception:
        artifact_root = Path("./data/artifacts")
    _resolve(root, artifact_root).mkdir(parents=True, exist_ok=True)


def _result(name: str, status: str, detail: str) -> dict[str, str]:
    return {"name": name, "status": status, "detail": detail}


def _port_is_free(port: int, host: str = "127.0.0.1") -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        try:
            sock.bind((host, port))
        except OSError:
            return False
    return True


def _docker_result(name: str, arguments: list[str], missing: str) -> dict[str, str]:
    try:
        completed = subprocess.run(arguments, capture_output=True, text=True, timeout=5)
    except (OSError, subprocess.TimeoutExpired):
        return _result(name, "WARN", missing)
    if completed.returncode != 0:
        return _result(name, "WARN", "不可用；只有 Docker 部署方式需要")
    first_line = completed.stdout.strip().splitlines()[0] if completed.stdout.strip() else "可用"
    return _result(name, "PASS", first_line[:120])


@lru_cache(maxsize=1)
def _backup_module():
    script = PROJECT_ROOT / "scripts" / "backup_runtime.py"
    spec = importlib.util.spec_from_file_location("atr_backup_runtime", script)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"无法加载备份脚本：{script}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _application_sha(root: Path) -> tuple[str, str]:
    """Return ``(value, kind)``; kind is ``git`` only when root itself is the repo."""
    try:
        completed = subprocess.run(
            ["git", "-C", str(root), "rev-parse", "--show-toplevel", "HEAD"],
            capture_output=True, text=True, timeout=5,
        )
    except (OSError, subprocess.TimeoutExpired):
        completed = None
    if completed is not None and completed.returncode == 0:
        lines = completed.stdout.strip().splitlines()
        value = lines[-1].strip() if lines else ""
        if len(lines) >= 2 and len(value) == 40 and all(c in "0123456789abcdef" for c in value):
            try:
                top = Path(lines[0]).resolve()
            except OSError:
                top = None
            if top == root.resolve():
                return value, "git"
    return __version__, "version"


def cmd_init(_: argparse.Namespace) -> int:
    root = _project_root()
    env_path = root / ".env"
    env_existed = env_path.exists()
    if not env_existed:
        example = root / ".env.example"
        if not example.is_file():
            print(f"未找到 {example}，无法生成 .env。")
            return 1
        try:
            rendered = _render_env(example.read_text(encoding="utf-8"))
        except OSError as exc:
            print(f"初始化失败：无法读取 .env.example（{exc.__class__.__name__}）。")
            return 1
    created_dirs = []
    gitkeep = root / "targets" / ".gitkeep"
    gitkeep_existed = gitkeep.exists()
    try:
        if env_existed:
            print("已存在 .env，跳过（不会覆盖）。")
        else:
            env_path.write_text(rendered, encoding="utf-8")
            print("已生成 .env（COLLECTOR_TOKEN 为随机值）。")
        for directory in (root / "data", root / "data" / "artifacts", root / "targets"):
            existed = directory.exists()
            directory.mkdir(parents=True, exist_ok=True)
            if not existed:
                created_dirs.append(directory)
        gitkeep.touch(exist_ok=True)
    except OSError as exc:
        if not gitkeep_existed:
            try:
                gitkeep.unlink()
            except OSError:
                pass
        for directory in reversed(created_dirs):
            try:
                directory.rmdir()
            except OSError:
                pass
        if not env_existed:
            try:
                env_path.unlink()
            except OSError:
                pass
        print(f"初始化失败：{exc.__class__.__name__}（{exc}）。本次创建的文件已回滚。")
        return 1
    print(f"项目目录：{root}")
    print("下一步：")
    print(f"  {_wrapper('serve')}    启动 API 与工作台（默认 127.0.0.1:8000）")
    print(f"  {_wrapper('doctor')}   体检配置、端口、目录与 Docker")
    return 0


def cmd_serve(args: argparse.Namespace) -> int:
    root = _project_root()
    _ensure_data_dirs(root)
    if not (root / "frontend" / "dist" / "index.html").is_file():
        print("警告：未找到 frontend/dist/index.html；API 仍可用，但没有工作台。", flush=True)
        print("      git clone 方式请先在 frontend 目录执行 npm ci && npm run build；", flush=True)
        print("      用发布包/源码包安装时 dist 已内置，无需安装 Node。", flush=True)
    backend = PROJECT_ROOT / "backend"
    if str(backend) not in sys.path:
        sys.path.insert(0, str(backend))
    os.chdir(root)
    print(f"访问地址：http://{args.host}:{args.port}", flush=True)
    print(f"健康检查：http://{args.host}:{args.port}/api/v1/health", flush=True)
    uvicorn.run("app.main:app", host=args.host, port=args.port, workers=1)
    return 0


def cmd_doctor(args: argparse.Namespace) -> int:
    root = _project_root()
    checks: list[dict[str, str]] = []

    version = sys.version_info
    checks.append(_result(
        "Python", "PASS" if version >= (3, 11) else "FAIL",
        f"{version.major}.{version.minor}.{version.micro}" + ("" if version >= (3, 11) else "；需要 3.11 或更高版本"),
    ))

    dist = root / "frontend" / "dist" / "index.html"
    checks.append(_result(
        "frontend/dist", "PASS" if dist.is_file() else "WARN",
        "工作台可用" if dist.is_file() else "缺少 dist；API 仍可用，但没有工作台",
    ))

    port_free = _port_is_free(args.port)
    checks.append(_result(
        f"端口 {args.port}", "PASS" if port_free else "FAIL",
        "空闲" if port_free else f"已被占用；改用 {_wrapper('serve --port <其它端口>')} 启动",
    ))

    data_dir = _data_path(root).parent
    try:
        data_dir.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(dir=data_dir, delete=True):
            pass
        checks.append(_result("数据目录", "PASS", f"可写：{data_dir}"))
    except OSError as exc:
        checks.append(_result("数据目录", "FAIL", f"不可写：{exc.__class__.__name__}"))

    env_file = root / ".env"
    checks.append(_result(
        ".env", "PASS" if env_file.is_file() else "WARN",
        str(env_file) if env_file.is_file() else "不存在；将使用默认配置",
    ))

    settings = None
    try:
        settings = _settings_for(root)
    except Exception as exc:
        checks.append(_result("配置解析", "FAIL", f"{type(exc).__name__}: {exc}"))
    if settings is not None:
        token = settings.collector_token.get_secret_value()
        token_ok = len(token) >= 32 and "replace-this" not in token
        checks.append(_result(
            "COLLECTOR_TOKEN", "PASS" if token_ok else "WARN",
            "有效" if token_ok else "未配置或无效；Docker 只读工具不可用，其余功能正常",
        ))
        missing = []
        for item in settings.repository_roots.split(","):
            item = item.strip()
            if item:
                path = _resolve(root, Path(item))
                if not path.is_dir():
                    missing.append(item)
        checks.append(_result(
            "repository roots", "FAIL" if missing else "PASS",
            f"缺少目录：{', '.join(missing)}" if missing else settings.repository_roots,
        ))

    checks.append(_docker_result(
        "Docker Engine", ["docker", "info", "--format", "{{.OSType}}"], "未找到 docker 命令；仅 Docker 部署需要",
    ))
    checks.append(_docker_result(
        "Docker Compose", ["docker", "compose", "version"], "未找到 docker compose；仅 Docker 部署需要",
    ))

    ok = not any(check["status"] == "FAIL" for check in checks)
    if args.json:
        print(json.dumps({"ok": ok, "checks": checks}, ensure_ascii=False, indent=2))
    else:
        for check in checks:
            print(f"[{check['status']}] {check['name']}: {check['detail']}")
        print("体检通过。" if ok else "存在 FAIL 项，请先修复。")
    return 0 if ok else 1


def _discard_partial_backup(destination: Path, existed: bool) -> bool:
    if existed or destination.is_symlink() or not destination.is_dir():
        return False
    shutil.rmtree(destination, ignore_errors=True)
    return not destination.exists()


def cmd_backup(args: argparse.Namespace) -> int:
    root = _project_root()
    destination = _resolve(root, args.out).resolve()
    try:
        settings = _validated_settings(root)
    except Exception as exc:
        print(f"备份失败：无法解析配置（{type(exc).__name__}: {exc}）。")
        print(f"请检查 {root / '.env'}，并参考 docs/deployment-new-machine-runbook.md；未创建任何备份文件。")
        return 1
    if args.data:
        data = _resolve(root, args.data).resolve()
        database = data / "runtime.sqlite3"
        artifacts = data / "artifacts"
    else:
        database = _resolve(root, settings.database_path).resolve()
        artifacts = _resolve(root, settings.artifact_root).resolve()
        data = database.parent
    application_sha, sha_kind = _application_sha(root)
    existed = destination.exists()
    try:
        result = _backup_module().backup(
            data, destination, application_sha,
            database=database, artifacts=artifacts, application_sha_kind=sha_kind,
        )
    except ValueError as exc:
        message = BACKUP_MESSAGES.get(str(exc), str(exc))
        cleaned = False if str(exc) in BACKUP_NO_CLEANUP else _discard_partial_backup(destination, existed)
        print(f"备份失败：{message}" + ("；未完成的备份目录已清理" if cleaned else ""))
        print(f"如需体检，可运行 {_wrapper('doctor')}。")
        return 1
    except (OSError, sqlite3.Error) as exc:
        cleaned = _discard_partial_backup(destination, existed)
        print(f"备份失败：无法读取数据库或写入备份目录（{exc.__class__.__name__}）"
              + ("；未完成的备份目录已清理" if cleaned else ""))
        return 1
    print(f"备份完成：{result['destination']}")
    print(f"应用版本：{application_sha}（来源：{sha_kind}）")
    print(f"数据库 SHA-256：{result['database_sha256']}")
    print(f"资料文件数：{result['artifact_file_count']}")
    return 0


def cmd_version(_: argparse.Namespace) -> int:
    root = _project_root()
    print(f"版本：{__version__}")
    print(f"构建：{os.environ.get('ATR_BUILD', '') or '(未设置)'}")
    try:
        print(f"决策协议：{_settings_for(root).agent_decision_protocol}")
    except Exception as exc:
        print(f"决策协议：(配置无效：{type(exc).__name__})")
    print(f"工作台：{'可用' if (root / 'frontend' / 'dist' / 'index.html').is_file() else '不可用（frontend/dist 缺失）'}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="atr", description="ATR 操作员命令行")
    commands = parser.add_subparsers(dest="command", required=True)

    init = commands.add_parser("init", help="生成 .env 随机 token 并创建数据目录")
    init.set_defaults(handler=cmd_init)

    serve = commands.add_parser("serve", help="单 worker 启动 API 与工作台")
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=8000)
    serve.set_defaults(handler=cmd_serve)

    doctor = commands.add_parser("doctor", help="体检配置、端口、目录与 Docker")
    doctor.add_argument("--port", type=int, default=8000)
    doctor.add_argument("--json", action="store_true")
    doctor.set_defaults(handler=cmd_doctor)

    backup = commands.add_parser("backup", help="生成停机一致的运行时备份")
    backup.add_argument("--out", type=Path, required=True)
    backup.add_argument("--data", type=Path)
    backup.set_defaults(handler=cmd_backup)

    version = commands.add_parser("version", help="打印版本与构建信息")
    version.set_defaults(handler=cmd_version)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return args.handler(args)


if __name__ == "__main__":
    raise SystemExit(main())
