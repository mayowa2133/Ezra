"""RenderSpec: the complete recipe for one clip version. Stored on the
ClipVersion so every published post can be traced back to the editing
choices that produced it (layout, captions, edits, brand, experiment)."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field, field_validator

ASPECTS = {"9:16": (1080, 1920), "1:1": (1080, 1080), "16:9": (1920, 1080), "4:5": (1080, 1350)}
LAYOUTS = {"auto", "track", "split", "blur", "center", "static"}
PUNCH_INS = {"none", "hook", "emphasis"}


class BrollInsert(BaseModel):
    at: float = Field(description="Seconds from clip start (edited timeline)")
    duration: float = Field(gt=0, le=10)
    asset_key: str = Field(description="Storage key of an authorized B-roll asset")
    query: str | None = None
    reason: str | None = None


class RenderSpec(BaseModel):
    aspect: str = "9:16"
    layout: str = "auto"
    crop_x: float | None = Field(None, ge=0, le=1, description="Pin the crop (0 left .. 1 right); overrides tracking")
    captions: bool = True
    caption_theme: str = "bold"
    caption_emoji: bool = False       # an emoji above captions with an illustratable word
    caption_font: str | None = None
    caption_size: int | None = Field(None, ge=24, le=200)
    caption_position: float | None = Field(None, ge=0.3, le=0.95, description="Vertical centre, fraction of height")
    highlight_keywords: list[str] = Field(default_factory=list)
    colors: dict[str, str] = Field(default_factory=dict, description="Overrides: text, active, keyword, box")
    hook_overlay: bool = True
    hook_text: str | None = None
    safe_zone: dict[str, float] = Field(default_factory=dict, description="top/bottom/left/right fractions")
    brand_kit_id: int | None = None
    logo_key: str | None = None
    logo_position: str = "top-right"
    watermark: str | None = None
    intro_key: str | None = None
    outro_key: str | None = None
    cta_text: str | None = None
    remove_silence: bool = True
    silence_threshold: float = Field(0.6, ge=0.2, le=3.0, description="Pauses longer than this are shortened")
    remove_fillers: bool = True
    normalize_audio: bool = True
    punch_in: str = "none"
    broll: list[BrollInsert] = Field(default_factory=list)
    thumbnail: bool = True
    variant: str | None = Field(None, description="Experiment variant key")
    platform: str | None = Field(None, description="Platform this variant targets")

    @field_validator("aspect")
    @classmethod
    def _aspect(cls, v: str) -> str:
        if v not in ASPECTS:
            raise ValueError(f"aspect must be one of {sorted(ASPECTS)}")
        return v

    @field_validator("layout")
    @classmethod
    def _layout(cls, v: str) -> str:
        if v not in LAYOUTS:
            raise ValueError(f"layout must be one of {sorted(LAYOUTS)}")
        return v

    @field_validator("punch_in")
    @classmethod
    def _punch(cls, v: str) -> str:
        if v not in PUNCH_INS:
            raise ValueError(f"punch_in must be one of {sorted(PUNCH_INS)}")
        return v

    @property
    def size(self) -> tuple[int, int]:
        return ASPECTS[self.aspect]

    def resolved_safe_zone(self) -> dict[str, float]:
        """Platform UI covers the top and (especially) bottom of vertical video."""
        base = {"top": 0.10, "bottom": 0.20, "left": 0.05, "right": 0.12} if self.aspect == "9:16" else \
            {"top": 0.06, "bottom": 0.10, "left": 0.04, "right": 0.04}
        return {**base, **self.safe_zone}


PLATFORM_PRESETS: dict[str, dict[str, Any]] = {
    "tiktok": {"aspect": "9:16", "max_duration": 600},
    "instagram": {"aspect": "9:16", "max_duration": 180},
    "youtube": {"aspect": "9:16", "max_duration": 180},
    "linkedin": {"aspect": "1:1", "max_duration": 600},
    "x": {"aspect": "16:9", "max_duration": 140},
    "facebook": {"aspect": "9:16", "max_duration": 90},
    "threads": {"aspect": "9:16", "max_duration": 300},
}


def spec_from_brand(base: RenderSpec, brand: Any | None, explicit: set[str] | None = None) -> RenderSpec:
    """Fill spec fields the caller did not set explicitly from a brand kit."""
    if brand is None:
        return base
    data = base.model_dump()
    set_fields = base.model_fields_set if explicit is None else explicit
    if "caption_theme" not in set_fields and brand.caption_theme:
        data["caption_theme"] = brand.caption_theme
    if "layout" not in set_fields and brand.default_layout:
        data["layout"] = brand.default_layout
    data["logo_key"] = data.get("logo_key") or brand.logo_key
    data["logo_position"] = data.get("logo_position") if "logo_position" in set_fields else brand.logo_position
    data["watermark"] = data.get("watermark") or brand.watermark_text
    data["intro_key"] = data.get("intro_key") or brand.intro_key
    data["outro_key"] = data.get("outro_key") or brand.outro_key
    data["cta_text"] = data.get("cta_text") or brand.cta_text
    data["colors"] = {**(brand.colors or {}), **data.get("colors", {})}
    data["safe_zone"] = {**(brand.safe_zone or {}), **data.get("safe_zone", {})}
    if not data.get("caption_font") and (brand.fonts or {}).get("caption"):
        data["caption_font"] = brand.fonts["caption"]
    data["brand_kit_id"] = brand.id
    return RenderSpec.model_validate(data)
