"""Helpers for the XiaoZhi ``llm.emotion`` device expression channel."""

from __future__ import annotations

LISTENING_EMOTION = "neutral"
WAKE_CONFIRM_EMOTION = "winking"
IDLE_EMOTION = "neutral"
DEAD_LED_RGB = (255, 0, 0)

SUPPORTED_DEVICE_EMOTIONS = frozenset(
    {
        "neutral",
        "happy",
        "sad",
        "angry",
        "doubt",
        "sleepy",
        "laughing",
        "funny",
        "loving",
        "delicious",
        "kissy",
        "winking",
        "silly",
        "crying",
        "shocked",
        "surprised",
        "thinking",
        "confused",
        "embarrassed",
        "relaxed",
        "cool",
        "confident",
    }
)

FACE_TO_DEVICE_EMOTION = {
    "idle": IDLE_EMOTION,
    "neutral": IDLE_EMOTION,
    "happy": "happy",
    "working_typing": "thinking",
    "juggling": "happy",
    "sweeping": IDLE_EMOTION,
    "sleeping": "sleepy",
    "sleepy": "sleepy",
    "embarrassed": "embarrassed",
    "error": "shocked",
    "notification": "surprised",
    "thinking": "thinking",
    "doubt": "doubt",
    "winking": "winking",
}


def normalize_device_emotion(emotion: str | None, *, default: str = IDLE_EMOTION) -> str:
    """Return a firmware-supported emotion name."""
    value = str(emotion or "").strip().lower()
    if value in SUPPORTED_DEVICE_EMOTIONS:
        return value
    return default


def face_to_device_emotion(face: str | None) -> str:
    """Map gateway avatar names onto firmware-supported ``llm.emotion`` names."""
    value = str(face or "").strip().lower()
    return FACE_TO_DEVICE_EMOTION.get(value, normalize_device_emotion(value))
