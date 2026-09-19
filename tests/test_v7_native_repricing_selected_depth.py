from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "src" / "v7_crypto_settlement_engine.cpp"


def test_repricing_label_captures_direction_selected_token_depth():
    text = SOURCE.read_text(encoding="utf-8")
    assert "auto point = observation(yes_book, kYes, 6);" not in text
    assert "const bool selected_up = window.direction > 0;" in text
    assert "selected_up ? yes_book : no_book" in text
    assert "selected_up ? kYes : kNo, 6" in text


def test_repricing_label_still_decorates_two_token_bbo_pair():
    text = SOURCE.read_text(encoding="utf-8")
    anchor = "selected_up ? kYes : kNo, 6);"
    start = text.index(anchor)
    window = text[start:start + 900]
    assert "decorate_pm_pair(point);" in window
