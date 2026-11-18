from pathlib import Path

import pytest

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture
def mini_path() -> Path:
    return FIXTURES / "mini.musicxml"


@pytest.fixture
def mini_xml(mini_path) -> str:
    return mini_path.read_text(encoding="utf-8")
