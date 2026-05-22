"""Tests for sources.checksums."""
import pytest
from sources.checksums import compute_file_checksum, compute_text_checksum


# ---------------------------------------------------------------------------
# compute_file_checksum
# ---------------------------------------------------------------------------

def test_checksum_returns_64_hex_chars(tmp_path):
    f = tmp_path / "file.txt"
    f.write_bytes(b"hello")
    result = compute_file_checksum(str(f))
    assert len(result) == 64
    int(result, 16)  # raises if not valid hex


def test_checksum_deterministic(tmp_path):
    f = tmp_path / "file.txt"
    f.write_bytes(b"stable content")
    c1 = compute_file_checksum(str(f))
    c2 = compute_file_checksum(str(f))
    assert c1 == c2


def test_checksum_differs_on_different_content(tmp_path):
    f1 = tmp_path / "a.txt"
    f2 = tmp_path / "b.txt"
    f1.write_bytes(b"content A")
    f2.write_bytes(b"content B")
    assert compute_file_checksum(str(f1)) != compute_file_checksum(str(f2))


def test_checksum_same_content_same_name_same_result(tmp_path):
    f1 = tmp_path / "x.txt"
    f1.write_bytes(b"identical")
    c1 = compute_file_checksum(str(f1))

    # Write same bytes to a different file
    f2 = tmp_path / "y.txt"
    f2.write_bytes(b"identical")
    c2 = compute_file_checksum(str(f2))

    assert c1 == c2


def test_checksum_changes_on_content_change(tmp_path):
    f = tmp_path / "file.txt"
    f.write_bytes(b"original")
    c1 = compute_file_checksum(str(f))

    f.write_bytes(b"modified")
    c2 = compute_file_checksum(str(f))

    assert c1 != c2


def test_checksum_binary_file(tmp_path):
    f = tmp_path / "data.bin"
    f.write_bytes(bytes(range(256)))
    result = compute_file_checksum(str(f))
    assert len(result) == 64


def test_checksum_empty_file(tmp_path):
    f = tmp_path / "empty.txt"
    f.write_bytes(b"")
    result = compute_file_checksum(str(f))
    assert len(result) == 64
    # SHA-256 of empty bytes is a known constant
    assert result == "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"


def test_checksum_file_not_found():
    with pytest.raises(FileNotFoundError):
        compute_file_checksum("/nonexistent/path/file.txt")


def test_checksum_directory_raises(tmp_path):
    with pytest.raises(IsADirectoryError):
        compute_file_checksum(str(tmp_path))


def test_checksum_lowercase(tmp_path):
    f = tmp_path / "f.txt"
    f.write_bytes(b"abc")
    result = compute_file_checksum(str(f))
    assert result == result.lower()


# ---------------------------------------------------------------------------
# compute_text_checksum
# ---------------------------------------------------------------------------

def test_text_checksum_returns_64_hex_chars():
    result = compute_text_checksum("hello")
    assert len(result) == 64
    int(result, 16)


def test_text_checksum_deterministic():
    assert compute_text_checksum("same") == compute_text_checksum("same")


def test_text_checksum_differs_on_different_text():
    assert compute_text_checksum("A") != compute_text_checksum("B")


def test_text_checksum_empty_string():
    result = compute_text_checksum("")
    assert len(result) == 64
    assert result == "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"


def test_text_checksum_unicode():
    result = compute_text_checksum("日本語テスト")
    assert len(result) == 64


def test_text_checksum_matches_file_checksum_for_utf8(tmp_path):
    """File checksum of UTF-8 encoded text must equal text checksum."""
    text = "hello world"
    f = tmp_path / "t.txt"
    f.write_bytes(text.encode("utf-8"))
    assert compute_file_checksum(str(f)) == compute_text_checksum(text)
