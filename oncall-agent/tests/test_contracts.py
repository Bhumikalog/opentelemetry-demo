import json

import pytest
from pydantic import ValidationError

from oncall.contracts import (
    ActionRequest,
    ActionResult,
    ApprovalRecord,
    Diagnosis,
    Evidence,
    IncidentInput,
    IncidentReport,
    RemediationProposal,
    ToolError,
    ToolErrorCode,
    ToolResult,
    VerificationResult,
    compute_proposal_hash,
    write_json_schemas,
)

INCIDENT_INPUT = {
    "incident_id": "INC-0001",
    "title": "Checkout failing for many customers",
    "description": "Users report 'something went wrong' when placing orders since ~10:00.",
    "reported_service": "frontend",
    "symptom_type": "errors",
    "detected_at": "2026-10-23T10:05:00Z",
    "severity": "SEV2",
    "source": "manual",
}

EVIDENCE = {
    "id": "ev-002",
    "source": "jaeger",
    "tool": "search_traces",
    "query": "GET /jaeger/ui/api/v3/traces?query.service_name=checkout&query.num_traces=50",
    "service": "checkout",
    "time_range": {"start": "2026-10-23T09:50:00Z", "end": "2026-10-23T10:05:00Z"},
    "collected_at": "2026-10-23T10:05:20Z",
    "summary": "23 of 50 traces contain error spans. Deepest erroring service: payment (span 'charge')",
    "data": {
        "traces_examined": 50,
        "traces_with_errors": 23,
        "error_paths": [
            {
                "path": ["frontend", "checkout", "payment"],
                "count": 23,
                "deepest_message": "Payment request failed. Invalid token.",
                "example_trace_id": "4bf92f3577b34da6a3ce929d0e0e4736",
            }
        ],
    },
    "truncated": False,
}

TOOL_RESULT_OK = {
    "ok": True,
    "tool": "get_service_health",
    "evidence": [EVIDENCE],
    "error": None,
    "duration_ms": 412,
    "cached": False,
}

TOOL_RESULT_ERROR = {
    "ok": False,
    "tool": "search_logs",
    "evidence": [],
    "error": {"code": "TIMEOUT", "message": "opensearch did not respond within 8s", "retryable": True},
    "duration_ms": 8003,
    "cached": False,
}

DIAGNOSIS = {
    "status": "diagnosed",
    "affected_services": ["frontend", "checkout"],
    "suspected_root_cause_service": "payment",
    "root_cause_hypothesis": "A configuration change caused payment to reject about half of charge requests.",
    "facts": [
        {"statement": "checkout error rate is 47% over the last 5 minutes", "evidence_ids": ["ev-001"]},
        {"statement": "23/50 checkout traces fail in payment 'charge'", "evidence_ids": ["ev-002"]},
    ],
    "assumptions": ["The flag file change time approximates when the change took effect"],
    "alternative_hypotheses": [
        {
            "hypothesis": "payment container crashed",
            "why_less_likely": "ev-005 shows payment running with 0 restarts",
        }
    ],
    "contradictions": [],
    "missing_evidence": [],
    "confidence": "high",
    "llm_confidence": "high",
    "recommended_runbook_id": "RB-CONFIG-REVERT",
    "checker_notes": [],
}

SUCCESS_CRITERIA = {
    "services": ["payment", "checkout"],
    "max_error_rate": 0.02,
    "min_requests": 20,
    "wait_seconds": 90,
    "window_seconds": 120,
}


def make_proposal_dict(**overrides):
    base = {
        "proposal_id": "p-7f3a",
        "incident_id": "INC-0001",
        "runbook_id": "RB-CONFIG-REVERT",
        "action_type": "revert_config_change",
        "target_service": "payment",
        "params": {"change_key": "paymentFailure"},
        "rationale": "Facts ev-002..ev-004 show payment errors began with the change.",
        "expected_effect": "Error rate on payment and checkout returns to baseline within ~2 minutes",
        "risk": "low",
        "adapter": "organizer",
        "simulated": False,
        "success_criteria": SUCCESS_CRITERIA,
        "created_at": "2026-10-23T10:06:01Z",
    }
    base.update(overrides)
    base["proposal_hash"] = compute_proposal_hash(
        incident_id=base["incident_id"],
        runbook_id=base["runbook_id"],
        action_type=base["action_type"],
        target_service=base["target_service"],
        params=base["params"],
        adapter=base["adapter"],
    )
    return base


APPROVAL = {
    "approval_id": "apr-21c9",
    "proposal_id": "p-7f3a",
    "proposal_hash": "sha256:" + "9c1e" * 16,
    "decision": "approved",
    "approver": "Person-2-name",
    "channel": "cli",
    "decided_at": "2026-10-23T10:06:40Z",
    "expires_at": "2026-10-23T10:16:40Z",
    "consumed_at": None,
    "reason": None,
}

ACTION_REQUEST = {
    "action_id": "act-5d01",
    "idempotency_key": "p-7f3a",
    "proposal_id": "p-7f3a",
    "approval_id": "apr-21c9",
    "action_type": "revert_config_change",
    "target_service": "payment",
    "params": {"change_key": "paymentFailure"},
}

ACTION_RESULT = {
    "action_id": "act-5d01",
    "status": "succeeded",
    "rejection_reason": None,
    "adapter": "organizer",
    "simulated": False,
    "started_at": "2026-10-23T10:06:41Z",
    "finished_at": "2026-10-23T10:06:43Z",
    "api_status_code": 200,
    "response_summary": "ok",
    "error": None,
}

VERIFICATION = {
    "status": "recovered",
    "checked_at": "2026-10-23T10:09:40Z",
    "window": {"start": "2026-10-23T10:07:10Z", "end": "2026-10-23T10:09:40Z"},
    "criteria": {"max_error_rate": 0.02, "min_requests": 20},
    "per_service": [
        {"service": "payment", "requests": 118, "error_rate": 0.0, "passed": True, "evidence_id": "ev-011"},
        {"service": "checkout", "requests": 121, "error_rate": 0.008, "passed": True, "evidence_id": "ev-012"},
    ],
    "baseline_before": {"payment": 0.48, "checkout": 0.47},
    "notes": ["Trace-based counts used because metrics lag ~60s"],
}

REPORT = {
    "report_schema": "oncall-agent/proposed/v1",
    "incident": INCIDENT_INPUT,
    "final_status": "resolved",
    "summary": "Payment reverted and recovery verified.",
    "diagnosis": DIAGNOSIS,
    "evidence": [EVIDENCE],
    "proposal": make_proposal_dict(),
    "approval": APPROVAL,
    "action": ACTION_RESULT,
    "verification": VERIFICATION,
    "timeline": [{"at": "2026-10-23T10:05:00Z", "state": "RECEIVED", "detail": ""}],
    "limitations": [],
    "agent_meta": {
        "model": "m",
        "provider": "p",
        "tool_calls": 5,
        "llm_calls": 4,
        "duration_s": 12.5,
        "adapter": "organizer",
        "version": "0.1.0",
    },
    "generated_at": "2026-10-23T10:10:00Z",
}


@pytest.mark.parametrize(
    "model,data",
    [
        (IncidentInput, INCIDENT_INPUT),
        (Evidence, EVIDENCE),
        (ToolResult, TOOL_RESULT_OK),
        (ToolResult, TOOL_RESULT_ERROR),
        (Diagnosis, DIAGNOSIS),
        (RemediationProposal, make_proposal_dict()),
        (ApprovalRecord, APPROVAL),
        (ActionRequest, ACTION_REQUEST),
        (ActionResult, ACTION_RESULT),
        (VerificationResult, VERIFICATION),
        (IncidentReport, REPORT),
    ],
)
def test_plan_examples_validate_and_round_trip(model, data):
    obj = model.model_validate(data)
    again = model.model_validate_json(obj.model_dump_json())
    assert again == obj


def test_timestamps_serialize_as_utc_z_strings():
    obj = IncidentInput.model_validate({**INCIDENT_INPUT, "detected_at": "2026-10-23T15:35:00+05:30"})
    assert json.loads(obj.model_dump_json())["detected_at"] == "2026-10-23T10:05:00Z"


def test_naive_timestamps_are_treated_as_utc():
    obj = IncidentInput.model_validate({**INCIDENT_INPUT, "detected_at": "2026-10-23T10:05:00"})
    assert json.loads(obj.model_dump_json())["detected_at"] == "2026-10-23T10:05:00Z"


def test_incident_defaults():
    obj = IncidentInput.model_validate({"incident_id": "INC-1", "title": "x"})
    assert obj.symptom_type.value == "other"
    assert obj.detected_at.tzinfo is not None
    assert obj.labels == {}


def test_incident_labels_accept_free_keys_but_other_fields_do_not():
    ok = IncidentInput.model_validate({**INCIDENT_INPUT, "labels": {"team": "x", "anything": "y"}})
    assert ok.labels["anything"] == "y"
    with pytest.raises(ValidationError):
        IncidentInput.model_validate({**INCIDENT_INPUT, "unknown_field": 1})


@pytest.mark.parametrize(
    "bad",
    [
        {**INCIDENT_INPUT, "incident_id": "has space"},
        {**INCIDENT_INPUT, "incident_id": ""},
        {**INCIDENT_INPUT, "incident_id": "x" * 65},
        {k: v for k, v in INCIDENT_INPUT.items() if k != "title"},
        {**INCIDENT_INPUT, "title": ""},
        {**INCIDENT_INPUT, "title": "t" * 201},
        {**INCIDENT_INPUT, "description": "d" * 4001},
        {**INCIDENT_INPUT, "severity": "SEV9"},
        {**INCIDENT_INPUT, "symptom_type": "slow"},
        {**INCIDENT_INPUT, "detected_at": "yesterday"},
        {**INCIDENT_INPUT, "labels": {"a": 1}},
    ],
)
def test_malformed_incident_input_is_rejected(bad):
    with pytest.raises(ValidationError):
        IncidentInput.model_validate(bad)


def test_evidence_rejects_bad_id_and_unknown_source_and_long_summary():
    for patch in ({"id": "ev-1"}, {"id": "evidence-001"}, {"source": "grafana"}, {"summary": "s" * 501}):
        with pytest.raises(ValidationError):
            Evidence.model_validate({**EVIDENCE, **patch})


def test_feature_flag_evidence_source_does_not_exist():
    with pytest.raises(ValidationError):
        Evidence.model_validate({**EVIDENCE, "source": "flag_config"})


def test_report_carries_prevention_suggestions():
    report = IncidentReport.model_validate({**REPORT, "prevention": ["Alert on payment error rate above 5%"]})
    assert report.prevention == ["Alert on payment error rate above 5%"]
    assert IncidentReport.model_validate(REPORT).prevention == []


def test_evidence_rejects_oversized_data():
    with pytest.raises(ValidationError):
        Evidence.model_validate({**EVIDENCE, "data": {"blob": "x" * 5000}})


def test_tool_result_ok_and_error_must_agree():
    with pytest.raises(ValidationError):
        ToolResult.model_validate({**TOOL_RESULT_OK, "error": TOOL_RESULT_ERROR["error"]})
    with pytest.raises(ValidationError):
        ToolResult.model_validate({**TOOL_RESULT_ERROR, "error": None})


def test_tool_error_retryable_helper_matches_plan_table():
    assert ToolError.of(ToolErrorCode.TIMEOUT, "x").retryable is True
    assert ToolError.of(ToolErrorCode.UNAVAILABLE, "x").retryable is True
    for code in (ToolErrorCode.BAD_ARGS, ToolErrorCode.NOT_FOUND, ToolErrorCode.BACKEND_ERROR, ToolErrorCode.BUDGET_EXCEEDED):
        assert ToolError.of(code, "x").retryable is False


def test_diagnosis_fact_must_cite_evidence():
    bad = {**DIAGNOSIS, "facts": [{"statement": "uncited", "evidence_ids": []}]}
    with pytest.raises(ValidationError):
        Diagnosis.model_validate(bad)


def test_diagnosis_rejects_malformed_evidence_ids_and_confidence():
    with pytest.raises(ValidationError):
        Diagnosis.model_validate({**DIAGNOSIS, "facts": [{"statement": "s", "evidence_ids": ["made-up"]}]})
    with pytest.raises(ValidationError):
        Diagnosis.model_validate({**DIAGNOSIS, "confidence": "certain"})


def test_diagnosed_needs_an_alternative_hypothesis_but_insufficient_does_not():
    with pytest.raises(ValidationError):
        Diagnosis.model_validate({**DIAGNOSIS, "alternative_hypotheses": []})
    ok = Diagnosis.model_validate(
        {"status": "insufficient_evidence", "confidence": "low", "missing_evidence": ["opensearch unreachable"]}
    )
    assert ok.facts == []


def test_hypothesis_length_limit():
    with pytest.raises(ValidationError):
        Diagnosis.model_validate({**DIAGNOSIS, "root_cause_hypothesis": "h" * 801})


def test_proposal_hash_is_deterministic_and_binds_every_field():
    base = dict(
        incident_id="INC-1",
        runbook_id="RB",
        action_type="a",
        target_service="payment",
        params={"b": 1, "a": 2},
        adapter="simulated",
    )
    first = compute_proposal_hash(**base)
    assert first.startswith("sha256:")
    assert first == compute_proposal_hash(**{**base, "params": {"a": 2, "b": 1}})
    for field, value in [
        ("incident_id", "INC-2"),
        ("runbook_id", "RB2"),
        ("action_type", "b"),
        ("target_service", "cart"),
        ("params", {"a": 3, "b": 1}),
        ("adapter", "organizer"),
    ]:
        assert compute_proposal_hash(**{**base, field: value}) != first


def test_proposal_detects_tampering_after_hashing():
    proposal = RemediationProposal.model_validate(make_proposal_dict())
    assert proposal.hash_is_valid()
    tampered = proposal.model_copy(update={"target_service": "cart"})
    assert not tampered.hash_is_valid()


@pytest.mark.parametrize(
    "patch",
    [
        {"proposal_id": "7f3a"},
        {"risk": "extreme"},
        {"proposal_hash": "md5:abc"},
        {"success_criteria": {**SUCCESS_CRITERIA, "max_error_rate": 1.5}},
        {"success_criteria": {**SUCCESS_CRITERIA, "services": []}},
    ],
)
def test_malformed_proposal_is_rejected(patch):
    data = {**make_proposal_dict(), **patch}
    with pytest.raises(ValidationError):
        RemediationProposal.model_validate(data)


def test_action_result_rejected_requires_reason_and_only_then():
    with pytest.raises(ValidationError):
        ActionResult.model_validate({**ACTION_RESULT, "status": "rejected"})
    with pytest.raises(ValidationError):
        ActionResult.model_validate({**ACTION_RESULT, "rejection_reason": "DUPLICATE"})
    ok = ActionResult.model_validate({**ACTION_RESULT, "status": "rejected", "rejection_reason": "DUPLICATE"})
    assert ok.rejection_reason.value == "DUPLICATE"


def test_time_range_must_be_ordered():
    with pytest.raises(ValidationError):
        Evidence.model_validate(
            {**EVIDENCE, "time_range": {"start": "2026-10-23T10:05:00Z", "end": "2026-10-23T09:50:00Z"}}
        )


def test_report_allows_missing_incident_for_rejected_input():
    report = IncidentReport.model_validate(
        {"final_status": "rejected_input", "summary": "bad input", "limitations": ["Invalid input at title"]}
    )
    assert report.incident is None


def test_report_rejects_unknown_final_status():
    with pytest.raises(ValidationError):
        IncidentReport.model_validate({"final_status": "fixed", "summary": "x"})


def test_schemas_are_written_and_proposed_report_is_labelled(tmp_path):
    written = write_json_schemas(tmp_path)
    names = {path.name for path in written}
    assert "incident_input.schema.json" in names
    assert "incident_report.proposed.schema.json" in names
    proposed = json.loads((tmp_path / "incident_report.proposed.schema.json").read_text())
    assert proposed["title"] == "PROPOSED by team (not official)"
    for path in written:
        json.loads(path.read_text())
