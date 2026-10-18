"""Serve an explicit private annotation queue on loopback; never expose a directory."""
from __future__ import annotations

import argparse
import copy
import hashlib
import hmac
import json
import math
import os
import re
import secrets
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from app.ml.rules import RULES


MAX_SAVE_BYTES = 1024 * 1024
UNKNOWN_GROUPS = {'organizer-single-camera', 'unknown-session', 'unknown', 'unassigned'}


def _json_document(raw: bytes) -> dict:
    def unique_object(pairs):
        value = {}
        for key, item in pairs:
            if key in value:
                raise ValueError('Duplicate fields are not allowed')
            value[key] = item
        return value
    def invalid_constant(_):
        raise ValueError('Non-finite numbers are not allowed')
    value = json.loads(raw, object_pairs_hook=unique_object, parse_constant=invalid_constant)
    if not isinstance(value, dict):
        raise ValueError('Expected a JSON object')
    return value


def _validate_saved(value: dict, records: dict) -> dict:
    if (set(value) != {'version', 'revision', 'annotations'} or type(value['version']) is not int
            or value['version'] != 1 or type(value['revision']) is not int or value['revision'] < 0
            or not isinstance(value['annotations'], dict)):
        raise ValueError('Invalid annotation document')
    annotations = value['annotations']
    if set(annotations) - set(records):
        raise ValueError('Unknown source ID')
    fields = {'sha256', 'reviewed', 'source_group', 'viewpoint', 'verdict', 'events', 'notes'}
    for key, annotation in annotations.items():
        if not isinstance(annotation, dict) or set(annotation) != fields:
            raise ValueError('Invalid annotation fields')
        if annotation['sha256'] != records[key]['sha256'] or type(annotation['reviewed']) is not bool:
            raise ValueError('Invalid annotation source or review flag')
        for name, limit in [('source_group', 512), ('viewpoint', 4096), ('notes', 32768)]:
            if not isinstance(annotation[name], str) or len(annotation[name]) > limit:
                raise ValueError('Invalid annotation text')
        group = annotation['source_group'].strip()
        if annotation['reviewed'] and (not group or group.lower() in UNKNOWN_GROUPS):
            raise ValueError('Reviewed annotation requires a known source group')
        verdict, events = annotation['verdict'], annotation['events']
        if (verdict not in ('violation', 'no_violation', 'uncertain')
                or not isinstance(events, list) or len(events) > 100):
            raise ValueError('Invalid verdict or events')
        if (verdict == 'violation' and not events) or (verdict == 'no_violation' and events):
            raise ValueError('Verdict contradicts events')
        for event in events:
            if not isinstance(event, dict) or set(event) != {'rule_id', 'start_sec', 'end_sec', 'observation'}:
                raise ValueError('Invalid event fields')
            if not isinstance(event['rule_id'], str) or event['rule_id'] not in {*RULES, 'OTHER'}:
                raise ValueError('Unknown event rule')
            start, end = event['start_sec'], event['end_sec']
            if (type(start) not in (int, float) or type(end) not in (int, float)
                    or not math.isfinite(start) or not math.isfinite(end)
                    or not 0 <= start <= end <= records[key]['duration_sec']):
                raise ValueError('Event interval is outside its source')
            if not isinstance(event['observation'], str) or not event['observation'].strip() or len(event['observation']) > 4096:
                raise ValueError('Invalid event observation')
    return annotations


def _atomic_save(path: Path, value: dict) -> None:
    raw = json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False).encode('utf-8')
    if len(raw) > MAX_SAVE_BYTES:
        raise ValueError('Saved annotation document is too large')
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode='wb', dir=path.parent, prefix='.human-review-', suffix='.tmp', delete=False) as stream:
            temporary = Path(stream.name)
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def prepare_queue(source: Path, destination: Path) -> dict:
    """Keep human drafts, omit machine suggestions, and select one file per hash."""
    manifest = json.loads((source / 'dataset.json').read_text(encoding='utf-8'))
    priority = json.loads((source / 'priority.json').read_text(encoding='utf-8'))
    seed = json.loads((source / 'review-seed.json').read_text(encoding='utf-8'))['annotations']
    old_path = source / 'previous-human-export.json'
    old = json.loads(old_path.read_text(encoding='utf-8'))['annotations'] if old_path.exists() else {}
    by_id = {record['id']: record for record in manifest['records']}
    records, annotations, hashes = [], {}, set()
    for item in priority:
        record = by_id[item['id']]
        if record['sha256'] in hashes:
            continue
        hashes.add(record['sha256'])
        draft = copy.deepcopy(seed.get(record['id'], {}))
        draft['notes'] = draft.get('notes', '').split('\nВторичный разбор ИИ', 1)[0]
        draft.update(sha256=record['sha256'], reviewed=False, source_group='', viewpoint='',
                     verdict='uncertain', events=[])
        previous = next((value for value in old.values() if value.get('sha256') == record['sha256']), None)
        if previous:
            draft.update(copy.deepcopy(previous))
            draft['reviewed'] = False
            draft['notes'] = (draft.get('notes', '') + '\nПрежний ручной черновик: повторно проверьте весь ролик, '
                              'тип события и группу съёмки; готовность эталона не подтверждена.')
        records.append(copy.deepcopy(record))
        annotations[record['id']] = draft
    if not records:
        raise ValueError('Priority queue is empty')
    queue = {'version': 1, 'manifest': {**manifest, 'source_grouping_reviewed': False, 'records': records},
             'annotations': annotations}
    destination.mkdir(parents=True, exist_ok=False)
    (destination / 'queue.json').write_text(json.dumps(queue, ensure_ascii=False, indent=2), encoding='utf-8')
    return {'videos': len(records), 'preserved_human_drafts': sum(
        any(value.get('sha256') == record['sha256'] for value in old.values()) for record in records)}


def make_server(queue_path: Path, *, port: int = 8766, save: bool = False) -> ThreadingHTTPServer:
    queue = json.loads(queue_path.read_text(encoding='utf-8'))
    records = queue['manifest']['records']
    by_id = {record['id']: record for record in records}
    if len(by_id) != len(records) or not records:
        raise ValueError('Queue must have unique source IDs')
    for record in records:
        duration = record['duration_sec']
        if not isinstance(record['id'], str) or type(duration) not in (int, float) or not math.isfinite(duration) or duration <= 0:
            raise ValueError('Invalid queue source metadata')
    paths = [Path(record['path']).resolve(strict=True) for record in records]
    for record, path in zip(records, paths):
        if path.suffix.lower() != '.mov' or not path.is_file():
            raise ValueError('Queue must contain existing MOV files')
        with path.open('rb') as stream:
            if hashlib.file_digest(stream, 'sha256').hexdigest() != record['sha256']:
                raise ValueError('Queue source hash mismatch')
    saved_path = queue_path.with_name('human-review.json')
    saved = {'version': 1, 'revision': 0, 'annotations': {}}
    if saved_path.exists():
        if saved_path.is_symlink() or not saved_path.is_file() or saved_path.stat().st_size > MAX_SAVE_BYTES:
            raise ValueError('Invalid local annotation file')
        try:
            saved = _json_document(saved_path.read_bytes())
            _validate_saved(saved, by_id)
        except (ValueError, TypeError, KeyError, RecursionError) as exc:
            raise ValueError('Local annotations are invalid; original file was not changed') from None
    state_lock = threading.Lock()
    public = copy.deepcopy(queue)
    public.setdefault('annotations', {}).update(copy.deepcopy(saved['annotations']))
    for index, record in enumerate(public['manifest']['records']):
        record.pop('path', None)
        record['media_url'] = f'/video/{index}'
    capability = secrets.token_urlsafe(32) if save else None
    html = Path(__file__).with_name('review.html').read_text(encoding='utf-8')
    config = {'endpoint': '/queue.json'}
    if save:
        config.update(save_endpoint='/annotations', save_token=capability)
    html = html.replace('</head>', f'<script>window.__LOCAL_REVIEW__={json.dumps(config)};</script></head>', 1)
    html_bytes = html.encode('utf-8')

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass  # Source names and annotation content never enter access logs.

        def _same_origin(self, *, writing=False):
            origin = f'http://127.0.0.1:{self.server.server_port}'
            return not (self.client_address[0] != '127.0.0.1'
                    or len(self.headers.get_all('Host', [])) != 1
                    or self.headers.get('Host') != origin.removeprefix('http://')
                    or len(self.headers.get_all('Origin', [])) > 1
                    or self.headers.get('Origin') not in ({origin} if writing else {None, origin})
                    or self.headers.get('Sec-Fetch-Site') in {'cross-site', 'same-site'})

        def _json(self, status, body):
            encoded = json.dumps(body, ensure_ascii=False).encode('utf-8')
            self.send_response(status)
            self.send_header('Content-Type', 'application/json; charset=utf-8')
            self.send_header('Content-Length', str(len(encoded)))
            self.send_header('Cache-Control', 'no-store')
            self.send_header('X-Content-Type-Options', 'nosniff')
            self.send_header('X-Frame-Options', 'DENY')
            self.end_headers()
            self.wfile.write(encoded)

        def do_POST(self):
            nonlocal saved
            if not self._same_origin(writing=True):
                self.send_error(403)
                return
            if self.path != '/annotations' or not save:
                self.send_error(404)
                return
            token = self.headers.get('X-Review-Token', '')
            if (len(self.headers.get_all('X-Review-Token', [])) != 1 or not token.isascii()
                    or not hmac.compare_digest(token, capability)):
                self.send_error(403)
                return
            length = self.headers.get('Content-Length', '')
            if (len(self.headers.get_all('Content-Length', [])) != 1 or not length.isascii()
                    or not length.isdigit() or self.headers.get('Transfer-Encoding')):
                self.send_error(400)
                return
            if len(length) > 10 or int(length) > MAX_SAVE_BYTES:
                self.send_error(413)
                return
            if self.headers.get('Content-Type', '').split(';', 1)[0].strip().lower() != 'application/json':
                self.send_error(415)
                return
            self.connection.settimeout(5)
            try:
                raw = self.rfile.read(int(length))
                if len(raw) != int(length):
                    raise ValueError('Incomplete request')
                value = _json_document(raw)
            except (ValueError, RecursionError):
                self._json(400, {'error': 'invalid_json'})
                return
            except (TimeoutError, OSError):
                self.send_error(408)
                return
            try:
                updates = _validate_saved(value, by_id)
                if not updates:
                    raise ValueError('No annotations supplied')
            except (ValueError, TypeError, KeyError, RecursionError):
                self._json(422, {'error': 'invalid_annotations'})
                return
            with state_lock:
                if value['revision'] != saved['revision']:
                    self._json(409, {'error': 'stale_revision'})
                    return
                replacement = {'version': 1, 'revision': saved['revision'] + 1,
                               'annotations': {**saved['annotations'], **updates}}
                try:
                    _atomic_save(saved_path, replacement)
                except ValueError:
                    self._json(413, {'error': 'annotations_too_large'})
                    return
                except OSError:
                    self._json(503, {'error': 'save_failed'})
                    return
                saved = replacement
                public['annotations'].update(copy.deepcopy(updates))
                self._json(200, {'saved': len(updates), 'revision': saved['revision']})

        def do_GET(self):
            if not self._same_origin():
                self.send_error(403)
                return
            if self.path in {'/', '/queue.json'}:
                with state_lock:
                    body = html_bytes if self.path == '/' else json.dumps(
                        {**public, 'save_revision': saved['revision']}, ensure_ascii=False).encode('utf-8')
                self.send_response(200)
                self.send_header('Content-Type', 'text/html; charset=utf-8' if self.path == '/' else 'application/json; charset=utf-8')
                self.send_header('Content-Length', str(len(body)))
                self.send_header('Cache-Control', 'no-store')
                self.send_header('X-Content-Type-Options', 'nosniff')
                self.send_header('X-Frame-Options', 'DENY')
                self.end_headers()
                self.wfile.write(body)
                return
            match = re.fullmatch(r'/video/(\d+)', self.path)
            if not match or int(match[1]) >= len(paths):
                self.send_error(404)
                return
            path = paths[int(match[1])]
            size = path.stat().st_size
            start, end = 0, size - 1
            requested = self.headers.get('Range')
            if requested:
                byte_range = re.fullmatch(r'bytes=(\d+)-(\d*)', requested)
                if not byte_range:
                    self.send_error(416)
                    return
                start = int(byte_range[1])
                end = min(int(byte_range[2]) if byte_range[2] else size - 1, size - 1)
                if not 0 <= start <= end < size:
                    self.send_error(416)
                    return
            self.send_response(206 if requested else 200)
            self.send_header('Content-Type', 'video/quicktime')
            self.send_header('Content-Length', str(end - start + 1))
            self.send_header('Accept-Ranges', 'bytes')
            self.send_header('Cache-Control', 'no-store')
            if requested:
                self.send_header('Content-Range', f'bytes {start}-{end}/{size}')
            self.end_headers()
            try:
                with path.open('rb') as stream:
                    stream.seek(start)
                    remaining = end - start + 1
                    while remaining:
                        chunk = stream.read(min(65536, remaining))
                        if not chunk:
                            break
                        self.wfile.write(chunk)
                        remaining -= len(chunk)
            except (ConnectionResetError, BrokenPipeError, ConnectionAbortedError):
                pass

    return ThreadingHTTPServer(('127.0.0.1', port), Handler)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--queue', type=Path)
    parser.add_argument('--prepare-from', type=Path)
    parser.add_argument('--destination', type=Path)
    parser.add_argument('--port', type=int, default=8766)
    parser.add_argument('--save', action='store_true', help='Allow local annotation saves to sibling human-review.json')
    args = parser.parse_args()
    if args.prepare_from:
        if not args.destination or args.queue:
            parser.error('Use --prepare-from SOURCE --destination NEW_DIRECTORY')
        print(json.dumps(prepare_queue(args.prepare_from, args.destination)))
        return
    if not args.queue:
        parser.error('--queue is required to serve an existing queue')
    server = make_server(args.queue, port=args.port, save=args.save)
    print(f'Local annotation queue: http://127.0.0.1:{server.server_port}/', flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == '__main__':
    main()
