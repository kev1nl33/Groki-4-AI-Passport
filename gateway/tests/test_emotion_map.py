from __future__ import annotations

from stackchan_mcp.emotion_map import INTENSITIES, MOODS, EmotionPlan, resolve


def _brightness(plan: EmotionPlan) -> int:
    return sum(plan.led_rgb)


def _angle_amplitude(plan: EmotionPlan) -> int:
    return abs(plan.head_pitch_offset) + abs(plan.head_yaw_offset)


def test_resolve_covers_all_supported_moods_and_intensities():
    expected_faces = {
        "excited": "happy",
        "happy": "happy",
        "neutral": "idle",
        "tired": "sleeping",
        "sad": "idle",
        "curious": "doubt",
        "apologetic": "embarrassed",
    }

    assert set(MOODS) == set(expected_faces)
    assert INTENSITIES == ("low", "high")
    for mood, face in expected_faces.items():
        for intensity in INTENSITIES:
            plan = resolve(mood, intensity)
            assert plan.face == face
            assert all(0 <= channel <= 255 for channel in plan.led_rgb)
            assert 0 <= plan.head_pitch <= 60
            assert -90 <= plan.head_yaw <= 90


def test_curious_does_not_use_thinking_face():
    for intensity in INTENSITIES:
        assert resolve("curious", intensity).face != "thinking"
        assert resolve("curious", intensity).face == "doubt"


def test_unknown_mood_falls_back_to_neutral():
    assert resolve("confused", "low") == resolve("neutral", "low")


def test_unknown_intensity_falls_back_to_low():
    assert resolve("happy", "medium") == resolve("happy", "low")


def test_high_intensity_increases_brightness_and_motion_amplitude():
    low = resolve("curious", "low")
    high = resolve("curious", "high")

    assert _brightness(high) > _brightness(low)
    assert _angle_amplitude(high) > _angle_amplitude(low)


def test_tired_and_sad_use_low_head_pose():
    assert resolve("tired", "low").head_pitch < resolve("neutral", "low").head_pitch
    assert resolve("sad", "high").head_pitch < resolve("neutral", "low").head_pitch
