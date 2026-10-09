from __future__ import annotations

import hashlib
import re
import stat
import tarfile
import time
import zipfile
from pathlib import Path, PurePosixPath


class UnsafeArtifact(ValueError):
    pass


def safe_name(name: str, max_depth: int = 16) -> str:
    while name.startswith("./"):
        name = name[2:]
    if (
        not name
        or len(name) > 240
        or any(ord(c) < 32 for c in name)
        or any(c in name for c in '\\<>"|?*')
        or ":" in name
    ):
        raise UnsafeArtifact("非法资料路径")
    path = PurePosixPath(name)
    if path.is_absolute() or any(p in {"", ".", ".."} for p in name.rstrip("/").split("/")):
        raise UnsafeArtifact("资料路径越界")
    if len(path.parts) > max_depth:
        raise UnsafeArtifact("资料目录层级超限")
    for part in path.parts:
        if part.endswith((".", " ")) or re.match(
            r"(?i)^(con|prn|aux|nul|com[0-9]|lpt[0-9])(?:\.|$)", part
        ):
            raise UnsafeArtifact("资料路径包含保留名称")
    return path.as_posix()


def unpack(source: Path, destination: Path, filename: str, limits: dict) -> list[dict]:
    """Streaming extraction into a fresh, generated directory; no extractall, links or nested archives."""
    destination.mkdir(parents=True, exist_ok=False)
    deadline = time.monotonic() + limits["timeout"]
    total = 0
    count = 0
    seen = set()
    result = []

    def entry(name, size, is_dir, opener):
        nonlocal total, count
        if time.monotonic() > deadline:
            raise UnsafeArtifact("解包耗时超限")
        name = safe_name(name, limits["depth"])
        count += 1
        if count > limits["files"] or name.casefold() in seen:
            raise UnsafeArtifact("文件数量超限或重复路径")
        seen.add(name.casefold())
        if size < 0 or size > limits["file_bytes"] or total + size > limits["total_bytes"]:
            raise UnsafeArtifact("解包后资料大小超限")
        path = destination / name
        if is_dir:
            path.mkdir(parents=True, exist_ok=True)
            return
        path.parent.mkdir(parents=True, exist_ok=True)
        sha = hashlib.sha256()
        written = 0
        with opener() as stream, path.open("xb") as out:
            while True:
                if time.monotonic() > deadline:
                    raise UnsafeArtifact("解包耗时超限")
                chunk = stream.read(min(65536, limits["file_bytes"] - written + 1))
                if not chunk:
                    break
                written += len(chunk)
                total += len(chunk)
                if (
                    written > size
                    or written > limits["file_bytes"]
                    or total > limits["total_bytes"]
                ):
                    raise UnsafeArtifact("实际解包大小超限")
                sha.update(chunk)
                out.write(chunk)
        if written != size:
            raise UnsafeArtifact("资料长度与声明不一致")
        result.append({"path": name, "bytes": written, "content_digest": sha.hexdigest()})

    lowered = filename.lower()
    if lowered.endswith(".zip"):
        if not zipfile.is_zipfile(source):
            raise UnsafeArtifact("ZIP 文件签名不匹配")
        with zipfile.ZipFile(source) as archive:
            if len(archive.infolist()) > limits["files"]:
                raise UnsafeArtifact("压缩包文件数量超限")
            for member in archive.infolist():
                mode = member.external_attr >> 16
                if (
                    stat.S_IFMT(mode) not in {0, stat.S_IFREG, stat.S_IFDIR}
                    or member.flag_bits & 1
                    or member.file_size / max(1, member.compress_size) > 200
                ):
                    raise UnsafeArtifact("拒绝链接、特殊文件、加密文件或高压缩比资料")
                entry(
                    member.filename,
                    member.file_size,
                    member.is_dir(),
                    lambda m=member: archive.open(m),
                )
    elif lowered.endswith((".tar", ".tar.gz", ".tgz")):
        with tarfile.open(source, mode="r|*") as archive:
            for member in archive:
                if member.isdir() and member.name in {".", "./"}:
                    continue
                if not member.isfile() and not member.isdir():
                    raise UnsafeArtifact("TAR 不允许链接或特殊设备文件")
                entry(
                    member.name,
                    member.size,
                    member.isdir(),
                    lambda m=member: archive.extractfile(m),
                )
        if total / max(1, source.stat().st_size) > 200:
            raise UnsafeArtifact("TAR 压缩比超限")
    else:
        entry(filename, source.stat().st_size, False, lambda: source.open("rb"))
    return result
