from __future__ import annotations

import logging
import multiprocessing
import os
import signal
import subprocess
import threading

from pydantic import ValidationError

from app.analyzer import (
    AnalysisContext,
    AnalyzerNotConfiguredError,
    LocalAnalysisResult,
    LocalAnalyzer,
    load_analyzer_factory,
)
from app.config import Settings
from app.evidence import EvidenceExtractionError, materialize_result
from app.rules import load_rule_catalog
from app.storage import JobStore


logger = logging.getLogger(__name__)
_WINDOWS_JOB_HANDLE = None


def _establish_process_isolation() -> None:
    if os.name != "nt":
        os.setsid()
        return
    # A Windows job closes the whole tree even if the analyzer crashes or exits
    # after spawning a helper. The handle is private and closes when this process exits.
    import ctypes
    from ctypes import wintypes

    class BasicLimits(ctypes.Structure):
        _fields_ = [
            ("PerProcessUserTimeLimit", ctypes.c_int64), ("PerJobUserTimeLimit", ctypes.c_int64),
            ("LimitFlags", wintypes.DWORD), ("MinimumWorkingSetSize", ctypes.c_size_t),
            ("MaximumWorkingSetSize", ctypes.c_size_t), ("ActiveProcessLimit", wintypes.DWORD),
            ("Affinity", ctypes.c_size_t), ("PriorityClass", wintypes.DWORD),
            ("SchedulingClass", wintypes.DWORD),
        ]

    class IoCounters(ctypes.Structure):
        _fields_ = [(name, ctypes.c_uint64) for name in (
            "ReadOperationCount", "WriteOperationCount", "OtherOperationCount",
            "ReadTransferCount", "WriteTransferCount", "OtherTransferCount",
        )]

    class ExtendedLimits(ctypes.Structure):
        _fields_ = [
            ("BasicLimitInformation", BasicLimits), ("IoInfo", IoCounters),
            ("ProcessMemoryLimit", ctypes.c_size_t), ("JobMemoryLimit", ctypes.c_size_t),
            ("PeakProcessMemoryUsed", ctypes.c_size_t), ("PeakJobMemoryUsed", ctypes.c_size_t),
        ]

    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
    kernel.CreateJobObjectW.restype = wintypes.HANDLE
    kernel.SetInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD]
    kernel.SetInformationJobObject.restype = wintypes.BOOL
    kernel.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
    kernel.AssignProcessToJobObject.restype = wintypes.BOOL
    kernel.GetCurrentProcess.restype = wintypes.HANDLE
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    handle = kernel.CreateJobObjectW(None, None)
    if not handle:
        raise OSError(ctypes.get_last_error(), "Could not create an isolated analyzer job")
    limits = ExtendedLimits()
    limits.BasicLimitInformation.LimitFlags = 0x00002000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
    if not kernel.SetInformationJobObject(handle, 9, ctypes.byref(limits), ctypes.sizeof(limits)) or not kernel.AssignProcessToJobObject(handle, kernel.GetCurrentProcess()):
        error = ctypes.get_last_error()
        kernel.CloseHandle(handle)
        raise OSError(error, "Could not isolate the analyzer process tree")
    global _WINDOWS_JOB_HANDLE
    _WINDOWS_JOB_HANDLE = handle


def _run_job(job_id: str, settings: Settings, analyzer: LocalAnalyzer | None) -> None:
    store = JobStore(settings.data_dir)
    video_path = store.job_dir(job_id) / "input.mov"
    try:
        _establish_process_isolation()
        record = store.get(job_id)
        if record is None:
            raise RuntimeError("Job record is missing")
        duration = record["metadata"]["duration_sec"]
        catalog = load_rule_catalog(settings.rule_catalog_path)
        if not catalog.enabled_rule_ids:
            store.set_failed(job_id, "analysis_not_ready", "No verified rules have been enabled")
            return
        local_analyzer = analyzer if analyzer is not None else load_analyzer_factory(settings.analyzer_factory)
        detected = LocalAnalysisResult.model_validate(local_analyzer.analyze(AnalysisContext(
            job_id=job_id,
            video_path=video_path,
            duration_sec=duration,
            enabled_rule_ids=catalog.enabled_rule_ids,
        )))
        completed = materialize_result(
            job_id=job_id,
            video_path=video_path,
            evidence_dir=store.evidence_dir(job_id),
            result=detected,
            ffmpeg_bin=settings.ffmpeg_bin,
            video_duration_sec=duration,
            rule_catalog=catalog,
        )
        store.set_done(job_id, completed.model_dump(mode="json", exclude={"job_id", "status"}))
    except AnalyzerNotConfiguredError as exc:
        store.set_failed(job_id, "analyzer_unavailable", str(exc))
    except EvidenceExtractionError:
        logger.exception("Evidence extraction failed for job %s", job_id)
        store.set_failed(job_id, "evidence_extraction_failed", "Could not extract real evidence frames from this video")
    except ValidationError:
        logger.exception("Invalid analyzer result for job %s", job_id)
        store.set_failed(job_id, "invalid_analysis_result", "The analyzer returned an invalid result")
    except Exception as exc:
        logger.exception("Local analysis failed for job %s", job_id)
        # Contract errors expose a stable safe code; arbitrary plugin messages may contain internals.
        code = getattr(exc, "code", "analysis_failed")
        messages = {
            "analyzer_configuration_error": "The local analyzer could not be initialized",
            "invalid_analysis_result": "The analyzer returned an invalid result",
            "insufficient_evidence": "The analyzer could not complete all required checks",
            "invalid_rule": "The result references an unverified or inapplicable rule",
        }
        store.set_failed(job_id, code if code in messages else "analysis_failed", messages.get(code, "Local video analysis failed"))


def _terminate_tree(process: multiprocessing.Process) -> None:
    """Stop the analyzer and the subprocesses it launched, including a stuck decoder."""
    if os.name == "nt":
        if not process.is_alive():
            return
        try:
            subprocess.run(
                ["taskkill", "/PID", str(process.pid), "/T", "/F"],
                check=False, capture_output=True, timeout=10,
            )
        except (OSError, subprocess.TimeoutExpired):
            logger.exception("Could not terminate analyzer process tree")
    else:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        except OSError:
            # The child can time out before it has established its own process group.
            process.kill()
    process.join(timeout=2)
    if process.is_alive():
        process.kill()
        process.join(timeout=2)


class JobRunner:
    """One reserved slot, no pending queue, and a killable process for each accepted job."""

    def __init__(self, settings: Settings, store: JobStore, analyzer: LocalAnalyzer | None) -> None:
        self.settings = settings
        self.store = store
        self.analyzer = analyzer
        self._slot = threading.Lock()
        self._state = threading.Lock()
        self._shutdown = threading.Event()
        self._process: multiprocessing.Process | None = None
        self._thread: threading.Thread | None = None

    @property
    def busy(self) -> bool:
        return self._slot.locked()

    def startup(self) -> None:
        self._shutdown.clear()

    def reserve(self) -> bool:
        return not self._shutdown.is_set() and self._slot.acquire(blocking=False)

    def release(self) -> None:
        self._slot.release()

    def start(self, job_id: str) -> None:
        process = None
        try:
            # Publish both owners before shutdown can inspect them. An admitted
            # upload may finish saving after shutdown has already begun.
            with self._state:
                if self._shutdown.is_set():
                    self.store.set_failed(job_id, "interrupted", "Processing was interrupted by server shutdown")
                    self.release()
                    return
                process = multiprocessing.get_context("spawn").Process(
                    target=_run_job,
                    args=(job_id, self.settings, self.analyzer),
                    name=f"analysis-{job_id}",
                )
                thread = threading.Thread(target=self._supervise, args=(job_id, process), daemon=True)
                process.start()
                self._process, self._thread = process, thread
                thread.start()
        except Exception:
            logger.exception("Could not start analysis worker")
            with self._state:
                self._process, self._thread = None, None
            try:
                if process is not None and process.pid is not None:
                    _terminate_tree(process)
            finally:
                try:
                    self.store.set_failed(job_id, "worker_start_failed", "Could not start the local analysis process")
                finally:
                    self.release()

    def _supervise(self, job_id: str, process: multiprocessing.Process) -> None:
        try:
            # Model loading, model inference and extraction all share this deadline.
            process.join(timeout=self.settings.job_timeout_sec)
            if process.is_alive():
                if self._shutdown.is_set():
                    self.store.set_failed(job_id, "interrupted", "Processing was interrupted by server shutdown")
                else:
                    self.store.set_failed(job_id, "analysis_timeout", "Analysis exceeded its processing time limit")
                _terminate_tree(process)
            else:
                record = self.store.get(job_id)
                if record and record["status"] == "processing":
                    if self._shutdown.is_set():
                        self.store.set_failed(job_id, "interrupted", "Processing was interrupted by server shutdown")
                    else:
                        self.store.set_failed(job_id, "worker_crashed", "The local analysis process stopped unexpectedly")
                if os.name != "nt":
                    _terminate_tree(process)
        except Exception:
            logger.exception("Analysis supervisor failed for job %s", job_id)
            _terminate_tree(process)
            self.store.set_failed(job_id, "worker_crashed", "The local analysis process stopped unexpectedly")
        finally:
            with self._state:
                self._process = None
            self.release()

    def shutdown(self) -> None:
        self._shutdown.set()
        with self._state:
            process, thread = self._process, self._thread
        if process is not None:
            _terminate_tree(process)
        if thread is not None:
            thread.join(timeout=15)
