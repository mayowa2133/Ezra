"""Brand kits: reusable look-and-feel (logo, fonts, colours, caption theme,
intro/outro, CTA, watermark, safe zones). Campaigns point at one; a render
spec may override any field."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field
from sqlalchemy import select

from . import db, security
from .db.models import BrandKit
from .storage import get_storage

POSITIONS = {"top-left", "top-right", "bottom-left", "bottom-right"}


class BrandKitSpec(BaseModel):
    name: str
    logo: str | None = Field(None, description="Path to a PNG/JPG logo")
    logo_position: str = "top-right"
    fonts: dict[str, str] = Field(default_factory=dict, description='{"caption": "/path/Font.ttf"}')
    colors: dict[str, str] = Field(default_factory=dict, description='{"primary": "#FFD400", "text": "#FFFFFF"}')
    caption_theme: str = "bold"
    intro: str | None = None
    outro: str | None = None
    default_layout: str = "auto"
    cta_text: str | None = None
    watermark_text: str | None = None
    safe_zone: dict[str, float] = Field(default_factory=dict)


def _store_asset(kit_name: str, kind: str, path: str | None, media: str) -> str | None:
    if not path:
        return None
    p = Path(path).expanduser().resolve()
    security.validate_media(p, "image" if media == "image" else "video")
    key = f"brand/{kit_name}/{kind}{p.suffix.lower()}"
    return get_storage().put_file(key, p)


def upsert(spec: BrandKitSpec) -> BrandKit:
    from .render.captions import THEMES

    if spec.logo_position not in POSITIONS:
        raise ValueError(f"logo_position must be one of {sorted(POSITIONS)}")
    if spec.caption_theme not in THEMES:
        raise ValueError(f"unknown caption theme {spec.caption_theme!r}; available: {sorted(THEMES)}")
    for role, font in spec.fonts.items():
        if not Path(font).expanduser().is_file():
            raise ValueError(f"font for {role!r} not found: {font}")
    with db.session() as s:
        kit = s.scalar(select(BrandKit).where(BrandKit.name == spec.name))
        if kit is None:
            kit = BrandKit(name=spec.name)
            s.add(kit)
        kit.logo_key = _store_asset(spec.name, "logo", spec.logo, "image") or kit.logo_key
        kit.intro_key = _store_asset(spec.name, "intro", spec.intro, "video") or kit.intro_key
        kit.outro_key = _store_asset(spec.name, "outro", spec.outro, "video") or kit.outro_key
        kit.logo_position = spec.logo_position
        kit.fonts = {k: str(Path(v).expanduser().resolve()) for k, v in spec.fonts.items()}
        kit.colors = spec.colors
        kit.caption_theme = spec.caption_theme
        kit.default_layout = spec.default_layout
        kit.cta_text = spec.cta_text
        kit.watermark_text = spec.watermark_text
        kit.safe_zone = spec.safe_zone
        s.flush()
        return kit


def get_brand_kit(ref: str | int) -> BrandKit:
    with db.session() as s:
        kit = s.scalar(select(BrandKit).where((BrandKit.name == str(ref)) |
                                              (BrandKit.id == (int(ref) if str(ref).isdigit() else -1))))
        if kit is None:
            raise LookupError(f"no brand kit {ref!r}")
        return kit


def list_kits() -> list[BrandKit]:
    with db.session() as s:
        return list(s.scalars(select(BrandKit).order_by(BrandKit.id)))


def to_dict(k: BrandKit) -> dict[str, Any]:
    return {"id": k.id, "name": k.name, "logo_key": k.logo_key, "logo_position": k.logo_position,
            "fonts": k.fonts, "colors": k.colors, "caption_theme": k.caption_theme,
            "intro_key": k.intro_key, "outro_key": k.outro_key, "default_layout": k.default_layout,
            "cta_text": k.cta_text, "watermark_text": k.watermark_text, "safe_zone": k.safe_zone}
