"""Public runtime installation is verified with tiny synthetic files only."""
import hashlib
import io
import json
import zipfile
from pathlib import Path

import pytest

from ml_tools import install_runtime
from ml_tools.runtime_report import build_report


def fixture_lock(tmp_path, monkeypatch):
    archive = io.BytesIO()
    with zipfile.ZipFile(archive, "w") as bundle:
        bundle.writestr("bin/llama-server.exe", b"synthetic runtime")
        bundle.writestr("bin/dependency.dll", b"synthetic dependency")
    blobs = {"runtime.zip": archive.getvalue(), "model.gguf": b"synthetic weights"}
    manifest = {"llama_release": "test", "files": [
        {"name": name, "kind": "runtime" if name.endswith(".zip") else "model",
         "sha256": hashlib.sha256(body).hexdigest(), "url": "https://example.invalid/" + name}
        for name, body in blobs.items()
    ]}
    lock = tmp_path / "lock.json"
    lock.write_text(json.dumps(manifest), encoding="utf-8")
    monkeypatch.setattr(install_runtime.urllib.request, "urlopen", lambda url, **kw: io.BytesIO(blobs[url.rsplit("/", 1)[1]]))
    return lock, manifest, blobs


def test_offline_verification_covers_weights_archive_and_extracted_runtime(tmp_path, monkeypatch):
    lock, _, _ = fixture_lock(tmp_path, monkeypatch)
    target = tmp_path / "prepared"
    install_runtime.install(lock, target)
    monkeypatch.setattr(install_runtime.urllib.request, "urlopen", lambda *a, **k: pytest.fail("offline verification used network"))
    before = {path: path.stat().st_mtime_ns for path in target.rglob("*") if path.is_file()}
    install_runtime.verify_installed(lock, target)
    assert before == {path: path.stat().st_mtime_ns for path in target.rglob("*") if path.is_file()}
    (target / "llama/llama-server.exe").write_bytes(b"corrupt runtime!!")
    with pytest.raises(ValueError, match="Runtime checksum mismatch"):
        install_runtime.verify_installed(lock, target)


def test_verification_of_missing_transfer_does_not_create_or_download(tmp_path, monkeypatch):
    lock, _, _ = fixture_lock(tmp_path, monkeypatch)
    target = tmp_path / "missing"
    monkeypatch.setattr(install_runtime.urllib.request, "urlopen", lambda *a, **k: pytest.fail("offline verification used network"))
    with pytest.raises(FileNotFoundError):
        install_runtime.verify_installed(lock, target)
    assert not target.exists()


def test_bad_download_is_never_installed(tmp_path, monkeypatch):
    lock, manifest, blobs = fixture_lock(tmp_path, monkeypatch)
    blobs["model.gguf"] = b"corrupt download"
    with pytest.raises(ValueError, match="Downloaded checksum mismatch"):
        install_runtime.install(lock, tmp_path / "prepared")
    assert not (tmp_path / "prepared/models/model.gguf").exists()
    assert not list((tmp_path / "prepared").rglob("*.part"))


@pytest.mark.parametrize("bad_name", ["../escape.gguf", "..\\escape.gguf", "C:model.gguf"])
def test_manifest_rejects_cross_platform_path_injection(tmp_path, monkeypatch, bad_name):
    lock, manifest, _ = fixture_lock(tmp_path, monkeypatch)
    manifest["files"][1]["name"] = bad_name
    lock.write_text(json.dumps(manifest), encoding="utf-8")
    with pytest.raises(ValueError, match="invalid filename"):
        install_runtime.install(lock, tmp_path / "prepared")
    assert not (tmp_path / "prepared").exists()


def test_colliding_archive_basenames_are_not_silently_overwritten(tmp_path, monkeypatch):
    lock, manifest, blobs = fixture_lock(tmp_path, monkeypatch)
    archive = io.BytesIO()
    with zipfile.ZipFile(archive, "w") as bundle:
        bundle.writestr("first/llama-server.exe", b"first")
        bundle.writestr("second/llama-server.exe", b"second")
    blobs["runtime.zip"] = archive.getvalue()
    manifest["files"][0]["sha256"] = hashlib.sha256(archive.getvalue()).hexdigest()
    lock.write_text(json.dumps(manifest), encoding="utf-8")
    with pytest.raises(ValueError, match="Duplicate runtime filename"):
        install_runtime.install(lock, tmp_path / "prepared")
    assert not (tmp_path / "prepared/llama/llama-server.exe").exists()


def test_wrong_existing_model_is_preserved_and_rejected(tmp_path, monkeypatch):
    lock, _, _ = fixture_lock(tmp_path, monkeypatch)
    target = tmp_path / "prepared"
    (target / "models").mkdir(parents=True)
    model = target / "models/model.gguf"
    model.write_bytes(b"existing different model")
    with pytest.raises(ValueError, match="Checksum mismatch"):
        install_runtime.install(lock, target)
    assert model.read_bytes() == b"existing different model"


def test_environment_report_uses_whitelist_and_never_reads_bank_data(tmp_path, monkeypatch):
    monkeypatch.setenv("APP_API_KEY", "secret-do-not-export")
    monkeypatch.setenv("APP_DATA_DIR", str(tmp_path / "private-bank-data"))
    monkeypatch.setenv("APP_ML_FRAMES", "24")
    monkeypatch.setenv("APP_LOCAL_MODEL_URL", "https://user:secret@example.invalid")
    monkeypatch.setattr("ml_tools.runtime_report.shutil.which", lambda name: None)
    report = build_report(tmp_path, lock=tmp_path / "missing-lock.json")
    serialized = json.dumps(report)
    assert "secret-do-not-export" not in serialized
    assert "private-bank-data" not in serialized
    assert "example.invalid" not in serialized
    assert report["settings_overrides"]["APP_ML_FRAMES"] == "24"
    assert report["model_files_verified"] is False
    assert report["inference_quality_tested"] is False
