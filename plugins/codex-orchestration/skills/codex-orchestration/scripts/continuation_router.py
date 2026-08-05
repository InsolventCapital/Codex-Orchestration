#!/usr/bin/env python3
"""Extract explicit machine events and classify a root-owned continuation.

This module is deliberately a read-only, deterministic classifier.  It does not
discover sessions, establish freshness, invoke a model, or perform the action it
describes.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
import json
import os
from pathlib import Path
import re
import stat
import sys
from types import MappingProxyType
from typing import Any, Callable, Mapping
import unicodedata


SCHEMA_VERSION = 1
MAX_FILE_BYTES = 1_048_576
MAX_JSONL_LINES = 4096
MAX_LINE_BYTES = 65_536
MAX_EVENTS = 32
MAX_OBJECT_DEPTH = 12
MAX_STRING_LENGTH = 4096
MAX_RECORDED_HASHES = 256
MAX_EVENT_ID_LENGTH = 128

_HASH = re.compile(r"^[0-9a-f]{64}$")
_TOKEN = re.compile(
    rf"^[A-Za-z0-9][A-Za-z0-9._:-]{{0,{MAX_EVENT_ID_LENGTH - 1}}}$"
)
_RFC3339_UTC = re.compile(
    r"^(\d{4}-\d{2}-\d{2})T(\d{2}:\d{2}:\d{2})(?:\.(\d{1,18}))?Z$"
)
_PHASE_ORDER = {"PLAN": 0, "DRIFT": 1, "VERIFY": 2}
_PAIR = {
    "ADVISOR_DECISION": "PLAN",
    "REGISTERED_DRIFT": "DRIFT",
    "VERIFICATION": "VERIFY",
}
_VERIFICATION_STATUSES = {"VERIFIED", "NOT_VERIFIED", "UNAVAILABLE", "MALFORMED"}
_CONTROL = re.compile(r"[\x00-\x1f\x7f-\x9f]")


class RouterError(RuntimeError):
    """Base class for expected failures."""


class InvocationError(RouterError):
    """The caller did not supply a valid explicit configuration."""


class InputError(RouterError):
    """Input is unsafe, malformed, untrusted, or conflicts with the schema."""


class BoundsError(RouterError):
    """A bounded read could not be completed from one stable file."""


def _is_deeply_immutable(value: Any) -> bool:
    if isinstance(value, MappingProxyType):
        return all(
            type(key) is str and _is_deeply_immutable(nested)
            for key, nested in value.items()
        )
    if type(value) is tuple:
        return all(_is_deeply_immutable(nested) for nested in value)
    return value is None or type(value) in {str, int, bool}


@dataclass(frozen=True, slots=True)
class ContinuationState:
    """Deeply immutable normalized input for pure continuation classification."""

    events: tuple[Mapping[str, Any], ...]
    context: Mapping[str, Any]

    def __post_init__(self) -> None:
        if (
            type(self.events) is not tuple
            or not isinstance(self.context, MappingProxyType)
            or not _is_deeply_immutable(self.events)
            or not _is_deeply_immutable(self.context)
        ):
            raise TypeError("ContinuationState requires deeply immutable normalized data")


class _ArgumentParser(argparse.ArgumentParser):
    def error(self, message: str) -> None:
        raise InvocationError(message)


def _require(condition: bool, detail: str) -> None:
    if not condition:
        raise InputError(detail)


def _lstat(path: Path) -> os.stat_result:
    return os.lstat(path)


def _fstat(descriptor: int) -> os.stat_result:
    return os.fstat(descriptor)


def _descriptor_read(descriptor: int, size: int) -> bytes:
    return os.read(descriptor, size)


def _identity(info: os.stat_result) -> tuple[int, int]:
    return info.st_dev, info.st_ino


def _metadata(info: os.stat_result) -> tuple[int, int, int, int, int]:
    return (
        info.st_dev,
        info.st_ino,
        info.st_size,
        info.st_mtime_ns,
        info.st_ctime_ns,
    )


def read_stable_regular_file(path: Path, *, maximum_bytes: int = MAX_FILE_BYTES) -> bytes:
    """Read an explicit regular file through one descriptor and reject races."""

    if not path.is_absolute():
        raise InvocationError("input paths must be absolute")
    try:
        before_path = _lstat(path)
    except OSError as exc:
        raise InputError("input path could not be inspected") from exc
    _require(stat.S_ISREG(before_path.st_mode), "input path is not a regular file")
    if before_path.st_size < 0 or before_path.st_size > maximum_bytes:
        raise BoundsError("input file exceeds the byte bound")

    flags = os.O_RDONLY
    if hasattr(os, "O_CLOEXEC"):
        flags |= os.O_CLOEXEC
    if hasattr(os, "O_BINARY"):
        flags |= os.O_BINARY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise InputError("input path could not be opened safely") from exc
    try:
        opened = _fstat(descriptor)
        if not stat.S_ISREG(opened.st_mode) or _identity(opened) != _identity(before_path):
            raise InputError("opened input does not match the inspected regular file")
        if opened.st_size < 0 or opened.st_size > maximum_bytes:
            raise BoundsError("input file exceeds the byte bound")

        chunks: list[bytes] = []
        total = 0
        while True:
            chunk = _descriptor_read(descriptor, min(65_536, maximum_bytes + 1 - total))
            if not chunk:
                break
            chunks.append(chunk)
            total += len(chunk)
            if total > maximum_bytes:
                raise BoundsError("input grew beyond the byte bound")
        after_descriptor = _fstat(descriptor)
    except OSError as exc:
        raise BoundsError("input could not be read completely") from exc
    finally:
        os.close(descriptor)

    if _metadata(opened) != _metadata(after_descriptor):
        raise BoundsError("input changed while being read")
    if total != opened.st_size:
        raise BoundsError("input byte count does not match descriptor size")
    try:
        after_path = _lstat(path)
    except OSError as exc:
        raise BoundsError("input path changed after reading") from exc
    if not stat.S_ISREG(after_path.st_mode) or _identity(after_path) != _identity(opened):
        raise BoundsError("input path was replaced while being read")
    if _metadata(after_path) != _metadata(after_descriptor):
        raise BoundsError("input path metadata changed while being read")
    return b"".join(chunks)


def _reject_duplicate_pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise InputError("JSON contains a duplicate key")
        result[key] = value
    return result


def _reject_json_constant(_: str) -> Any:
    raise InputError("JSON contains a non-standard numeric constant")


def _validate_json_shape(value: Any, *, depth: int = 0) -> None:
    if depth > MAX_OBJECT_DEPTH:
        raise InputError("JSON nesting exceeds the depth bound")
    if isinstance(value, str):
        if len(value) > MAX_STRING_LENGTH:
            raise InputError("JSON string exceeds the length bound")
        return
    if type(value) is dict:
        for key, nested in value.items():
            if len(key) > MAX_STRING_LENGTH:
                raise InputError("JSON key exceeds the length bound")
            _validate_json_shape(nested, depth=depth + 1)
        return
    if type(value) is list:
        for nested in value:
            _validate_json_shape(nested, depth=depth + 1)


def _load_json(data: bytes, *, label: str) -> Any:
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise InputError(f"{label} is not valid UTF-8") from exc
    try:
        value = json.loads(
            text,
            object_pairs_hook=_reject_duplicate_pairs,
            parse_constant=_reject_json_constant,
        )
    except InputError:
        raise
    except (json.JSONDecodeError, RecursionError) as exc:
        raise InputError(f"{label} is malformed JSON") from exc
    _validate_json_shape(value)
    return value


def _exact_keys(value: Any, expected: set[str], label: str) -> dict[str, Any]:
    _require(type(value) is dict, f"{label} must be an object")
    _require(set(value) == expected, f"{label} has missing or unknown keys")
    return value


def _integer(value: Any, label: str, *, positive: bool = False) -> int:
    _require(type(value) is int, f"{label} must be an integer")
    if positive:
        _require(value > 0, f"{label} must be positive")
    return value


def _clean_string(value: Any, label: str, *, maximum: int = MAX_STRING_LENGTH) -> str:
    _require(type(value) is str and 0 < len(value) <= maximum, f"{label} is invalid")
    _require(_CONTROL.search(value) is None, f"{label} contains control characters")
    _require(
        all(
            not unicodedata.category(character).startswith("C")
            and unicodedata.category(character) not in {"Zl", "Zp"}
            and character.isprintable()
            for character in value
        ),
        f"{label} contains unsafe Unicode",
    )
    return value


def _hash(value: Any, label: str) -> str:
    _require(type(value) is str and _HASH.fullmatch(value) is not None, f"{label} is not canonical SHA-256")
    return value


def _timestamp(value: Any) -> tuple[int, Decimal]:
    _require(type(value) is str, "occurred_at must be a string")
    match = _RFC3339_UTC.fullmatch(value)
    _require(match is not None, "occurred_at must be strict RFC3339 UTC ending Z")
    try:
        base = datetime.strptime(f"{match.group(1)}T{match.group(2)}", "%Y-%m-%dT%H:%M:%S")
    except ValueError as exc:
        raise InputError("occurred_at is not a valid UTC date-time") from exc
    seconds = (
        (base.toordinal() - 1) * 86_400
        + base.hour * 3600
        + base.minute * 60
        + base.second
    )
    fraction = Decimal(f"0.{match.group(3)}") if match.group(3) else Decimal(0)
    return seconds, fraction


def _validate_payload(event_type: str, payload: Any) -> dict[str, Any]:
    if event_type == "ADVISOR_DECISION":
        value = _exact_keys(payload, {"decision", "round"}, "advisor payload")
        _require(
            type(value["decision"]) is str
            and value["decision"] in {"PLAN_APPROVED", "PLAN_REVISE"},
            "advisor decision is invalid",
        )
        _integer(value["round"], "advisor round", positive=True)
        return value
    if event_type == "REGISTERED_DRIFT":
        value = _exact_keys(
            payload,
            {"pre_attributed", "signature_match", "disjoint_from_work", "drift_manifest_hash"},
            "drift payload",
        )
        for key in ("pre_attributed", "signature_match", "disjoint_from_work"):
            _require(type(value[key]) is bool, f"{key} must be boolean")
        _hash(value["drift_manifest_hash"], "drift_manifest_hash")
        return value
    _require(type(payload) is dict, "verification payload must be an object")
    value = payload
    _require("status" in value, "verification payload must contain status")
    _require(
        type(value["status"]) is str and value["status"] in _VERIFICATION_STATUSES,
        "verification status is invalid",
    )
    if value["status"] == "VERIFIED":
        _exact_keys(value, {"status", "packet_hash", "manifest_hash"}, "verified payload")
        _hash(value["packet_hash"], "verification packet_hash")
        _hash(value["manifest_hash"], "verification manifest_hash")
    else:
        _exact_keys(value, {"status"}, "non-verified payload")
    return value


def _validate_event(raw: Any) -> dict[str, Any]:
    event = _exact_keys(
        raw,
        {"schema_version", "event_id", "sequence", "occurred_at", "phase", "type", "payload"},
        "machine event",
    )
    _require(type(event["schema_version"]) is int and event["schema_version"] == SCHEMA_VERSION, "event schema_version must be integer 1")
    _require(type(event["event_id"]) is str and _TOKEN.fullmatch(event["event_id"]) is not None, "event_id is not a strict token")
    _integer(event["sequence"], "event sequence", positive=True)
    _timestamp(event["occurred_at"])
    _require(
        type(event["phase"]) is str and event["phase"] in _PHASE_ORDER,
        "event phase is invalid",
    )
    _require(
        type(event["type"]) is str and event["type"] in _PAIR,
        "event type is invalid",
    )
    _require(_PAIR[event["type"]] == event["phase"], "event phase/type pair is invalid")
    _validate_payload(event["type"], event["payload"])
    return event


def extract_events(data: bytes) -> list[dict[str, Any]]:
    """Validate JSONL records and return only normalized machine events."""

    if len(data) > MAX_FILE_BYTES:
        raise BoundsError("input file exceeds the byte bound")
    physical = data.split(b"\n")
    if physical and physical[-1] == b"":
        physical.pop()
    if len(physical) > MAX_JSONL_LINES:
        raise BoundsError("JSONL exceeds the physical line bound")
    _require(bool(physical), "input JSONL is empty")

    events: list[dict[str, Any]] = []
    identifiers: set[str] = set()
    types: set[str] = set()
    previous_sequence = 0
    previous_time: tuple[int, Decimal] | None = None
    previous_phase = -1
    terminal = False
    for index, raw_line in enumerate(physical, start=1):
        if len(raw_line) > MAX_LINE_BYTES:
            raise BoundsError(f"JSONL line {index} exceeds the byte bound")
        if raw_line.endswith(b"\r"):
            raw_line = raw_line[:-1]
        _require(bool(raw_line), f"JSONL line {index} is blank")
        record = _load_json(raw_line, label=f"JSONL line {index}")
        _require(type(record) is dict, f"JSONL line {index} must be an object")
        kind = record.get("record_kind")
        if kind == "message":
            message = _exact_keys(record, {"record_kind", "role", "content"}, "message record")
            _require(
                type(message["role"]) is str
                and message["role"] in {"user", "assistant", "tool"},
                "message role is invalid",
            )
            _require(type(message["content"]) is str and len(message["content"]) <= MAX_STRING_LENGTH, "message content is invalid")
            continue
        _require(kind == "machine_event", "record_kind is invalid")
        _exact_keys(record, {"record_kind", "event"}, "machine event record")
        if terminal:
            raise InputError("governed event follows a terminal event")
        event = _validate_event(record["event"])
        _require(event["event_id"] not in identifiers, "duplicate event_id")
        _require(event["type"] not in types, "duplicate or conflicting event type")
        when = _timestamp(event["occurred_at"])
        _require(event["sequence"] > previous_sequence, "event sequences are not strictly increasing")
        _require(previous_time is None or when > previous_time, "event timestamps are not strictly increasing")
        phase = _PHASE_ORDER[event["phase"]]
        _require(phase >= previous_phase, "event phases are decreasing")
        identifiers.add(event["event_id"])
        types.add(event["type"])
        events.append(event)
        if len(events) > MAX_EVENTS:
            raise BoundsError("event count exceeds the bound")
        previous_sequence = event["sequence"]
        previous_time = when
        previous_phase = phase
        payload = event["payload"]
        if event["type"] == "ADVISOR_DECISION" and payload["decision"] == "PLAN_REVISE":
            terminal = True
        if event["type"] == "REGISTERED_DRIFT":
            terminal = True
    return events


def validate_context(raw: Any) -> dict[str, Any]:
    _require(type(raw) is dict, "context must be an object")
    tier = raw.get("tier")
    _require(type(tier) is str and tier in {"DIRECT", "FULL"}, "context tier is invalid")
    common = {
        "schema_version",
        "tier",
        "configured_advisor_cap",
        "authority_context",
        "intended_action",
        "packet_hash",
        "manifest_hash",
        "recorded_drift_manifest_hashes",
    }
    expected = common | ({"direct_execution_receipt_hash"} if tier == "DIRECT" else set())
    context = _exact_keys(raw, expected, "context")
    _require(type(context["schema_version"]) is int and context["schema_version"] == SCHEMA_VERSION, "context schema_version must be integer 1")
    _integer(context["configured_advisor_cap"], "configured_advisor_cap", positive=True)
    authority = _exact_keys(
        context["authority_context"],
        {"authority_owner_id", "tier_map_reference", "authority_mode"},
        "authority_context",
    )
    for key in ("authority_owner_id", "tier_map_reference"):
        _clean_string(authority[key], key, maximum=1024)
    _require(
        type(authority["authority_mode"]) is str
        and authority["authority_mode"]
        in {"DIRECT_SUPERVISED_SETTLED", "FULL_OWNER_GATE_REQUIRED"},
        "authority_mode is invalid",
    )
    _clean_string(context["intended_action"], "intended_action")
    _hash(context["packet_hash"], "context packet_hash")
    _hash(context["manifest_hash"], "context manifest_hash")
    hashes = context["recorded_drift_manifest_hashes"]
    _require(type(hashes) is list, "recorded drift hashes must be a list")
    if len(hashes) > MAX_RECORDED_HASHES:
        raise BoundsError("recorded drift hash list exceeds the bound")
    for value in hashes:
        _hash(value, "recorded drift manifest hash")
    _require(hashes == sorted(hashes) and len(hashes) == len(set(hashes)), "recorded drift hashes must be sorted and duplicate-free")
    if tier == "DIRECT":
        _hash(context["direct_execution_receipt_hash"], "direct execution receipt hash")
    return context


def _freeze(value: Any) -> Any:
    if type(value) is dict:
        return MappingProxyType({key: _freeze(nested) for key, nested in value.items()})
    if type(value) is list:
        return tuple(_freeze(nested) for nested in value)
    if value is None or type(value) in {str, int, bool}:
        return value
    raise InputError("normalized state contains a mutable or unsupported value")


def _state_from_validated(
    events: list[dict[str, Any]], context: dict[str, Any]
) -> ContinuationState:
    frozen_events = _freeze(events)
    frozen_context = _freeze(context)
    if type(frozen_events) is not tuple or not isinstance(frozen_context, Mapping):
        raise InputError("normalized continuation state has an invalid shape")
    return ContinuationState(events=frozen_events, context=frozen_context)


def normalize_state(input_data: bytes, context_data: bytes) -> ContinuationState:
    """Validate explicit bytes and return a deeply immutable normalized state."""

    events = extract_events(input_data)
    context = validate_context(_load_json(context_data, label="context"))
    return _state_from_validated(events, context)


def _result(
    action: str,
    reason: str,
    *,
    required_inputs: list[str] | None = None,
    stop_conditions: list[str] | None = None,
    owner_request_template: str | None = None,
    record_instruction: str | None = None,
) -> dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "action": action,
        "reason_codes": [reason],
        "required_inputs": required_inputs or [],
        "stop_conditions": stop_conditions or [],
        "owner_request_template": owner_request_template,
        "record_instruction": record_instruction,
    }


def classify(state: ContinuationState) -> dict[str, Any]:
    """Purely classify a validated explicit snapshot."""

    if type(state) is not ContinuationState:
        raise TypeError("classify requires ContinuationState")
    events = state.events
    context = state.context

    authority_mode = context["authority_context"]["authority_mode"]
    if (context["tier"] == "DIRECT") != (
        authority_mode == "DIRECT_SUPERVISED_SETTLED"
    ):
        return _result("STOP", "TIER_MODE_MISMATCH", stop_conditions=["CORRECT_CONTEXT_BEFORE_CONTINUATION"])

    by_type = {event["type"]: event for event in events}
    advisor = by_type.get("ADVISOR_DECISION")
    if advisor and advisor["payload"]["decision"] == "PLAN_REVISE":
        round_number = advisor["payload"]["round"]
        if round_number < context["configured_advisor_cap"]:
            return _result(
                "PLANNER",
                "ADVISOR_REVISION_WITHIN_CAP",
                required_inputs=["canonical_current_plan", "complete_findings_ledger", "latest_advisor_critique"],
                stop_conditions=["ROOT_MUST_SELECT_AND_INVOKE_THE_CONFIGURED_PLANNER"],
            )
        return _result(
            "STOP",
            "ADVISOR_REVISION_CAP_EXHAUSTED",
            stop_conditions=["OWNER_DIRECTION_REQUIRED_FOR_ANY_NEW_LINEAGE"],
        )

    drift = by_type.get("REGISTERED_DRIFT")
    if drift:
        payload = drift["payload"]
        qualifies = payload["pre_attributed"] and payload["signature_match"] and payload["disjoint_from_work"]
        if not qualifies:
            return _result("STOP", "UNQUALIFIED_OR_AMBIGUOUS_DRIFT", stop_conditions=["DO_NOT_ATTRIBUTE_OR_RECORD_THIS_DRIFT"])
        digest = payload["drift_manifest_hash"]
        if digest in context["recorded_drift_manifest_hashes"]:
            return _result("STOP", "ALREADY_RECORDED_NO_ACTION", stop_conditions=["DO_NOT_RECORD_THE_SAME_DRIFT_TWICE"])
        return _result(
            "RECORD_AND_CONTINUE",
            "QUALIFYING_REGISTERED_DRIFT",
            stop_conditions=["ROOT_OWNS_RECORDING_AND_ANY_CONTINUATION"],
            record_instruction=f"Record drift manifest SHA-256: {digest}",
        )

    verification = by_type.get("VERIFICATION")
    if not verification:
        if advisor and advisor["payload"]["decision"] == "PLAN_APPROVED":
            return _result("STOP", "NO_ACTIONABLE_CONTINUATION", stop_conditions=["WAIT_FOR_AN_EXPLICIT_ACTIONABLE_MACHINE_EVENT"])
        return _result("STOP", "MISSING_VERIFICATION", stop_conditions=["NO_VERIFICATION_CONCLUSION_IS_AVAILABLE"])

    payload = verification["payload"]
    status = payload["status"]
    if status != "VERIFIED":
        reason = {
            "NOT_VERIFIED": "VERIFICATION_NOT_VERIFIED",
            "UNAVAILABLE": "VERIFIER_UNAVAILABLE",
            "MALFORMED": "VERIFICATION_MALFORMED",
        }[status]
        template = {
            "NOT_VERIFIED": (
                "WHY IT STOPPED\n"
                "Verifier status: NOT_VERIFIED.\n"
                "The supplied Verifier artifact did not establish VERIFIED. No authorization has been granted.\n\n"
                "OPTIONS\n"
                "- Close or explicitly decline the intended action.\n"
                "- Explicitly authorize a new planning lineage for structural review of the underlying evidence.\n\n"
                "VERIFIER'S VIEW\n"
                "The Verifier is artifact-only evidence and is not gate-eligible by itself. Do not retry, reuse, or bypass this consumed Verifier result."
            ),
            "UNAVAILABLE": (
                "WHY IT STOPPED\n"
                "Verifier status: UNAVAILABLE.\n"
                "No usable Verifier result is available. No authorization has been granted.\n\n"
                "OPTIONS\n"
                "- Close or explicitly decline the intended action.\n"
                "- Explicitly authorize a new planning lineage for structural review of the missing verification evidence.\n\n"
                "VERIFIER'S VIEW\n"
                "The Verifier is artifact-only evidence and is not gate-eligible by itself. Do not retry, reuse, or bypass a missing or unavailable Verifier result."
            ),
            "MALFORMED": (
                "WHY IT STOPPED\n"
                "Verifier status: MALFORMED.\n"
                "The supplied Verifier result was semantically marked MALFORMED. No authorization has been granted.\n\n"
                "OPTIONS\n"
                "- Close or explicitly decline the intended action.\n"
                "- Explicitly authorize a new planning lineage for structural review of the verification artifact.\n\n"
                "VERIFIER'S VIEW\n"
                "The Verifier is artifact-only evidence and is not gate-eligible by itself. Do not retry, reuse, or bypass this malformed Verifier result."
            ),
        }[status]
        return _result(
            "STOP",
            reason,
            stop_conditions=["DO_NOT_RETRY_REUSE_OR_BYPASS_THE_CONSUMED_VERIFIER"],
            owner_request_template=template,
        )
    if payload["packet_hash"] != context["packet_hash"] or payload["manifest_hash"] != context["manifest_hash"]:
        return _result("STOP", "VERIFICATION_HASH_MISMATCH", stop_conditions=["VERIFICATION_DOES_NOT_BIND_TO_THIS_CONTEXT"])

    if context["tier"] == "DIRECT":
        return _result(
            "CLOSE_EVIDENCE",
            "DIRECT_VERIFICATION_CLOSES_EVIDENCE_ONLY",
            stop_conditions=[
                "SUPERVISED_SETTLEMENT_ALREADY_HAPPENED",
                "THIS_RESULT_NEITHER_SETTLES_NOR_AUTHORIZES",
            ],
        )

    authority = context["authority_context"]
    template = (
        "OWNER GATE REQUEST — FULL\n"
        f"Packet SHA-256: {context['packet_hash']}\n"
        f"Manifest SHA-256: {context['manifest_hash']}\n"
        f"Intended action: {context['intended_action']}\n"
        "Tier: FULL\n"
        f"Authority context: {authority['authority_owner_id']}; {authority['tier_map_reference']}\n"
        "Decision requested: explicitly authorize the intended action, explicitly decline it, or direct a new authorized plan.\n"
        "This request is non-authoritative. No router, model seat, or Verifier result has authorized the action."
    )
    return _result(
        "REQUEST_OWNER",
        "FULL_VERIFICATION_REQUIRES_OWNER_GATE",
        required_inputs=["explicit_owner_decision"],
        stop_conditions=["NO_ACTION_BEFORE_EXPLICIT_OWNER_DECISION"],
        owner_request_template=template,
    )


def _parser() -> _ArgumentParser:
    parser = _ArgumentParser(description=__doc__)
    parser.add_argument("--input", required=True)
    parser.add_argument("--context", required=True)
    return parser


def run(input_path: Path, context_path: Path) -> dict[str, Any]:
    input_data = read_stable_regular_file(input_path)
    context_data = read_stable_regular_file(context_path)
    return classify(normalize_state(input_data, context_data))


def main(
    argv: list[str] | None = None,
    *,
    emit: Callable[[str], Any] = print,
    emit_error: Callable[[str], Any] | None = None,
) -> int:
    error = emit_error or (lambda value: print(value, file=sys.stderr))
    try:
        arguments = _parser().parse_args(argv)
        input_path = Path(arguments.input)
        context_path = Path(arguments.context)
        if not input_path.is_absolute() or not context_path.is_absolute():
            raise InvocationError("--input and --context must be explicit absolute paths")
        result = run(input_path, context_path)
        emit(json.dumps(result, sort_keys=True))
        return 0
    except InvocationError as exc:
        error(f"continuation-router: invocation error: {exc}")
        return 2
    except InputError as exc:
        error(f"continuation-router: input rejected: {exc}")
        return 3
    except BoundsError as exc:
        error(f"continuation-router: bounded read failed: {exc}")
        return 4
    except SystemExit as exc:
        if exc.code in (None, 0):
            return 0
        error("continuation-router: unexpected internal failure")
        return 5
    except (KeyboardInterrupt, GeneratorExit):
        error("continuation-router: unexpected interruption")
        return 5
    except BaseException:
        error("continuation-router: unexpected internal failure")
        return 5


if __name__ == "__main__":
    raise SystemExit(main())
