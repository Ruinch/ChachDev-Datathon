from __future__ import annotations

import math
import os
import threading
import time
from collections import Counter
from dataclasses import dataclass

from app.analyzer import AnalysisContext, DetectedViolation, InsufficientEvidenceError, LocalAnalysisResult
from app.ml.model import LoopbackModel, WindowResult
from app.ml.rules import RULES
from app.ml.video import Frame, VideoAnalysisError, check_deadline, decode_video, select_frames, windows


@dataclass(frozen=True)
class AnalyzerConfig:
    endpoint: str = "http://127.0.0.1:8091"
    budget_sec: float = 210
    window_sec: float = 24
    stride_sec: float = 18
    sample_fps: float = 2
    frames_per_window: int = 12
    max_side: int = 848
    max_duration_sec: float = 180
    max_refinements: int = 12
    input_mode: str = 'images'

    def __post_init__(self):
        for value in (self.budget_sec, self.window_sec, self.stride_sec, self.sample_fps, self.max_duration_sec):
            if not math.isfinite(value) or value <= 0:
                raise ValueError("Timing and sampling settings must be finite and positive")
        if self.input_mode not in {'images', 'video'}:
            raise ValueError('Unsupported model input mode')
        if self.stride_sec > self.window_sec or not 2 <= self.frames_per_window <= (32 if self.input_mode == 'video' else 16):
            raise ValueError("Invalid window coverage or frame count")
        if not 128 <= self.max_side <= 1024 or not 0 <= self.max_refinements <= 100:
            raise ValueError("Invalid image size or refinement limit")


@dataclass(frozen=True)
class Candidate:
    rule_id: str
    start: float
    end: float
    peak: float
    observation: str
    context_start: float = 0
    context_end: float = 0
    context_frames: tuple[Frame, ...] = ()
    sample_tolerance: float = 0


def candidates(result: WindowResult, frames: list[Frame]) -> list[Candidate]:
    return [Candidate(e.rule_id, frames[e.start_frame].timestamp, frames[e.end_frame].timestamp,
                      frames[e.peak_frame].timestamp, e.observation,
                      frames[0].timestamp, frames[-1].timestamp, tuple(frames),
                      max(b.timestamp-a.timestamp for a, b in zip(frames, frames[1:]))/2)
            for e in result.events]


def merge_events(events: list[Candidate]) -> list[Candidate]:
    """Merge overlapping same-rule event intervals, never entire search windows."""
    merged: list[Candidate] = []
    for event in sorted(events, key=lambda e: (e.rule_id, e.start, e.end)):
        if merged and merged[-1].rule_id == event.rule_id and event.start <= merged[-1].end:
            previous = merged[-1]
            merged[-1] = Candidate(previous.rule_id, previous.start, max(previous.end, event.end), previous.peak, previous.observation,
                                  min(previous.context_start, event.context_start), max(previous.context_end, event.context_end),
                                  tuple(sorted({f.timestamp: f for f in (*previous.context_frames, *event.context_frames)}.values(),
                                               key=lambda f: f.timestamp)),
                                  max(previous.sample_tolerance, event.sample_tolerance))
        else:
            merged.append(event)
    return sorted(merged, key=lambda e: e.peak)


def refinement_frames(video, event: Candidate, count: int) -> list[Frame]:
    """Retain the screening evidence and add samples around the detected action.

    Resampling an entire window at a different uniform spacing can drop the very
    handover/role cue which supported a candidate. Only merged contexts exceeding
    the input cap must be reduced; retain their event anchors and outer context.
    """
    if count < 5:
        raise ValueError('Refinement needs space for event and context anchors')
    original = list(event.context_frames) or select_frames(video, event.context_start, event.context_end, count)
    anchors = {event.start, event.peak, event.end, original[0].timestamp, original[-1].timestamp}
    if len(original) > count:
        chosen = {f.timestamp: f for f in original if f.timestamp in anchors}
        remaining = [f for f in original if f.timestamp not in chosen]
        slots = count-len(chosen)
        indices = ([round(i*(len(remaining)-1)/(slots-1)) for i in range(slots)] if slots > 1
                   else [len(remaining)//2] if slots == 1 else [])
        chosen.update((remaining[i].timestamp, remaining[i]) for i in indices)
    else:
        chosen = {f.timestamp: f for f in original}
    nearby = [f for f in video.frames if max(0, event.start-2) <= f.timestamp <= event.end+2
              and f.timestamp not in chosen]
    nearby.sort(key=lambda f: (min(abs(f.timestamp-t) for t in (event.start, event.peak, event.end)), f.timestamp))
    for frame in nearby[:max(0, count-len(chosen))]:
        chosen[frame.timestamp] = frame
    return sorted(chosen.values(), key=lambda f: f.timestamp)


def same_candidate(original: Candidate, refined: Candidate) -> bool:
    # A point between sparse screening samples may move by half a sample spacing.
    return (original.rule_id == refined.rule_id
            and refined.start <= original.end+original.sample_tolerance
            and refined.end >= original.start-original.sample_tolerance)


class QwenLocalAnalyzer:
    """Experimental local VLM implementation of the existing LocalAnalyzer protocol.

    The service serializes jobs and the external llama server serializes GPU calls.
    Queue waiting counts toward the budget. Unassessable windows forbid an all-clear
    but do not erase independently confirmed positive events.
    """
    _gpu_lock = threading.Lock()

    def __init__(self, config: AnalyzerConfig | None = None, *, model=None):
        self.config = config or AnalyzerConfig()
        self.model = model or LoopbackModel(self.config.endpoint, input_mode=self.config.input_mode)

    def analyze(self, context: AnalysisContext) -> LocalAnalysisResult:
        result, _ = self.analyze_with_metrics(context)
        return result

    def analyze_with_metrics(self, context: AnalysisContext):
        started = time.monotonic()
        enabled = context.enabled_rule_ids
        if not enabled:
            raise InsufficientEvidenceError("No visual procedures are enabled")
        if set(enabled) - set(RULES):
            raise VideoAnalysisError("The local model does not support every enabled procedure")
        deadline = started + self.config.budget_sec
        if not self._gpu_lock.acquire(timeout=check_deadline(deadline)):
            raise VideoAnalysisError("Local GPU queue exceeded analysis budget")
        try:
            acquired = time.monotonic()
            cfg = self.config
            video = decode_video(context.video_path, fps=cfg.sample_fps, max_side=cfg.max_side,
                                 deadline=deadline, max_duration=cfg.max_duration_sec)
            if abs(video.duration - context.duration_sec) > 0.5:
                raise VideoAnalysisError("Decoder and service disagree on source duration")
            decoded = time.monotonic()
            pending = []
            unassessable = 0
            spans = windows(video.duration, cfg.window_sec, cfg.stride_sec)
            # A global pass connects customer handovers across window boundaries.
            if len(spans) > 1:
                spans = [(0, video.duration)] + spans
            for start, end in spans:
                frames = select_frames(video, start, end, cfg.frames_per_window)
                result = self.model.infer(frames, check_deadline(deadline), rule_ids=enabled)
                unassessable += not result.assessable
                pending.extend(candidates(result, frames))
            pending = merge_events(pending)
            if len(pending) > cfg.max_refinements:
                raise VideoAnalysisError("Too many candidates to verify within configured budget")
            refined = []
            for event in pending:
                # Preserve the original operation context: cropping to the peak alone
                # can remove the customer handover/start and make refinement invalid.
                frames = refinement_frames(video, event, min(32 if cfg.input_mode == 'video' else 16, cfg.frames_per_window + 4))
                result = self.model.infer(frames, check_deadline(deadline), refine=True, rule_ids=(event.rule_id,))
                if not result.assessable:
                    unassessable += 1
                # New unrelated claims at refinement cannot bypass the two-pass check.
                refined.extend(e for e in candidates(result, frames) if same_candidate(event, e))
            check_deadline(deadline)
            events = merge_events(refined)
            # Specific next-customer event takes precedence over generic cash storage.
            events = [e for e in events if e.rule_id != 'R20-C' or not any(
                b.rule_id == 'R20-B' and b.start <= e.end and b.end >= e.start for b in events)]
            violations = []
            for event in events:
                evidence = sorted(set(t for t in (event.start, event.peak, event.end) if t < context.duration_sec))
                if event.peak >= context.duration_sec or not evidence:
                    raise VideoAnalysisError("Event is outside the source timeline")
                violations.append(DetectedViolation(rule_id=event.rule_id, timecode_sec=event.peak,
                    description=event.observation + " Требуется проверка человеком по указанному пункту.",
                    evidence_timestamps_sec=evidence))
            if unassessable and not violations:
                raise InsufficientEvidenceError("Some video windows lack sufficient visual context")
            result = LocalAnalysisResult(verdict="violation" if violations else "no_violation", violations=violations,
                analysis_complete=not unassessable, checked_rule_ids=list(enabled),
                limitations=["Часть последовательности не позволяет проверить все включённые процедуры."] if unassessable else [])
            metrics = {"elapsed_sec": round(time.monotonic() - started, 3), "queue_sec": round(acquired - started, 3),
                       "decode_sec": round(decoded - acquired, 3), "duration_sec": round(video.duration, 3),
                       "sampled_frames": len(video.frames), "decoded_frames": video.decoded_frames,
                       "windows": len(spans), "refinements": len(pending), "events": len(violations),
                       "candidate_counts": dict(Counter(e.rule_id for e in pending))}
            return result, metrics
        finally:
            self._gpu_lock.release()


def build_analyzer() -> QwenLocalAnalyzer:
    """Factory for APP_ANALYZER_FACTORY=app.local_analyzer:build_analyzer."""
    return QwenLocalAnalyzer(AnalyzerConfig(
        endpoint=os.getenv('APP_LOCAL_MODEL_URL', 'http://127.0.0.1:8091'),
        budget_sec=float(os.getenv('APP_ML_BUDGET_SECONDS', '210')),
        max_side=int(os.getenv('APP_ML_MAX_SIDE', '848')),
        frames_per_window=int(os.getenv('APP_ML_FRAMES', '12')),
        input_mode=os.getenv('APP_ML_INPUT_MODE', 'images'),
    ))
