"""Separate media interfaces. Never fabricate topology from an unsupported image/PDF."""

from app.business.ingestion import UnsafeArtifact


def parse_image(raw: bytes, suffix: str) -> dict:
    if suffix == ".png" and not raw.startswith(b"\x89PNG\r\n\x1a\n"):
        raise UnsafeArtifact("PNG 签名不匹配")
    if suffix in {".jpg", ".jpeg"} and not raw.startswith(b"\xff\xd8\xff"):
        raise UnsafeArtifact("JPEG 签名不匹配")
    return {"parse_state": "pending_manual", "reason": "尚未配置经过验证的图像解析适配器"}


def parse_pdf(raw: bytes) -> dict:
    if not raw.startswith(b"%PDF-"):
        raise UnsafeArtifact("PDF 签名不匹配")
    return {"parse_state": "pending_manual", "reason": "PDF 解析独立接口已保留；请人工录入部署摘要"}
