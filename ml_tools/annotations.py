"""Validate human annotations and prevent same-source leakage before holdout use."""
from __future__ import annotations

import argparse
import json
import math
from pathlib import Path

from app.ml.rules import RULES


def validate_annotations(manifest: dict, annotations: dict, *, require_all: bool = False) -> dict:
    records = {r['id']: r for r in manifest['records']}
    if set(annotations) - set(records):
        raise ValueError('Annotations contain unknown video IDs')
    if require_all and set(annotations) != set(records):
        raise ValueError('Every video needs a reviewed source group before holdout evaluation')
    group_splits: dict[str, set] = {}
    duplicate_groups: dict[str, set] = {}
    reviewed = 0
    for key, annotation in annotations.items():
        record = records[key]
        if annotation.get('sha256') != record['sha256']:
            raise ValueError('Annotation source hash does not match manifest')
        if annotation.get('reviewed') is not True:
            if require_all:
                raise ValueError('Some videos have not been reviewed')
            continue
        group = annotation.get('source_group', '').strip()
        if not group:
            raise ValueError('Reviewed annotation requires a source/camera/session group')
        if group == 'organizer-single-camera':
            raise ValueError('Legacy automatic camera placeholder must be replaced after local source-group review')
        reviewed += 1
        duplicate_groups.setdefault(record['sha256'], set()).add(group)
        if record['split'] != 'quarantine':
            group_splits.setdefault(group, set()).add(record['split'])
        verdict = annotation.get('verdict')
        events = annotation.get('events', [])
        if verdict not in {'violation', 'no_violation', 'uncertain'} or not isinstance(events, list):
            raise ValueError('Invalid reviewed verdict or event list')
        if (verdict == 'violation' and not events) or (verdict == 'no_violation' and events):
            raise ValueError('Reviewed verdict contradicts event list')
        for event in events:
            if event.get('rule_id') not in {*RULES, 'OTHER'}:
                raise ValueError('Unknown reviewed event type')
            start, end = event.get('start_sec'), event.get('end_sec')
            if (isinstance(start, bool) or isinstance(end, bool) or not isinstance(start, (int,float))
                    or not isinstance(end, (int,float)) or not math.isfinite(start) or not math.isfinite(end)
                    or not 0 <= start <= end < record['duration_sec'] + 0.001):
                raise ValueError('Reviewed event lies outside source video')
    if any(len(groups) != 1 for groups in duplicate_groups.values()):
        raise ValueError('Identical videos assigned to different source groups')
    leakage = sum(len(splits) > 1 for splits in group_splits.values())
    if require_all and leakage:
        raise ValueError('Source groups overlap dev and holdout; regroup before finalizing')
    return {'reviewed': reviewed, 'source_groups': len(group_splits), 'leaking_groups': leakage}


def match_events(truth: list[dict], predictions: list[dict], tolerance_sec: float = 2) -> dict:
    """Maximum one-to-one matching by rule and annotated interval + fixed tolerance.

    No interval IoU is claimed: the public prediction supplies a point only.
    """
    edges = [[i for i, event in enumerate(truth) if event['rule_id'] == p['rule_id']
              and event['start_sec'] - tolerance_sec <= p['timecode_sec'] <= event['end_sec'] + tolerance_sec]
             for p in predictions]
    matches = {}
    def augment(pred, seen):
        for event in edges[pred]:
            if event in seen:
                continue
            seen.add(event)
            if event not in matches or augment(matches[event], seen):
                matches[event] = pred
                return True
        return False
    for index in range(len(predictions)):
        augment(index, set())
    tp = len(matches)
    return {'tp': tp, 'fp': len(predictions)-tp, 'fn': len(truth)-tp}


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('annotations', type=Path)
    parser.add_argument('--manifest', type=Path, default=Path('.local_ml/dataset.json'))
    parser.add_argument('--finalize-groups', action='store_true')
    args = parser.parse_args()
    manifest = json.loads(args.manifest.read_text(encoding='utf-8'))
    annotations = json.loads(args.annotations.read_text(encoding='utf-8'))['annotations']
    result = validate_annotations(manifest, annotations, require_all=args.finalize_groups)
    if args.finalize_groups:
        manifest['source_grouping_reviewed'] = True
        args.manifest.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps(result))
