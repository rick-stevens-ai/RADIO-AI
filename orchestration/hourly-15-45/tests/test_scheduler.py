from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import multiprocessing
from pathlib import Path
import json
import os
import subprocess
import sys
import threading

import pytest

from hourly_scheduler import (
    CYCLE_SECONDS,
    FT8_SCOUT_END,
    MODES,
    CycleStateStore,
    EventRecorder,
    JobSpec,
    ScheduleError,
    build_cycle,
    cycle_window,
    execute_cycle,
    simulate_full_hour,
    slot_admission,
)


def test_scout_has_thirty_tx_and_thirty_rx_slots_with_concurrent_jobs():
    plan = build_cycle(cycle_number=0)

    assert [slot.offset for slot in plan.ft8_slots] == list(range(0, 900, 15))
    assert len(plan.ft8_slots) == 60
    assert sum(slot.direction == "TX" for slot in plan.ft8_slots) == 30
    assert sum(slot.direction == "RX" for slot in plan.ft8_slots) == 30
    assert [slot.direction for slot in plan.ft8_slots] == ["TX" if i % 2 == 0 else "RX" for i in range(60)]
    assert all(slot.reserved and slot.rf_authorized is False for slot in plan.ft8_slots)
    assert {job.name for job in plan.jobs if job.start < FT8_SCOUT_END} == {
        "band-sweep", "pskreporter", "sdr-capture", "sdr-decode-ranking", "sealing", "next-cell-prep"
    }
    # Event intervals, rather than declaration order, prove actual planned overlap.
    scout_jobs = [j for j in plan.jobs if j.start < FT8_SCOUT_END]
    assert any(a.start < b.end and b.start < a.end for i, a in enumerate(scout_jobs) for b in scout_jobs[i + 1 :])


def test_eight_mode_lane_is_exclusive_rotated_and_ends_at_hard_boundary():
    first = build_cycle(cycle_number=0)
    second = build_cycle(cycle_number=1)

    expected_first = [mode for mode in MODES if mode != "BPSK"]
    expected_second = [mode for mode in MODES[1:] + MODES[:1] if mode != "BPSK"]
    assert [cell.mode for cell in first.rf_cells] == expected_first
    assert [cell.mode for cell in second.rf_cells] == expected_second
    assert [cell.mode for cell in first.offline_cells] == ["BPSK"]
    assert first.rf_cells[0].start == 900
    assert first.rf_cells[-1].end == CYCLE_SECONDS
    assert all(a.end <= b.start for a, b in zip(first.rf_cells, first.rf_cells[1:]))
    assert all(cell.rf_authorized is False for cell in first.rf_cells)


def test_optional_processing_does_not_serially_delay_rf_lane():
    optional_release = threading.Event()
    second_rf_started = threading.Event()

    def runner(spec: JobSpec) -> None:
        if spec.name == "optional-long":
            optional_release.wait(timeout=2)
        elif spec.name == "rf-1":
            second_rf_started.set()

    jobs = (
        JobSpec("rf-0", "rf", 900, 1000),
        JobSpec("optional-long", "processing", 900, 2000, mandatory=False),
        JobSpec("rf-1", "rf", 1000, 1100),
    )
    with ThreadPoolExecutor(max_workers=3) as pool:
        execute_cycle(jobs, runner=runner, executor=pool)
        assert second_rf_started.wait(timeout=1), "optional processing serialized the RF lane"
        optional_release.set()


def test_mandatory_safety_dependency_may_block_rf():
    jobs = (
        JobSpec("safety", "processing", 890, 950, mandatory=True),
        JobSpec("rf", "rf", 900, 1000, safety_dependencies=("safety",)),
    )
    release = threading.Event()
    rf_started = threading.Event()

    def runner(spec: JobSpec) -> None:
        if spec.name == "safety":
            release.wait(timeout=2)
        else:
            rf_started.set()

    with ThreadPoolExecutor(max_workers=2) as pool:
        future = pool.submit(execute_cycle, jobs, runner, pool)
        assert not rf_started.wait(timeout=0.1)
        release.set()
        future.result(timeout=1)
        assert rf_started.is_set()


def test_event_recorder_proves_runtime_overlap_from_intervals():
    recorder = EventRecorder()
    both_started = threading.Barrier(2)
    release = threading.Event()

    def run(spec: JobSpec) -> None:
        with recorder.interval(spec):
            both_started.wait(timeout=1)
            release.wait(timeout=1)

    jobs = (
        JobSpec("capture", "processing", 0, 10),
        JobSpec("decode", "processing", 1, 9),
    )
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = execute_cycle(jobs, runner=run, executor=pool)
        release.set()
        for future in futures:
            future.result(timeout=1)

    intervals = recorder.intervals()
    assert {interval.name for interval in intervals} == {"capture", "decode"}
    assert recorder.overlaps("capture", "decode")
    assert all(interval.end >= interval.start for interval in intervals)


def test_atomic_reservation_is_no_replay_even_after_interruption(tmp_path: Path):
    state = CycleStateStore(tmp_path / "state.json")
    reservation = state.reserve("2026-10-04T21", "KEY-CW")
    assert reservation["status"] == "reserved"

    reloaded = CycleStateStore(tmp_path / "state.json")
    with pytest.raises(ScheduleError, match="already reserved"):
        reloaded.reserve("2026-10-04T21", "KEY-CW")
    assert not list(tmp_path.glob("*.tmp"))


def _reserve_worker(path,queue):
    try:CycleStateStore(path).reserve('hour','mode');queue.put('owned')
    except ScheduleError:queue.put('rejected')

def test_cross_process_reservation_has_exactly_one_owner(tmp_path):
    ctx=multiprocessing.get_context('fork');q=ctx.Queue();path=tmp_path/'state.json'
    ps=[ctx.Process(target=_reserve_worker,args=(path,q)) for _ in range(2)]
    for p in ps:p.start()
    for p in ps:p.join(2)
    assert sorted([q.get(timeout=1),q.get(timeout=1)])==['owned','rejected']

def test_jobs_cannot_cross_hard_cycle_boundary():
    with pytest.raises(ScheduleError, match="hard 3600s boundary"):
        execute_cycle((JobSpec("late", "processing", 3599, 3601),), runner=lambda _: None)


def test_absolute_slot_admission_skips_late_and_never_catches_up():
    anchor = 1_000_000.0
    assert slot_admission(anchor, 30, anchor + 29.9, max_late_s=1.0).action == "wait"
    assert slot_admission(anchor, 30, anchor + 30.5, max_late_s=1.0).action == "run"
    late = slot_admission(anchor, 30, anchor + 31.1, max_late_s=1.0)
    assert late.action == "skip"
    assert late.reason == "late-no-catch-up"


def test_cycle_anchor_and_deadline_are_immutable():
    window = cycle_window(1_000_001.0)
    assert window.anchor_epoch == 997_200.0
    assert window.deadline_epoch == 1_000_800.0
    with pytest.raises(ScheduleError, match="outside cycle"):
        slot_admission(window.anchor_epoch, 3600, window.anchor_epoch + 3600)


def test_full_hour_runner_emits_every_observed_interval_and_overlap_proof():
    report = simulate_full_hour(cycle_number=0)

    assert len(report.intervals) == 81
    assert sum(event.lane == "ft8" for event in report.intervals) == 60
    assert sum(event.lane == "scout-processing" for event in report.intervals) == 6
    assert sum(event.lane == "rf" for event in report.intervals) == 7
    assert sum(event.lane == "cell-processing" for event in report.intervals) == 7
    assert sum(event.name == "BPSK" and event.lane == "offline-processing" for event in report.intervals) == 1
    assert report.optional_processing_blocked_rf is False
    assert report.mandatory_safety_blocked_rf is False
    assert report.rf_exclusive is True
    assert report.per_cell_processing_overlaps_rf == 6
    assert max(event.end for event in report.intervals) <= CYCLE_SECONDS


def test_full_hour_runner_allows_only_mandatory_safety_to_delay_rf():
    report = simulate_full_hour(cycle_number=0, mandatory_safety_delay=30)

    assert report.mandatory_safety_blocked_rf is True
    assert report.optional_processing_blocked_rf is False
    assert report.rf_exclusive is True
    assert max(event.end for event in report.intervals) <= CYCLE_SECONDS


def test_full_hour_report_is_json_serializable():
    payload = simulate_full_hour(cycle_number=2).to_json()
    assert '"ft8-TX-00"' in payload
    assert '"all_tasks_by_deadline": true' in payload


def test_module_cli_emits_full_hour_json():
    environment = os.environ.copy()
    environment["PYTHONPATH"] = str(Path(__file__).parents[1] / "src")
    result = subprocess.run(
        [sys.executable, "-m", "hourly_scheduler", "--mandatory-safety-delay", "30"],
        check=True,
        capture_output=True,
        env=environment,
        text=True,
    )
    payload = json.loads(result.stdout)
    assert len(payload["intervals"]) == 81
    assert payload["mandatory_safety_blocked_rf"] is True
    assert payload["optional_processing_blocked_rf"] is False
