"""Windows profile discovery uses explicit paths before the local pointer."""
from __future__ import annotations

import os
from pathlib import Path
import subprocess

import pytest


pytestmark = pytest.mark.skipif(os.name != "nt", reason="Windows launcher integration")
COMMON = Path(__file__).resolve().parents[1] / "scripts" / "common.ps1"


def resolve(root: Path, *, pointer: str | None = None, explicit: Path | None = None):
    if pointer is not None:
        local = root / ".local_data"
        local.mkdir(parents=True)
        (local / "laptop-profile-path.txt").write_text(pointer, encoding="utf-8")
    env = os.environ.copy()
    env.update(TEST_COMMON=str(COMMON), TEST_ROOT=str(root),
               TEST_EXPLICIT=str(explicit) if explicit else "",
               LOCALAPPDATA=str(root / "synthetic-localappdata"))
    command = (
        "$ErrorActionPreference='Stop'; . $env:TEST_COMMON; "
        "Resolve-LaptopProfilePath -ProjectRoot $env:TEST_ROOT -ProfilePath $env:TEST_EXPLICIT"
    )
    return subprocess.run(
        ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", command],
        env=env, capture_output=True, text=True, timeout=30,
    )


def test_profile_pointer_selects_absolute_portable_location(tmp_path):
    target = tmp_path / "portable profile" / "jury"
    result = resolve(tmp_path, pointer=f"{target}\n")
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == str(target)


def test_explicit_profile_overrides_even_malformed_pointer(tmp_path):
    target = tmp_path / "explicit" / "jury"
    result = resolve(tmp_path, pointer="not a valid absolute path", explicit=target)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == str(target)


def test_missing_pointer_preserves_localappdata_default(tmp_path):
    result = resolve(tmp_path)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == str(tmp_path / "synthetic-localappdata" / "ForteBankDatathon" / "jury")


@pytest.mark.parametrize("pointer", [
    "", "relative/jury", r"C:relative\jury", r"\rooted-but-not-absolute",
    '"C:\\private\\jury"', "C:\\first\nC:\\second", r"C:\private\*",
])
def test_malformed_pointer_does_not_fall_back_to_another_profile(tmp_path, pointer):
    result = resolve(tmp_path, pointer=pointer)
    assert result.returncode != 0
    assert "profile pointer" in result.stderr.lower()
