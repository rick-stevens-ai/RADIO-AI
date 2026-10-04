from __future__ import annotations

from pathlib import Path
import json
import os
import subprocess
import sys

import pytest

from ft8_scout import (
    ScoutConfig,
    ScoutController,
    ScoutFault,
    SlotStateStore,
    build_dry_run,
)


class FakeClock:
    def __init__(self, epoch: float):
        self.now = epoch
        self.sleeps: list[float] = []

    def time(self) -> float:
        return self.now

    def sleep_until(self, epoch: float) -> None:
        self.sleeps.append(epoch)
        self.now = max(self.now, epoch)


class FakeRig:
    def __init__(self, *, bad_after_tx: int | None = None):
        self.snapshots = 0
        self.restores = 0
        self.telemetry_calls = 0
        self.bad_after_tx = bad_after_tx
        self.tx_count = 0

    def snapshot(self) -> dict[str, object]:
        self.snapshots += 1
        return {"frequency": 14_074_000, "mode": "USB-D"}

    def telemetry(self) -> dict[str, object]:
        self.telemetry_calls += 1
        healthy = self.bad_after_tx is None or self.tx_count < self.bad_after_tx
        return {"complete": healthy, "fresh": healthy, "ptt": False, "tx_enabled": False}

    def restore(self, snapshot: dict[str, object]) -> None:
        self.restores += 1


class FakeTx:
    def __init__(self, rig: FakeRig, *, fail_at: int | None = None):
        self.rig = rig
        self.fail_at = fail_at
        self.calls: list[int] = []

    def invoke(self, slot_index: int, planned_epoch: float, deadline_epoch: float) -> dict[str, object]:
        self.calls.append(slot_index)
        self.rig.tx_count += 1
        if self.fail_at == slot_index:
            raise RuntimeError("transmitter fault")
        return {
            "slot_index": slot_index,
            "planned_epoch": planned_epoch,
            "exit_code": 0,
            "sent": True,
            "validated": True,
        }


def controller(tmp_path, clock, rig, tx, callbacks=(), submit_processing=None):
    return ScoutController(
        config=ScoutConfig(anchor_epoch=clock.time(), max_late_s=0.5),
        clock=clock,
        rig=rig,
        transmitter=tx,
        state=SlotStateStore(tmp_path / "state.json"),
        rx_callbacks=callbacks,
        submit_processing=submit_processing,
    )


def test_complete_window_is_utc_aligned_thirty_tx_thirty_rx_and_restores_once(tmp_path):
    clock = FakeClock(1_800_000_000.0)  # divisible by 900 and 15
    rig = FakeRig()
    tx = FakeTx(rig)

    report = controller(tmp_path, clock, rig, tx).run()

    assert report.anchor_epoch % 900 == 0
    assert report.deadline_epoch == report.anchor_epoch + 900
    assert report.tx_completed == 30
    assert report.rx_completed == 30
    assert report.skipped == 0
    assert tx.calls == list(range(0, 60, 2))
    assert rig.snapshots == 1
    assert rig.restores == 1
    assert rig.telemetry_calls == 60  # pre/post for each invocation
    assert clock.time() == report.deadline_epoch


def test_late_slots_are_persistently_skipped_and_never_replayed(tmp_path):
    anchor = 1_800_000_000.0
    clock = FakeClock(anchor + 61)
    rig = FakeRig()
    tx = FakeTx(rig)
    ctl = ScoutController(
        ScoutConfig(anchor_epoch=anchor, max_late_s=0.5), clock, rig, tx,
        SlotStateStore(tmp_path / "state.json"), (),
    )

    report = ctl.run()
    assert report.skipped == 5
    assert tx.calls == list(range(6, 60, 2))

    with pytest.raises(ScoutFault, match="already started"):
        ScoutController(
            ScoutConfig(anchor_epoch=anchor, max_late_s=0.5), FakeClock(anchor),
            FakeRig(), FakeTx(FakeRig()), SlotStateStore(tmp_path / "state.json"), (),
        ).run()


def test_transmitter_fault_aborts_remaining_slots_and_always_restores(tmp_path):
    clock = FakeClock(1_800_000_000.0)
    rig = FakeRig()
    tx = FakeTx(rig, fail_at=4)

    with pytest.raises(ScoutFault, match="slot 4"):
        controller(tmp_path, clock, rig, tx).run()

    assert tx.calls == [0, 2, 4]
    assert rig.snapshots == 1
    assert rig.restores == 1


def test_bad_post_tx_telemetry_aborts_before_next_tx(tmp_path):
    clock = FakeClock(1_800_000_000.0)
    rig = FakeRig(bad_after_tx=1)
    tx = FakeTx(rig)

    with pytest.raises(ScoutFault, match="telemetry"):
        controller(tmp_path, clock, rig, tx).run()

    assert tx.calls == [0]
    assert rig.restores == 1


def test_rx_callbacks_are_delegated_to_external_processing_owner(tmp_path):
    clock = FakeClock(1_800_000_000.0)
    rig = FakeRig()
    tx = FakeTx(rig)
    submitted=[]
    def callback(event): pass
    def submit(name,fn,event): submitted.append((name,fn,event))
    report=controller(tmp_path,clock,rig,tx,(callback,callback),submit).run()
    assert report.callback_submissions==60
    assert len(submitted)==60
    assert all(item[1] is callback and item[2].direction=='RX' for item in submitted)


def test_hard_deadline_prevents_tx_that_cannot_finish(tmp_path):
    anchor = 1_800_000_000.0
    clock = FakeClock(anchor + 899.8)
    rig = FakeRig()
    tx = FakeTx(rig)
    report = ScoutController(
        ScoutConfig(anchor_epoch=anchor, max_late_s=1.0), clock, rig, tx,
        SlotStateStore(tmp_path / "state.json"), (),
    ).run()

    assert tx.calls == []
    assert clock.time() == anchor + 900


def test_dry_run_constructs_no_rig_and_has_no_effect_adapters():
    constructed = 0

    def forbidden_rig_factory():
        nonlocal constructed
        constructed += 1
        raise AssertionError("Rig constructed")

    plan = build_dry_run(now_epoch=1_800_000_007.0, rig_factory=forbidden_rig_factory)

    assert constructed == 0
    assert plan["dry_run"] is True
    assert plan["rf_authorized"] is False
    assert len(plan["slots"]) == 60
    assert sum(slot["direction"] == "TX" for slot in plan["slots"]) == 30
    assert all(slot["action"] == "would-run" for slot in plan["slots"])


def test_dry_run_cli_emits_plan_without_creating_files(tmp_path: Path):
    environment = os.environ.copy()
    environment["PYTHONPATH"] = str(Path(__file__).parents[1] / "src")
    before = set(tmp_path.iterdir())
    result = subprocess.run(
        [sys.executable, "-m", "ft8_scout_cli", "--dry-run", "--now", "1800000007"],
        check=True,
        capture_output=True,
        text=True,
        env=environment,
        cwd=tmp_path,
    )
    payload = json.loads(result.stdout)
    assert payload["dry_run"] is True
    assert payload["rf_authorized"] is False
    assert set(tmp_path.iterdir()) == before
