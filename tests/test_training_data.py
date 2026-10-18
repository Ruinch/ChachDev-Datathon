"""Training data gates exercised only with locally generated synthetic media."""
from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path

import pytest

av = pytest.importorskip("av")
Image = pytest.importorskip("PIL.Image")

from app.ml.model import PROMPT
from app.ml.rules import RULES
from ml_tools.training_data import compile_review, draft_review


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def synthetic_video(path: Path, offset: int) -> None:
    with av.open(str(path), "w", format="mov") as container:
        stream = container.add_stream("mpeg4", rate=4)
        stream.width, stream.height, stream.pix_fmt = 96, 64, "yuv420p"
        for index in range(12):
            image = Image.new("RGB", (96, 64), (offset, index * 17, 255 - offset))
            frame = av.VideoFrame.from_image(image)
            for packet in stream.encode(frame):
                container.mux(packet)
        for packet in stream.encode():
            container.mux(packet)


@pytest.fixture
def review(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    paths = []
    for index in range(3):
        path = tmp_path / f"private-source-{index}.mov"
        synthetic_video(path, 30 + index * 60)
        paths.append(path)
    records = [{"id": f"PRIVATE-ID-{index}", "file_name": path.name, "path": str(path),
                "sha256": digest(path), "split": "dev" if index < 2 else "holdout",
                "label": "PRIVATE-LABEL", "notes": "PRIVATE-DESCRIPTION"}
               for index, path in enumerate(paths)]
    records.append(copy.deepcopy(records[0]))
    output = tmp_path / ".local_ml" / "training-review"
    summary = draft_review({"version": 1, "records": records}, output,
                           window_sec=3, stride_sec=3)
    path = output / "review.json"
    return {"path": path, "output": output, "summary": summary,
            "data": json.loads(path.read_text(encoding="utf-8")), "records": records}


def write(review):
    review["path"].write_text(json.dumps(review["data"], ensure_ascii=False), encoding="utf-8")


def approve(review):
    for index, example in enumerate(review["data"]["examples"]):
        example.update(reviewed=True, exhaustive=True, source_group=f"human-session-{index}",
                       partition="train" if index == 0 else "validation",
                       answer={"assessable": True, "events": []})
    write(review)


def test_synthetic_video_draft_requires_human_answers_and_compiles_isolated_sets(review):
    summary = review["summary"]
    assert summary["dev_sources"] == 2
    assert summary["duplicate_dev_records"] == 1
    assert summary["excluded_sources"] == 1
    assert summary["examples"] == 2
    raw = review["path"].read_text(encoding="utf-8")
    for private_value in ["PRIVATE-ID", "private-source-", "PRIVATE-LABEL", "PRIVATE-DESCRIPTION"]:
        assert private_value not in raw
    assert review["data"]["prompt"] == PROMPT
    assert review["data"]["prompt_sha256"] == hashlib.sha256(PROMPT.encode()).hexdigest()
    assert set(review["data"]["rule_ids"]) == set(RULES)
    for example in review["data"]["examples"]:
        assert example["answer"] is None
        assert example["reviewed"] is False and example["exhaustive"] is False
        assert example["source_group"] == ""
        assert len(example["frames"]) >= 2
        for frame in example["frames"]:
            assert not Path(frame["path"]).is_absolute()
            assert digest(review["output"] / frame["path"]) == frame["sha256"]
    with pytest.raises(ValueError, match="one reviewed train"):
        compile_review(review["path"])
    approve(review)
    bundle = compile_review(review["path"])
    assert len(bundle["train"]) == len(bundle["validation"]) == 1
    assert bundle["summary"]["train_sources"] == bundle["summary"]["validation_sources"] == 1
    assert bundle["summary"]["skipped_unreviewed"] == 0
    for split in ("train", "validation"):
        entry = bundle[split][0]
        assert set(entry) == {"frames", "answer"}
        assert entry["answer"] == {"assessable": True, "events": []}
        assert all(Path(frame["path"]).is_absolute() for frame in entry["frames"])
        assert all(Path(frame["path"]).is_relative_to(review["output"]) for frame in entry["frames"])


def test_compile_ignores_unreviewed_answer_but_counts_the_window(review):
    approve(review)
    extra = copy.deepcopy(review["data"]["examples"][0])
    extra.update(reviewed=False, exhaustive=False, answer={"not": "a target"})
    review["data"]["examples"].append(extra)
    write(review)
    bundle = compile_review(review["path"])
    assert bundle["summary"]["skipped_unreviewed"] == 1
    assert len(bundle["train"]) == 1


@pytest.mark.parametrize("leak", ["source", "group", "frame"])
@pytest.mark.parametrize("unreviewed", [False, True])
def test_sources_groups_and_frame_hashes_cannot_cross_partitions_even_unreviewed(review, leak, unreviewed):
    approve(review)
    first, second = review["data"]["examples"]
    extra = copy.deepcopy(second)
    if leak == "source":
        extra["source_sha256"] = first["source_sha256"]
        extra["source_group"] = first["source_group"]
    elif leak == "group":
        extra["source_group"] = first["source_group"].upper()
    else:
        extra["frames"] = copy.deepcopy(first["frames"])
    if unreviewed:
        extra.update(reviewed=False, exhaustive=False, answer=None)
    review["data"]["examples"][1] = extra
    write(review)
    with pytest.raises(ValueError, match="overlap"):
        compile_review(review["path"])


@pytest.mark.parametrize("change", ["partition", "protected_source"])
def test_holdout_is_forbidden_even_when_unreviewed(review, change):
    approve(review)
    example = review["data"]["examples"][0]
    example["reviewed"] = False
    if change == "partition":
        example["partition"] = "holdout"
    else:
        example["source_sha256"] = review["records"][2]["sha256"]
    write(review)
    with pytest.raises(ValueError, match="holdout"):
        compile_review(review["path"])


def test_draft_rejects_hash_changes_and_dev_holdout_duplicates(review, tmp_path):
    manifest = {"version": 1, "records": copy.deepcopy(review["records"])}
    source = Path(manifest["records"][0]["path"])
    source.write_bytes(source.read_bytes() + b"changed")
    with pytest.raises(ValueError, match="hash"):
        draft_review(manifest, tmp_path / ".local_ml" / "new-draft")
    manifest["records"][2]["sha256"] = manifest["records"][0]["sha256"]
    with pytest.raises(ValueError, match="protected"):
        draft_review(manifest, tmp_path / ".local_ml" / "new-draft")


def test_draft_rejects_nonprivate_or_nonempty_output(review, tmp_path):
    manifest = {"version": 1, "records": review["records"]}
    with pytest.raises(ValueError, match="inside .local_ml"):
        draft_review(manifest, tmp_path / "public-review")
    with pytest.raises(ValueError, match="new or empty"):
        draft_review(manifest, review["output"])


@pytest.mark.parametrize("change", ["prompt", "prompt_sha256", "rule_ids"])
def test_prompt_snapshot_cannot_change_after_review(review, change):
    approve(review)
    review["data"][change] = "changed" if change != "rule_ids" else ["R22"]
    write(review)
    with pytest.raises(ValueError, match="snapshot"):
        compile_review(review["path"])


@pytest.mark.parametrize("change", ["modified_bytes", "fake_jpeg", "traversal", "absolute"])
def test_changed_invalid_or_escaping_images_are_rejected(review, tmp_path, change):
    approve(review)
    frame = review["data"]["examples"][0]["frames"][0]
    image = review["output"] / frame["path"]
    if change in {"modified_bytes", "fake_jpeg"}:
        image.write_bytes(b"not an image")
        if change == "fake_jpeg":
            frame["sha256"] = digest(image)
    elif change == "traversal":
        frame["path"] = "../escape.jpg"
    else:
        frame["path"] = str(image)
    write(review)
    with pytest.raises(ValueError, match="hash changed|decodable JPEG|inside its review"):
        compile_review(review["path"])


def valid_event():
    return {"rule_id": "R22", "start_frame": 0, "peak_frame": 1, "end_frame": 1,
            "conditions": list(RULES["R22"].criteria), "observation": "Synthetic human observation only"}


@pytest.mark.parametrize("invalid", ["unknown_rule", "wrong_conditions", "frame_bounds", "frame_order",
                                     "extra_field", "coerced_bool", "non_exhaustive", "unknown_group",
                                     "same_timestamp", "single_frame"])
def test_invalid_human_targets_or_review_requirements_are_rejected(review, invalid):
    approve(review)
    example = review["data"]["examples"][0]
    event = valid_event()
    example["answer"]["events"] = [event]
    if invalid == "unknown_rule":
        event["rule_id"] = "OTHER"
    elif invalid == "wrong_conditions":
        event["conditions"] = ["invented"] * 4
    elif invalid == "frame_bounds":
        event["end_frame"] = len(example["frames"])
    elif invalid == "frame_order":
        event["start_frame"], event["end_frame"] = 1, 0
    elif invalid == "extra_field":
        example["answer"]["certainty"] = 0.99
    elif invalid == "coerced_bool":
        example["answer"]["assessable"] = "true"
    elif invalid == "non_exhaustive":
        example["exhaustive"] = False
    elif invalid == "unknown_group":
        example["source_group"] = "organizer-single-camera"
    elif invalid == "same_timestamp":
        example["frames"][1]["timestamp_sec"] = example["frames"][0]["timestamp_sec"]
    else:
        example["frames"] = example["frames"][:1]
    write(review)
    with pytest.raises(ValueError):
        compile_review(review["path"])


def test_valid_positive_and_uncertain_targets_keep_exact_structure(review):
    approve(review)
    first, second = review["data"]["examples"]
    first["answer"] = {"assessable": True, "events": [valid_event()]}
    second["answer"] = {"assessable": False, "events": []}
    write(review)
    bundle = compile_review(review["path"])
    assert bundle["train"][0]["answer"] == first["answer"]
    assert bundle["validation"][0]["answer"] == second["answer"]


def test_validation_review_cannot_be_omitted(review):
    approve(review)
    review["data"]["examples"][1]["reviewed"] = False
    write(review)
    with pytest.raises(ValueError, match="one reviewed validation"):
        compile_review(review["path"])
