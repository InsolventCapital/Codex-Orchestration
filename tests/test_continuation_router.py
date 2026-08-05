from __future__ import annotations

import importlib.util
import io
import json
import os
from pathlib import Path
import stat
import sys
import tempfile
import unittest
from unittest import mock


REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = (
    REPO_ROOT
    / "plugins/codex-orchestration/skills/codex-orchestration/scripts/continuation_router.py"
)
SPEC = importlib.util.spec_from_file_location("continuation_router", SCRIPT)
assert SPEC and SPEC.loader
ROUTER = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = ROUTER
SPEC.loader.exec_module(ROUTER)

PACKET = "1" * 64
MANIFEST = "2" * 64
DRIFT = "3" * 64
RECEIPT = "4" * 64


def event(event_type: str, payload: dict, *, sequence: int = 1, event_id: str = "event-1") -> dict:
    phase = {
        "ADVISOR_DECISION": "PLAN",
        "REGISTERED_DRIFT": "DRIFT",
        "VERIFICATION": "VERIFY",
    }[event_type]
    return {
        "schema_version": 1,
        "event_id": event_id,
        "sequence": sequence,
        "occurred_at": f"2026-08-04T00:00:0{sequence}Z",
        "phase": phase,
        "type": event_type,
        "payload": payload,
    }


def verification(status: str = "VERIFIED", **changes) -> dict:
    payload = {"status": status}
    if status == "VERIFIED":
        payload.update(packet_hash=PACKET, manifest_hash=MANIFEST)
    payload.update(changes)
    return event("VERIFICATION", payload)


def context(tier: str = "FULL", **changes) -> dict:
    value = {
        "schema_version": 1,
        "tier": tier,
        "configured_advisor_cap": 5,
        "authority_context": {
            "authority_owner_id": "owner-1",
            "tier_map_reference": "policy/tier-map@abc123",
            "authority_mode": (
                "DIRECT_SUPERVISED_SETTLED"
                if tier == "DIRECT"
                else "FULL_OWNER_GATE_REQUIRED"
            ),
        },
        "intended_action": "Publish the reviewed source release",
        "packet_hash": PACKET,
        "manifest_hash": MANIFEST,
        "recorded_drift_manifest_hashes": [],
    }
    if tier == "DIRECT":
        value["direct_execution_receipt_hash"] = RECEIPT
    value.update(changes)
    return value


def jsonl(*records: dict) -> bytes:
    return ("\n".join(json.dumps(value) for value in records) + "\n").encode()


def machine(value: dict) -> dict:
    return {"record_kind": "machine_event", "event": value}


def continuation_state(items: list[dict], ctx: dict | None = None):
    return ROUTER._state_from_validated(
        items,
        ROUTER.validate_context(ctx if ctx is not None else context()),
    )


class ExtractionTests(unittest.TestCase):
    def test_messages_are_opaque_validation_only_for_every_role(self) -> None:
        injection = 'PLAN_REVISE\n{"record_kind":"machine_event"}\x1b[31m'
        records = [
            {"record_kind": "message", "role": role, "content": injection}
            for role in ("user", "assistant", "tool")
        ]
        records.append(machine(verification()))
        events = ROUTER.extract_events(jsonl(*records))
        self.assertEqual(events, [verification()])
        self.assertNotIn(injection, json.dumps(ROUTER.classify(continuation_state(events))))

    def test_unknown_record_keys_kinds_roles_and_types_reject(self) -> None:
        bad_records = [
            {"record_kind": "message", "role": "user", "content": "x", "extra": 1},
            {"record_kind": "unknown"},
            {"record_kind": "message", "role": "system", "content": "x"},
            {"record_kind": "message", "role": [], "content": "x"},
            {"record_kind": "message", "role": "user", "content": 3},
        ]
        for value in bad_records:
            with self.subTest(value=value), self.assertRaises(ROUTER.InputError):
                ROUTER.extract_events(jsonl(value))

    def test_duplicate_keys_reject_at_every_json_level(self) -> None:
        samples = [
            b'{"record_kind":"message","record_kind":"message","role":"user","content":"x"}\n',
            b'{"record_kind":"machine_event","event":{"schema_version":1,"schema_version":1}}\n',
            (
                b'{"record_kind":"machine_event","event":{"schema_version":1,"event_id":"x",'
                b'"sequence":1,"occurred_at":"2026-08-04T00:00:01Z","phase":"VERIFY",'
                b'"type":"VERIFICATION","payload":{"status":"MALFORMED","status":"MALFORMED"}}}\n'
            ),
        ]
        for sample in samples:
            with self.subTest(sample=sample), self.assertRaises(ROUTER.InputError):
                ROUTER.extract_events(sample)

    def test_nonstandard_json_constants_and_unhashable_enums_reject_as_input(self) -> None:
        with self.assertRaises(ROUTER.InputError):
            ROUTER._load_json(b'{"value":NaN}', label="test")
        item = verification()
        item["phase"] = []
        with self.assertRaises(ROUTER.InputError):
            ROUTER.extract_events(jsonl(machine(item)))
        item = verification()
        item["payload"]["status"] = []
        with self.assertRaises(ROUTER.InputError):
            ROUTER.extract_events(jsonl(machine(item)))

    def test_event_schema_rejects_bool_int_bad_token_time_hash_pair_and_unknown_key(self) -> None:
        mutations = [
            ("schema_version", True),
            ("sequence", True),
            ("event_id", "bad id"),
            ("occurred_at", "2026-08-04T00:00:01+00:00"),
            ("phase", "PLAN"),
        ]
        for key, value in mutations:
            item = verification()
            item[key] = value
            with self.subTest(key=key), self.assertRaises(ROUTER.InputError):
                ROUTER.extract_events(jsonl(machine(item)))
        item = verification()
        item["unknown"] = 1
        with self.assertRaises(ROUTER.InputError):
            ROUTER.extract_events(jsonl(machine(item)))
        item = verification(packet_hash="A" * 64)
        with self.assertRaises(ROUTER.InputError):
            ROUTER.extract_events(jsonl(machine(item)))

    def test_payload_exactness_for_all_variants(self) -> None:
        valid = [
            event("ADVISOR_DECISION", {"decision": "PLAN_APPROVED", "round": 1}),
            event("REGISTERED_DRIFT", {"pre_attributed": True, "signature_match": True, "disjoint_from_work": True, "drift_manifest_hash": DRIFT}),
            verification(),
            verification("NOT_VERIFIED"),
            verification("UNAVAILABLE"),
            verification("MALFORMED"),
        ]
        for item in valid:
            with self.subTest(item=item):
                self.assertEqual(len(ROUTER.extract_events(jsonl(machine(item)))), 1)
        invalid_payloads = [
            event("ADVISOR_DECISION", {"decision": "PLAN_APPROVED", "round": True}),
            event("ADVISOR_DECISION", {"decision": "PLAN_APPROVED", "round": 1, "x": 1}),
            event("REGISTERED_DRIFT", {"pre_attributed": 1, "signature_match": True, "disjoint_from_work": True, "drift_manifest_hash": DRIFT}),
            verification("NOT_VERIFIED", packet_hash=PACKET),
            event("VERIFICATION", {"status": "VERIFIED", "packet_hash": PACKET}),
            event("VERIFICATION", {"status": "OTHER"}),
        ]
        for item in invalid_payloads:
            with self.subTest(item=item), self.assertRaises(ROUTER.InputError):
                ROUTER.extract_events(jsonl(machine(item)))

    def test_order_phase_uniqueness_and_terminal_rules_reject(self) -> None:
        approved = event("ADVISOR_DECISION", {"decision": "PLAN_APPROVED", "round": 1})
        verify2 = verification()
        verify2.update(sequence=2, event_id="event-2", occurred_at="2026-08-04T00:00:02Z")
        self.assertEqual(len(ROUTER.extract_events(jsonl(machine(approved), machine(verify2)))), 2)

        cases = []
        duplicate = verification()
        duplicate.update(sequence=2, event_id="event-2", occurred_at="2026-08-04T00:00:02Z")
        cases.append([verification(), duplicate])
        decreasing = verification()
        decreasing.update(sequence=1, event_id="event-2", occurred_at="2026-08-04T00:00:02Z")
        cases.append([approved, decreasing])
        revise = event("ADVISOR_DECISION", {"decision": "PLAN_REVISE", "round": 1})
        cases.append([revise, verify2])
        drift = event("REGISTERED_DRIFT", {"pre_attributed": True, "signature_match": True, "disjoint_from_work": True, "drift_manifest_hash": DRIFT})
        cases.append([drift, verify2])
        for records in cases:
            with self.subTest(records=records), self.assertRaises(ROUTER.InputError):
                ROUTER.extract_events(jsonl(*(machine(item) for item in records)))

    def test_recorded_and_unrecorded_drift_followed_by_verify_both_reject(self) -> None:
        drift = event("REGISTERED_DRIFT", {"pre_attributed": True, "signature_match": True, "disjoint_from_work": True, "drift_manifest_hash": DRIFT})
        verify2 = verification()
        verify2.update(sequence=2, event_id="event-2", occurred_at="2026-08-04T00:00:02Z")
        data = jsonl(machine(drift), machine(verify2))
        for hashes in ([], [DRIFT]):
            with self.subTest(hashes=hashes), self.assertRaises(ROUTER.InputError):
                events = ROUTER.extract_events(data)
                ROUTER.classify(
                    continuation_state(
                        events,
                        context(recorded_drift_manifest_hashes=hashes),
                    )
                )

    def test_bounds_depth_strings_lines_and_empty_input(self) -> None:
        with self.assertRaises(ROUTER.InputError):
            ROUTER.extract_events(b"")
        with self.assertRaises(ROUTER.BoundsError):
            ROUTER.extract_events(b"x" * (ROUTER.MAX_LINE_BYTES + 1))
        too_many = b"{}\n" * (ROUTER.MAX_JSONL_LINES + 1)
        with self.assertRaises(ROUTER.BoundsError):
            ROUTER.extract_events(too_many)
        nested = '"x"'
        for _ in range(ROUTER.MAX_OBJECT_DEPTH + 2):
            nested = '{"x":' + nested + "}"
        with self.assertRaises(ROUTER.InputError):
            ROUTER._load_json(nested.encode(), label="test")

        oversized_message = {
            "record_kind": "message",
            "role": "user",
            "content": "x" * (ROUTER.MAX_STRING_LENGTH + 1),
        }
        with self.assertRaises(ROUTER.InputError):
            ROUTER.extract_events(jsonl(oversized_message))

        with mock.patch.object(ROUTER, "MAX_EVENTS", 0):
            with self.assertRaises(ROUTER.BoundsError):
                ROUTER.extract_events(jsonl(machine(verification())))


class ContextTests(unittest.TestCase):
    def test_direct_and_full_exact_contexts_validate(self) -> None:
        self.assertEqual(ROUTER.validate_context(context())["tier"], "FULL")
        self.assertEqual(ROUTER.validate_context(context("DIRECT"))["tier"], "DIRECT")
        full_receipt = context(direct_execution_receipt_hash=RECEIPT)
        with self.assertRaises(ROUTER.InputError):
            ROUTER.validate_context(full_receipt)
        direct_missing = context("DIRECT")
        del direct_missing["direct_execution_receipt_hash"]
        with self.assertRaises(ROUTER.InputError):
            ROUTER.validate_context(direct_missing)

    def test_context_rejects_bool_int_unsorted_duplicate_hashes_and_controls(self) -> None:
        bad = [
            context(schema_version=True),
            context(configured_advisor_cap=True),
            context(recorded_drift_manifest_hashes=["f" * 64, "a" * 64]),
            context(recorded_drift_manifest_hashes=[DRIFT, DRIFT]),
            context(intended_action="publish\nnow"),
            context(tier=[]),
            context(mode=[]),
        ]
        authority = context()
        authority["authority_context"]["authority_owner_id"] = "owner\x1b[2J"
        bad.append(authority)
        for value in bad:
            with self.subTest(value=value), self.assertRaises(ROUTER.InputError):
                ROUTER.validate_context(value)

    def test_nested_tier_mode_mismatches_normalize_to_stop(self) -> None:
        full = context()
        full["authority_context"]["authority_mode"] = "DIRECT_SUPERVISED_SETTLED"
        direct = context("DIRECT")
        direct["authority_context"]["authority_mode"] = "FULL_OWNER_GATE_REQUIRED"
        for raw in (full, direct):
            with self.subTest(raw=raw):
                result = ROUTER.classify(continuation_state([verification()], raw))
                self.assertEqual(
                    (result["action"], result["reason_codes"]),
                    ("STOP", ["TIER_MODE_MISMATCH"]),
                )

    def test_authority_mode_is_nested_exact_and_bounded_hashes_reject(self) -> None:
        invalid_mode = context()
        invalid_mode["authority_context"]["authority_mode"] = "AUTOAUTHORIZE"
        with self.assertRaises(ROUTER.InputError):
            ROUTER.validate_context(invalid_mode)

        missing_mode = context()
        del missing_mode["authority_context"]["authority_mode"]
        with self.assertRaises(ROUTER.InputError):
            ROUTER.validate_context(missing_mode)

        top_level_mode = context()
        top_level_mode["mode"] = "FULL_OWNER_GATE_REQUIRED"
        with self.assertRaises(ROUTER.InputError):
            ROUTER.validate_context(top_level_mode)

        too_many_hashes = [f"{value:064x}" for value in range(ROUTER.MAX_RECORDED_HASHES + 1)]
        with self.assertRaises(ROUTER.BoundsError):
            ROUTER.validate_context(context(recorded_drift_manifest_hashes=too_many_hashes))

    def test_output_bound_strings_reject_hostile_unicode_in_every_field(self) -> None:
        hostile = (
            "line\nfeed",
            "tab\tstop",
            "bidi\u202eoverride",
            "zero\u200bwidth",
            "private\ue000use",
            "surrogate\ud800value",
            "unassigned\u0378value",
            "line\u2028separator",
            "paragraph\u2029separator",
        )
        for field in ("intended_action", "authority_owner_id", "tier_map_reference"):
            for value in hostile:
                raw = context()
                if field == "intended_action":
                    raw[field] = value
                else:
                    raw["authority_context"][field] = value
                with self.subTest(field=field, value=ascii(value)):
                    with self.assertRaises(ROUTER.InputError):
                        ROUTER.validate_context(raw)

    def test_printable_unicode_is_accepted_and_deterministic(self) -> None:
        raw = context()
        raw["intended_action"] = "Publier café — 東京 🚀"
        raw["authority_context"]["authority_owner_id"] = "propriétaire-東京"
        raw["authority_context"]["tier_map_reference"] = "politique/échelle-東京"
        first = ROUTER.classify(continuation_state([verification()], raw))
        second = ROUTER.classify(continuation_state([verification()], raw))
        self.assertEqual(
            json.dumps(first, sort_keys=True).encode(),
            json.dumps(second, sort_keys=True).encode(),
        )
        self.assertIn("Publier café — 東京 🚀", first["owner_request_template"])


class ImmutableStateTests(unittest.TestCase):
    def test_normalized_state_is_deeply_immutable_and_detached(self) -> None:
        decoded_events = [verification()]
        decoded_context = ROUTER.validate_context(context())
        state = ROUTER._state_from_validated(decoded_events, decoded_context)

        with self.assertRaises(AttributeError):
            state.context = {}  # type: ignore[misc]
        with self.assertRaises(TypeError):
            state.context["tier"] = "DIRECT"  # type: ignore[index]
        with self.assertRaises(TypeError):
            state.events[0]["payload"]["status"] = "MALFORMED"  # type: ignore[index]
        with self.assertRaises(AttributeError):
            state.context["recorded_drift_manifest_hashes"].append(DRIFT)
        with self.assertRaises(TypeError):
            state.events[0] = {}  # type: ignore[index]

        decoded_events[0]["payload"]["status"] = "MALFORMED"
        decoded_context["authority_context"]["authority_owner_id"] = "changed"
        decoded_context["recorded_drift_manifest_hashes"].append(DRIFT)
        self.assertEqual(state.events[0]["payload"]["status"], "VERIFIED")
        self.assertEqual(
            state.context["authority_context"]["authority_owner_id"], "owner-1"
        )
        self.assertEqual(state.context["recorded_drift_manifest_hashes"], ())

    def test_identical_bytes_make_equal_state_and_identical_result_bytes(self) -> None:
        input_bytes = jsonl(machine(verification()))
        context_bytes = json.dumps(context(), sort_keys=True).encode()
        first = ROUTER.normalize_state(input_bytes, context_bytes)
        second = ROUTER.normalize_state(input_bytes, context_bytes)
        self.assertEqual(first, second)
        self.assertEqual(
            json.dumps(ROUTER.classify(first), sort_keys=True).encode(),
            json.dumps(ROUTER.classify(second), sort_keys=True).encode(),
        )
        with self.assertRaises(TypeError):
            ROUTER.classify([verification()])
        with self.assertRaises(TypeError):
            ROUTER.ContinuationState(events=(), context={})


class TransitionTests(unittest.TestCase):
    def classify(self, items, ctx=None):
        return ROUTER.classify(continuation_state(items, ctx))

    def test_advisor_revision_within_and_at_cap(self) -> None:
        within = event("ADVISOR_DECISION", {"decision": "PLAN_REVISE", "round": 4})
        exhausted = event("ADVISOR_DECISION", {"decision": "PLAN_REVISE", "round": 5})
        self.assertEqual(self.classify([within])["action"], "PLANNER")
        self.assertEqual(self.classify([within])["reason_codes"], ["ADVISOR_REVISION_WITHIN_CAP"])
        self.assertEqual(self.classify([exhausted])["reason_codes"], ["ADVISOR_REVISION_CAP_EXHAUSTED"])

    def test_plan_approved_without_later_event_stops(self) -> None:
        approved = event("ADVISOR_DECISION", {"decision": "PLAN_APPROVED", "round": 1})
        self.assertEqual(self.classify([approved])["reason_codes"], ["NO_ACTIONABLE_CONTINUATION"])

    def test_drift_transitions_and_false_combinations(self) -> None:
        for values in ((True, True, True), (False, True, True), (True, False, True), (True, True, False)):
            drift = event("REGISTERED_DRIFT", {
                "pre_attributed": values[0], "signature_match": values[1],
                "disjoint_from_work": values[2], "drift_manifest_hash": DRIFT,
            })
            result = self.classify([drift])
            if all(values):
                self.assertEqual(result["action"], "RECORD_AND_CONTINUE")
                self.assertEqual(result["record_instruction"], f"Record drift manifest SHA-256: {DRIFT}")
            else:
                self.assertEqual(result["reason_codes"], ["UNQUALIFIED_OR_AMBIGUOUS_DRIFT"])
        qualified = event("REGISTERED_DRIFT", {
            "pre_attributed": True, "signature_match": True,
            "disjoint_from_work": True, "drift_manifest_hash": DRIFT,
        })
        recorded = context(recorded_drift_manifest_hashes=[DRIFT])
        self.assertEqual(self.classify([qualified], recorded)["reason_codes"], ["ALREADY_RECORDED_NO_ACTION"])

    def test_verification_only_is_valid_and_statuses_stop_without_retry(self) -> None:
        mapping = {
            "NOT_VERIFIED": "VERIFICATION_NOT_VERIFIED",
            "UNAVAILABLE": "VERIFIER_UNAVAILABLE",
            "MALFORMED": "VERIFICATION_MALFORMED",
        }
        for status, reason in mapping.items():
            result = self.classify([verification(status)])
            self.assertEqual(result["reason_codes"], [reason])
            self.assertIn("DO_NOT_RETRY_REUSE_OR_BYPASS", result["stop_conditions"][0])

    def test_non_verified_statuses_have_exact_non_authoritative_templates(self) -> None:
        expected = {
            "NOT_VERIFIED": (
                "WHY IT STOPPED\nVerifier status: NOT_VERIFIED.\n"
                "The supplied Verifier artifact did not establish VERIFIED. No authorization has been granted.\n\n"
                "OPTIONS\n- Close or explicitly decline the intended action.\n"
                "- Explicitly authorize a new planning lineage for structural review of the underlying evidence.\n\n"
                "VERIFIER'S VIEW\nThe Verifier is artifact-only evidence and is not gate-eligible by itself. "
                "Do not retry, reuse, or bypass this consumed Verifier result."
            ),
            "UNAVAILABLE": (
                "WHY IT STOPPED\nVerifier status: UNAVAILABLE.\n"
                "No usable Verifier result is available. No authorization has been granted.\n\n"
                "OPTIONS\n- Close or explicitly decline the intended action.\n"
                "- Explicitly authorize a new planning lineage for structural review of the missing verification evidence.\n\n"
                "VERIFIER'S VIEW\nThe Verifier is artifact-only evidence and is not gate-eligible by itself. "
                "Do not retry, reuse, or bypass a missing or unavailable Verifier result."
            ),
            "MALFORMED": (
                "WHY IT STOPPED\nVerifier status: MALFORMED.\n"
                "The supplied Verifier result was semantically marked MALFORMED. No authorization has been granted.\n\n"
                "OPTIONS\n- Close or explicitly decline the intended action.\n"
                "- Explicitly authorize a new planning lineage for structural review of the verification artifact.\n\n"
                "VERIFIER'S VIEW\nThe Verifier is artifact-only evidence and is not gate-eligible by itself. "
                "Do not retry, reuse, or bypass this malformed Verifier result."
            ),
        }
        for status, template in expected.items():
            result = self.classify([verification(status)])
            self.assertEqual(result["owner_request_template"], template)
            serialized = json.dumps(result, sort_keys=True)
            self.assertNotIn(PACKET, serialized)
            self.assertNotIn(MANIFEST, serialized)

    def test_verified_hash_mismatch_direct_close_and_full_owner_exact_template(self) -> None:
        mismatch = verification(packet_hash="9" * 64)
        self.assertEqual(self.classify([mismatch])["reason_codes"], ["VERIFICATION_HASH_MISMATCH"])
        direct = self.classify([verification()], context("DIRECT"))
        self.assertEqual(direct["action"], "CLOSE_EVIDENCE")
        self.assertEqual(direct["reason_codes"], ["DIRECT_VERIFICATION_CLOSES_EVIDENCE_ONLY"])
        self.assertIn("THIS_RESULT_NEITHER_SETTLES_NOR_AUTHORIZES", direct["stop_conditions"])
        full = self.classify([verification()])
        self.assertEqual(full["action"], "REQUEST_OWNER")
        expected = (
            f"OWNER GATE REQUEST — FULL\nPacket SHA-256: {PACKET}\nManifest SHA-256: {MANIFEST}\n"
            "Intended action: Publish the reviewed source release\nTier: FULL\n"
            "Authority context: owner-1; policy/tier-map@abc123\n"
            "Decision requested: explicitly authorize the intended action, explicitly decline it, or direct a new authorized plan.\n"
            "This request is non-authoritative. No router, model seat, or Verifier result has authorized the action."
        )
        self.assertEqual(full["owner_request_template"], expected)

    def test_repeated_serialization_is_byte_identical_and_has_exact_keys(self) -> None:
        result = self.classify([verification()])
        first = json.dumps(result, sort_keys=True).encode()
        second = json.dumps(self.classify([verification()]), sort_keys=True).encode()
        self.assertEqual(first, second)
        self.assertEqual(set(result), {"schema_version", "action", "reason_codes", "required_inputs", "stop_conditions", "owner_request_template", "record_instruction"})


class FileAndCliTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.input = self.root / "events.jsonl"
        self.context = self.root / "context.json"
        self.input.write_bytes(jsonl(machine(verification())))
        self.context.write_text(json.dumps(context()), encoding="utf-8")

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_regular_descriptor_read_and_all_intended_exit_codes(self) -> None:
        self.assertEqual(ROUTER.read_stable_regular_file(self.input), self.input.read_bytes())
        output = []
        errors = []
        self.assertEqual(ROUTER.main(["--input", str(self.input), "--context", str(self.context)], emit=output.append, emit_error=errors.append), 0)
        self.assertEqual(json.loads(output[0])["action"], "REQUEST_OWNER")
        self.assertEqual(errors, [])
        codes = [0]
        codes.append(ROUTER.main([], emit_error=errors.append))
        self.input.write_text("{bad}\n", encoding="utf-8")
        codes.append(ROUTER.main(["--input", str(self.input), "--context", str(self.context)], emit_error=errors.append))
        self.input.write_bytes(b"x" * (ROUTER.MAX_FILE_BYTES + 1))
        codes.append(ROUTER.main(["--input", str(self.input), "--context", str(self.context)], emit_error=errors.append))
        with mock.patch.object(ROUTER, "run", side_effect=RuntimeError("secret detail")):
            codes.append(ROUTER.main(["--input", str(self.input), "--context", str(self.context)], emit_error=errors.append))
        self.assertEqual(codes, [0, 2, 3, 4, 5])
        self.assertNotIn(1, codes)
        self.assertNotIn("secret detail", errors[-1])

    def test_relative_paths_are_invocation_errors_before_reads(self) -> None:
        with mock.patch.object(ROUTER, "read_stable_regular_file") as read:
            self.assertEqual(ROUTER.main(["--input", "relative", "--context", str(self.context)]), 2)
            read.assert_not_called()

    def test_symlink_directory_and_nonregular_reject_without_following(self) -> None:
        directory = self.root / "directory"
        directory.mkdir()
        with self.assertRaises(ROUTER.InputError):
            ROUTER.read_stable_regular_file(directory)

        for mode in (stat.S_IFLNK, stat.S_IFIFO):
            inspected = mock.Mock(st_mode=mode, st_size=0)
            with (
                self.subTest(mode=mode),
                mock.patch.object(ROUTER, "_lstat", return_value=inspected),
                mock.patch.object(ROUTER.os, "open") as opened,
                self.assertRaises(ROUTER.InputError),
            ):
                ROUTER.read_stable_regular_file(self.input)
            opened.assert_not_called()

    def test_descriptor_identity_replacement_truncation_and_metadata_races_reject(self) -> None:
        actual = os.lstat(self.input)
        different = os.lstat(self.context)
        with mock.patch.object(ROUTER, "_lstat", side_effect=[different]):
            with self.assertRaises(ROUTER.InputError):
                ROUTER.read_stable_regular_file(self.input)
        with mock.patch.object(ROUTER, "_descriptor_read", return_value=b""):
            with self.assertRaises(ROUTER.BoundsError):
                ROUTER.read_stable_regular_file(self.input)
        with mock.patch.object(ROUTER, "_lstat", side_effect=[actual, different]):
            with self.assertRaises(ROUTER.BoundsError):
                ROUTER.read_stable_regular_file(self.input)

    def test_simulated_growth_beyond_descriptor_size_rejects(self) -> None:
        original = self.input.read_bytes()
        reads = iter((original + b"x", b""))
        with mock.patch.object(ROUTER, "_descriptor_read", side_effect=lambda *_: next(reads)):
            with self.assertRaises(ROUTER.BoundsError):
                ROUTER.read_stable_regular_file(self.input)

    def test_argparse_help_returns_zero_and_unexpected_system_exit_fails_closed(self) -> None:
        with mock.patch("sys.stdout", new=io.StringIO()):
            self.assertEqual(ROUTER.main(["--help"]), 0)
        errors = []
        with mock.patch.object(ROUTER, "run", side_effect=SystemExit(7)):
            result = ROUTER.main(
                ["--input", str(self.input), "--context", str(self.context)],
                emit_error=errors.append,
            )
        self.assertEqual(result, 5)
        self.assertEqual(errors, ["continuation-router: unexpected internal failure"])

    def test_open_is_read_only_close_on_exec_and_no_follow_when_available(self) -> None:
        real_open = os.open
        seen = []
        def capture(path, flags):
            seen.append(flags)
            return real_open(path, flags)
        with mock.patch.object(ROUTER.os, "open", side_effect=capture):
            ROUTER.read_stable_regular_file(self.input)
        flags = seen[0]
        self.assertEqual(flags & os.O_ACCMODE, os.O_RDONLY)
        if hasattr(os, "O_CLOEXEC"):
            self.assertTrue(flags & os.O_CLOEXEC)
        if hasattr(os, "O_NOFOLLOW"):
            self.assertTrue(flags & os.O_NOFOLLOW)

    def test_no_discovery_network_subprocess_or_write_api_is_used(self) -> None:
        with (
            mock.patch("pathlib.Path.home", side_effect=AssertionError("discovery")),
            mock.patch("os.walk", side_effect=AssertionError("discovery")),
            mock.patch("subprocess.run", side_effect=AssertionError("subprocess")),
            mock.patch("socket.create_connection", side_effect=AssertionError("network")),
            mock.patch("builtins.open", side_effect=AssertionError("write or fallback read")),
        ):
            result = ROUTER.run(self.input, self.context)
        self.assertEqual(result["action"], "REQUEST_OWNER")


if __name__ == "__main__":
    unittest.main()
