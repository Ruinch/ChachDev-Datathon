from __future__ import annotations

import argparse
import hashlib
import json
import math
import random
import re
from collections import Counter, defaultdict
from pathlib import Path


def canonical_id(value: str) -> str:
    return Path(str(value).strip()).stem.upper().replace("Х", "X").replace("–", "-").replace(" ", "")


def parse_timestamp(value) -> float | None:
    if value is None or value == "":
        return None
    if isinstance(value, (float, int)):
        result = float(value)
    else:
        text = str(value).strip().lower().replace(",", ".")
        if re.fullmatch(r"\d+:\d+(?:\.\d+)?", text):
            minutes, seconds = text.split(":")
            result = 60 * float(minutes) + float(seconds)
        else:
            match = re.fullmatch(r"(?:(\d+)\s*мин(?:ута|уты|ут)?\s*)?(\d+(?:\.\d+)?)\s*сек(?:унда|унды|унд)?", text)
            if not match:
                raise ValueError("Unrecognized annotation timestamp")
            result = 60 * int(match[1] or 0) + float(match[2])
    if not math.isfinite(result) or result < 0:
        raise ValueError("Invalid annotation timestamp")
    return result


def assign_splits(records: list[dict], seed: int = 26) -> None:
    groups = defaultdict(list)
    for record in records:
        groups[record["sha256"]].append(record)
    extremes = {min(records, key=lambda r: r["duration_sec"])["sha256"], max(records, key=lambda r: r["duration_sec"])["sha256"]}
    buckets = defaultdict(list)
    for digest, members in groups.items():
        conflict = len({(r["label"], r["point_sec"]) for r in members}) > 1
        for r in members:
            r["duplicate_count"] = len(members)
            r["label_conflict"] = conflict
            r["split"] = "quarantine" if conflict else "dev"
        if not conflict and digest not in extremes:
            buckets[members[0]["label"]].append(digest)
    rng = random.Random(seed)
    for label in sorted(buckets):
        digests = sorted(buckets[label])
        rng.shuffle(digests)
        for digest in digests[:max(1, round(len(digests) * 0.2))]:
            for record in groups[digest]:
                record["split"] = "holdout"


def audit(dataset: Path, output: Path) -> dict:
    import av
    import openpyxl

    table = dataset / "open_labels.xlsx"
    workbook = openpyxl.load_workbook(table, read_only=True, data_only=True)
    try:
        rows = list(workbook.active.values)
    finally:
        workbook.close()
    labels = {}
    for name, timestamp, label, *_ in rows[1:]:
        if name is None:
            continue
        key = canonical_id(name)
        if key in labels:
            raise ValueError("Duplicate normalized label ID")
        if label not in {"нарушение", "без нарушения"}:
            raise ValueError("Unknown source label")
        labels[key] = ("violation" if label == "нарушение" else "no_violation", parse_timestamp(timestamp))
    records = []
    seen = set()
    for path in sorted((dataset / "videos").glob("*")):
        if path.suffix.lower() != ".mov":
            continue
        key = canonical_id(path.name)
        if key not in labels or key in seen:
            raise ValueError("Missing label or duplicate normalized video ID")
        seen.add(key)
        with path.open("rb") as f:
            digest = hashlib.file_digest(f, "sha256").hexdigest()
        with av.open(str(path)) as container:
            stream = container.streams.video[0]
            if not stream.duration:
                raise ValueError("Missing video duration")
            duration = float(stream.duration * stream.time_base)
            metadata = dict(duration_sec=duration, width=stream.width, height=stream.height,
                            fps=float(stream.average_rate), codec=stream.codec_context.name)
        label, point = labels[key]
        if point is not None and not 0 <= point < duration:
            raise ValueError("Annotation point outside source video")
        if (label == "violation") != (point is not None):
            raise ValueError("Source label and timestamp are inconsistent")
        records.append(dict(id=key, file_name=path.name, path=str(path.resolve()), bytes=path.stat().st_size,
                            sha256=digest, label=label, point_sec=point, **metadata))
    if seen != set(labels) or not records:
        raise ValueError("Video/label mapping is incomplete")
    assign_splits(records)
    summary = {"videos": len(records), "unique_videos": len({r['sha256'] for r in records}),
               "labels": dict(Counter(r['label'] for r in records)), "splits": dict(Counter(r['split'] for r in records)),
               "min_duration_sec": min(r['duration_sec'] for r in records), "max_duration_sec": max(r['duration_sec'] for r in records),
               "total_duration_sec": sum(r['duration_sec'] for r in records)}
    manifest = {"version": 1, "seed": 26, "source_grouping_reviewed": False,
                "notice": "Provisional split by SHA-256. Review same-camera/session groups before claiming independent quality.",
                "summary": summary, "records": records}
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    return summary


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("dataset", type=Path)
    parser.add_argument("--output", type=Path, default=Path(".local_ml/dataset.json"))
    args = parser.parse_args()
    print(json.dumps(audit(args.dataset, args.output), ensure_ascii=False, indent=2))
