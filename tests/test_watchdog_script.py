"""Smoke test: watchdog.ps1 must run cleanly the way Task Scheduler invokes it.

Regression: $PSScriptRoot is empty inside param() defaults under
`powershell -File` (5.1); an early version used it there and the scheduled run
exited 1 while direct `.\\watchdog.ps1` runs worked.
"""
import os
import shutil
import subprocess

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
POWERSHELL = shutil.which("powershell.exe")

pytestmark = pytest.mark.skipif(POWERSHELL is None, reason="Windows PowerShell not available")


def _run(*args):
    return subprocess.run(
        [POWERSHELL, "-NoProfile", "-ExecutionPolicy", "Bypass", "-File",
         os.path.join(ROOT, "watchdog.ps1"), *args],
        capture_output=True, text=True, timeout=60,
    )


def test_dry_run_via_file_exits_zero():
    r = _run("-DryRun")
    assert r.returncode == 0, r.stderr


def test_dry_run_with_nothing_running_takes_no_action_and_exits_zero():
    # patterns match nothing -> "dead" branch; -DryRun must not start the task
    r = _run("-DryRun", "-ProcessPattern", "zzz_no_such_proc", "-LoopPattern", "zzz_no_such_proc")
    assert r.returncode == 0, r.stderr
