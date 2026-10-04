"""Reusable FT8 transmitter session core for thirty alternating slots.

This is an isolated, dependency-injected reference implementation.  It does not
import station CAT, audio, or TX modules and cannot operate RF by itself.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Protocol

SESSION_SECONDS = 900.0
SLOT_PERIOD_SECONDS = 30.0
MAX_SLOTS = 30


class SessionError(RuntimeError):
    pass


class DeadlineExceeded(SessionError):
    pass


@dataclass(frozen=True)
class SessionConfig:
    run_id: str
    anchor_epoch: float
    duration_s: float = SESSION_SECONDS

    def __post_init__(self) -> None:
        if self.duration_s != SESSION_SECONDS:
            raise ValueError("FT8 session deadline is fixed at 900 seconds")

    @property
    def deadline_epoch(self) -> float:
        return self.anchor_epoch + SESSION_SECONDS


@dataclass(frozen=True)
class SlotConfig:
    band: str
    dial_hz: int
    audio_offset_hz: int
    rfpower_percent: float
    message: str


class RigPort(Protocol):
    def snapshot(self) -> dict: ...
    def set_freq(self, value: int) -> None: ...
    def set_mode(self, mode: str, passband: int) -> None: ...
    def set_rfpower(self, value: float) -> None: ...
    def set_ptt(self, value: bool) -> None: ...
    def sample_telemetry(self) -> tuple[float | None, float | None, float | None]: ...
    def restore(self, state: dict) -> None: ...
    def readback(self) -> dict: ...


class TxPort(Protocol):
    def guards(self, dial_hz: int, max_power_w: float) -> None: ...
    def unkey(self, rig: RigPort) -> None: ...
    def disable(self) -> None: ...
    def enabled(self) -> bool: ...


class FT8Session:
    """One CAT lifecycle spanning up to thirty 30-second TX reservations."""

    def __init__(
        self,
        config: SessionConfig,
        *,
        rig: RigPort,
        tx: TxPort,
        encode: Callable[[str, int], str],
        play: Callable[[str], object],
        receipt: Callable[[dict], None],
        wall_clock: Callable[[], float],
        monotonic_clock: Callable[[], float],
        sleep: Callable[[float], None],
    ) -> None:
        self.config = config
        self.rig = rig
        self.tx = tx
        self.encode = encode
        self.play = play
        self.receipt = receipt
        self.wall_clock = wall_clock
        self.monotonic_clock = monotonic_clock
        self.sleep = sleep
        self._original: dict | None = None
        self._closed = False
        self._completed: set[int] = set()

    @property
    def deadline_epoch(self) -> float:
        return self.config.deadline_epoch

    def open(self) -> "FT8Session":
        if self._closed:
            raise SessionError("session already closed")
        if self._original is None:
            if self.wall_clock() >= self.deadline_epoch:
                raise DeadlineExceeded("immutable 900s deadline reached")
            self._original = self.rig.snapshot()
        return self

    def __enter__(self) -> "FT8Session":
        return self.open()

    def __exit__(self, exc_type, exc, traceback) -> None:
        self.close()

    def _check_deadline(self) -> None:
        if self.wall_clock() >= self.deadline_epoch:
            raise DeadlineExceeded("immutable 900s deadline reached")

    def run_slot(self, slot_index: int, slot: SlotConfig) -> dict:
        if self._original is None or self._closed:
            raise SessionError("session is not open")
        if not 0 <= slot_index < MAX_SLOTS:
            raise ValueError("slot index must be in 0..29")
        if slot_index in self._completed:
            raise SessionError("slot already completed; replay refused")
        target = self.config.anchor_epoch + slot_index * SLOT_PERIOD_SECONDS
        if self.wall_clock() > target + 1.0:
            raise DeadlineExceeded("slot is late; catch-up refused")
        self.sleep(max(0.0, target - self.wall_clock()))
        self._check_deadline()

        doc = {
            "schema": "ft8-session-slot-v1",
            "run_id": self.config.run_id,
            "slot_index": slot_index,
            "slot_epoch": target,
            "deadline_epoch": self.deadline_epoch,
            "band": slot.band,
            "dial_hz": slot.dial_hz,
            "audio_offset_hz": slot.audio_offset_hz,
            "started_epoch": self.wall_clock(),
            "telemetry": [],
            "transmission_complete": False,
        }
        player = None
        try:
            self.tx.guards(slot.dial_hz, 20.0)
            wav = self.encode(slot.message, slot.audio_offset_hz)
            self.rig.set_freq(slot.dial_hz)
            self.rig.set_mode("PKTUSB", 3000)
            self.rig.set_rfpower(slot.rfpower_percent / 100.0)
            self.rig.set_ptt(True)
            player = self.play(wav)
            missing = 0
            begin = self.monotonic_clock()
            while player.poll() is None:
                self._check_deadline()
                forward, swr, alc = self.rig.sample_telemetry()
                if None in (forward, swr, alc):
                    missing += 1
                    if missing >= 3 or (not doc["telemetry"] and self.monotonic_clock() - begin > 1.0):
                        raise SessionError("telemetry unavailable")
                else:
                    missing = 0
                    sample = {"epoch": self.wall_clock(), "forward_power_w": forward, "swr": swr, "alc": alc}
                    doc["telemetry"].append(sample)
                    if forward > 20 or swr > 2 or alc > 0.9:
                        raise SessionError("telemetry safety limit")
                self.sleep(0.25)
            if player.returncode:
                raise SessionError(f"player exit {player.returncode}")
            if not any(sample["forward_power_w"] > 0 for sample in doc["telemetry"]):
                raise SessionError("no positive RF telemetry")
            doc["transmission_complete"] = True
            self._completed.add(slot_index)
            return doc
        except Exception as error:
            doc["error"] = repr(error)
            raise
        finally:
            if player is not None and player.poll() is None:
                player.terminate()
                player.wait(timeout=3)
            self.tx.unkey(self.rig)
            doc["finished_epoch"] = self.wall_clock()
            self.receipt(doc)

    def close(self) -> dict:
        if self._closed:
            raise SessionError("session already closed")
        self._closed = True
        errors: list[str] = []
        try:
            self.tx.unkey(self.rig)
        except Exception as error:
            errors.append(f"unkey: {error!r}")
        try:
            self.tx.disable()
        except Exception as error:
            errors.append(f"disable: {error!r}")
        if self._original is not None:
            try:
                self.rig.restore(self._original)
            except Exception as error:
                errors.append(f"restore: {error!r}")
        try:
            state = self.rig.readback()
        except Exception as error:
            errors.append(f"readback: {error!r}")
            state = {"ptt": None}
        restored = self._original is not None and all(
            state.get(key) == self._original.get(key) for key in ("freq_hz", "mode", "rfpower")
        )
        result = {
            "schema": "ft8-session-closeout-v1",
            "run_id": self.config.run_id,
            "deadline_epoch": self.deadline_epoch,
            "completed_slots": sorted(self._completed),
            "post_state": state,
            "restored": restored,
            "safe": state.get("ptt") is False and self.tx.enabled() is False and restored and not errors,
            "errors": errors,
            "finished_epoch": self.wall_clock(),
        }
        self.receipt(result)
        return result
