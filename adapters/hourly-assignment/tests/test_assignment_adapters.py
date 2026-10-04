import copy
import hashlib
import json
import pathlib
import sys
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from assignment_adapters import (
    ALL_MODES,
    AssignmentError,
    build_command_plans,
    canonical_payload_bytes,
    seal_assignment,
    validate_assignment,
)


def assignment_payload():
    responsibilities = {
        mode: {
            "owner": "offline-harness" if mode == "bpsk" else "mode-executor",
            "checks": ["assignment-fresh", "band-legal", "sdr-quorum"],
        }
        for mode in ALL_MODES
    }
    return {
        "schema": "hourly-15-45-assignment-v1",
        "hour_id": "2026-10-04T22Z",
        "run_id": 42,
        "selected_band": "20m",
        "expires_epoch": 1791154800,
        "assigned_sdrs": [
            {"endpoint": "kiwi.example.net:8073", "receiver_id": "sdr-a", "site": "EN61"},
            {"endpoint": "kiwi2.example.net:8074", "receiver_id": "sdr-b", "site": "FN31"},
        ],
        "source_admission_sha256": "a" * 64,
        "modes": responsibilities,
    }


class AssignmentSchemaTests(unittest.TestCase):
    def test_valid_sealed_assignment_round_trips(self):
        assignment = seal_assignment(assignment_payload())
        validated = validate_assignment(assignment, now_epoch=1791151200)
        self.assertEqual(validated, assignment)
        expected = hashlib.sha256(canonical_payload_bytes(assignment_payload())).hexdigest()
        self.assertEqual(assignment["seal"], {"algorithm": "sha256", "digest": expected})

    def test_mutation_breaks_seal(self):
        assignment = seal_assignment(assignment_payload())
        assignment["selected_band"] = "40m"
        with self.assertRaisesRegex(AssignmentError, "seal"):
            validate_assignment(assignment, now_epoch=1791151200)

    def test_expired_assignment_is_rejected(self):
        assignment = seal_assignment(assignment_payload())
        with self.assertRaisesRegex(AssignmentError, "expired"):
            validate_assignment(assignment, now_epoch=1791154800)

    def test_unknown_fields_and_ft8_dial_or_lane_bindings_are_rejected(self):
        for forbidden in ("ft8_dial_hz", "dial_hz", "lane_hz"):
            payload = assignment_payload()
            payload[forbidden] = 14074000
            with self.subTest(forbidden=forbidden), self.assertRaises(AssignmentError):
                seal_assignment(payload)

    def test_all_eight_mode_responsibilities_are_required(self):
        payload = assignment_payload()
        del payload["modes"]["weft"]
        with self.assertRaisesRegex(AssignmentError, "modes"):
            seal_assignment(payload)

    def test_sdr_sites_and_endpoints_must_be_unique(self):
        payload = assignment_payload()
        payload["assigned_sdrs"][1]["site"] = "EN61"
        with self.assertRaisesRegex(AssignmentError, "site"):
            seal_assignment(payload)


class AdapterTests(unittest.TestCase):
    def test_all_eight_modes_consume_the_same_band_and_sdr_assignment(self):
        assignment = seal_assignment(assignment_payload())
        plans = build_command_plans(
            assignment,
            now_epoch=1791151200,
            output_root="/safe/plans",
        )
        consumed = {mode for plan in plans for mode in plan["consumes_modes"]}
        self.assertEqual(consumed, set(ALL_MODES))
        expected_sdrs = assignment["assigned_sdrs"]
        for plan in plans:
            binding = plan["assignment_binding"]
            self.assertEqual(binding["selected_band"], "20m")
            self.assertEqual(binding["assigned_sdrs"], expected_sdrs)
            self.assertEqual(binding["assignment_seal_sha256"], assignment["seal"]["digest"])

    def test_bpsk_remains_offline_only_and_has_no_rf_executor(self):
        plans = build_command_plans(
            seal_assignment(assignment_payload()),
            now_epoch=1791151200,
            output_root="/safe/plans",
        )
        bpsk = next(plan for plan in plans if plan["consumes_modes"] == ["bpsk"])
        self.assertEqual(bpsk["execution_status"], "offline-only-until-qualified")
        self.assertEqual(bpsk["executor_kind"], "offline-test")
        self.assertFalse(bpsk["rf_capable"])
        self.assertNotIn("run_application_cell.py", " ".join(bpsk["argv"]))
        self.assertNotIn("run_live_triarm_campaign.py", " ".join(bpsk["argv"]))

    def test_current_executor_limitations_are_fail_closed_not_hidden(self):
        plans = build_command_plans(
            seal_assignment(assignment_payload()),
            now_epoch=1791151200,
            output_root="/safe/plans",
        )
        rf_plans = [plan for plan in plans if plan["rf_capable"]]
        self.assertTrue(rf_plans)
        for plan in rf_plans:
            self.assertEqual(plan["dispatch_ready"], False)
            self.assertIn("does not accept sealed band/SDR assignment", plan["block_reason"])

    def test_generation_is_pure_and_deterministic(self):
        assignment = seal_assignment(assignment_payload())
        before = copy.deepcopy(assignment)
        first = build_command_plans(assignment, now_epoch=1791151200, output_root="/safe/plans")
        second = build_command_plans(assignment, now_epoch=1791151200, output_root="/safe/plans")
        self.assertEqual(first, second)
        self.assertEqual(assignment, before)
        self.assertFalse(pathlib.Path("/safe/plans").exists())


if __name__ == "__main__":
    unittest.main()
