from __future__ import annotations

import pytest

from aevrel.storage.file import FileStorage, StorageError, _is_windows_drive_path


def test_file_storage_read_write_ranges(tmp_path) -> None:
    path = tmp_path / "data.bin"
    storage = FileStorage()
    uri = path.as_uri()

    storage.ensure_size(uri, 12)
    storage.write_range(uri, 2, b"abcd")
    assert storage.read_range(uri, 0, 12) == b"\x00\x00abcd\x00\x00\x00\x00\x00\x00"
    assert storage.size(uri) == 12


def test_file_storage_rejects_short_read(tmp_path) -> None:
    path = tmp_path / "data.bin"
    path.write_bytes(b"abc")
    storage = FileStorage()
    with pytest.raises(StorageError):
        storage.read_range(path.as_uri(), 0, 4)


def test_file_storage_rejects_non_file_uri() -> None:
    storage = FileStorage()
    with pytest.raises(StorageError):
        storage.exists("https://example.com/data.bin")


def test_file_storage_can_fsync_written_file(tmp_path) -> None:
    path = tmp_path / "written.bin"
    storage = FileStorage()
    storage.ensure_size(path.as_uri(), 4)
    storage.write_range(path.as_uri(), 0, b"data")
    storage.fsync(path.as_uri())
    assert path.read_bytes() == b"data"


def test_windows_drive_uri_path_detection() -> None:
    assert _is_windows_drive_path("/C:/Users/demo/data.bin")
    assert not _is_windows_drive_path("/tmp/data.bin")
