"""Private post-inference diagnostics against human note anchors and proposed views.

No data from this module reaches the analyzer. Note anchors are not exhaustive
event labels; this score must never be called interval accuracy or event recall.
"""
from __future__ import annotations

import argparse
import json
from collections import Counter, defaultdict
from pathlib import Path

from ml_tools.evaluate import summarize


def score_anchors(rows: list[dict], predictions: dict, targets: dict, tolerance: float = 5) -> dict:
    checked = 0
    type_matches = 0
    timed_matches = 0
    for row in rows:
        target = targets.get(row['id'])
        if not target:
            continue
        if target['sha256'] != row['sha256']:
            raise ValueError('Human note anchor hash does not match evaluated source')
        if target.get('source_role') != 'human' or target.get('interpretation_reviewed') is not False:
            raise ValueError('Note anchors must explicitly retain human source and unreviewed type interpretation')
        checked += 1
        events = predictions.get(row['id'], {}).get('violations', []) if row['status'] == 'done' else []
        compatible = [e for e in events if e['rule_id'] in target['candidate_rule_ids']]
        type_matches += bool(compatible)
        timed_matches += any(abs(e['timecode_sec']-target['point_sec']) <= tolerance for e in compatible)
    return {'note_anchors': checked, 'compatible_type': type_matches,
            'compatible_type_and_point': timed_matches, 'point_tolerance_sec': tolerance,
            'scope': 'Development proxy from human notes, with unreviewed rule interpretation. Not exhaustive event/interval ground truth.'}


def by_view(rows: list[dict], views: dict) -> dict:
    mapping = {r['sha256']: r['view_group'] for r in views['records']}
    groups = defaultdict(list)
    for row in rows:
        if row['sha256'] not in mapping:
            raise ValueError('Evaluated source is missing from view proposals')
        groups[mapping[row['sha256']]].append(row)
    return {'human_verified': views['human_verified'],
            'scope': 'Diagnostic strata only; similarity groups are not independent camera/session holdouts.',
            'groups': {group: summarize(items) for group, items in sorted(groups.items())}}


def negative_diagnostics(rows: list[dict], predictions: dict) -> dict:
    negatives = [r for r in rows if r['label'] == 'no_violation']
    completed = [r for r in negatives if r['status'] == 'done']
    counts = Counter()
    alarms = 0
    for row in completed:
        events = predictions[row['id']]['violations']
        alarms += bool(events)
        counts.update(e['rule_id'] for e in events)
    return {'negative_videos': len(negatives), 'completed': len(completed),
            'failed': len(negatives)-len(completed), 'videos_with_alarms': alarms,
            'false_events_by_rule': dict(counts),
            'scope': 'Uses original negative labels; extra events in positive videos still require exhaustive human annotation.'}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('evaluation', type=Path)
    parser.add_argument('--note-targets', type=Path)
    parser.add_argument('--views', type=Path)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    if args.output.exists():
        raise SystemExit('Choose a fresh output file')
    report = json.loads((args.evaluation/'metrics.json').read_text(encoding='utf-8'))
    predictions = {r['id']: json.loads((args.evaluation/(r['id']+'.result.json')).read_text(encoding='utf-8'))
                   for r in report['rows'] if r['status'] == 'done'}
    output = {'binary': report['summary'], 'note_anchors': None, 'views': None,
              'negative_checks': negative_diagnostics(report['rows'], predictions)}
    if args.note_targets:
        targets = json.loads(args.note_targets.read_text(encoding='utf-8'))['targets']
        output['note_anchors'] = score_anchors(report['rows'], predictions, targets)
    if args.views:
        output['views'] = by_view(report['rows'], json.loads(args.views.read_text(encoding='utf-8')))
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(output, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps({'binary': output['binary'], 'note_anchors': output['note_anchors'],
                      'negative_checks': output['negative_checks'],
                      'proposed_views_tested': len(output['views']['groups']) if output['views'] else None}))


if __name__ == '__main__':
    main()
