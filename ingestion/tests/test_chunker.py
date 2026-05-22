"""Tests for ingestion.chunker."""
import pytest
from ingestion.chunker import (
    chunk_by_paragraph,
    chunk_fixed_size,
    chunk_document,
    DEFAULT_MAX_CHUNK_CHARS,
    DEFAULT_OVERLAP_CHARS,
)
from ingestion.parser import parse_text
from ingestion.models import Chunk


def _doc(text: str):
    return parse_text(text, source_path="<test>")


# ---------------------------------------------------------------------------
# chunk_by_paragraph
# ---------------------------------------------------------------------------

def test_paragraph_basic():
    doc = _doc("Para one.\n\nPara two.")
    chunks = chunk_by_paragraph(doc)
    assert len(chunks) == 2
    assert chunks[0].text == "Para one."
    assert chunks[1].text == "Para two."


def test_paragraph_returns_chunks():
    doc = _doc("Hello.\n\nWorld.")
    chunks = chunk_by_paragraph(doc)
    for c in chunks:
        assert isinstance(c, Chunk)


def test_paragraph_source_path_propagated():
    doc = parse_text("A.\n\nB.", source_path="my/file.txt")
    chunks = chunk_by_paragraph(doc)
    for c in chunks:
        assert c.source_path == "my/file.txt"


def test_paragraph_source_id_propagated():
    doc = _doc("A.\n\nB.")
    chunks = chunk_by_paragraph(doc)
    for c in chunks:
        assert c.source_id == doc.source_id


def test_paragraph_chunk_index_sequential():
    doc = _doc("A.\n\nB.\n\nC.")
    chunks = chunk_by_paragraph(doc)
    assert [c.chunk_index for c in chunks] == [0, 1, 2]


def test_paragraph_offsets_correct():
    text = "Para one.\n\nPara two."
    doc = _doc(text)
    chunks = chunk_by_paragraph(doc)
    # Verify offsets point into raw_text
    for c in chunks:
        assert doc.raw_text[c.start_char:c.end_char] == c.text


def test_paragraph_skips_blank_chunks():
    doc = _doc("A.\n\n\n\nB.")
    chunks = chunk_by_paragraph(doc)
    # Only non-empty paragraphs become chunks
    texts = [c.text.strip() for c in chunks]
    assert "" not in texts


def test_paragraph_single_para():
    doc = _doc("No blank lines here.")
    chunks = chunk_by_paragraph(doc)
    assert len(chunks) == 1
    assert chunks[0].text == "No blank lines here."


def test_paragraph_multi_blank_lines():
    doc = _doc("A.\n\n\n\n\nB.")
    chunks = chunk_by_paragraph(doc)
    assert len(chunks) == 2


def test_paragraph_oversized_sub_chunked():
    # One paragraph longer than max_chunk_chars
    long_para = "word " * 500  # 2500 chars
    doc = _doc(long_para.strip())
    chunks = chunk_by_paragraph(doc, max_chunk_chars=200)
    assert len(chunks) > 1
    for c in chunks:
        assert len(c.text) <= 200 + 10  # word-aligned may slightly exceed


def test_paragraph_no_empty_chunks():
    doc = _doc("  \n\nActual content.\n\n  ")
    chunks = chunk_by_paragraph(doc)
    for c in chunks:
        assert c.text.strip()


# ---------------------------------------------------------------------------
# chunk_fixed_size
# ---------------------------------------------------------------------------

def test_fixed_size_basic():
    doc = _doc("word " * 100)
    chunks = chunk_fixed_size(doc, max_chunk_chars=50, overlap_chars=10)
    assert len(chunks) > 1


def test_fixed_size_chunk_below_max():
    doc = _doc("word " * 100)
    chunks = chunk_fixed_size(doc, max_chunk_chars=100, overlap_chars=0)
    for c in chunks:
        assert len(c.text) <= 110  # word boundary may extend slightly


def test_fixed_size_overlap():
    # With overlap, adjacent chunks share some text
    text = "alpha beta gamma delta epsilon zeta eta theta iota kappa lambda mu nu xi"
    doc = _doc(text)
    chunks = chunk_fixed_size(doc, max_chunk_chars=30, overlap_chars=10)
    if len(chunks) >= 2:
        # End of chunk[0] should overlap with start of chunk[1] in raw text
        end0 = chunks[0].end_char
        start1 = chunks[1].start_char
        assert start1 < end0  # overlap exists


def test_fixed_size_no_empty_chunks():
    doc = _doc("  some   text  with   spaces  ")
    chunks = chunk_fixed_size(doc, max_chunk_chars=50)
    for c in chunks:
        assert c.text.strip()


def test_fixed_size_source_info():
    doc = parse_text("Hello world test.", source_path="s/path.txt")
    chunks = chunk_fixed_size(doc)
    for c in chunks:
        assert c.source_path == "s/path.txt"
        assert c.source_id == doc.source_id


def test_fixed_size_single_chunk_short_text():
    doc = _doc("short text")
    chunks = chunk_fixed_size(doc, max_chunk_chars=2000)
    assert len(chunks) == 1
    assert chunks[0].text == "short text"


def test_fixed_size_deterministic():
    doc = _doc("The quick brown fox jumps over the lazy dog.")
    chunks1 = chunk_fixed_size(doc, max_chunk_chars=20, overlap_chars=5)
    chunks2 = chunk_fixed_size(doc, max_chunk_chars=20, overlap_chars=5)
    assert [c.text for c in chunks1] == [c.text for c in chunks2]


def test_fixed_size_offsets_correct():
    text = "The quick brown fox jumps over the lazy dog."
    doc = _doc(text)
    chunks = chunk_fixed_size(doc, max_chunk_chars=15, overlap_chars=3)
    for c in chunks:
        # start_char and end_char are offsets into doc.raw_text
        raw_slice = doc.raw_text[c.start_char:c.end_char]
        # The chunk text may be stripped, so the raw slice contains it
        assert c.text in raw_slice or raw_slice.strip() == c.text.strip()


# ---------------------------------------------------------------------------
# chunk_document (auto-selection)
# ---------------------------------------------------------------------------

def test_chunk_document_selects_paragraph_when_blank_lines():
    doc = _doc("Para A.\n\nPara B.")
    chunks = chunk_document(doc)
    assert len(chunks) == 2


def test_chunk_document_selects_fixed_size_when_no_blank_lines():
    long_text = "word " * 600  # no double newlines
    doc = _doc(long_text.strip())
    chunks = chunk_document(doc, max_chunk_chars=200)
    assert len(chunks) > 1


def test_chunk_document_returns_non_empty():
    doc = _doc("Anything here.")
    chunks = chunk_document(doc)
    assert len(chunks) >= 1
    for c in chunks:
        assert c.text.strip()


def test_chunk_document_deterministic():
    doc = _doc("A.\n\nB.\n\nC.")
    assert chunk_document(doc) == chunk_document(doc)
