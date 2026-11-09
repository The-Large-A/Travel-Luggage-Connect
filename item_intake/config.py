"""Environment-driven settings for the depot smart-intake scan (documented in .env.example)."""
import os
from dataclasses import dataclass

from match_notify.config import _bool, _choice, _float, _int
from passenger_auth.config import ConfigError

PROVIDERS = ("florence", "basic")


@dataclass(frozen=True)
class IntakeConfig:
    # Who writes the description: "florence" = Florence-2 run locally on CPU
    # (offline; ~1 min per item on a laptop), "basic" = a sentence built from
    # the extracted attributes. The interface also leaves room for a cloud
    # provider (e.g. Claude Haiku) later without touching the pipeline.
    vision_provider: str = "florence"
    florence_model: str = "florence-community/Florence-2-base"
    # An attribute becomes a security question only at or above this confidence;
    # below it, it is written into the private notes only.
    answer_min_confidence: float = 0.6
    # Frame gate (thresholds tuned on the analysis-size frame, see vision.py).
    min_sharpness: float = 40.0
    min_brightness: float = 18.0
    max_brightness: float = 95.0
    min_view_distance: int = 10          # pHash Hamming distance: how different a new view must be
    target_views: int = 6
    max_views: int = 8
    max_inside: int = 4
    max_closeups: int = 4
    min_views_to_save: int = 2
    async_jobs: bool = True
    draft_ttl_hours: int = 24

    def __post_init__(self):
        if self.vision_provider not in PROVIDERS:
            raise ConfigError(f"INTAKE_VISION_PROVIDER must be one of {PROVIDERS}")
        if not 1 <= self.min_views_to_save <= self.target_views <= self.max_views:
            raise ConfigError("Require 1 <= INTAKE_MIN_VIEWS <= INTAKE_TARGET_VIEWS <= INTAKE_MAX_VIEWS.")

    @classmethod
    def from_env(cls, environ=None) -> "IntakeConfig":
        env = os.environ if environ is None else environ
        return cls(
            vision_provider=_choice(env, "INTAKE_VISION_PROVIDER", "florence", PROVIDERS),
            florence_model=(env.get("INTAKE_FLORENCE_MODEL") or "florence-community/Florence-2-base").strip(),
            answer_min_confidence=_float(env, "INTAKE_ANSWER_MIN_CONFIDENCE", 0.6, 0.3, 0.99),
            min_sharpness=_float(env, "INTAKE_MIN_SHARPNESS", 40.0, 1.0, 2000.0),
            target_views=_int(env, "INTAKE_TARGET_VIEWS", 6, 2, 12),
            max_views=_int(env, "INTAKE_MAX_VIEWS", 8, 2, 16),
            min_views_to_save=_int(env, "INTAKE_MIN_VIEWS", 2, 1, 12),
            async_jobs=_bool(env, "INTAKE_ASYNC", True),
        )
