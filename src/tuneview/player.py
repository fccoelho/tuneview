"""Playback: melody and a simple comping band, with optional swing.

Everything is planned in quarter notes first (melody from Verovio's timemap, chords
from the chart, bar starts from Verovio's timemap), then the same swing warp,
tempo map and speed turn it into seconds — so the sound and the bar highlight
can never drift apart.

The sound is made in Python. The desktop client plays it with miniaudio, which
bundles its own audio backend, because Flet's Linux desktop build has no audio
plugin; the web version sends the PCM to the browser and plays it there.
"""

from __future__ import annotations

import bisect
import io
import os
import re
import threading
import wave
from collections.abc import Callable
from dataclasses import dataclass

import numpy as np

from .engraver import ChartBar, split_chord

SAMPLE_RATE = 44100

# Share of the beat given to the first of two eighth notes. Swing is opt-in:
# "Off" is the default; "Auto" uses should_swing() to pick Medium or Off per tune.
SWING = {"Off": 0.5, "Auto": None, "Light": 0.58, "Medium": 0.64, "Hard": 0.7}

MELODY, CHORDS = "melody", "chords"
PLAY_MODES = {
    "melody": "Melody",
    "both": "Melody + chords",
    "chords": "Chords only",
}


# ----------------------------------------------------------------------------
# Timing
# ----------------------------------------------------------------------------
@dataclass(frozen=True)
class QNote:
    start: float  # quarter notes from the top
    end: float
    pitch: int  # MIDI note number
    voice: str = "melody"  # melody | chords | bass
    velocity: float = 1.0


@dataclass(frozen=True)
class Note:
    start: float  # seconds
    duration: float
    pitch: int
    voice: str = "melody"
    velocity: float = 1.0


class TempoMap:
    """Quarter-note position -> seconds, from (position, seconds per quarter) changes."""

    def __init__(self, changes: list[tuple[float, float]]):
        changes = sorted(changes) or [(0.0, 0.5)]
        if changes[0][0] > 0:
            changes.insert(0, (0.0, changes[0][1]))
        self.qs = [q for q, _ in changes]
        self.spq = [s for _, s in changes]
        self.secs = [0.0]
        for i in range(1, len(changes)):
            self.secs.append(self.secs[-1] + (self.qs[i] - self.qs[i - 1]) * self.spq[i - 1])

    def seconds(self, q: float) -> float:
        i = bisect.bisect_right(self.qs, q) - 1
        return self.secs[i] + (q - self.qs[i]) * self.spq[i]


def swing(q: float, ratio: float) -> float:
    """Warp a position so each beat's first eighth takes `ratio` of the beat.

    ratio 0.5 is straight; 2/3 is a triplet swing. The warp is continuous, so
    durations stretch and shrink with their notes.
    """
    if ratio == 0.5:
        return q
    beat, frac = divmod(q, 1.0)
    if frac < 0.5:
        return beat + frac * 2 * ratio
    return beat + ratio + (frac - 0.5) * 2 * (1 - ratio)


def melody(notes: list[tuple[float, float, int]], *, transpose: int = 0) -> list[QNote]:
    """Engraver melody tuples -> QNotes, shifted by transpose semitones."""
    return [QNote(s, e, p + transpose) for s, e, p in notes]


def tempo_map(tempos: list[tuple[float, float]]) -> TempoMap:
    """From (position in quarters, beats per minute) changes; ♩ = 120 if none."""
    return TempoMap([(q, 60.0 / bpm) for q, bpm in tempos if bpm > 0] or [(0.0, 0.5)])


def should_swing(notes: list[QNote], time_signature: str, words: str = "") -> bool:
    """A guess at whether a tune swings: quarter-note meter, mostly eighth-note
    motion (sixteenth-heavy melodies are funk, fusion or latin) and no straight-feel
    marking in the chart."""
    if not time_signature.endswith(("/4", "/2")):
        return False  # 6/8, 12/8… are already in three
    if re.search(r"latin|bossa|samba|straight|even 8|funk|rock|calypso|afro", words, re.I):
        return False
    if not notes:
        return True
    short = sum(1 for n in notes if (n.end - n.start) < 0.375)
    sixteenths = sum(1 for n in notes if (n.start * 4) % 2 == 1)  # on an odd 16th
    return sixteenths / len(notes) < 0.12 and short / len(notes) < 0.75


# ----------------------------------------------------------------------------
# Chords -> voicings
# ----------------------------------------------------------------------------
PITCH_CLASS = {"C": 0, "D": 2, "E": 4, "F": 5, "G": 7, "A": 9, "B": 11}
ACCIDENTAL = {"♭": -1, "b": -1, "♯": 1, "#": 1, "𝄪": 2, "𝄫": -2}


def pitch_class(name: str) -> int | None:
    if not name or name[0] not in PITCH_CLASS:
        return None
    return (PITCH_CLASS[name[0]] + sum(ACCIDENTAL.get(c, 0) for c in name[1:])) % 12


def chord_tones(quality: str) -> list[int]:
    """Intervals above the root for a chord-symbol quality, most important first:
    3rd (or sus), 7th/6th, extensions and alterations, then the 5th."""
    q = quality.replace(" ", "").replace("(", "").replace(")", "").replace(",", "")
    q = q.replace("#", "♯").replace("b5", "♭5").replace("b9", "♭9").replace("b13", "♭13")
    lower = q.lower()

    minor = bool(re.match(r"(min|mi|m(?!a)|-)", q)) and not lower.startswith("maj")
    dim = lower.startswith("dim") or q.startswith("°") or "o7" == lower[:2]
    half_dim = "ø" in q or ("7♭5" in q and minor)
    aug = lower.startswith(("aug", "+")) or "♯5" in q
    sus = "sus" in lower
    major7 = bool(re.search(r"maj|ma7|ma9|ma13|Δ|△|^M(7|9|13)", q))
    nums = [int(n) for n in re.findall(r"(?<![♭♯\d])(\d+)", q)]
    top = max([n for n in nums if n in (6, 7, 9, 11, 13)], default=0)
    if lower == "5":  # power chord
        return [7]

    third = 5 if sus and "sus2" not in lower else 2 if "sus2" in lower else 3 if (minor or dim or half_dim) else 4
    fifth = 6 if (dim or half_dim or "♭5" in q) else 8 if aug else 7
    tones = [third]
    if dim and top >= 7:
        tones.append(9)  # diminished 7th
    elif top >= 7 or major7 or half_dim:
        tones.append(11 if major7 else 10)
    if 6 in nums or "6/9" in q:
        tones.append(9)
    if top >= 9 or "6/9" in q or "add9" in lower:
        tones.append(14)
    if top >= 11 and minor:
        tones.append(17)
    if top >= 13:
        tones.append(21)
    for alt, iv in (("♭9", 13), ("♯9", 15), ("♯11", 18), ("♭13", 20)):
        if alt in q:
            tones.append(iv)
    if "alt" in lower:
        tones += [13, 15, 20]
    tones.append(fifth)
    seen: list[int] = []
    for t in tones:
        if t not in seen:
            seen.append(t)
    return seen


def voice_chord(text: str, transpose: int = 0) -> tuple[int, int, list[int]] | None:
    """(bass note, fifth above the bass, piano voicing) as MIDI notes, or None
    for a symbol we can't read (N.C., etc.)."""
    root_name, quality, bass_name = split_chord(text)
    root = pitch_class(root_name)
    if root is None:
        return None
    root = (root + transpose) % 12
    bass_pc = pitch_class(bass_name)
    bass_pc = root if bass_pc is None else (bass_pc + transpose) % 12

    tones = chord_tones(quality)
    # Rootless voicing: the four most important tones, each in the octave above E3.
    pcs = [(root + t) % 12 for t in tones[:4]]
    if len(pcs) < 3:  # triad-ish: add the root back in
        pcs.append(root)
    voicing = sorted({52 + (pc - 52) % 12 for pc in pcs})
    bass = 36 + (bass_pc - 36) % 12  # C2..B2
    fifth = bass + 7 if bass + 7 <= 50 else bass - 5
    return bass, fifth, voicing


def accompaniment(
    timeline: list[tuple[str, float]], bars: dict[str, ChartBar], *, transpose: int = 0
) -> list[QNote]:
    """A simple band from the chord symbols: bass on the chord (and the fifth on
    beat 3 of a long chord), piano on the beat and the and-of-2 (the Charleston).
    A bar with no chord carries the previous one."""
    notes: list[QNote] = []
    current: str | None = None
    for i, (bar_id, bar_start) in enumerate(timeline):
        bar = bars.get(bar_id)
        if bar is None:
            continue
        beat_q = 4 / bar.unit
        bar_len = bar.beats * beat_q
        if i + 1 < len(timeline):
            bar_len = max(bar_len, timeline[i + 1][1] - bar_start)  # multi-bar rests
        changes = [(bar_start + (max(1.0, c.beat) - 1) * beat_q, c.text) for c in bar.chords]
        if not changes or changes[0][0] > bar_start + 1e-6:
            if current is not None:
                changes.insert(0, (bar_start, current))
        for j, (start, text) in enumerate(changes):
            if text.startswith("/") and current:  # "/G": same chord, new bass note
                root, quality, _ = split_chord(current)
                text = f"{root}{quality}{text}"
            if voice_chord(text) is None:  # unreadable: keep the previous harmony
                if current is None:
                    continue
                text = current
            current = text
            end = changes[j + 1][0] if j + 1 < len(changes) else bar_start + bar_len
            voiced = voice_chord(text, transpose)
            if voiced is None:
                continue
            bass, fifth, voicing = voiced
            # one "bar" of pattern at a time, so multi-bar rests keep comping
            seg = start
            while seg < end - 1e-6:
                seg_end = min(end, seg + bar.beats * beat_q)
                length = seg_end - seg
                notes.append(QNote(seg, seg + min(length, 2.0) - 0.05, bass, "bass", 0.9))
                if length >= 4 and bar.unit == 4:
                    notes.append(QNote(seg + 2, seg + 3.95, fifth, "bass", 0.8))
                hits = [(seg, min(length, 1.5))]
                if length >= 4 and bar.unit == 4:
                    hits = [(seg, 1.4), (seg + 1.5, 2.3)]  # Charleston
                for h_start, h_len in hits:
                    for p in voicing:
                        notes.append(QNote(h_start, h_start + h_len, p, "chords", 0.5))
                seg = seg_end
    return notes


# ----------------------------------------------------------------------------
# Performance
# ----------------------------------------------------------------------------
def perform(notes: list[QNote], tempo: TempoMap, *, swing_ratio: float = 0.5,
            speed: float = 1.0) -> list[Note]:
    out = []
    for n in notes:
        start = tempo.seconds(swing(n.start, swing_ratio)) / speed
        end = tempo.seconds(swing(n.end, swing_ratio)) / speed
        out.append(Note(start, max(0.03, end - start), n.pitch, n.voice, n.velocity))
    out.sort(key=lambda n: n.start)
    return out


def bar_times(timeline: list[tuple[str, float]], tempo: TempoMap, *, swing_ratio: float = 0.5,
              speed: float = 1.0) -> list[tuple[float, str]]:
    """(seconds, bar id) for each bar in playing order."""
    return [(tempo.seconds(swing(q, swing_ratio)) / speed, bar_id) for bar_id, q in timeline]


# Additive "instruments": (harmonic, amplitude) pairs, decay rate, loudness.
TIMBRES = {
    "melody": (((1, 1.0), (2, 0.35), (3, 0.12), (4, 0.06)), 1.8, 1.0),  # vibraphone-ish
    "chords": (((1, 1.0), (2, 0.25), (3, 0.06)), 1.2, 0.22),  # soft electric piano
    "bass": (((1, 1.0), (2, 0.55), (3, 0.25), (4, 0.1)), 2.6, 0.75),  # plucked bass
}


def synthesize(notes: list[Note], *, speed: float = 1.0, sr: int = SAMPLE_RATE) -> np.ndarray:
    """Render notes to mono int16 PCM. speed > 1 plays faster (performances from
    perform() are already timed, so they use the default)."""
    if not notes:
        return np.zeros(0, dtype=np.int16)
    release = 0.08
    end = max(n.start + n.duration for n in notes) / speed + release + 0.3
    out = np.zeros(int(end * sr) + 1, dtype=np.float32)

    for n in notes:
        harmonics, decay, gain = TIMBRES.get(n.voice, TIMBRES["melody"])
        dur = n.duration / speed
        length = int((dur + release) * sr)
        t = np.arange(length, dtype=np.float32) / sr
        freq = 440.0 * 2 ** ((n.pitch - 69) / 12)
        tone = sum(a * np.sin(2 * np.pi * freq * h * t) for h, a in harmonics if freq * h < sr / 2)
        env = np.minimum(1.0, t / 0.008) * np.exp(-decay * t)  # quick attack, gentle decay
        off = t > dur
        env[off] *= np.exp(-(t[off] - dur) / (release / 4))  # short release
        start = int(n.start / speed * sr)
        out[start:start + length] += (tone * env * gain * n.velocity).astype(np.float32)

    peak = float(np.max(np.abs(out))) or 1.0
    return (out / peak * 0.6 * 32767).astype(np.int16)


def wav_bytes(pcm: np.ndarray, sr: int = SAMPLE_RATE) -> bytes:
    """Encode mono int16 PCM as a WAV file, for the browser to play."""
    out = io.BytesIO()
    with wave.open(out, "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(sr)
        wav.writeframes(pcm.astype("<i2", copy=False).tobytes())
    return out.getvalue()


# ----------------------------------------------------------------------------
# Output
# ----------------------------------------------------------------------------
class Player:
    """Plays one buffer of PCM at a time on the default output device."""

    def __init__(self, sr: int = SAMPLE_RATE):
        self.sr = sr
        self._device = None
        self._lock = threading.Lock()
        self._pos = 0
        self._total = 0
        self._paused = False

    @property
    def playing(self) -> bool:
        """True from play() until stop(), including while paused."""
        return self._device is not None

    @property
    def paused(self) -> bool:
        return self._paused

    def pause(self) -> None:
        """Hold the position; the device plays silence until resume()."""
        if self.playing:
            self._paused = True

    def resume(self) -> None:
        self._paused = False

    @property
    def position(self) -> float:
        """Seconds played so far."""
        return self._pos / self.sr

    @property
    def length(self) -> float:
        return self._total / self.sr

    def play(self, pcm: np.ndarray, on_finished: Callable[[], None] | None = None) -> None:
        import miniaudio  # imported lazily: opening audio is only needed to play

        self.stop()
        with self._lock:
            self._pos, self._total, self._paused = 0, len(pcm), False
            data = pcm.tobytes()

            def stream():
                frames = yield b""
                while self._pos < self._total:
                    if self._paused:
                        frames = yield b"\x00\x00" * frames
                        continue
                    chunk = data[self._pos * 2:(self._pos + frames) * 2]
                    self._pos += len(chunk) // 2
                    frames = yield chunk
                if on_finished:
                    threading.Thread(target=on_finished, daemon=True).start()
                while True:  # keep the device fed with silence until stop()
                    frames = yield b"\x00\x00" * frames

            # TUNEVIEW_AUDIO=null plays into miniaudio's silent backend (tests, demos).
            backends = [miniaudio.Backend.NULL] if os.environ.get("TUNEVIEW_AUDIO") == "null" else None
            device = miniaudio.PlaybackDevice(
                output_format=miniaudio.SampleFormat.SIGNED16, nchannels=1, sample_rate=self.sr,
                backends=backends,
            )
            gen = stream()
            next(gen)
            device.start(gen)
            self._device = device

    def stop(self) -> None:
        with self._lock:
            device, self._device = self._device, None
            self._paused = False
        if device is not None:
            device.close()
