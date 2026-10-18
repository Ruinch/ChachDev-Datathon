"""Resolve byte-identical source aliases without choosing truth by input order."""
from __future__ import annotations


def resolve_annotations_by_sha(records: list[dict], annotations: dict) -> dict:
    """Prefer reviewed truth across every alias; refuse conflicting reviewed truth.

    Notes and event ordering are not scoring content. Draft annotations never
    override a reviewed alias and cannot become reviewed through this resolver.
    Structural annotation validation remains the caller's responsibility.
    """
    grouped: dict[str, list[tuple[str, dict]]] = {}
    for record in records:
        if record['id'] in annotations:
            grouped.setdefault(record['sha256'], []).append((record['id'], annotations[record['id']]))
    resolved = {}
    for digest, aliases in grouped.items():
        aliases.sort(key=lambda pair: pair[0])
        reviewed = [(key, annotation) for key, annotation in aliases if annotation.get('reviewed') is True]
        signatures = {
            (annotation.get('verdict'), tuple(sorted(
                (event['rule_id'], event['start_sec'], event['end_sec'])
                for event in annotation.get('events', [])
            ))) for _, annotation in reviewed
        }
        if len(signatures) > 1:
            raise ValueError('Identical videos have conflicting reviewed verdicts or event intervals')
        resolved[digest] = (reviewed or aliases)[0][1]
    return resolved


def select_unique_sources(records: list[dict], eligible_splits: set[str],
                          requested_ids: list[str] | None = None) -> list[dict]:
    """Select each source once; an explicit ID can name any of its aliases.

    Preserve insertion order for unique hashes. A quarantined alias excludes its
    entire source. Eligible duplicate metadata must agree before an alias is
    chosen, so ordering/names cannot improve label or timing measurements.
    """
    quarantined = {record['sha256'] for record in records if record['split'] == 'quarantine'}
    requested = None if not requested_ids else {
        record['sha256'] for record in records if record['id'] in requested_ids
    }
    grouped: dict[str, list[dict]] = {}
    for record in records:
        if (record['split'] in eligible_splits and record['sha256'] not in quarantined
                and (requested is None or record['sha256'] in requested)):
            grouped.setdefault(record['sha256'], []).append(record)
    selected = []
    for aliases in grouped.values():
        if len({(record.get('label'), record['duration_sec'], record.get('point_sec'))
                for record in aliases}) > 1:
            raise ValueError('Identical eligible videos have conflicting labels, durations or point hints; reconcile or quarantine them')
        selected.append(min(aliases, key=lambda record: record['id']))
    return selected
