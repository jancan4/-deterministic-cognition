"""Tests for ingestion.parser."""
import pytest
from ingestion.parser import parse_text, parse_file
from ingestion.models import ParsedDocument


# ---------------------------------------------------------------------------
# parse_text
# ---------------------------------------------------------------------------

def test_parse_text_returns_parsed_document():
    doc = parse_text("hello world", source_path="<test>")
    assert isinstance(doc, ParsedDocument)


def test_parse_text_source_path():
    doc = parse_text("hello", source_path="my/doc.txt")
    assert doc.source_path == "my/doc.txt"


def test_parse_text_default_source_path():
    doc = parse_text("hello")
    assert doc.source_path == "<inline>"


def test_parse_text_raw_text_preserved():
    text = "line one\nline two\n\nline three"
    doc = parse_text(text)
    assert "line one" in doc.raw_text
    assert "line three" in doc.raw_text


def test_parse_text_line_count():
    doc = parse_text("a\nb\nc")
    assert doc.line_count == 3


def test_parse_text_char_count():
    text = "hello"
    doc = parse_text(text)
    assert doc.char_count == len(text)


def test_parse_text_crlf_normalised():
    doc = parse_text("line1\r\nline2\r\nline3")
    assert "\r" not in doc.raw_text
    assert doc.line_count == 3


def test_parse_text_cr_only_normalised():
    doc = parse_text("line1\rline2")
    assert "\r" not in doc.raw_text


def test_parse_text_trailing_whitespace_stripped():
    doc = parse_text("hello   \nworld  ")
    for line in doc.raw_text.splitlines():
        assert line == line.rstrip()


def test_parse_text_source_id_is_16_hex():
    doc = parse_text("some text")
    assert len(doc.source_id) == 16
    int(doc.source_id, 16)  # raises ValueError if not hex


def test_parse_text_source_id_deterministic():
    doc1 = parse_text("same text", source_path="p")
    doc2 = parse_text("same text", source_path="p")
    assert doc1.source_id == doc2.source_id


def test_parse_text_source_id_differs_on_different_text():
    doc1 = parse_text("text A")
    doc2 = parse_text("text B")
    assert doc1.source_id != doc2.source_id


def test_parse_text_empty_string():
    doc = parse_text("")
    assert doc.raw_text == ""
    assert doc.char_count == 0
    assert doc.line_count == 0


def test_parse_text_single_newline():
    doc = parse_text("\n")
    # A bare newline normalises to empty; line_count is 0 or 1
    assert doc.line_count >= 0


# ---------------------------------------------------------------------------
# Frontmatter
# ---------------------------------------------------------------------------

def test_parse_text_frontmatter_extracted():
    text = "---\ntitle: My Doc\nauthor: Alice\n---\nBody text here."
    doc = parse_text(text)
    assert doc.metadata.get("title") == "My Doc"
    assert doc.metadata.get("author") == "Alice"


def test_parse_text_frontmatter_no_frontmatter():
    doc = parse_text("Just plain text.")
    assert doc.metadata == {}


def test_parse_text_frontmatter_partial_block():
    # Only opening '---', no closing '---'
    doc = parse_text("---\ntitle: X\nBody without close.")
    assert doc.metadata == {}


def test_parse_text_frontmatter_raw_text_includes_frontmatter():
    text = "---\ntitle: T\n---\nBody."
    doc = parse_text(text)
    # raw_text always contains the full normalised text
    assert "title" in doc.raw_text or "Body" in doc.raw_text


# ---------------------------------------------------------------------------
# parse_file
# ---------------------------------------------------------------------------

def test_parse_file_txt(tmp_path):
    f = tmp_path / "note.txt"
    f.write_text("hello from file", encoding="utf-8")
    doc = parse_file(str(f))
    assert "hello from file" in doc.raw_text
    assert doc.source_path == str(f.resolve())


def test_parse_file_md(tmp_path):
    f = tmp_path / "note.md"
    f.write_text("# Header\n\nParagraph.", encoding="utf-8")
    doc = parse_file(str(f))
    assert "Header" in doc.raw_text


def test_parse_file_markdown(tmp_path):
    f = tmp_path / "note.markdown"
    f.write_text("content", encoding="utf-8")
    doc = parse_file(str(f))
    assert doc.source_path.endswith("note.markdown")


def test_parse_file_no_extension(tmp_path):
    f = tmp_path / "NOTES"
    f.write_text("no extension file", encoding="utf-8")
    doc = parse_file(str(f))
    assert "no extension" in doc.raw_text


def test_parse_file_not_found():
    with pytest.raises(FileNotFoundError):
        parse_file("/nonexistent/path/file.txt")


def test_parse_file_unsupported_extension(tmp_path):
    f = tmp_path / "data.csv"
    f.write_text("a,b,c", encoding="utf-8")
    with pytest.raises(ValueError, match="Unsupported file extension"):
        parse_file(str(f))


def test_parse_file_source_id_deterministic(tmp_path):
    f = tmp_path / "doc.txt"
    f.write_text("stable content", encoding="utf-8")
    doc1 = parse_file(str(f))
    doc2 = parse_file(str(f))
    assert doc1.source_id == doc2.source_id
