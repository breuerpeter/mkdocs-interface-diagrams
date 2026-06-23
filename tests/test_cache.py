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


def test_job_key_changes_when_doc_content_changes(tmp_path):
    section = tmp_path / "sys"
    section.mkdir()
    doc = section / "a.md"
    doc.write_text("# one\n")
    key1 = cache.job_key(section, "x")
    assert cache.job_key(section, "x") == key1  # stable for same content
    doc.write_text("# two\n")
    assert cache.job_key(section, "x") != key1  # content edit invalidates
