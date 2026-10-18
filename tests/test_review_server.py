from __future__ import annotations

import hashlib
import http.client
import json
import re
import threading
from contextlib import contextmanager

import pytest

from ml_tools.review_server import make_server, prepare_queue


@contextmanager
def running_queue(queue_path, *, save=True):
    server = make_server(queue_path, port=0, save=save)
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()
    origin = f'http://127.0.0.1:{server.server_port}'

    def request(method, path, body=None, headers=None):
        connection = http.client.HTTPConnection('127.0.0.1', server.server_port, timeout=5)
        connection.request(method, path, body=body, headers=headers or {})
        response = connection.getresponse()
        result = response.status, response.read()
        connection.close()
        return result

    try:
        status, html = request('GET', '/')
        assert status == 200
        config = json.loads(re.search(rb'window\.__LOCAL_REVIEW__=(.*?);</script>', html)[1])
        headers = {'Origin': origin, 'Content-Type': 'application/json',
                   'X-Review-Token': config.get('save_token', '')}
        yield request, headers
    finally:
        server.shutdown()
        server.server_close()
        worker.join(timeout=3)


@pytest.fixture
def save_queue(tmp_path):
    source = source_queue(tmp_path)
    destination = tmp_path / 'prepared'
    prepare_queue(source, destination)
    return destination / 'queue.json'


def draft_payload(queue_path, *, revision=0):
    queue = json.loads(queue_path.read_text(encoding='utf-8'))
    draft = queue['annotations']['synthetic']
    draft['notes'] = 'Human draft: сохранить эту заметку'
    return {'version': 1, 'revision': revision, 'annotations': {'synthetic': draft}}


def test_save_is_opt_in_and_survives_restart_without_altering_queue(save_queue):
    original = save_queue.read_bytes()
    with running_queue(save_queue, save=False) as (request, headers):
        assert request('POST', '/annotations', '{}', headers)[0] == 404
    assert not save_queue.with_name('human-review.json').exists()
    payload = draft_payload(save_queue)
    with running_queue(save_queue) as (request, headers):
        status, body = request('POST', '/annotations', json.dumps(payload), headers)
        assert status == 200 and json.loads(body)['revision'] == 1
        assert request('GET', '/human-review.json')[0] == 404
    assert save_queue.read_bytes() == original
    with running_queue(save_queue) as (request, headers):
        status, body = request('GET', '/queue.json')
        restored = json.loads(body)
        assert status == 200 and restored['save_revision'] == 1
        assert restored['annotations']['synthetic'] == payload['annotations']['synthetic']
        assert request('POST', '/annotations', json.dumps(payload), headers)[0] == 409


@pytest.mark.parametrize('change', [
    {'Origin': 'https://example.com'}, {'Origin': ''}, {'Host': 'example.com'},
    {'X-Review-Token': ''}, {'X-Review-Token': 'invalid'}, {'Sec-Fetch-Site': 'cross-site'},
])
def test_save_requires_same_origin_and_server_capability(save_queue, change):
    with running_queue(save_queue) as (request, headers):
        headers.update(change)
        assert request('POST', '/annotations', json.dumps(draft_payload(save_queue)), headers)[0] == 403
    assert not save_queue.with_name('human-review.json').exists()


@pytest.mark.parametrize('change', [
    {'sha256': 'wrong'}, {'reviewed': 'true'}, {'reviewed': True, 'source_group': ''},
    {'reviewed': True, 'source_group': 'unknown'}, {'verdict': 'violation'},
    {'notes': ['not-text']},
])
def test_save_validates_drafts_and_does_not_promote_unreviewed_groups(save_queue, change):
    payload = draft_payload(save_queue)
    payload['annotations']['synthetic'].update(change)
    with running_queue(save_queue) as (request, headers):
        assert request('POST', '/annotations', json.dumps(payload), headers)[0] == 422
    assert not save_queue.with_name('human-review.json').exists()


@pytest.mark.parametrize('event', [
    {'rule_id': 'UNKNOWN', 'start_sec': 1, 'end_sec': 2, 'observation': 'Synthetic'},
    {'rule_id': 'OTHER', 'start_sec': -1, 'end_sec': 2, 'observation': 'Synthetic'},
    {'rule_id': 'R22', 'start_sec': 4, 'end_sec': 3, 'observation': 'Synthetic'},
    {'rule_id': 'R22', 'start_sec': 1, 'end_sec': 6, 'observation': 'Synthetic'},
    {'rule_id': 'R22', 'start_sec': True, 'end_sec': 2, 'observation': 'Synthetic'},
    {'rule_id': 'R22', 'start_sec': 1, 'end_sec': 2, 'observation': ''},
])
def test_save_rejects_unknown_rules_and_bad_source_intervals(save_queue, event):
    payload = draft_payload(save_queue)
    payload['annotations']['synthetic'].update(verdict='violation', events=[event])
    with running_queue(save_queue) as (request, headers):
        assert request('POST', '/annotations', json.dumps(payload), headers)[0] == 422


def test_other_event_allowed_but_no_violation_cannot_contain_events(save_queue):
    payload = draft_payload(save_queue)
    payload['annotations']['synthetic'].update(
        reviewed=True, source_group='human-session-a', verdict='violation',
        events=[dict(rule_id='OTHER', start_sec=0, end_sec=5, observation='Synthetic bag-like object')])
    with running_queue(save_queue) as (request, headers):
        assert request('POST', '/annotations', json.dumps(payload), headers)[0] == 200
        payload['revision'] = 1
        payload['annotations']['synthetic']['verdict'] = 'no_violation'
        assert request('POST', '/annotations', json.dumps(payload), headers)[0] == 422
        payload['annotations'] = {'foreign': payload['annotations']['synthetic']}
        assert request('POST', '/annotations', json.dumps(payload), headers)[0] == 422


def test_save_rejects_oversize_body_before_reading(save_queue):
    with running_queue(save_queue) as (request, headers):
        headers['Content-Length'] = str(2 * 1024 * 1024)
        assert request('POST', '/annotations', b'{}', headers)[0] == 413


def test_atomic_replace_failure_keeps_previous_notes_and_revision(save_queue, monkeypatch):
    payload = draft_payload(save_queue)
    with running_queue(save_queue) as (request, headers):
        assert request('POST', '/annotations', json.dumps(payload), headers)[0] == 200
        saved = save_queue.with_name('human-review.json').read_bytes()
        payload['revision'] = 1
        payload['annotations']['synthetic']['notes'] = 'Changed but not persisted'
        def fail_replace(*_):
            raise OSError('synthetic disk failure')
        monkeypatch.setattr('ml_tools.review_server.os.replace', fail_replace)
        assert request('POST', '/annotations', json.dumps(payload), headers)[0] == 503
        assert save_queue.with_name('human-review.json').read_bytes() == saved
        status, body = request('GET', '/queue.json')
        assert status == 200 and json.loads(body)['save_revision'] == 1
        assert json.loads(body)['annotations']['synthetic']['notes'] != 'Changed but not persisted'


def test_partial_saves_preserve_other_records_and_never_replace_original_notes(save_queue):
    queue = json.loads(save_queue.read_text(encoding='utf-8'))
    second = {**queue['manifest']['records'][0], 'id': 'second'}
    queue['manifest']['records'].append(second)
    queue['annotations']['second'] = {**queue['annotations']['synthetic'], 'notes': 'Second original note'}
    save_queue.write_text(json.dumps(queue), encoding='utf-8')
    original = save_queue.read_bytes()
    first = draft_payload(save_queue)
    with running_queue(save_queue) as (request, headers):
        assert request('POST', '/annotations', json.dumps(first), headers)[0] == 200
        second_update = {'version': 1, 'revision': 1, 'annotations': {
            'second': {**queue['annotations']['second'], 'notes': 'Second edited note'},
        }}
        assert request('POST', '/annotations', json.dumps(second_update), headers)[0] == 200
        status, raw = request('GET', '/queue.json')
        restored = json.loads(raw)['annotations']
        assert status == 200
        assert restored['synthetic']['notes'] == first['annotations']['synthetic']['notes']
        assert restored['second']['notes'] == 'Second edited note'
    assert save_queue.read_bytes() == original
    with running_queue(save_queue) as (request, _):
        assert json.loads(request('GET', '/queue.json')[1])['annotations'] == restored


def test_invalid_saved_source_is_rejected_at_restart_without_clobbering(save_queue):
    saved_path = save_queue.with_name('human-review.json')
    payload = draft_payload(save_queue)
    payload['annotations']['synthetic']['sha256'] = 'wrong-source'
    saved_path.write_text(json.dumps(payload), encoding='utf-8')
    original = saved_path.read_bytes()
    with pytest.raises(ValueError, match='original file was not changed'):
        make_server(save_queue, port=0, save=True)
    assert saved_path.read_bytes() == original


def test_token_rotates_after_restart_and_missing_origin_is_rejected(save_queue):
    with running_queue(save_queue) as (_, first_headers):
        previous_token = first_headers['X-Review-Token']
    with running_queue(save_queue) as (request, headers):
        assert headers['X-Review-Token'] != previous_token
        raw = json.dumps(draft_payload(save_queue))
        assert request('POST', '/annotations', raw, {**headers, 'X-Review-Token': previous_token})[0] == 403
        headers.pop('Origin')
        assert request('POST', '/annotations', raw, headers)[0] == 403


@pytest.mark.parametrize('body', ['{"version":1,"version":1}', '{"value":NaN}'])
def test_ambiguous_or_nonfinite_json_is_rejected(save_queue, body):
    with running_queue(save_queue) as (request, headers):
        assert request('POST', '/annotations', body, headers)[0] == 400


def source_queue(tmp_path):
    source = tmp_path / 'source'
    source.mkdir()
    video = source / 'synthetic.mov'
    video.write_bytes(b'synthetic-local-media')
    digest = hashlib.sha256(video.read_bytes()).hexdigest()
    record = dict(id='synthetic', path=str(video), sha256=digest, file_name=video.name,
                  duration_sec=5.0, split='dev', duplicate_count=2)
    (source / 'dataset.json').write_text(json.dumps({'records': [record]}), encoding='utf-8')
    (source / 'priority.json').write_text(json.dumps([{'id': 'synthetic'}, {'id': 'synthetic'}]), encoding='utf-8')
    seed = dict(notes='Human note\nВторичный разбор ИИ: unverified machine suggestion',
                events=[dict(rule_id='R22')], reviewed=True)
    (source / 'review-seed.json').write_text(json.dumps({'annotations': {'synthetic': seed}}), encoding='utf-8')
    return source


def test_preparation_deduplicates_and_keeps_human_notes_without_ai_truth(tmp_path):
    source = source_queue(tmp_path)
    original = (source / 'review-seed.json').read_bytes()
    destination = tmp_path / 'prepared'
    assert prepare_queue(source, destination) == dict(videos=1, preserved_human_drafts=0)
    queue = json.loads((destination / 'queue.json').read_text(encoding='utf-8'))
    assert len(queue['manifest']['records']) == 1
    draft = queue['annotations']['synthetic']
    assert draft['notes'] == 'Human note'
    assert draft['events'] == [] and draft['reviewed'] is False
    assert draft['source_group'] == '' and draft['verdict'] == 'uncertain'
    assert (source / 'review-seed.json').read_bytes() == original
    with pytest.raises(FileExistsError):
        prepare_queue(source, destination)


def test_previous_human_draft_is_preserved_without_promoting_it(tmp_path):
    source = source_queue(tmp_path)
    record = json.loads((source / 'dataset.json').read_text(encoding='utf-8'))['records'][0]
    previous = dict(sha256=record['sha256'], reviewed=True, source_group='known-session',
                    verdict='violation', events=[dict(rule_id='OTHER', start_sec=1, end_sec=2)],
                    notes='Earlier human draft')
    (source / 'previous-human-export.json').write_text(json.dumps({'annotations': {'synthetic': previous}}), encoding='utf-8')
    destination = tmp_path / 'prepared'
    assert prepare_queue(source, destination)['preserved_human_drafts'] == 1
    draft = json.loads((destination / 'queue.json').read_text(encoding='utf-8'))['annotations']['synthetic']
    assert draft['events'] == previous['events']
    assert draft['reviewed'] is False
    assert draft['notes'].startswith(previous['notes'])


def test_loopback_server_only_serves_selected_queue_and_rejects_other_origins(tmp_path):
    source = source_queue(tmp_path)
    destination = tmp_path / 'prepared'
    prepare_queue(source, destination)
    server = make_server(destination / 'queue.json', port=0)
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()

    def get(path, headers=None):
        connection = http.client.HTTPConnection('127.0.0.1', server.server_port)
        connection.request('GET', path, headers=headers or {})
        response = connection.getresponse()
        result = response.status, dict(response.getheaders()), response.read()
        connection.close()
        return result

    try:
        assert server.server_address[0] == '127.0.0.1'
        status, _, html = get('/')
        assert status == 200 and b'window.__LOCAL_REVIEW__' in html
        status, _, body = get('/queue.json')
        assert status == 200
        record = json.loads(body)['manifest']['records'][0]
        assert 'path' not in record and record['media_url'] == '/video/0'
        status, headers, body = get('/video/0', {'Range': 'bytes=1-4'})
        assert status == 206 and body == b'ynth'
        assert headers['Content-Range'] == 'bytes 1-4/21'
        for path in ['/video/1', '/video/../../source/synthetic.mov', '/dataset.json']:
            assert get(path)[0] == 404
        assert get('/video/0', {'Range': 'bytes=999-'})[0] == 416
        assert get('/', {'Origin': 'https://example.com'})[0] == 403
        assert get('/', {'Host': 'example.com'})[0] == 403
        assert get('/queue.json', {'Sec-Fetch-Site': 'cross-site'})[0] == 403
    finally:
        server.shutdown()
        server.server_close()
        worker.join(timeout=3)
