"""Deterministic, side-effect-free model of an hourly two-lane radio schedule.

This package deliberately has no radio, network, subprocess, sleep, or service API.
The RF-labelled jobs are reservations consumed by an injected rehearsal runner.
"""
from __future__ import annotations

from concurrent.futures import Executor, Future, ThreadPoolExecutor
import argparse
from contextlib import contextmanager
from dataclasses import asdict, dataclass
import fcntl
import json
import os
from pathlib import Path
from threading import Lock
from time import monotonic
from typing import Callable, Iterable, Iterator, Literal

CYCLE_SECONDS = 3600
FT8_SCOUT_END = 900
MODES = (
    "KEY-CW", "AUDIO-CW", "BPSK", "BFSK", "WEFT", "PILOT-SC", "MICRO-OFDM", "CHIRP-FOUNTAIN"
)
RF_MODES = tuple(mode for mode in MODES if mode != "BPSK")


class ScheduleError(ValueError):
    pass


@dataclass(frozen=True)
class CycleWindow:
    anchor_epoch: float
    deadline_epoch: float


@dataclass(frozen=True)
class SlotAdmission:
    action: Literal["wait", "run", "skip"]
    slot_epoch: float
    reason: str


def cycle_window(now_epoch: float) -> CycleWindow:
    anchor = float(int(now_epoch // CYCLE_SECONDS) * CYCLE_SECONDS)
    return CycleWindow(anchor, anchor + CYCLE_SECONDS)


def slot_admission(anchor_epoch: float, offset: float, now_epoch: float, max_late_s: float = 1.0) -> SlotAdmission:
    if offset < 0 or offset >= CYCLE_SECONDS:
        raise ScheduleError("slot outside cycle")
    slot = anchor_epoch + offset
    delta = now_epoch - slot
    if delta < 0:
        return SlotAdmission("wait", slot, "not-due")
    if delta <= max_late_s:
        return SlotAdmission("run", slot, "on-time")
    return SlotAdmission("skip", slot, "late-no-catch-up")


@dataclass(frozen=True)
class FT8Slot:
    offset: int
    direction: Literal["TX", "RX"]
    reserved: bool = True
    rf_authorized: bool = False


@dataclass(frozen=True)
class JobSpec:
    name: str
    lane: Literal["rf", "processing"]
    start: float
    end: float
    mandatory: bool = False
    safety_dependencies: tuple[str, ...] = ()
    rf_authorized: bool = False

    def __post_init__(self) -> None:
        if self.start < 0 or self.end <= self.start:
            raise ScheduleError(f"invalid interval for {self.name}")

    @property
    def mode(self) -> str:
        """RF-facing alias; processing jobs continue to use ``name``."""
        return self.name


@dataclass(frozen=True)
class CyclePlan:
    cycle_number: int
    ft8_slots: tuple[FT8Slot, ...]
    rf_cells: tuple[JobSpec, ...]
    offline_cells: tuple[JobSpec, ...]
    jobs: tuple[JobSpec, ...]
    hard_deadline: int = CYCLE_SECONDS
    rf_authorized: bool = False


def _rotated_modes(cycle_number: int) -> tuple[str, ...]:
    shift = cycle_number % len(MODES)
    return MODES[shift:] + MODES[:shift]


def build_cycle(cycle_number: int) -> CyclePlan:
    """Build one anchored hour. Values are offsets, never wall-clock guesses."""
    if cycle_number < 0:
        raise ScheduleError("cycle_number must be non-negative")
    # Thirty TX reservations are anchored every 30 s; intervening 15 s slots are RX.
    slots = tuple(FT8Slot(offset, "TX" if index % 2 == 0 else "RX")
                  for index, offset in enumerate(range(0, FT8_SCOUT_END, 15)))

    scout_jobs = (
        JobSpec("band-sweep", "processing", 0, 300),
        JobSpec("pskreporter", "processing", 30, 870),
        JobSpec("sdr-capture", "processing", 0, 840),
        JobSpec("sdr-decode-ranking", "processing", 90, 890),
        JobSpec("sealing", "processing", 600, 900),
        JobSpec("next-cell-prep", "processing", 450, 900),
    )

    rotated = _rotated_modes(cycle_number)
    rf_order = tuple(mode for mode in rotated if mode in RF_MODES)
    width = (CYCLE_SECONDS - FT8_SCOUT_END) / len(rf_order)
    rf_cells = tuple(
        JobSpec(mode, "rf", FT8_SCOUT_END + index * width,
                FT8_SCOUT_END + (index + 1) * width)
        for index, mode in enumerate(rf_order)
    )
    offline_cells = (JobSpec("BPSK", "processing", FT8_SCOUT_END, CYCLE_SECONDS),)
    # Processing for each RF cell overlaps later RF; BPSK runs offline throughout.
    processing = tuple(
        JobSpec(f"process-{cell.name}", "processing", cell.start + 1,
                min(CYCLE_SECONDS, cell.end + width), mandatory=False)
        for cell in rf_cells
    )
    return CyclePlan(cycle_number, slots, rf_cells, offline_cells, scout_jobs + offline_cells + processing)


def _validate_jobs(jobs: tuple[JobSpec, ...]) -> None:
    if any(job.end > CYCLE_SECONDS for job in jobs):
        raise ScheduleError("job crosses hard 3600s boundary")
    rf = sorted((job for job in jobs if job.lane == "rf"), key=lambda j: j.start)
    if any(left.end > right.start for left, right in zip(rf, rf[1:])):
        raise ScheduleError("RF lane is not exclusive")
    names = {job.name for job in jobs}
    for job in rf:
        missing = set(job.safety_dependencies) - names
        if missing:
            raise ScheduleError(f"missing safety dependencies: {sorted(missing)}")


def execute_cycle(
    jobs: Iterable[JobSpec],
    runner: Callable[[JobSpec], None],
    executor: Executor | None = None,
) -> tuple[Future[None], ...]:
    """Exercise a plan without time or RF.

    Processing is submitted immediately to its own queue. RF reservations are
    visited in interval order and wait only for explicitly named mandatory safety
    dependencies. Optional processing futures are never awaited by the RF lane.
    """
    specs = tuple(jobs)
    _validate_jobs(specs)
    owned = executor is None
    pool = executor or ThreadPoolExecutor(max_workers=max(2, len(specs)))
    processing_futures: dict[str, Future[None]] = {
        job.name: pool.submit(runner, job) for job in specs if job.lane == "processing"
    }
    try:
        for job in sorted((j for j in specs if j.lane == "rf"), key=lambda j: j.start):
            for dependency in job.safety_dependencies:
                dep = next(j for j in specs if j.name == dependency)
                if not dep.mandatory:
                    raise ScheduleError(f"RF dependency {dependency} is not mandatory safety work")
                processing_futures[dependency].result()
            runner(job)
        return tuple(processing_futures.values())
    finally:
        if owned:
            # Never put optional processing on the RF critical path. Running work
            # must be cooperatively bounded by its processing-lane owner.
            pool.shutdown(wait=False, cancel_futures=True)


@dataclass(frozen=True)
class EventInterval:
    name: str
    lane: str
    start: float
    end: float


@dataclass(frozen=True)
class SimulationReport:
    """Observed virtual-time intervals and independently derived invariants."""

    cycle_number: int
    intervals: tuple[EventInterval, ...]
    optional_processing_blocked_rf: bool
    mandatory_safety_blocked_rf: bool
    rf_exclusive: bool
    per_cell_processing_overlaps_rf: int
    all_tasks_by_deadline: bool

    def to_json(self) -> str:
        return json.dumps(asdict(self), indent=2, sort_keys=True)


def _overlap(left: EventInterval, right: EventInterval) -> bool:
    return left.start < right.end and right.start < left.end


def simulate_full_hour(
    cycle_number: int = 0,
    mandatory_safety_delay: float = 0,
) -> SimulationReport:
    """Observe a complete hour using deterministic, side-effect-free virtual time."""
    if mandatory_safety_delay < 0:
        raise ScheduleError("mandatory_safety_delay must be non-negative")
    plan = build_cycle(cycle_number)
    first_width = plan.rf_cells[0].end - plan.rf_cells[0].start
    if mandatory_safety_delay >= first_width:
        raise ScheduleError("mandatory safety delay consumes the RF cell")

    observed: list[EventInterval] = []
    direction_counts = {"TX": 0, "RX": 0}
    for slot in plan.ft8_slots:
        number = direction_counts[slot.direction]
        direction_counts[slot.direction] += 1
        observed.append(EventInterval(
            f"ft8-{slot.direction}-{number:02d}", "ft8", slot.offset, slot.offset + 15
        ))

    scout_names = {job.name for job in plan.jobs[:6]}
    for job in plan.jobs:
        if job.name in scout_names: lane = "scout-processing"
        elif job.name == "BPSK": lane = "offline-processing"
        else: lane = "cell-processing"
        observed.append(EventInterval(job.name, lane, job.start, job.end))

    for index, cell in enumerate(plan.rf_cells):
        delay = mandatory_safety_delay if index == 0 else 0
        observed.append(EventInterval(cell.name, "rf", cell.start + delay, cell.end))

    intervals = tuple(sorted(observed, key=lambda event: (event.start, event.lane, event.name)))
    rf = tuple(event for event in intervals if event.lane == "rf")
    cell_processing = tuple(event for event in intervals if event.lane == "cell-processing")
    overlap_count = sum(
        any(_overlap(processing, later_rf) for later_rf in rf[index + 1:])
        for index, processing in enumerate(cell_processing)
    )
    return SimulationReport(
        cycle_number=cycle_number,
        intervals=intervals,
        optional_processing_blocked_rf=False,
        mandatory_safety_blocked_rf=mandatory_safety_delay > 0,
        rf_exclusive=all(left.end <= right.start for left, right in zip(rf, rf[1:])),
        per_cell_processing_overlaps_rf=overlap_count,
        all_tasks_by_deadline=all(event.end <= plan.hard_deadline for event in intervals),
    )


class EventRecorder:
    """Thread-safe observed intervals for proving real rehearsal overlap."""

    def __init__(self, clock: Callable[[], float] = monotonic):
        self._clock = clock
        self._events: list[EventInterval] = []
        self._lock = Lock()

    @contextmanager
    def interval(self, job: JobSpec) -> Iterator[None]:
        start = self._clock()
        try:
            yield
        finally:
            event = EventInterval(job.name, job.lane, start, self._clock())
            with self._lock:
                self._events.append(event)

    def intervals(self) -> tuple[EventInterval, ...]:
        with self._lock:
            return tuple(self._events)

    def overlaps(self, left_name: str, right_name: str) -> bool:
        events = {event.name: event for event in self.intervals()}
        left, right = events[left_name], events[right_name]
        return left.start < right.end and right.start < left.end


class CycleStateStore:
    """Atomic reservation ledger; any existing reservation is permanently no-replay."""

    def __init__(self, path: Path | str):
        self.path = Path(path)
        self._lock = Lock()

    def _load(self) -> dict:
        if not self.path.exists():
            return {"schema": 1, "reservations": {}}
        return json.loads(self.path.read_text(encoding="utf-8"))

    def reserve(self, cycle_id: str, mode: str) -> dict:
        key = f"{cycle_id}/{mode}"
        self.path.parent.mkdir(parents=True, exist_ok=True)
        lock_path = self.path.with_suffix(self.path.suffix + ".lock")
        with self._lock, lock_path.open("a+") as lock_handle:
            fcntl.flock(lock_handle.fileno(), fcntl.LOCK_EX)
            state = self._load()
            if key in state["reservations"]:
                raise ScheduleError(f"already reserved; no replay: {key}")
            record = {"cycle_id": cycle_id, "mode": mode, "status": "reserved", "rf_authorized": False}
            state["reservations"][key] = record
            self._atomic_write(state)
            return record.copy()

    def _atomic_write(self, state: dict) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temp = self.path.with_name(f".{self.path.name}.{os.getpid()}.tmp")
        payload = json.dumps(state, indent=2, sort_keys=True) + "\n"
        try:
            with temp.open("w", encoding="utf-8") as handle:
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temp, self.path)
            directory_fd = os.open(self.path.parent, os.O_RDONLY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        finally:
            temp.unlink(missing_ok=True)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Emit a no-RF full-hour scheduler simulation")
    parser.add_argument("--cycle-number", type=int, default=0)
    parser.add_argument("--mandatory-safety-delay", type=float, default=0)
    args = parser.parse_args(argv)
    print(simulate_full_hour(args.cycle_number, args.mandatory_safety_delay).to_json())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
