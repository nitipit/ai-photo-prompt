"""Generate the reviewable Photo Prompt arcade sound candidates."""

from __future__ import annotations

import math
import random
import wave
from collections.abc import Callable
from pathlib import Path

SAMPLE_RATE = 44_100
PEAK_AMPLITUDE = 0.32
OUTPUT_DIR = Path(__file__).parent
Waveform = Callable[[float], float]


def sine(phase: float) -> float:
    return math.sin(phase * math.tau)


def square(phase: float) -> float:
    return 1.0 if phase % 1.0 < 0.5 else -1.0


def triangle(phase: float) -> float:
    return 1.0 - 4.0 * abs((phase % 1.0) - 0.5)


def envelope(position: float, duration: float, *, attack: float, release: float) -> float:
    if position < 0.0 or position >= duration:
        return 0.0
    attack_gain = min(1.0, position / max(attack, 1 / SAMPLE_RATE))
    release_gain = min(1.0, (duration - position) / max(release, 1 / SAMPLE_RATE))
    return attack_gain * release_gain


def tone(
    samples: list[float],
    *,
    start: float,
    duration: float,
    frequency: float,
    gain: float,
    waveform: Waveform = square,
    end_frequency: float | None = None,
    attack: float = 0.006,
    release: float = 0.04,
) -> None:
    first = round(start * SAMPLE_RATE)
    count = round(duration * SAMPLE_RATE)
    phase = 0.0
    final_frequency = frequency if end_frequency is None else end_frequency
    for offset in range(count):
        index = first + offset
        if index >= len(samples):
            break
        position = offset / SAMPLE_RATE
        progress = position / duration
        current_frequency = frequency + (final_frequency - frequency) * progress
        phase += current_frequency / SAMPLE_RATE
        samples[index] += (
            waveform(phase) * gain * envelope(position, duration, attack=attack, release=release)
        )


def noise_burst(
    samples: list[float], *, start: float, duration: float, gain: float, seed: int
) -> None:
    random_source = random.Random(seed)
    first = round(start * SAMPLE_RATE)
    count = round(duration * SAMPLE_RATE)
    for offset in range(count):
        index = first + offset
        if index >= len(samples):
            break
        position = offset / SAMPLE_RATE
        samples[index] += (
            random_source.uniform(-1.0, 1.0)
            * gain
            * envelope(position, duration, attack=0.002, release=duration * 0.8)
        )


def new_sound(duration: float) -> list[float]:
    return [0.0] * round(duration * SAMPLE_RATE)


def ui_click() -> list[float]:
    samples = new_sound(0.16)
    tone(samples, start=0.00, duration=0.07, frequency=880, gain=0.7, waveform=square)
    tone(samples, start=0.055, duration=0.085, frequency=1320, gain=0.7, waveform=triangle)
    return samples


def prompt_submit() -> list[float]:
    samples = new_sound(0.48)
    for index, frequency in enumerate((523.25, 659.25, 783.99, 1046.50)):
        start = index * 0.085
        tone(
            samples,
            start=start,
            duration=0.16,
            frequency=frequency,
            gain=0.52,
            waveform=square,
            release=0.07,
        )
        tone(
            samples,
            start=start,
            duration=0.18,
            frequency=frequency / 2,
            gain=0.24,
            waveform=triangle,
            release=0.09,
        )
    return samples


def countdown_tick() -> list[float]:
    samples = new_sound(0.14)
    noise_burst(samples, start=0.0, duration=0.025, gain=0.18, seed=17)
    tone(
        samples,
        start=0.0,
        duration=0.11,
        frequency=1180,
        end_frequency=920,
        gain=0.75,
        waveform=square,
        attack=0.002,
        release=0.075,
    )
    return samples


def generation_complete() -> list[float]:
    samples = new_sound(0.82)
    notes = (523.25, 659.25, 783.99, 1046.50, 1318.51)
    for index, frequency in enumerate(notes):
        start = index * 0.095
        tone(
            samples,
            start=start,
            duration=0.29,
            frequency=frequency,
            gain=0.46,
            waveform=triangle,
            release=0.18,
        )
        tone(
            samples,
            start=start,
            duration=0.18,
            frequency=frequency * 2,
            gain=0.17,
            waveform=square,
            release=0.11,
        )
    return samples


def score_reveal() -> list[float]:
    samples = new_sound(1.10)
    for start, frequency in ((0.00, 392.00), (0.13, 523.25), (0.26, 659.25), (0.39, 783.99)):
        tone(
            samples,
            start=start,
            duration=0.22,
            frequency=frequency,
            gain=0.42,
            waveform=square,
            release=0.10,
        )
    for frequency, gain in ((523.25, 0.34), (659.25, 0.30), (783.99, 0.28), (1046.50, 0.22)):
        tone(
            samples,
            start=0.56,
            duration=0.48,
            frequency=frequency,
            gain=gain,
            waveform=triangle,
            release=0.30,
        )
    return samples


def generation_error() -> list[float]:
    samples = new_sound(0.62)
    for index, frequency in enumerate((392.00, 311.13, 261.63)):
        tone(
            samples,
            start=index * 0.13,
            duration=0.25,
            frequency=frequency,
            end_frequency=frequency * 0.94,
            gain=0.48,
            waveform=triangle,
            attack=0.008,
            release=0.14,
        )
    return samples


def write_wav(path: Path, samples: list[float]) -> None:
    peak = max(abs(sample) for sample in samples) or 1.0
    scale = PEAK_AMPLITUDE / peak
    frames = bytearray()
    for sample in samples:
        value = round(max(-1.0, min(1.0, sample * scale)) * 32_767)
        frames.extend(value.to_bytes(2, byteorder="little", signed=True))
    with wave.open(str(path), "wb") as output:
        output.setnchannels(1)
        output.setsampwidth(2)
        output.setframerate(SAMPLE_RATE)
        output.writeframes(frames)


def main() -> None:
    sounds = {
        "ui-click.wav": ui_click(),
        "prompt-submit.wav": prompt_submit(),
        "countdown-tick.wav": countdown_tick(),
        "generation-complete.wav": generation_complete(),
        "score-reveal.wav": score_reveal(),
        "generation-error.wav": generation_error(),
    }
    for filename, samples in sounds.items():
        write_wav(OUTPUT_DIR / filename, samples)
        print(f"generated {filename} ({len(samples) / SAMPLE_RATE:.2f}s)")


if __name__ == "__main__":
    main()
