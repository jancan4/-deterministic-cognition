"""
Deterministic SHA-256 checksum computation for source documents.

Rules:
  - Binary file reading only (no encoding assumptions).
  - Same file content always produces the same checksum.
  - Changed file content always produces a different checksum.
  - No network access. No side effects.
"""
import hashlib
from pathlib import Path


def compute_file_checksum(path: str) -> str:
    """
    Compute the SHA-256 hex digest of a file's raw bytes.

    Reads in 64 KiB chunks to handle large files without loading them fully
    into memory. Returns a lowercase 64-character hex string.

    Raises FileNotFoundError if the path does not exist.
    Raises IsADirectoryError if the path is a directory.
    """
    p = Path(path)
    if not p.exists():
        raise FileNotFoundError(f"File not found: {path!r}")
    if p.is_dir():
        raise IsADirectoryError(f"Path is a directory, not a file: {path!r}")

    h = hashlib.sha256()
    with p.open('rb') as fh:
        for chunk in iter(lambda: fh.read(65536), b''):
            h.update(chunk)
    return h.hexdigest()


def compute_text_checksum(text: str) -> str:
    """
    Compute the SHA-256 hex digest of a UTF-8 encoded string.

    Deterministic: same text always produces the same digest.
    """
    return hashlib.sha256(text.encode('utf-8')).hexdigest()
