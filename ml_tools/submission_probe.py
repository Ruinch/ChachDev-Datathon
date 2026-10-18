"""Measure one real upload without retrying POST; print only redacted aggregates.

Run on the laptop for loopback acceptance, then on another network for HTTPS.
This verifies transport/contracts, not event truth or legal applicability.
"""
from __future__ import annotations

import argparse
import base64
import getpass
import hashlib
import http.client
from io import BytesIO
import json
import math
import os
from pathlib import Path
import secrets
import time
from urllib.parse import urlsplit
import uuid

from PIL import Image


class ProbeError(RuntimeError):
    """Only fixed, non-sensitive diagnostic codes may reach CLI output."""


class Client:
    def __init__(self, url: str, key: str):
        target = urlsplit(url)
        if (target.username or target.password or target.query or target.fragment
                or target.path not in ('', '/') or not target.hostname
                or target.scheme not in ('http', 'https')
                or (target.scheme == 'http' and target.hostname not in ('127.0.0.1', '::1'))):
            raise ProbeError('require_https_origin_or_literal_loopback_http')
        if not key or any(c in key for c in '\r\n'):
            raise ProbeError('invalid_api_key')
        self.target, self.key = target, key

    def connection(self, timeout=45):
        cls = http.client.HTTPSConnection if self.target.scheme == 'https' else http.client.HTTPConnection
        # No proxy environment, redirects, cookie jar or TLS verification bypass.
        return cls(self.target.hostname, self.target.port, timeout=timeout)

    def request(self, path: str, *, authenticated=True, headers=None, limit=32*1024*1024):
        connection = self.connection()
        try:
            fields = {'X-API-Key': self.key} if authenticated else {}
            fields.update(headers or {})
            connection.request('GET', path, headers=fields)
            response = connection.getresponse()
            body = response.read(limit+1)
            if len(body)>limit:
                raise ProbeError('response_size_limit')
            return response.status, body
        finally:
            connection.close()

    def get_json(self, path):
        status, body = self.request(path)
        if status != 200:
            raise ProbeError(f'get_http_{status}')
        return json.loads(body)

    def upload(self, video: Path, *, rate_mbps: float | None = None):
        if rate_mbps is not None and (not math.isfinite(rate_mbps) or rate_mbps<=0):
            raise ProbeError('rate_mbps_must_be_finite_positive')
        boundary = 'probe-'+secrets.token_hex(16)
        prefix = (f'--{boundary}\r\nContent-Disposition: form-data; name="file"; filename="probe.mov"'
                  '\r\nContent-Type: video/quicktime\r\n\r\n').encode('ascii')
        suffix = f'\r\n--{boundary}--\r\n'.encode('ascii')
        connection = self.connection()
        started = time.monotonic()
        try:
            connection.putrequest('POST', '/api/v1/analyze')
            connection.putheader('X-API-Key', self.key)
            connection.putheader('Content-Type', 'multipart/form-data; boundary='+boundary)
            connection.putheader('Content-Length', str(len(prefix)+video.stat().st_size+len(suffix)))
            connection.endheaders()
            connection.send(prefix)
            sent = 0
            with video.open('rb') as stream:
                while chunk := stream.read(64*1024):
                    connection.send(chunk)
                    sent += len(chunk)
                    if rate_mbps:
                        pause = sent*8/(rate_mbps*1_000_000) - (time.monotonic()-started)
                        if pause>0:
                            time.sleep(pause)
            connection.send(suffix)
            sent_sec = time.monotonic()-started
            response = connection.getresponse()
            body = response.read(64*1024+1)
            if response.status != 202:
                raise ProbeError(f'upload_http_{response.status}')
            if len(body)>64*1024:
                raise ProbeError('upload_acceptance_invalid_check_history')
            data = json.loads(body)
            if data.get('status') != 'processing':
                raise ProbeError('upload_acceptance_invalid_check_history')
            return str(uuid.UUID(data['job_id'])), sent_sec
        except (OSError, http.client.HTTPException):
            raise ProbeError('upload_outcome_unknown_check_history_do_not_retry') from None
        finally:
            connection.close()


def probe(base_url: str, video: Path, key: str, *, verify_source=False, rate_mbps=None) -> dict:
    client = Client(base_url, key)
    if video.suffix.lower() != '.mov' or not video.is_file():
        raise ProbeError('existing_mov_required')
    status, _ = client.request('/', authenticated=False)
    if status != 200:
        raise ProbeError('homepage_unavailable')
    unauthorized, _ = client.request('/api/v1/system', authenticated=False)
    if unauthorized != 401:
        raise ProbeError('unauthenticated_api_not_rejected')
    state = client.get_json('/api/v1/system')
    if not state['ready']:
        raise ProbeError('system_not_ready_no_upload_sent')
    catalog = client.get_json('/api/v1/rules')['items']
    started = time.monotonic()
    job_id, sent_sec = client.upload(video, rate_mbps=rate_mbps)
    accepted_sec = time.monotonic()-started
    while True:
        if time.monotonic()-started >= 300:
            raise ProbeError('completion_deadline_300s_check_history')
        result = client.get_json('/api/v1/jobs/'+job_id)
        if result['status'] != 'processing':
            break
        time.sleep(.25)
    elapsed = time.monotonic()-started
    summary = {'status': result['status'], 'verdict': result.get('verdict'),
               'error_code': result.get('error', {}).get('code'),
               'unauthenticated_status': unauthorized,
               'file_mib': round(video.stat().st_size/2**20, 3),
               'body_sent_sec': round(sent_sec, 3),
               'accepted_sec_including_probe': round(accepted_sec, 3),
               'end_to_end_sec': round(elapsed, 3), 'within_300_seconds': elapsed<=300,
               'upload_timeout_sec': state['limits']['upload_timeout_sec'],
               'events': len(result.get('violations', [])), 'valid_jpeg_frames': 0,
               'catalog_references_match': True, 'source_sha256_match': None,
               'scope': 'Transport and schema acceptance only; no claim of ML or legal accuracy.'}
    for event in result.get('violations', []):
        if not any(r['enabled'] and r['event_type']==event['type'] and r['reference']==event['rule'] for r in catalog):
            raise ProbeError('event_reference_differs_from_server_catalog')
        if not 1<=len(event['evidence_frames'])<=3:
            raise ProbeError('invalid_evidence_count')
        for frame in event['evidence_frames']:
            raw = base64.b64decode(frame['image_base64'], validate=True)
            with Image.open(BytesIO(raw)) as image:
                if image.format != 'JPEG':
                    raise ProbeError('evidence_not_jpeg')
                image.load()
            summary['valid_jpeg_frames'] += 1
    summary['result_reopened'] = client.get_json('/api/v1/jobs/'+job_id)==result
    history = client.get_json('/api/v1/jobs?limit=100')['items']
    summary['listed_in_history'] = any(item['job_id']==job_id for item in history)
    status, source = client.request('/api/v1/jobs/'+job_id+'/video', headers={'Range':'bytes=0-31'}, limit=32)
    with video.open('rb') as stream:
        summary['range_matches_source'] = status==206 and source==stream.read(32)
    if verify_source:
        status, source = client.request('/api/v1/jobs/'+job_id+'/video', limit=video.stat().st_size)
        with video.open('rb') as stream:
            summary['source_sha256_match'] = status==200 and hashlib.sha256(source).digest()==hashlib.file_digest(stream,'sha256').digest()
    if not all(summary[k] for k in ('result_reopened','listed_in_history','range_matches_source')) or summary['source_sha256_match'] is False:
        raise ProbeError('saved_result_or_original_check_failed')
    return summary


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--base-url', required=True)
    parser.add_argument('--video', required=True, type=Path)
    parser.add_argument('--output', required=True, type=Path)
    parser.add_argument('--verify-source', action='store_true')
    parser.add_argument('--rate-mbps', type=float, help='Optional local throttling experiment; not an external network measurement')
    args = parser.parse_args()
    if args.output.exists():
        raise SystemExit('Choose a fresh output file; nothing uploaded.')
    key = os.environ.get('APP_API_KEY') or getpass.getpass('API key (hidden): ')
    try:
        summary = probe(args.base_url, args.video, key, verify_source=args.verify_source, rate_mbps=args.rate_mbps)
    except ProbeError as error:
        summary = {'status':'probe_failed','error_code':str(error)}
    except (OSError, ValueError, KeyError, TypeError, http.client.HTTPException):
        summary = {'status':'probe_failed','error_code':'transport_or_contract_error_check_history'}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open('x', encoding='utf-8') as stream:
        json.dump(summary, stream, indent=2)
    print(json.dumps(summary))
    if summary['status'] != 'done' or not summary.get('within_300_seconds'):
        raise SystemExit(1)


if __name__ == '__main__':
    main()
