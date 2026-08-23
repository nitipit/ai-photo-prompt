from __future__ import annotations

import wave
from pathlib import Path

ROOT = Path(__file__).parents[2]
AUDIO_DIR = ROOT / "design" / "audio"
APP_BUILD = ROOT / "src" / "build" / "app.ts"
TEMPLATES = ROOT / "src" / "app" / "templates"
EXPECTED_DURATIONS = {
    "ui-click.wav": 0.16,
    "prompt-submit.wav": 0.48,
    "countdown-tick.wav": 0.14,
    "generation-complete.wav": 0.82,
    "score-reveal.wav": 1.10,
    "generation-error.wav": 0.62,
}


def test_approved_sound_assets_are_bounded_browser_wav_files() -> None:
    assert {path.name for path in AUDIO_DIR.glob("*.wav")} == set(EXPECTED_DURATIONS)

    for filename, expected_duration in EXPECTED_DURATIONS.items():
        with wave.open(str(AUDIO_DIR / filename), "rb") as audio:
            assert audio.getnchannels() == 1
            assert audio.getsampwidth() == 2
            assert audio.getframerate() == 44_100
            duration = audio.getnframes() / audio.getframerate()
        assert duration == expected_duration


def test_frontend_build_publishes_only_the_approved_sound_files() -> None:
    build_source = APP_BUILD.read_text(encoding="utf-8")

    assert 'const audioSourceRoot = new URL("design/audio/", projectRoot);' in build_source
    assert 'const audioOutputRoot = new URL("dist/audio/", projectRoot);' in build_source
    assert "await publishApprovedAudio();" in build_source
    for filename in EXPECTED_DURATIONS:
        assert build_source.count(f'"{filename}"') == 1


def test_sound_controls_are_persisted_accessible_and_non_blocking() -> None:
    sound_source = (TEMPLATES / "_sound.ts").read_text(encoding="utf-8")
    base_source = (TEMPLATES / "_base.ts").read_text(encoding="utf-8")

    assert 'const STORAGE_KEY = "photo-prompt:sound-muted";' in sound_source
    assert 'button.setAttribute("aria-pressed", String(muted));' in sound_source
    assert 'button.setAttribute("aria-label", muted ? "เปิดเสียง" : "ปิดเสียง");' in sound_source
    assert "catch (_error)" in sound_source
    assert "return false;" in sound_source
    assert "installSoundControls(root);" in base_source


def test_gameplay_scenes_use_each_approved_sound_cue_once_per_event() -> None:
    level_source = (TEMPLATES / "level.ts").read_text(encoding="utf-8")
    prompt_source = (TEMPLATES / "prompt.ts").read_text(encoding="utf-8")
    generating_source = (TEMPLATES / "generating.ts").read_text(encoding="utf-8")
    result_source = (TEMPLATES / "result.ts").read_text(encoding="utf-8")
    leaderboard_source = (TEMPLATES / "leaderboard.ts").read_text(encoding="utf-8")

    assert 'playSound("ui-click")' in level_source
    assert 'playSound("countdown-tick")' in prompt_source
    assert 'playSound("prompt-submit")' in generating_source
    assert 'playSound("generation-complete")' in generating_source
    assert 'playSound("generation-error")' in generating_source
    assert 'playSound("score-reveal")' in result_source
    assert 'playSound("score-reveal")' not in leaderboard_source
