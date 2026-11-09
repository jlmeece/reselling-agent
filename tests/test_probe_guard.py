import os
import time

import pytest

from tools import probe_guard as pg


def test_no_lock_file_means_free(tmp_path):
    assert pg.lock_holder(str(tmp_path / "nope")) is None
    pg.refuse_if_scheduler_busy(["probe"], path=str(tmp_path / "nope"))      # does not exit


def test_fresh_lock_refuses_with_holder(tmp_path):
    lock = tmp_path / ".scheduler_lock"
    lock.write_text("research 2026-11-07 14:09:00")
    assert pg.lock_holder(str(lock)) == "research 2026-11-07 14:09:00"
    with pytest.raises(SystemExit) as e:
        pg.refuse_if_scheduler_busy(["probe"], path=str(lock))
    assert "research 2026-11-07 14:09:00" in str(e.value) and "--force" in str(e.value)


def test_force_skips_the_check(tmp_path):
    lock = tmp_path / ".scheduler_lock"
    lock.write_text("research x")
    pg.refuse_if_scheduler_busy(["probe", "--force"], path=str(lock))


def test_stale_lock_is_ignored(tmp_path):
    lock = tmp_path / ".scheduler_lock"
    lock.write_text("research x")
    old = time.time() - 46 * 60
    os.utime(lock, (old, old))
    assert pg.lock_holder(str(lock)) is None


def test_stale_minutes_match_the_scheduler():
    from agents import scheduler
    assert pg.LOCK_STALE_MINUTES == scheduler.LOCK_STALE_MINUTES
    assert os.path.normcase(pg.LOCK_FILE) == os.path.normcase(scheduler.LOCK_FILE)
