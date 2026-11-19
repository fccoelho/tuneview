import time

import numpy as np
import pytest

from tuneview.engraver import ChartBar, ChartChord, Engraver, chart_from_mei
from tuneview.player import (
    SAMPLE_RATE,
    Note,
    Player,
    QNote,
    TempoMap,
    accompaniment,
    bar_times,
    chord_tones,
    melody,
    perform,
    should_swing,
    swing,
    synthesize,
    tempo_map,
    voice_chord,
)


@pytest.fixture
def eng(mini_xml):
    return Engraver().load(mini_xml)


# -- timing --------------------------------------------------------------
def test_tempo_map_handles_changes():
    tm = TempoMap([(0, 0.5), (4, 0.25)])  # ♩=120, then ♩=240 from beat 4
    assert tm.seconds(2) == pytest.approx(1.0)
    assert tm.seconds(4) == pytest.approx(2.0)
    assert tm.seconds(8) == pytest.approx(3.0)
    assert tempo_map([]).seconds(1) == pytest.approx(0.5)  # default ♩=120


@pytest.mark.parametrize("q, ratio, expected", [
    (0.0, 0.64, 0.0), (0.5, 0.64, 0.64), (1.5, 2 / 3, 1 + 2 / 3), (0.25, 0.64, 0.32),
    (0.75, 0.64, 0.82), (3.0, 0.7, 3.0), (0.5, 0.5, 0.5),
])
def test_swing_moves_offbeat_eighths(q, ratio, expected):
    assert swing(q, ratio) == pytest.approx(expected)


def test_should_swing():
    eighths = [QNote(i / 2, i / 2 + 0.5, 60) for i in range(16)]
    sixteenths = [QNote(i / 4, i / 4 + 0.25, 60) for i in range(32)]
    assert should_swing(eighths, "4/4")
    assert not should_swing(sixteenths, "4/4")
    assert not should_swing(eighths, "12/8")
    assert not should_swing(eighths, "4/4", words="Bossa nova")


def test_perform_and_bar_times(eng):
    tune, tempo = melody(eng.melody), tempo_map(eng.tempos)
    notes = perform(tune, tempo, speed=2.0)
    assert [n.pitch for n in notes] == [69, 65, 67, 69, 65, 65]
    assert notes[1].start == pytest.approx(0.5)  # beat 3 at ♩=120, double speed
    times = bar_times(eng.timeline, tempo)
    assert [t for t, _ in times] == pytest.approx([0, 2, 4, 6, 8, 10])
    assert melody(eng.melody, transpose=-2)[0].pitch == 67


# -- chords ---------------------------------------------------------------
@pytest.mark.parametrize("quality, tones", [
    ("", [4, 7]), ("min", [3, 7]), ("7", [4, 10, 7]), ("maj7", [4, 11, 7]),
    ("min7", [3, 10, 7]), ("min9", [3, 10, 14, 7]), ("13", [4, 10, 14, 21, 7]),
    ("dim7", [3, 9, 6]), ("min7♭5", [3, 10, 6]), ("7sus", [5, 10, 7]), ("6", [4, 9, 7]),
    ("7(♯5)", [4, 10, 8]), ("7♭9", [4, 10, 13, 7]), ("min(maj7)", [3, 11, 7]),
    ("maj9", [4, 11, 14, 7]), ("5", [7]),
    ("△7", [4, 11, 7]), ("m7", [3, 10, 7]), ("ø7", [3, 10, 6]), ("°7", [3, 9, 6]),
    ("°", [3, 6]), ("m(△7)", [3, 11, 7]), ("△9", [4, 11, 14, 7]), ("△7sus", [5, 11, 7]),
])
def test_chord_tones(quality, tones):
    assert chord_tones(quality) == tones


def test_voice_chord():
    bass, fifth, voicing = voice_chord("Dmin7")
    assert bass % 12 == 2 and fifth % 12 == 9 and 36 <= bass <= 47
    assert {p % 12 for p in voicing} == {5, 0, 9}  # F, C, A: 3rd, 7th, 5th
    assert all(52 <= p < 64 for p in voicing)
    assert voice_chord("B♭maj7/D")[0] % 12 == 2  # slash bass
    assert voice_chord("C7", transpose=2)[0] % 12 == 2
    assert voice_chord("N.C.") is None


def test_accompaniment_comps_carries_and_slash_chords():
    bars = {
        "a": ChartBar("1", id="a", chords=[ChartChord("C7", 1)]),
        "b": ChartBar("2", id="b"),  # no chord: carry C7
        "c": ChartBar("3", id="c", chords=[ChartChord("F", 1), ChartChord("/A", 3)]),
    }
    notes = accompaniment([("a", 0), ("b", 4), ("c", 8)], bars)
    bass = [(n.start, n.pitch % 12) for n in notes if n.voice == "bass"]
    assert (0, 0) in bass and (2, 7) in bass  # root, then the fifth on beat 3
    assert (4, 0) in bass  # carried into the empty bar
    assert (8, 5) in bass and (10, 9) in bass  # F, then F/A
    hits = sorted({n.start for n in notes if n.voice == "chords" and n.start < 4})
    assert hits == [0, 1.5]  # the Charleston


def test_accompaniment_from_a_real_chart(eng):
    bars = {b.id: b for b in chart_from_mei(eng.mei)}
    notes = accompaniment(eng.timeline, bars)
    assert {n.voice for n in notes} == {"bass", "chords"}
    assert max(n.end for n in notes) <= 24


# -- sound ----------------------------------------------------------------
def test_synthesize_pitch_level_and_voices(eng):
    notes = perform(melody(eng.melody), tempo_map(eng.tempos))
    pcm = synthesize(notes)
    assert pcm.dtype == np.int16 and np.abs(pcm).max() < 32767
    first = pcm[: SAMPLE_RATE // 2].astype(float)
    freqs = np.fft.rfftfreq(len(first), 1 / SAMPLE_RATE)
    assert freqs[np.argmax(np.abs(np.fft.rfft(first)))] == pytest.approx(440, abs=3)  # A4
    assert len(synthesize(notes, speed=2.0)) < 0.6 * len(pcm)
    assert len(synthesize([Note(0, 0.5, 40, "bass"), Note(0, 0.5, 60, "chords")])) > 0


def test_synthesize_empty():
    assert len(synthesize([])) == 0


def test_player_plays_to_the_end_and_stops(monkeypatch):
    monkeypatch.setenv("TUNEVIEW_AUDIO", "null")
    player = Player()
    player.play(np.zeros(SAMPLE_RATE // 10, dtype=np.int16))
    assert player.playing
    deadline = time.time() + 3
    while player.position < player.length and time.time() < deadline:
        time.sleep(0.02)
    assert player.position == pytest.approx(player.length)
    player.stop()
    assert not player.playing


def test_player_pause_holds_the_position(monkeypatch):
    monkeypatch.setenv("TUNEVIEW_AUDIO", "null")
    player = Player()
    player.play(np.zeros(SAMPLE_RATE * 2, dtype=np.int16))
    time.sleep(0.15)
    player.pause()
    assert player.paused and player.playing
    time.sleep(0.05)  # let a buffer already in flight finish
    held = player.position
    time.sleep(0.2)
    assert player.position == held
    player.resume()
    deadline = time.time() + 2
    while player.position == held and time.time() < deadline:
        time.sleep(0.02)
    assert player.position > held and not player.paused
    player.pause()
    player.stop()
    assert not player.playing and not player.paused
