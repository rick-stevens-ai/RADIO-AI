import pathlib
import sys

import pytest

HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

from ft8_session import FT8Session, SessionConfig, SlotConfig, DeadlineExceeded


class FakeClock:
    def __init__(self, wall=1_000.0):
        self.wall = float(wall)
        self.mono = 0.0

    def time(self):
        return self.wall

    def monotonic(self):
        return self.mono

    def sleep(self, seconds):
        seconds = max(0.0, float(seconds))
        self.wall += seconds
        self.mono += seconds


class FakePlayer:
    def __init__(self, clock, polls=2, returncode=0):
        self.clock = clock
        self.polls = polls
        self.returncode = None
        self.final_returncode = returncode
        self.terminated = False

    def poll(self):
        if self.polls > 0:
            self.polls -= 1
            return None
        self.returncode = self.final_returncode
        return self.returncode

    def terminate(self):
        self.terminated = True
        self.returncode = -15

    def wait(self, timeout=None):
        return self.returncode


class FakeRig:
    def __init__(self, telemetry=None):
        self.constructed = 1
        self.freq = 7_074_000
        self.mode = ("PKTUSB", 3000)
        self.rfpower = 0.03
        self.ptt = False
        self.telemetry = list(telemetry or [(5.0, 1.2, 0.1)] * 100)
        self.snapshot_count = 0
        self.restore_count = 0
        self.set_freq_calls = []

    def snapshot(self):
        self.snapshot_count += 1
        return {"freq_hz": self.freq, "mode": self.mode, "rfpower": self.rfpower}

    def set_freq(self, value):
        self.freq = value
        self.set_freq_calls.append(value)

    def set_mode(self, mode, passband):
        self.mode = (mode, passband)

    def set_rfpower(self, value):
        self.rfpower = value

    def set_ptt(self, value):
        self.ptt = bool(value)

    def sample_telemetry(self):
        return self.telemetry.pop(0)

    def restore(self, state):
        self.restore_count += 1
        self.freq = state["freq_hz"]
        self.mode = tuple(state["mode"])
        self.rfpower = state["rfpower"]

    def readback(self):
        return {"freq_hz": self.freq, "mode": self.mode, "rfpower": self.rfpower, "ptt": self.ptt}


class FakeTx:
    def __init__(self):
        self.disable_count = 0
        self.unkey_count = 0

    def guards(self, dial_hz, max_power_w):
        return None

    def unkey(self, rig):
        self.unkey_count += 1
        rig.set_ptt(False)

    def disable(self):
        self.disable_count += 1

    def enabled(self):
        return False


def make_session(clock, rig, tx, players, receipts, anchor_epoch=None):
    def play(_wav):
        player = FakePlayer(clock, polls=3)
        players.append(player)
        return player

    return FT8Session(
        SessionConfig(run_id="run-1", anchor_epoch=clock.time() if anchor_epoch is None else anchor_epoch),
        rig=rig,
        tx=tx,
        encode=lambda message, offset: f"{message}-{offset}.wav",
        play=play,
        receipt=receipts.append,
        wall_clock=clock.time,
        monotonic_clock=clock.monotonic,
        sleep=clock.sleep,
    )


def test_two_slots_share_one_snapshot_and_restore_only_at_close():
    clock, rig, tx = FakeClock(), FakeRig(), FakeTx()
    players, receipts = [], []
    session = make_session(clock, rig, tx, players, receipts)

    session.open()
    first = session.run_slot(0, SlotConfig("20m", 14_074_000, 1200, 10, "CQ TEST"))
    second = session.run_slot(1, SlotConfig("40m", 7_074_000, 1500, 10, "CQ TEST"))

    assert rig.snapshot_count == 1
    assert rig.restore_count == 0
    assert first["slot_index"] == 0 and second["slot_index"] == 1
    assert len(first["telemetry"]) > 0 and len(second["telemetry"]) > 0
    closeout = session.close()
    assert rig.restore_count == 1
    assert closeout["safe"] is True and closeout["restored"] is True


def test_failed_slot_emits_telemetry_receipt_and_final_closeout():
    clock = FakeClock()
    rig = FakeRig(telemetry=[(None, None, None)] * 3)
    tx, players, receipts = FakeTx(), [], []
    session = make_session(clock, rig, tx, players, receipts)

    session.open()
    with pytest.raises(Exception, match="telemetry unavailable"):
        session.run_slot(0, SlotConfig("20m", 14_074_000, 1200, 10, "CQ TEST"))
    assert receipts[0]["slot_index"] == 0
    assert receipts[0]["transmission_complete"] is False
    assert "telemetry unavailable" in receipts[0]["error"]
    assert rig.ptt is False

    closeout = session.close()
    assert receipts[-1] == closeout
    assert closeout["completed_slots"] == []
    assert closeout["safe"] is True


def test_deadline_is_anchor_plus_900_and_cannot_be_extended():
    clock, rig, tx = FakeClock(), FakeRig(), FakeTx()
    session = make_session(clock, rig, tx, [], [])
    assert session.deadline_epoch == 1900.0
    with pytest.raises(AttributeError):
        session.deadline_epoch = 9999
    with pytest.raises(ValueError, match="fixed at 900"):
        SessionConfig("run-2", 1000, duration_s=901)


def test_slot_29_is_last_and_late_slots_never_catch_up():
    clock, rig, tx = FakeClock(), FakeRig(), FakeTx()
    players, receipts = [], []
    session = make_session(clock, rig, tx, players, receipts).open()
    clock.wall = session.config.anchor_epoch + 29 * 30
    session.run_slot(29, SlotConfig("20m", 14_074_000, 1200, 10, "CQ TEST"))
    assert receipts[0]["slot_epoch"] == 1870.0
    with pytest.raises(ValueError, match="0..29"):
        session.run_slot(30, SlotConfig("20m", 14_074_000, 1200, 10, "CQ TEST"))
    session.close()

    late_clock = FakeClock(1002)
    late = make_session(late_clock, FakeRig(), FakeTx(), [], [], anchor_epoch=1000).open()
    with pytest.raises(DeadlineExceeded, match="late"):
        late.run_slot(0, SlotConfig("20m", 14_074_000, 1200, 10, "CQ TEST"))
    late.close()


def test_thirty_slot_rehearsal_has_one_cat_lifecycle_and_thirty_receipts():
    clock, rig, tx = FakeClock(), FakeRig(), FakeTx()
    players, receipts = [], []
    session = make_session(clock, rig, tx, players, receipts).open()
    for index in range(30):
        session.run_slot(index, SlotConfig("20m", 14_074_000, 1200, 10, "CQ TEST"))
    closeout = session.close()

    assert rig.snapshot_count == 1 and rig.restore_count == 1
    assert [row["slot_index"] for row in receipts[:-1]] == list(range(30))
    assert all(row["telemetry"] for row in receipts[:-1])
    assert closeout["completed_slots"] == list(range(30))
    assert closeout["deadline_epoch"] == 1900.0
