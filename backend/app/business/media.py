"""Separate media interfaces. Never fabricate topology from an unsupported image/PDF."""

import re
from pathlib import PurePosixPath

from app.business.ingestion import UnsafeArtifact


def optional_image(path):
    name = PurePosixPath(path.replace("\\", "/")).stem.casefold()
    if re.search(r"architecture|topology|diagram|deployment|架构|拓扑|部署", path, re.I):
        return False
    return bool(re.fullmatch(r"(?:favicon(?:-\d+x\d+)?|icon|app-icon|apple-touch-icon|logo|decorative(?:-[a-z0-9_-]+)?|icon-[a-z0-9_-]+)", name)
                or re.search(r"(?:^|/)(?:icons|favicons|decorations)/", path, re.I))


def parse_image(raw: bytes, suffix: str, path="") -> dict:
    if suffix == ".png" and not raw.startswith(b"\x89PNG\r\n\x1a\n"):
        raise UnsafeArtifact("PNG 签名不匹配")
    if suffix in {".jpg", ".jpeg"} and not raw.startswith(b"\xff\xd8\xff"):
        raise UnsafeArtifact("JPEG 签名不匹配")
    pending = {"parse_state": "pending_manual", "reason": "尚未配置经过验证的图像解析适配器"}
    if optional_image(path):
        return {"parse_state": "optional_static_attachment", "media_role": "optional_static", "reason": "明确图标或装饰资源；未提取业务事实，可撤销分类",
                "optional_previous": pending, "classification_basis": "explicit_resource_path"}
    return pending


def parse_pdf(raw: bytes) -> dict:
    if not raw.startswith(b"%PDF-"):
        raise UnsafeArtifact("PDF 签名不匹配")
    return {"parse_state": "pending_manual", "reason": "PDF 解析独立接口已保留；请人工录入部署摘要"}
