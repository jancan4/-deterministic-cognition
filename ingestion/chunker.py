"""
Deterministic text chunker: splits a ParsedDocument into Chunks.

Chunking strategy:
  1. Paragraph chunking (default): split on double-newlines. Each paragraph
     is one chunk, preserving its character offset in the source document.
  2. Fixed-size chunking: split at every N characters with M-char overlap,
     aligned to word boundaries. Used when paragraph structure is absent.

Chunks are always non-empty and carry their start/end character offsets
within the document's raw_text, enabling precise source attribution.

Deterministic: same document → same chunks. No randomness.
"""
import re
from typing import List

from .models import Chunk, ParsedDocument

# Paragraph boundary: one or more blank lines
_PARA_SPLIT_RE = re.compile(r'\n{2,}')

# Max characters for a single paragraph chunk before fallback splitting
DEFAULT_MAX_CHUNK_CHARS = 2000
DEFAULT_OVERLAP_CHARS = 100


def chunk_by_paragraph(
    doc: ParsedDocument,
    max_chunk_chars: int = DEFAULT_MAX_CHUNK_CHARS,
) -> List[Chunk]:
    """
    Split the document on paragraph boundaries (double newlines).

    Paragraphs longer than max_chunk_chars are further split by
    fixed-size chunking to keep individual chunks tractable.

    Returns an ordered list of non-empty Chunk objects with correct
    start_char / end_char offsets into doc.raw_text.
    """
    text = doc.raw_text
    raw_paras = _PARA_SPLIT_RE.split(text)

    # Recompute offsets by scanning the original text
    chunks: List[Chunk] = []
    cursor = 0
    chunk_index = 0

    for para in raw_paras:
        if not para.strip():
            cursor += len(para) + 2  # skip the blank-line separator
            continue

        # Find this paragraph in the original text starting from cursor
        para_start = text.find(para, cursor)
        if para_start == -1:
            para_start = cursor
        para_end = para_start + len(para)

        if len(para) <= max_chunk_chars:
            chunks.append(Chunk(
                source_path=doc.source_path,
                source_id=doc.source_id,
                chunk_index=chunk_index,
                text=para,
                start_char=para_start,
                end_char=para_end,
            ))
            chunk_index += 1
        else:
            # Sub-chunk the oversized paragraph
            sub_chunks = _fixed_size_chunks(
                text=para,
                source_path=doc.source_path,
                source_id=doc.source_id,
                start_offset=para_start,
                chunk_index_start=chunk_index,
                max_chars=max_chunk_chars,
            )
            chunks.extend(sub_chunks)
            chunk_index += len(sub_chunks)

        cursor = para_end

    return chunks


def chunk_fixed_size(
    doc: ParsedDocument,
    max_chunk_chars: int = DEFAULT_MAX_CHUNK_CHARS,
    overlap_chars: int = DEFAULT_OVERLAP_CHARS,
) -> List[Chunk]:
    """
    Split the document into fixed-size chunks with character-level overlap,
    aligned to word boundaries.

    Use this when the document has no paragraph structure (e.g. a log file
    or a dense note without blank lines).
    """
    return _fixed_size_chunks(
        text=doc.raw_text,
        source_path=doc.source_path,
        source_id=doc.source_id,
        start_offset=0,
        chunk_index_start=0,
        max_chars=max_chunk_chars,
        overlap_chars=overlap_chars,
    )


def _fixed_size_chunks(
    text: str,
    source_path: str,
    source_id: str,
    start_offset: int,
    chunk_index_start: int,
    max_chars: int = DEFAULT_MAX_CHUNK_CHARS,
    overlap_chars: int = DEFAULT_OVERLAP_CHARS,
) -> List[Chunk]:
    """
    Split text into max_chars-sized chunks with overlap_chars overlap,
    breaking at the nearest word boundary before the limit.
    """
    chunks: List[Chunk] = []
    pos = 0
    chunk_index = chunk_index_start
    text_len = len(text)

    while pos < text_len:
        end = min(pos + max_chars, text_len)

        # Align to word boundary if not at the end
        if end < text_len:
            # Search backward for a space or newline
            boundary = text.rfind(' ', pos, end)
            if boundary == -1:
                boundary = text.rfind('\n', pos, end)
            if boundary > pos:
                end = boundary

        segment = text[pos:end].strip()
        if segment:
            chunks.append(Chunk(
                source_path=source_path,
                source_id=source_id,
                chunk_index=chunk_index,
                text=segment,
                start_char=start_offset + pos,
                end_char=start_offset + end,
            ))
            chunk_index += 1

        # When we've consumed the last byte, stop immediately.
        # Otherwise advance by (chunk_size - overlap); minimum 1 to avoid
        # an infinite loop when overlap_chars >= chunk size.
        if end >= text_len:
            break
        advance = max(end - pos - overlap_chars, 1)
        pos += advance

    return chunks


def chunk_document(
    doc: ParsedDocument,
    max_chunk_chars: int = DEFAULT_MAX_CHUNK_CHARS,
) -> List[Chunk]:
    """
    Auto-select chunking strategy based on document structure.

    Uses paragraph chunking when the document has at least one blank line;
    falls back to fixed-size chunking for dense single-paragraph documents.
    """
    has_paragraphs = '\n\n' in doc.raw_text or '\n\r\n' in doc.raw_text
    if has_paragraphs:
        return chunk_by_paragraph(doc, max_chunk_chars=max_chunk_chars)
    return chunk_fixed_size(doc, max_chunk_chars=max_chunk_chars)
