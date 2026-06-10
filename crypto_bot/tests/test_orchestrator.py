"""Phase 13 (DB-free): single-instance lock + scheduler wiring."""

import pytest

from crypto_bot import orchestrator as orch


def test_single_instance_lock_blocks_reentry():
    with orch.single_instance_lock():
        with pytest.raises(orch.AlreadyRunning):
            with orch.single_instance_lock():
                pass


def test_single_instance_lock_releases():
    with orch.single_instance_lock():
        pass
    # Re-acquirable after the first context exits.
    with orch.single_instance_lock():
        pass


def test_build_scheduler_has_cycle_job():
    sched = orch.build_scheduler()
    job = sched.get_job("cycle")
    assert job is not None
    assert job.max_instances == 1


def test_run_cycle_safe_skips_when_locked():
    with orch.single_instance_lock():
        # Lock already held -> the safe wrapper returns None instead of raising.
        assert orch.run_cycle_safe() is None
