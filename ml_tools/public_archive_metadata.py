"""Inspect two public dataset archives without downloading or extracting videos.

Uses stdlib only. Reads an exact TAR header or ZIP end record/central directory;
rejects servers that ignore byte ranges. No dataset SDK, model, or GPU is used.
"""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import struct
import urllib.request


SOURCES = {
    'merl': 'https://www.merl.com/pub/tmarks/MERL_Shopping_Dataset/Videos_MERL_Shopping_Dataset.zip',
    'retail': 'https://huggingface.co/datasets/standard-cognition/RetailAction/resolve/main/data/validation.tar',
}
MAX_METADATA_BYTES = 2 * 1024 * 1024


def read_range(url: str, start: int, end: int, total: int) -> bytes:
    size = end - start + 1
    if url not in SOURCES.values() or not 0 <= start <= end < total or size > MAX_METADATA_BYTES:
        raise ValueError('Unsupported source or metadata range')
    request = urllib.request.Request(url, headers={'Range': f'bytes={start}-{end}',
                                                 'Accept-Encoding': 'identity'})
    with urllib.request.urlopen(request, timeout=20) as response:
        if response.status != 206 or response.headers.get('Content-Range') != f'bytes {start}-{end}/{total}':
            raise ValueError('Exact byte range not honored; no response body was downloaded')
        if response.headers.get('Content-Encoding', 'identity') not in ('', 'identity'):
            raise ValueError('Encoded range response is unsupported')
        data = response.read(size + 1)
        if len(data) != size:
            raise ValueError('Unexpected range length')
        return data


def parse_zip_directory(data: bytes, expected_entries: int) -> list[dict]:
    rows = []
    offset = 0
    while offset < len(data):
        if len(data) - offset < 46 or data[offset:offset + 4] != b'PK\x01\x02':
            raise ValueError('Invalid ZIP central directory')
        fields = struct.unpack('<4s6H3L5H2L', data[offset:offset + 46])
        name_size, extra_size, comment_size = fields[10:13]
        end = offset + 46 + name_size + extra_size + comment_size
        if end > len(data) or fields[13] != 0:
            raise ValueError('Truncated or split ZIP entry')
        name = data[offset + 46:offset + 46 + name_size].decode('utf-8' if fields[3] & 2048 else 'cp437')
        rows.append({'path': name, 'compressed_bytes': fields[8], 'bytes': fields[9],
                     'compression': fields[4], 'crc32': f'{fields[7]:08x}', 'local_header_offset': fields[16]})
        offset = end
    if len(rows) != expected_entries:
        raise ValueError('ZIP entry count mismatch')
    return rows


def inspect_archive(source: str, retail_offset: int = 0) -> dict:
    url = SOURCES[source]
    with urllib.request.urlopen(urllib.request.Request(url, method='HEAD'), timeout=20) as response:
        total = int(response.headers['Content-Length'])
    if source == 'merl':
        # This official archive has no ZIP comment. Fail closed if its layout changes;
        # reading a blind 64 KiB suffix could include compressed video payload.
        footer = read_range(url, total - 22, total - 1, total)
        signature, disk, directory_disk, disk_entries, count, size, offset, comment = struct.unpack('<4s4H2LH', footer)
        if signature != b'PK\x05\x06' or disk or directory_disk or disk_entries != count or comment:
            raise ValueError('Unsupported ZIP end record; full download is not a fallback')
        if count == 65535 or size == 4294967295 or offset + size != total - 22:
            raise ValueError('Unsupported ZIP64 or central-directory layout')
        body = read_range(url, offset, offset + size - 1, total)
        details = {'entries': parse_zip_directory(body, count), 'central_directory_offset': offset}
        downloaded = len(footer) + len(body)
    else:
        if retail_offset < 0 or retail_offset % 512:
            raise ValueError('TAR header offset must be a nonnegative multiple of 512')
        body = read_range(url, retail_offset, retail_offset + 511, total)
        expected = int(body[148:156].strip(b'\0 ') or b'0', 8)
        if expected != sum(body[:148]) + 8 * 32 + sum(body[156:]):
            raise ValueError('Offset is not a valid TAR header')
        size = int(body[124:136].strip(b'\0 ') or b'0', 8)
        name = body[:100].rstrip(b'\0').decode('utf-8')
        details = {'entry': {'path': name, 'bytes': size, 'type': body[156:157].decode('ascii'),
                             'header_offset': retail_offset,
                             'next_header_offset': retail_offset + 512 + ((size + 511) // 512) * 512}}
        downloaded = len(body)
    return {'source_url': url, 'archive_bytes': total, 'metadata_downloaded_bytes': downloaded,
            'metadata_sha256': hashlib.sha256(body).hexdigest(), 'video_downloaded': False,
            'archive_extracted': False, **details}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('source', choices=tuple(SOURCES))
    parser.add_argument('--retail-offset', type=int, default=0)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    output = args.output.resolve()
    private_root = Path('.local_ml').resolve()
    if output == private_root or not output.is_relative_to(private_root) or output.exists():
        parser.error('Choose a new JSON file under .local_ml; existing results are preserved')
    try:
        report = inspect_archive(args.source, args.retail_offset)
        output.parent.mkdir(parents=True, exist_ok=True)
        with output.open('x', encoding='utf-8') as stream:
            json.dump(report, stream, indent=2)
        print(json.dumps({key: report[key] for key in ('archive_bytes', 'metadata_downloaded_bytes',
                                                      'video_downloaded', 'archive_extracted')}))
        return 0
    except Exception as error:
        print(json.dumps({'status': 'failed', 'error_type': type(error).__name__,
                          'notice': 'No full archive download or extraction fallback.'}))
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
