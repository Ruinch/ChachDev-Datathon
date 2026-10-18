"""Private, human-reviewed multi-image training data; never produces pseudo-labels."""
from __future__ import annotations

import argparse
import hashlib
import io
import json
import math
import re
import time
from collections import defaultdict
from pathlib import Path

from pydantic import ValidationError

from app.ml.model import PROMPT, WindowResult
from app.ml.rules import RULES
from app.ml.video import VideoAnalysisError, decode_video, select_frames, windows


_SHA256 = re.compile(r"[0-9a-f]{64}")
_PLACEHOLDERS = {"organizer-single-camera", "unknown-session", "unknown", "unassigned",
                 "none", "null", "n/a", "na", "todo", "tbd", "automatic", "auto"}


def _digest(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def _file_digest(path: Path) -> str:
    try:
        with path.open("rb") as stream:
            return hashlib.file_digest(stream, "sha256").hexdigest()
    except OSError:
        raise ValueError("A local training source is unavailable") from None


def _hash(value: object) -> str:
    if not isinstance(value, str) or not _SHA256.fullmatch(value):
        raise ValueError("Training data requires a lowercase SHA-256 digest")
    return value


def _private_output(output: Path) -> Path:
    cwd = Path.cwd().resolve()
    resolved = output.resolve()
    allowed = []
    for name in (".local_ml", ".local_data"):
        root = cwd / name
        # A symlinked private root must not redirect generated frames elsewhere.
        if root.resolve() == root:
            allowed.append(root)
    if not any(resolved != root and resolved.is_relative_to(root) for root in allowed):
        raise ValueError("Training output must be inside .local_ml/ or .local_data/ of the current project")
    if resolved.exists() and (not resolved.is_dir() or any(resolved.iterdir())):
        raise ValueError("Training output must be a new or empty directory")
    return resolved


def draft_review(manifest: dict, output: Path, *, frames_per_window: int = 4,
                 max_side: int = 448, window_sec: float = 24,
                 stride_sec: float = 18) -> dict:
    """Extract unlabelled dev windows into an ignored, empty local directory.

    Only frame content and original timestamps will become model inputs. Source
    names, source labels, annotation notes and source-group names are not inputs.
    The manifest's protected hashes remain in the local review for leakage checks.
    """
    if isinstance(frames_per_window, bool) or not isinstance(frames_per_window, int) or not 2 <= frames_per_window <= 128:
        raise ValueError("frames_per_window must be an integer between 2 and 128")
    if isinstance(max_side, bool) or not isinstance(max_side, int) or not 32 <= max_side <= 2048:
        raise ValueError("max_side must be an integer between 32 and 2048")
    if any(isinstance(value, bool) or not isinstance(value, (int, float))
           or not math.isfinite(value) or value <= 0 for value in (window_sec, stride_sec)) or stride_sec > window_sec:
        raise ValueError("Require finite 0 < stride_sec <= window_sec")
    if not isinstance(manifest, dict) or manifest.get("version") != 1 or not isinstance(manifest.get("records"), list):
        raise ValueError("Unsupported source manifest")
    output = _private_output(Path(output))
    dev: dict[str, dict] = {}
    reserved = set()
    duplicate_dev = 0
    for record in manifest["records"]:
        if not isinstance(record, dict):
            raise ValueError("Invalid source manifest record")
        digest = _hash(record.get("sha256"))
        split = record.get("split")
        if split not in {"dev", "holdout", "quarantine"}:
            raise ValueError("Unknown manifest source split")
        if split != "dev":
            reserved.add(digest)
            continue
        if digest in dev:
            duplicate_dev += 1
        else:
            dev[digest] = record
    if set(dev) & reserved:
        raise ValueError("A dev source overlaps protected holdout/quarantine sources")
    if not dev:
        raise ValueError("No eligible dev sources are available for training review")
    sources = []
    for digest, record in dev.items():
        path_value = record.get("path")
        if not isinstance(path_value, str) or not path_value:
            raise ValueError("A dev source has no local video path")
        source = Path(path_value).resolve()
        if not source.is_file() or _file_digest(source) != digest:
            raise ValueError("A dev source hash no longer matches the source manifest")
        sources.append((digest, source))

    output.mkdir(parents=True, exist_ok=True)
    frame_root = output / "frames"
    frame_root.mkdir()
    examples = []
    written_frames = 0
    skipped_short = 0
    for source_index, (digest, source) in enumerate(sources):
        video = decode_video(source, fps=max(1.0, frames_per_window / window_sec),
                             max_side=max_side, deadline=time.monotonic() + 300)
        if _file_digest(source) != digest:
            raise ValueError("A dev source changed while preparing training frames")
        for start, end in windows(video.duration, window_sec, stride_sec):
            selected = select_frames(video, start, end, frames_per_window)
            if len(selected) < 2:
                skipped_short += 1
                continue
            frames = []
            for index, frame in enumerate(selected):
                relative = Path("frames") / f"window-{len(examples):06d}-frame-{index:03d}.jpg"
                (output / relative).write_bytes(frame.jpeg)
                frames.append({"path": relative.as_posix(), "timestamp_sec": frame.timestamp,
                               "sha256": _digest(frame.jpeg)})
                written_frames += 1
            examples.append({"source_index": source_index, "source_sha256": digest,
                             "source_group": "", "partition": "train", "reviewed": False,
                             "exhaustive": False, "window_start_sec": start, "window_end_sec": end,
                             "frames": frames, "answer": None})
    if not examples:
        raise ValueError("No chronological training windows contain at least two frames")
    review = {"version": 1, "prompt": PROMPT, "prompt_sha256": _digest(PROMPT.encode("utf-8")),
              "rule_ids": list(RULES),
              "source_policy": {"dev_sha256": sorted(dev), "reserved_sha256": sorted(reserved)},
              "examples": examples}
    (output / "review.json").write_text(json.dumps(review, ensure_ascii=False, indent=2), encoding="utf-8")
    return {"source_records": len(manifest["records"]), "dev_sources": len(sources),
            "excluded_sources": len(reserved), "duplicate_dev_records": duplicate_dev,
            "examples": len(examples), "frames": written_frames,
            "skipped_short_windows": skipped_short, "reviewed_examples": 0}


def _frame_path(root: Path, value: object) -> Path:
    if not isinstance(value, str) or not value or "\0" in value:
        raise ValueError("Training frame path must be relative to its review directory")
    relative = Path(value)
    if relative.is_absolute() or ".." in relative.parts or relative.suffix.lower() not in {".jpg", ".jpeg"}:
        raise ValueError("Training frame path must be a JPEG inside its review directory")
    try:
        resolved = (root / relative).resolve(strict=True)
    except (OSError, ValueError):
        raise ValueError("A training frame is unavailable") from None
    if not resolved.is_relative_to(root) or not resolved.is_file():
        raise ValueError("Training frame escapes its review directory")
    return resolved


def _valid_jpeg(raw: bytes) -> None:
    from PIL import Image

    try:
        with Image.open(io.BytesIO(raw)) as image:
            if image.format != "JPEG" or image.width * image.height > 4096 * 4096:
                raise ValueError("Training evidence must be a bounded JPEG image")
            image.load()
    except Exception:
        raise ValueError("Training frame is not a decodable JPEG image") from None


def _answer(value: object, frame_count: int) -> dict:
    if not isinstance(value, dict):
        raise ValueError("A reviewed window requires a human WindowResult answer")
    try:
        result = WindowResult.model_validate(value)
    except ValidationError:
        raise ValueError("Human training answer does not match WindowResult") from None
    for event in result.events:
        if not 0 <= event.start_frame <= event.peak_frame <= event.end_frame < frame_count:
            raise ValueError("Human training answer contains invalid frame indices")
        if len(set(event.conditions)) != 4 or set(event.conditions) != set(RULES[event.rule_id].criteria):
            raise ValueError("Human training answer lacks the exact visible conditions for its rule")
    return result.model_dump(mode="json")


def compile_review(review_path: Path) -> dict:
    """Validate human answers and provenance before producing a local bundle.

    Unreviewed answers never enter the bundle. Their source/group/frame hashes
    still participate in split checks, preventing a later review from concealing
    already mixed sources. No model or network is called by this function.
    """
    review_path = Path(review_path).resolve()
    try:
        review = json.loads(review_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        raise ValueError("Local training review is unavailable or invalid JSON") from None
    if not isinstance(review, dict) or review.get("version") != 1:
        raise ValueError("Unsupported training review version")
    digest = _digest(PROMPT.encode("utf-8"))
    if review.get("prompt") != PROMPT or review.get("prompt_sha256") != digest or review.get("rule_ids") != list(RULES):
        raise ValueError("Prompt snapshot or rules changed; regenerate and review training windows again")
    policy = review.get("source_policy")
    if not isinstance(policy, dict) or not isinstance(policy.get("dev_sha256"), list) or not isinstance(policy.get("reserved_sha256"), list):
        raise ValueError("Training review lacks its dev-only source policy")
    dev = {_hash(value) for value in policy["dev_sha256"]}
    reserved = {_hash(value) for value in policy["reserved_sha256"]}
    if not dev or dev & reserved:
        raise ValueError("Training dev sources overlap protected holdout/quarantine sources")
    examples = review.get("examples")
    if not isinstance(examples, list) or not examples:
        raise ValueError("Training review contains no windows")
    source_partitions = defaultdict(set)
    group_partitions = defaultdict(set)
    source_groups = defaultdict(set)
    frame_partitions = defaultdict(set)
    bundle = {"version": 1, "prompt": PROMPT, "prompt_sha256": digest, "rule_ids": list(RULES),
              "train": [], "validation": []}
    reviewed_sources = defaultdict(set)
    reviewed_groups = defaultdict(set)
    skipped = 0
    root = review_path.parent
    for example in examples:
        if not isinstance(example, dict):
            raise ValueError("Invalid training window")
        source = _hash(example.get("source_sha256"))
        if source not in dev or source in reserved:
            raise ValueError("A training window is not from an eligible dev source; holdout/quarantine is forbidden")
        partition = example.get("partition")
        if partition not in {"train", "validation"}:
            raise ValueError("Every window partition must be train or validation; holdout is forbidden")
        group_value = example.get("source_group")
        if not isinstance(group_value, str):
            raise ValueError("Training source_group must be a human-reviewed string")
        group = group_value.strip()
        group_key = group.casefold()
        source_partitions[source].add(partition)
        if group_key:
            group_partitions[group_key].add(partition)
            source_groups[source].add(group_key)
        reviewed = example.get("reviewed")
        if not isinstance(reviewed, bool):
            raise ValueError("Training reviewed must be a boolean")
        frames = example.get("frames")
        if not isinstance(frames, list):
            raise ValueError("Training frames must be a list")
        validated_frames = []
        last_timestamp = -math.inf
        for frame in frames:
            if not isinstance(frame, dict):
                raise ValueError("Invalid training frame entry")
            frame_hash = _hash(frame.get("sha256"))
            path = _frame_path(root, frame.get("path"))
            try:
                raw = path.read_bytes()
            except OSError:
                raise ValueError("A training frame is unavailable") from None
            if _digest(raw) != frame_hash:
                raise ValueError("Training frame hash changed after extraction or review")
            frame_partitions[frame_hash].add(partition)
            timestamp = frame.get("timestamp_sec")
            if isinstance(timestamp, bool) or not isinstance(timestamp, (int, float)) or not math.isfinite(timestamp) or timestamp < 0 or timestamp <= last_timestamp:
                raise ValueError("Training frames require strictly increasing finite source timestamps")
            last_timestamp = timestamp
            if reviewed:
                _valid_jpeg(raw)
            validated_frames.append({"path": str(path), "sha256": frame_hash, "timestamp_sec": timestamp})
        if not reviewed:
            skipped += 1
            continue
        if example.get("exhaustive") is not True:
            raise ValueError("A reviewed window must be exhaustively checked for every listed procedure")
        if not group or group_key in _PLACEHOLDERS or group_key.startswith("unknown-"):
            raise ValueError("A reviewed window requires a known human-reviewed source group, not a placeholder")
        if len(validated_frames) < 2:
            raise ValueError("A reviewed temporal window requires at least two frames")
        answer = _answer(example.get("answer"), len(validated_frames))
        bundle[partition].append({"frames": validated_frames, "answer": answer})
        reviewed_sources[partition].add(source)
        reviewed_groups[partition].add(group_key)
    if any(len(values) > 1 for values in source_partitions.values()):
        raise ValueError("Source SHA-256 overlaps train and validation, including unreviewed windows")
    if any(len(values) > 1 for values in group_partitions.values()):
        raise ValueError("Source groups overlap train and validation, including unreviewed windows")
    if any(len(values) > 1 for values in frame_partitions.values()):
        raise ValueError("Frame SHA-256 overlaps train and validation, including unreviewed windows")
    if any(len(values) > 1 for values in source_groups.values()):
        raise ValueError("The same source has inconsistent source groups")
    if not bundle["train"] or not bundle["validation"]:
        raise ValueError("At least one reviewed train window and one reviewed validation window are required")
    bundle["summary"] = {"total_examples": len(examples), "skipped_unreviewed": skipped,
                         **{f"{split}_examples": len(bundle[split]) for split in ("train", "validation")},
                         **{f"{split}_sources": len(reviewed_sources[split]) for split in ("train", "validation")},
                         **{f"{split}_groups": len(reviewed_groups[split]) for split in ("train", "validation")},
                         **{f"{split}_frames": sum(len(item["frames"]) for item in bundle[split]) for split in ("train", "validation")}}
    return bundle


def main() -> None:
    parser = argparse.ArgumentParser(description="Prepare local, human-reviewed multi-image training windows")
    commands = parser.add_subparsers(dest="command", required=True)
    draft = commands.add_parser("draft", help="Extract unlabelled dev windows into an ignored local directory")
    draft.add_argument("--manifest", type=Path, required=True)
    draft.add_argument("--output", type=Path, required=True)
    draft.add_argument("--frames-per-window", type=int, default=4)
    draft.add_argument("--max-side", type=int, default=448)
    draft.add_argument("--window-sec", type=float, default=24)
    draft.add_argument("--stride-sec", type=float, default=18)
    check = commands.add_parser("check", help="Validate human review and report aggregate counts only")
    check.add_argument("review", type=Path)
    args = parser.parse_args()
    try:
        if args.command == "draft":
            manifest = json.loads(args.manifest.read_text(encoding="utf-8"))
            summary = draft_review(manifest, args.output, frames_per_window=args.frames_per_window,
                                   max_side=args.max_side, window_sec=args.window_sec, stride_sec=args.stride_sec)
        else:
            summary = compile_review(args.review)["summary"]
    except (ValueError, VideoAnalysisError) as error:
        parser.exit(2, f"Training data preparation failed: {error}\n")
    except OSError:
        parser.exit(2, "Training data preparation failed: a required local file is unavailable\n")
    print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
