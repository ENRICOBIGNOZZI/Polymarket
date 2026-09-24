from scripts.v7_tape_health import tape_growth


def _sample(size, rows):
    return {
        "state": "OBSERVED",
        "generation": "g",
        "rows_written": rows,
        "files": {
            "dev:ino": {
                "path": "current.jsonl",
                "bytes": size,
                "mtime_ns": size,
                "prefix_length": 1,
                "prefix_hex": "7b",
                "prefix_sha256": "same",
            }
        },
    }


def test_byte_growth_wins_over_stale_row_counter():
    result = tape_growth(_sample(100, 10), _sample(200, 10))
    assert result["state"] == "CONTINUOUS"
    assert result["bytes_added_lower_bound"] == 100
    assert result["live"] is True
    assert result["rows_added"] is None
    assert result["exact_rows"] is False


def test_advancing_row_counter_remains_exact():
    result = tape_growth(_sample(100, 10), _sample(200, 12))
    assert result["live"] is True
    assert result["rows_added"] == 2
    assert result["exact_rows"] is True
