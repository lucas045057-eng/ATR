"""Separate media interfaces. Never fabricate topology from an unsupported image/PDF."""

import re
from pathlib import PurePosixPath
from typing import Literal

from pydantic import Field, field_validator

from app.business.ingestion import UnsafeArtifact
from app.domain import StrictModel


class VisualRegion(StrictModel):
    region: list[float] = Field(min_length=4,max_length=4)

    @field_validator('region',mode='before')
    @classmethod
    def bounded_region(cls,value):
        if (not isinstance(value,list) or len(value)!=4 or any(type(x) not in {int,float} or not 0<=x<=1 for x in value)
                or not value[0]<value[2] or not value[1]<value[3]):
            raise ValueError('Region must be a normalized nonempty bounding box')
        return value


class VisualComponent(VisualRegion):
    id: str = Field(pattern=r'^[A-Za-z0-9_-]{1,40}$')
    name: str = Field(min_length=1,max_length=120)
    kind: Literal['component','datastore','interface','worker','proxy','unknown']


class VisualRelation(VisualRegion):
    source: str = Field(pattern=r'^[A-Za-z0-9_-]{1,40}$')
    target: str = Field(pattern=r'^[A-Za-z0-9_-]{1,40}$')
    relation: Literal['calls','reads','writes','depends_on','forwards','flow']
    direction: Literal['forward','unknown']
    certainty: Literal['explicit','uncertain']
    confidence: float = Field(ge=0,le=1,strict=True)


class VisualPage(StrictModel):
    summary: str = Field(max_length=1000)
    components: list[VisualComponent] = Field(max_length=30)
    relations: list[VisualRelation] = Field(max_length=50)
    unknowns: list[str] = Field(max_length=20)


def validate_visual_page(value):
    parsed=VisualPage.model_validate(value).model_dump()
    ids={component['id'] for component in parsed['components']}
    if len(ids)!=len(parsed['components']) or any(relation['source'] not in ids or relation['target'] not in ids for relation in parsed['relations']):
        raise ValueError('Visual references must identify supplied components')
    known=[]
    for relation in parsed['relations']:
        if relation['direction']=='forward' and relation['certainty']=='explicit' and relation['confidence']>=.8:
            known.append(relation)
        else:
            parsed['unknowns'].append(f"{relation['source']} → {relation['target']}：方向或含义不清楚，未生成关系事实")
    parsed['relations']=known
    parsed['unknowns']=[entry[:300] for entry in parsed['unknowns'][:30]]
    return parsed


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
