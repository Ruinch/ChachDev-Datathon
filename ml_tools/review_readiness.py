"""Aggregate-only bridge from whole-video review to private training preparation.

This selects sources, never creates frame answers, negatives, or trained weights.
The original manifest remains authoritative for holdout/quarantine protection.
"""
from __future__ import annotations

import argparse
import copy
import json
import math
from pathlib import Path
import re

from app.ml.rules import RULES
from ml_tools.annotations import normalize_source_group, validate_annotations
from ml_tools.source_aliases import resolve_annotations_by_sha, select_unique_sources


_UNKNOWN = {"organizer-single-camera", "unknown-session", "unknown", "unassigned",
            "none", "null", "n/a", "na", "todo", "tbd", "automatic", "auto"}


def _known_group(annotation: dict | None) -> str:
    if not annotation or annotation.get("reviewed") is not True:
        return ""
    group = normalize_source_group(annotation.get("source_group", ""))
    return group if group and group not in _UNKNOWN and not group.startswith("unknown-") else ""


def _records(manifest: dict) -> list[dict]:
    if not isinstance(manifest, dict) or manifest.get("version") != 1 or not isinstance(manifest.get("records"), list):
        raise ValueError("Invalid source manifest")
    seen = set()
    for row in manifest["records"]:
        if (not isinstance(row, dict) or not isinstance(row.get("id"), str)
                or not row["id"] or row["id"] in seen
                or not isinstance(row.get("sha256"), str)
                or not re.fullmatch(r"[0-9a-f]{64}", row["sha256"])
                or row.get("split") not in {"dev", "holdout", "quarantine"}
                or type(row.get("duration_sec")) not in (int, float)
                or not math.isfinite(row["duration_sec"]) or row["duration_sec"] <= 0):
            raise ValueError("Invalid source manifest record")
        seen.add(row["id"])
    return manifest["records"]


def prepare_sources(queue: dict, manifest: dict, saved: dict | None = None,
                    *, confirm_whole_video_review: bool = False) -> tuple[dict, dict | None]:
    """Return public-safe counts and an optional PRIVATE source-only manifest.

    Saved annotations can cover the full original manifest, including protected
    sources needed to check camera/session separation. A queue subset cannot
    silently relabel those sources as development data.
    """
    records = _records(manifest)
    by_id = {row["id"]: row for row in records}
    if not isinstance(queue, dict) or queue.get("version") != 1:
        raise ValueError("Invalid review queue")
    queued = _records(queue.get("manifest"))
    for row in queued:
        if row["id"] not in by_id or row["sha256"] != by_id[row["id"]]["sha256"]:
            raise ValueError("Queue source does not match original manifest")
    seed = queue.get("annotations", {})
    if not isinstance(seed, dict):
        raise ValueError("Invalid queue annotations")
    annotations = copy.deepcopy(seed)
    if saved is not None:
        if (not isinstance(saved, dict) or saved.get("version") != 1
                or not isinstance(saved.get("annotations"), dict)):
            raise ValueError("Invalid saved annotation document")
        annotations.update(saved["annotations"])
    # Do not turn truthy strings or machine draft fields into human approval.
    if any(not isinstance(item, dict) or type(item.get("reviewed")) is not bool
           for item in annotations.values()):
        raise ValueError("Invalid human review flag")
    for item in annotations.values():
        if item["reviewed"] and (not isinstance(item.get("events", []), list)
                                 or any(not isinstance(event, dict) for event in item.get("events", []))):
            raise ValueError("Invalid reviewed event list")
    validation = validate_annotations(manifest, annotations)
    resolved = resolve_annotations_by_sha(records, annotations)
    queued_hashes = {row["sha256"] for row in queued}
    protected_hashes = {row["sha256"] for row in records if row["split"] != "dev"}
    protected_groups = {_known_group(resolved.get(digest)) for digest in protected_hashes}
    protected_missing = sum(not _known_group(resolved.get(digest)) for digest in protected_hashes)
    protected_groups.discard("")
    eligible, groups = [], {}
    counts = {"queue_sources": len(queued_hashes), "reviewed_queue_sources": 0,
              "pending_whole_video_sources": 0, "uncertain_sources": 0,
              "outside_catalog_sources": 0, "reviewed_supported_events": 0,
              "reviewed_negative_sources": 0, "unknown_group_sources": 0,
              "protected_queue_sources": len(queued_hashes & protected_hashes)}
    for digest in queued_hashes:
        annotation = resolved.get(digest)
        if not annotation or annotation.get("reviewed") is not True:
            counts["pending_whole_video_sources"] += 1
            continue
        counts["reviewed_queue_sources"] += 1
        counts["reviewed_supported_events"] += sum(event["rule_id"] in RULES for event in annotation.get("events", []))
        counts["reviewed_negative_sources"] += annotation["verdict"] == "no_violation"
        counts["uncertain_sources"] += annotation["verdict"] == "uncertain"
        outside = any(event["rule_id"] not in RULES for event in annotation.get("events", []))
        counts["outside_catalog_sources"] += outside
        group = _known_group(annotation)
        counts["unknown_group_sources"] += not bool(group)
        if (digest not in protected_hashes and annotation["verdict"] != "uncertain"
                and not outside and group):
            eligible.append(digest)
            groups[digest] = group
    blocked = []
    if not confirm_whole_video_review:
        blocked.append("confirm_whole_video_review_for_export")
    if not eligible:
        blocked.append("review_supported_dev_sources")
    if manifest.get("source_grouping_reviewed") is not True:
        blocked.append("finalize_original_source_groups")
    if protected_missing:
        blocked.append("supply_reviewed_groups_for_all_protected_sources")
    # The base validator reports dev/holdout leakage; this also protects any
    # quarantined source whose known session overlaps the selected dev subset.
    leaking = len(set(groups.values()) & protected_groups)
    if validation["leaking_groups"] or leaking:
        blocked.append("separate_protected_source_groups")
    report = {**counts, "eligible_dev_sources": len(eligible),
              "eligible_source_groups": len(set(groups.values())),
              "protected_sources": len(protected_hashes),
              "protected_sources_without_reviewed_group": protected_missing,
              "source_grouping_reviewed": manifest.get("source_grouping_reviewed") is True,
              "leaking_source_groups": max(validation["leaking_groups"], leaking),
              "can_export_source_manifest": not blocked, "source_manifest_exported": False,
              "can_train": False, "export_blockers": blocked,
              "remaining_training_checks": ["exact_frame_answers_reviewed_and_exhaustive",
                                            "disjoint_train_validation_source_groups",
                                            "local_training_weights_packages_and_memory"],
              "training_scope": "source_selection_only_no_frame_truth_or_training_performed"}
    if blocked:
        return report, None
    # Choose duplicate aliases deterministically and preserve every protected
    # hash. No organizer label, human note, or interval becomes a model answer.
    selected = select_unique_sources(records, {"dev"})
    selected = [row for row in selected if row["sha256"] in eligible]
    output_rows = []
    for row in selected:
        if not isinstance(row.get("path"), str) or not row["path"]:
            raise ValueError("Selected source has no local path")
        output_rows.append({key: row[key] for key in ("id", "sha256", "path", "split", "duration_sec")})
    for row in records:
        if row["split"] != "dev":
            output_rows.append({key: row[key] for key in ("id", "sha256", "split", "duration_sec")})
    export = {"version": 1, "source_grouping_reviewed": True, "records": output_rows,
              "reviewed_source_groups": groups,
              "training_scope": report["training_scope"],
              "requires_exact_frame_review": True}
    return report, export


def write_private_manifest(path: Path, manifest: dict) -> None:
    cwd = Path.cwd().resolve()
    destination = path.resolve()
    roots = [cwd / name for name in (".local_ml", ".local_data")]
    if not any(root.resolve() == root and destination != root and destination.is_relative_to(root) for root in roots):
        raise ValueError("Choose a private output within this project")
    if destination.exists():
        raise ValueError("Choose a fresh manifest path")
    destination.parent.mkdir(parents=True, exist_ok=True)
    with destination.open("x", encoding="utf-8") as stream:
        json.dump(manifest, stream, ensure_ascii=False, indent=2, allow_nan=False)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Check human whole-video review; print aggregate counts only")
    parser.add_argument("--queue", type=Path, required=True)
    parser.add_argument("--annotations", type=Path, help="Defaults to optional sibling human-review.json")
    parser.add_argument("--manifest", type=Path, required=True, help="Original manifest preserving formal protected splits")
    parser.add_argument("--export-manifest", type=Path)
    parser.add_argument("--confirm-whole-video-review", action="store_true")
    args = parser.parse_args(argv)
    try:
        queue = json.loads(args.queue.read_text(encoding="utf-8"))
        manifest = json.loads(args.manifest.read_text(encoding="utf-8"))
        annotation_path = args.annotations or args.queue.with_name("human-review.json")
        saved = (json.loads(annotation_path.read_text(encoding="utf-8"))
                 if args.annotations or annotation_path.is_file() else None)
        report, export = prepare_sources(queue, manifest, saved,
                                         confirm_whole_video_review=args.confirm_whole_video_review)
        if args.export_manifest and export is not None:
            write_private_manifest(args.export_manifest, export)
            report["source_manifest_exported"] = True
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 2 if args.export_manifest and export is None else 0
    except (OSError, ValueError, TypeError, KeyError):
        # Neither validation text nor an exception traceback may expose private
        # source IDs, paths, labels, notes, or annotation content to the console.
        print(json.dumps({"can_train": False, "source_manifest_exported": False,
                          "error": "local_review_input_or_output_invalid"}))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
