"""
Document parser: converts a source file or raw text into a ParsedDocument.

Supported input formats:
  - Plain text (.txt, no extension)
  - Markdown (.md)

Parsing is deterministic: same input always produces the same ParsedDocument.
No network calls. No LLM calls. No filesystem writes.

Frontmatter (YAML-like key: value header block delimited by '---') is parsed
for metadata if present. The raw_text field always contains the full original
text including any frontmatter.
"""
import hashlib
import re
from pathlib import Path
from typing import Optional

from .models import ParsedDocument

PARSER_VERSION = '1.0'

# Frontmatter block: optional '---' delimited header at the start of the file
_FRONTMATTER_RE = re.compile(
    r'\A---\s*\n(.*?)\n---\s*\n',
    re.DOTALL,
)

# Simple key: value parser for frontmatter lines
_KV_RE = re.compile(r'^([A-Za-z_][A-Za-z0-9_]*)\s*:\s*(.+)$')


def _source_id(text: str) -> str:
    """Deterministic 16-char hex identifier from SHA-256 of the raw text."""
    return hashlib.sha256(text.encode('utf-8')).hexdigest()[:16]


def _parse_frontmatter(text: str) -> dict:
    """
    Extract key-value pairs from a YAML-like frontmatter block.
    Returns an empty dict if no frontmatter is present.
    Pure function — no side effects.
    """
    m = _FRONTMATTER_RE.match(text)
    if not m:
        return {}
    metadata = {}
    for line in m.group(1).splitlines():
        kv = _KV_RE.match(line.strip())
        if kv:
            metadata[kv.group(1)] = kv.group(2).strip()
    return metadata


def _normalise_text(raw: str) -> str:
    """
    Normalise line endings to LF and strip trailing whitespace from each line.
    Preserves blank lines (paragraph boundaries).
    """
    lines = raw.replace('\r\n', '\n').replace('\r', '\n').splitlines()
    return '\n'.join(line.rstrip() for line in lines)


def parse_text(
    text: str,
    source_path: str = '<inline>',
) -> ParsedDocument:
    """
    Parse a raw text string into a ParsedDocument.

    source_path is used for source attribution in extracted candidates.
    It does not need to correspond to a real filesystem path.

    Deterministic: same text + same source_path → same ParsedDocument.
    """
    normalised = _normalise_text(text)
    metadata = _parse_frontmatter(normalised)
    source_id = _source_id(normalised)
    line_count = normalised.count('\n') + 1 if normalised else 0
    char_count = len(normalised)

    return ParsedDocument(
        source_path=source_path,
        source_id=source_id,
        raw_text=normalised,
        metadata=metadata,
        line_count=line_count,
        char_count=char_count,
    )


def parse_file(path: str) -> ParsedDocument:
    """
    Read a file from disk and parse it into a ParsedDocument.

    Raises FileNotFoundError if the path does not exist.
    Raises ValueError if the file extension is not supported.
    """
    p = Path(path)
    if not p.exists():
        raise FileNotFoundError(f"File not found: {path!r}")

    suffix = p.suffix.lower()
    if suffix not in ('', '.txt', '.md', '.markdown'):
        raise ValueError(
            f"Unsupported file extension {suffix!r}. "
            f"Supported: .txt, .md, .markdown, (no extension)"
        )

    raw = p.read_text(encoding='utf-8')
    return parse_text(raw, source_path=str(p.resolve()))
