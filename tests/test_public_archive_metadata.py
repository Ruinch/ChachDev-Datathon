import io
import struct
import zipfile

import pytest

from ml_tools import public_archive_metadata as metadata


@pytest.mark.parametrize('status,content_range', [(200, None), (206, 'bytes 1-4/10')])
def test_ignored_or_wrong_range_never_reads_large_body(monkeypatch, status, content_range):
    class Response:
        headers = {'Content-Range': content_range}
        def __enter__(self):
            return self
        def __exit__(self, *args):
            pass
        def read(self, *args):
            pytest.fail('Invalid range response must be rejected before reading its body')
    response = Response()
    response.status = status
    monkeypatch.setattr(metadata.urllib.request, 'urlopen', lambda *args, **kwargs: response)
    with pytest.raises(ValueError, match='not honored'):
        metadata.read_range(metadata.SOURCES['merl'], 0, 3, 10)


def test_range_budget_and_source_checked_before_network(monkeypatch):
    monkeypatch.setattr(metadata.urllib.request, 'urlopen', lambda *args, **kwargs: pytest.fail('No network expected'))
    with pytest.raises(ValueError):
        metadata.read_range(metadata.SOURCES['retail'], 0, metadata.MAX_METADATA_BYTES, 10_000_000)
    with pytest.raises(ValueError):
        metadata.read_range('https://example.com/archive.zip', 0, 3, 10)


def test_zip_directory_inspection_never_extracts_names(tmp_path):
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, 'w') as archive:
        archive.writestr('../outside.mp4', b'synthetic payload')
        archive.writestr('fixture.mp4', b'another payload')
    raw = stream.getvalue()
    footer = struct.unpack('<4s4H2LH', raw[-22:])
    size, offset = footer[5:7]
    rows = metadata.parse_zip_directory(raw[offset:offset + size], 2)
    assert rows[0]['path'] == '../outside.mp4'  # Metadata only, never used as an output path.
    assert not (tmp_path.parent / 'outside.mp4').exists()
    assert rows[0]['bytes'] == len(b'synthetic payload')
    with pytest.raises(ValueError):
        metadata.parse_zip_directory(raw[offset:offset + size - 1], 2)
    with pytest.raises(ValueError, match='count mismatch'):
        metadata.parse_zip_directory(raw[offset:offset + size], 3)
