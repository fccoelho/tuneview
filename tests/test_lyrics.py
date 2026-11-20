import pytest

from tuneview.lyrics import (
    Verse, chunks, extract_lyrics, heading_of, join_bars, join_words, lyric_words, spread_words,
    split_bars, split_words, verses,
)


@pytest.mark.parametrize(
    "line, heading",
    [("[Chorus]", "Chorus"), ("[Last time, slower]", "Last time, slower"), ("Verse 2:", "Verse 2"),
     ("CHORUS:", "CHORUS"), ("Refrão:", "Refrão"), ("Bridge:", "Bridge"), ("A:", "A"), ("A2:", "A2"),
     ("And she said:", None), ("A man said:", None), ("Bridge", None), ("[ ]", None),
     ("Fly me to the moon", None)],
)
def test_heading_of(line, heading):
    assert heading_of(line) == heading


def test_verses_split_at_blank_lines_with_headings():
    text = "Verse 1:\nFly me to the moon\n  Let me play  \n\n\n[Chorus]\r\nIn other words\n \nAnd she said:\nhi\n"
    assert verses(text) == [
        Verse("Verse 1", ["Fly me to the moon", "Let me play"]),
        Verse("Chorus", ["In other words"]),
        Verse("", ["And she said:", "hi"]),
    ]
    assert verses("") == [] and verses("\n \n") == []
    assert verses("[Coda]") == [Verse("Coda", [])]


def test_lyric_words_join_verses_and_skip_headings():
    text = "[Chorus]\nFly me to the moon\n\nVerse 2:\nLet me play\n"
    assert lyric_words(text) == ["Fly", "me", "to", "the", "moon", "Let", "me", "play"]
    assert lyric_words("") == []
    assert lyric_words("[Coda]") == []


def test_spread_words_covers_every_word():
    assert spread_words(8, 4) == [2, 2, 2, 2]
    assert spread_words(3, 4) == [1, 1, 0, 1]
    assert spread_words(5, 0) == []
    assert spread_words(0, 3) == [0, 0, 0]
    for words, bars in [(17, 32), (1, 8), (40, 12)]:
        counts = spread_words(words, bars)
        assert len(counts) == bars and sum(counts) == words and all(c >= 0 for c in counts)


def test_chunks_split_words_by_bar():
    assert chunks(["a", "b", "c", "d"], [2, 1, 1]) == [["a", "b"], ["c"], ["d"]]
    assert chunks(["a", "b", "c"], [1, 1]) == [["a"], ["b", "c"]]  # overflow on the last bar
    assert chunks([], [0, 0]) == [[], []]


def test_split_words_and_join_words_keep_the_words_in_order():
    assert split_words("All of me", 3) == ("All", "of me")
    assert split_words("All of", 0) == ("", "All of")
    assert split_words("All of", 6) == ("All of", "")
    assert split_words("All of me", 99) == ("All of me", "")
    assert join_words("All", "of me") == "All of me"
    assert join_words("All of", "") == "All of"
    assert join_words("", "All of") == "All of"
    for left, right in [("All of", "me Why"), ("", "me Why"), ("All of", "")]:
        assert " ".join(join_words(left, right).split()) == " ".join(f"{left} {right}".split())


def _all_words(bars):
    return [word for line in bars for word in line.split()]


def test_split_bars_shifts_the_later_bars_forward():
    bars = ["a b", "c", "d", "e"]
    assert split_bars(bars, 0, 1) == ["a", "b", "c", "d e"]
    assert split_bars(bars, 0, 0) == ["", "a b", "c", "d e"]  # an empty bar at the start
    assert split_bars(bars, 1, 0) == ["a b", "", "c", "d e"]
    assert split_bars(["a", "b"], 0, 1) == ["a", "b"]  # nothing after the caret
    assert split_bars(bars, 3, 1) == bars  # the last bar has nothing to push into
    assert split_bars(bars, 9, 1) == bars
    assert _all_words(split_bars(bars, 0, 0)) == _all_words(bars)
    assert _all_words(split_bars(bars, 0, 1)) == _all_words(bars)


def test_join_bars_shifts_the_later_bars_back():
    bars = ["a b", "c", "d", "e"]
    assert join_bars(bars, 1) == ["a b c", "d", "e", ""]
    assert join_bars(bars, 3) == ["a b", "c", "d e", ""]
    assert join_bars(bars, 0) == bars
    assert join_bars(bars, 4) == bars
    assert _all_words(join_bars(bars, 1)) == _all_words(bars)
    assert _all_words(join_bars(bars, 3)) == _all_words(bars)


def test_extract_lyrics_joins_syllables_and_splits_verses():
    xml = """<score-partwise><part-list><score-part id="P1"><part-name>Voice</part-name>
      </score-part></part-list><part id="P1"><measure number="1">
      <note><lyric number="1"><syllabic>begin</syllabic><text>Hel</text></lyric></note>
      <note><lyric number="1"><syllabic>end</syllabic><text>lo</text></lyric></note>
      <note><lyric number="1"><syllabic>single</syllabic><text>world</text></lyric></note>
      <note><lyric number="2"><text>Bonjour</text></lyric></note>
    </measure></part></score-partwise>"""
    assert extract_lyrics(xml) == "Hello world\n\nBonjour"
    assert extract_lyrics("<score-partwise><part id='P1'/></score-partwise>") == ""
    assert extract_lyrics("not xml") == ""
