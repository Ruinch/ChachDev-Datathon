"""Synthetic source metadata only; no private video or model is accessed."""
from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

from ml_tools.review_readiness import main, prepare_sources, write_private_manifest


@pytest.fixture
def inputs():
    records = [{"id": f"PRIVATE-ID-{i}", "sha256": str(i + 1) * 64,
                "path": f"PRIVATE-PATH-{i}.mov", "duration_sec": 100,
                "point_sec": 15, "label": "PRIVATE-LABEL", "notes": "PRIVATE-NOTE",
                "split": "dev" if i < 2 else "holdout" if i == 2 else "quarantine"}
               for i in range(4)]
    manifest = {"version": 1, "source_grouping_reviewed": True, "records": records}
    annotations = {
        row["id"]: {"sha256": row["sha256"], "reviewed": True,
                    "source_group": f"human-camera-session-{i}", "verdict": "no_violation",
                    "events": [], "notes": "PRIVATE-NOTE"}
        for i, row in enumerate(records)
    }
    annotations[records[0]["id"]].update(verdict="violation", events=[
        {"rule_id": "R20-B", "start_sec": 10, "end_sec": 20, "observation": "PRIVATE-OBSERVATION"}])
    queue = {"version": 1, "manifest": {"version": 1, "records": copy.deepcopy(records[:2])},
             "annotations": copy.deepcopy(annotations)}
    return queue, manifest


def test_export_is_only_source_selection_with_all_protected_hashes(inputs):
    queue, manifest = inputs
    report, exported = prepare_sources(queue, manifest, confirm_whole_video_review=True)
    assert report["can_export_source_manifest"] and not report["can_train"]
    assert report["eligible_dev_sources"] == 2
    assert report["reviewed_supported_events"] == 1
    assert report["reviewed_negative_sources"] == 1
    assert report["protected_sources"] == 2
    assert exported["requires_exact_frame_review"] is True
    assert {r["sha256"] for r in exported["records"] if r["split"] != "dev"} == {
        r["sha256"] for r in manifest["records"] if r["split"] != "dev"}
    raw = json.dumps(exported)
    assert all(value not in raw for value in ("PRIVATE-NOTE", "PRIVATE-OBSERVATION", "PRIVATE-LABEL", '"answer"', '"events"'))
    public = json.dumps(report)
    assert "PRIVATE" not in public and "human-camera" not in public
    assert "disjoint_train_validation_source_groups" in report["remaining_training_checks"]


def test_export_requires_explicit_whole_video_confirmation(inputs):
    report, exported = prepare_sources(*inputs)
    assert exported is None
    assert "confirm_whole_video_review_for_export" in report["export_blockers"]


def test_drafts_remain_pending_even_when_notes_and_events_are_populated(inputs):
    queue, manifest = inputs
    for item in queue["annotations"].values():
        item["reviewed"] = False
    report, exported = prepare_sources(queue, manifest, confirm_whole_video_review=True)
    assert exported is None
    assert report["pending_whole_video_sources"] == 2
    assert report["reviewed_supported_events"] == 0
    assert report["eligible_dev_sources"] == 0
    assert report["protected_sources_without_reviewed_group"] == 2


def test_uncertain_and_other_are_not_negatives_even_with_supported_events(inputs):
    queue, manifest = inputs
    values = list(queue["annotations"].values())
    values[0]["events"].append({"rule_id": "OTHER", "start_sec": 21, "end_sec": 22})
    values[1]["verdict"] = "uncertain"
    report, exported = prepare_sources(queue, manifest, confirm_whole_video_review=True)
    assert exported is None
    assert report["outside_catalog_sources"] == 1
    assert report["uncertain_sources"] == 1
    assert report["reviewed_supported_events"] == 1
    assert report["reviewed_negative_sources"] == 0
    assert report["eligible_dev_sources"] == 0


def test_saved_human_revision_can_revoke_seed_review(inputs):
    queue, manifest = inputs
    key = manifest["records"][0]["id"]
    saved = {"version": 1, "annotations": {key: {**queue["annotations"][key], "reviewed": False}}}
    report, exported = prepare_sources(queue, manifest, saved, confirm_whole_video_review=True)
    assert report["pending_whole_video_sources"] == 1
    assert report["eligible_dev_sources"] == 1
    assert len([row for row in exported["records"] if row["split"] == "dev"]) == 1


@pytest.mark.parametrize("split", ["holdout", "quarantine"])
def test_original_manifest_protection_cannot_be_relabelled_by_queue(inputs, split):
    queue, manifest = inputs
    manifest["records"][0]["split"] = split
    assert queue["manifest"]["records"][0]["split"] == "dev"
    report, exported = prepare_sources(queue, manifest, confirm_whole_video_review=True)
    assert report["eligible_dev_sources"] == 1
    assert report["protected_queue_sources"] == 1
    assert all(row["sha256"] != manifest["records"][0]["sha256"]
               for row in exported["records"] if row["split"] == "dev")


def test_reviewed_duplicate_alias_propagates_once_and_conflict_rejected(inputs):
    queue, manifest = inputs
    original = manifest["records"][0]
    alias = {**original, "id": "PRIVATE-ALIAS"}
    manifest["records"].append(alias)
    annotation = queue["annotations"].pop(original["id"])
    queue["annotations"][alias["id"]] = annotation
    report, exported = prepare_sources(queue, manifest, confirm_whole_video_review=True)
    assert report["eligible_dev_sources"] == 2
    assert len([row for row in exported["records"] if row["split"] == "dev"]) == 2
    queue["annotations"][original["id"]] = {**annotation, "verdict": "no_violation", "events": []}
    with pytest.raises(ValueError, match="conflicting reviewed"):
        prepare_sources(queue, manifest, confirm_whole_video_review=True)


@pytest.mark.parametrize("protected_index", [2, 3])
def test_casefold_session_overlap_with_holdout_or_quarantine_blocks(inputs, protected_index):
    queue, manifest = inputs
    values = list(queue["annotations"].values())
    values[protected_index]["source_group"] = "  HUMAN-CAMERA-SESSION-0  "
    report, exported = prepare_sources(queue, manifest, confirm_whole_video_review=True)
    assert exported is None
    assert "separate_protected_source_groups" in report["export_blockers"]


def test_finalization_flag_alone_cannot_prove_protected_groups(inputs):
    queue, manifest = inputs
    for row in manifest["records"][2:]:
        queue["annotations"].pop(row["id"])
    report, exported = prepare_sources(queue, manifest, confirm_whole_video_review=True)
    assert exported is None
    assert report["protected_sources_without_reviewed_group"] == 2


def test_unfinalized_original_groups_block_export(inputs):
    queue, manifest = inputs
    manifest["source_grouping_reviewed"] = False
    report, exported = prepare_sources(queue, manifest, confirm_whole_video_review=True)
    assert exported is None
    assert "finalize_original_source_groups" in report["export_blockers"]


def test_unknown_extended_group_placeholder_cannot_become_training_group(inputs):
    queue, manifest = inputs
    next(iter(queue["annotations"].values()))["source_group"] = "unknown-camera-2"
    report, exported = prepare_sources(queue, manifest, confirm_whole_video_review=True)
    assert report["unknown_group_sources"] == 1
    assert report["eligible_dev_sources"] == 1
    assert len(exported["reviewed_source_groups"]) == 1


def test_private_fresh_export_preserves_existing_files(inputs, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    _, exported = prepare_sources(*inputs, confirm_whole_video_review=True)
    destination = tmp_path / ".local_ml" / "training" / "sources.json"
    write_private_manifest(destination, exported)
    previous = destination.read_bytes()
    with pytest.raises(ValueError, match="fresh"):
        write_private_manifest(destination, {})
    assert destination.read_bytes() == previous
    with pytest.raises(ValueError, match="private"):
        write_private_manifest(tmp_path / "public.json", exported)


def _files(tmp_path, queue, manifest):
    q = tmp_path / "queue.json"
    m = tmp_path / "manifest.json"
    q.write_text(json.dumps(queue), encoding="utf-8")
    m.write_text(json.dumps(manifest), encoding="utf-8")
    return ["--queue", str(q), "--manifest", str(m)]


def test_cli_missing_optional_saved_file_uses_drafts_only(inputs, tmp_path, capsys):
    queue, manifest = inputs
    for row in queue["annotations"].values():
        row["reviewed"] = False
    code = main(_files(tmp_path, queue, manifest))
    text = capsys.readouterr().out
    assert code == 0
    assert json.loads(text)["pending_whole_video_sources"] == 2
    assert "PRIVATE" not in text and "human-camera" not in text


def test_cli_blocked_export_creates_nothing(inputs, tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    destination = tmp_path / ".local_ml" / "blocked.json"
    code = main(_files(tmp_path, *inputs) + ["--export-manifest", str(destination)])
    assert code == 2 and not destination.exists()
    assert not json.loads(capsys.readouterr().out)["can_train"]


def test_cli_errors_are_sanitized_and_do_not_read_media(inputs, tmp_path, capsys):
    queue, manifest = inputs
    queue["manifest"]["records"][0]["sha256"] = "a" * 64
    code = main(_files(tmp_path, queue, manifest))
    captured = capsys.readouterr()
    assert code == 2
    assert json.loads(captured.out)["error"] == "local_review_input_or_output_invalid"
    assert "PRIVATE" not in captured.out + captured.err
    assert str(tmp_path) not in captured.out + captured.err
    assert "Traceback" not in captured.out + captured.err


def test_explicit_missing_annotation_file_is_not_silently_ignored(inputs, tmp_path, capsys):
    code = main(_files(tmp_path, *inputs) + ["--annotations", str(tmp_path / "PRIVATE-MISSING.json")])
    assert code == 2
    assert "PRIVATE" not in capsys.readouterr().out


def test_malformed_reviewed_event_does_not_expose_input_in_traceback(inputs, tmp_path, capsys):
    queue, manifest = inputs
    next(iter(queue["annotations"].values()))["events"] = ["PRIVATE-BANK-DESCRIPTION"]
    assert main(_files(tmp_path, queue, manifest)) == 2
    captured = capsys.readouterr()
    assert json.loads(captured.out)["error"] == "local_review_input_or_output_invalid"
    assert "PRIVATE" not in captured.out + captured.err
    assert "Traceback" not in captured.out + captured.err
