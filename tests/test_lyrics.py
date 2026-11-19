import pytest

from tuneview.lyrics import Verse, heading_of, verses


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
