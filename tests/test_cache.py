from pathlib import Path

from interface_diagrams import cache

FIX = Path(__file__).resolve().parent / "fixtures" / "parity"


def test_fresh_after_write_stale_on_change(tmp_path):
    out = tmp_path / "out"
    out.mkdir()
    (out / "x.svg").write_text("<svg/>")
    key = cache.job_key(FIX, "v1")
    assert not cache.is_fresh(out, key)
    cache.write(out, key)
    assert cache.is_fresh(out, key)
    assert not cache.is_fresh(out, cache.job_key(FIX, "v2"))  # extra changed
