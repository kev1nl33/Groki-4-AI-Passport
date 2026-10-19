"""Emotion-to-body mapping for Gemini Live expression calls."""

from __future__ import annotations

from dataclasses import dataclass

NEUTRAL_HEAD_YAW = 0
NEUTRAL_HEAD_PITCH = 15


@dataclass(frozen=True)
class EmotionPlan:
    """Gateway-owned physical plan for a model-level mood.

    ``head_pitch_offset`` and ``head_yaw_offset`` are relative to the neutral
    forward pose used by the simulator and gateway idle reset.
    """

    face: str
    led_rgb: tuple[int, int, int]
    head_pitch_offset: int = 0
    head_yaw_offset: int = 0

    @property
    def head_pitch(self) -> int:
        return max(0, min(60, NEUTRAL_HEAD_PITCH + self.head_pitch_offset))

    @property
    def head_yaw(self) -> int:
        return max(-90, min(90, NEUTRAL_HEAD_YAW + self.head_yaw_offset))


_EMOTION_PLANS: dict[str, dict[str, EmotionPlan]] = {
    "excited": {
        "low": EmotionPlan("happy", (255, 150, 35), head_pitch_offset=8),
        "high": EmotionPlan("happy", (255, 205, 60), head_pitch_offset=14),
    },
    "happy": {
        "low": EmotionPlan("happy", (190, 95, 25)),
        "high": EmotionPlan("happy", (245, 135, 35), head_pitch_offset=4),
    },
    "neutral": {
        "low": EmotionPlan("idle", (0, 0, 0)),
        "high": EmotionPlan("idle", (35, 35, 35)),
    },
    "tired": {
        "low": EmotionPlan("sleeping", (18, 28, 68), head_pitch_offset=-6),
        "high": EmotionPlan("sleeping", (28, 42, 95), head_pitch_offset=-10),
    },
    "sad": {
        "low": EmotionPlan("idle", (35, 18, 75), head_pitch_offset=-10),
        "high": EmotionPlan("idle", (58, 28, 115), head_pitch_offset=-14),
    },
    "curious": {
        "low": EmotionPlan("doubt", (0, 145, 165), head_pitch_offset=2, head_yaw_offset=10),
        "high": EmotionPlan("doubt", (0, 220, 235), head_pitch_offset=4, head_yaw_offset=18),
    },
    "apologetic": {
        "low": EmotionPlan("embarrassed", (120, 55, 12), head_pitch_offset=-8),
        "high": EmotionPlan("embarrassed", (170, 78, 22), head_pitch_offset=-12),
    },
}


MOODS = tuple(_EMOTION_PLANS)
INTENSITIES = ("low", "high")


def resolve(mood: str, intensity: str = "low") -> EmotionPlan:
    """Return the physical plan for a Gemini emotion request.

    Unknown moods fall back to ``neutral``. Unknown intensities fall back to
    ``low`` so model typos never produce unexpected high-energy motion.
    """
    normalized_mood = str(mood or "neutral").strip().lower()
    normalized_intensity = str(intensity or "low").strip().lower()
    if normalized_mood not in _EMOTION_PLANS:
        normalized_mood = "neutral"
    if normalized_intensity not in INTENSITIES:
        normalized_intensity = "low"
    return _EMOTION_PLANS[normalized_mood][normalized_intensity]
