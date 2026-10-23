from datetime import datetime, timezone

import pytest

from oncall.config import load_settings
from oncall.contracts import (
    ActionResult,
    ActionStatus,
    ApprovalDecision,
    ApprovalRecord,
    Confidence,
    Diagnosis,
    DiagnosisStatus,
    Evidence,
    FinalStatus,
    IncidentReport,
    RejectionReason,
    RemediationProposal,
    VerificationResult,
    VerificationStatus,
    compute_proposal_hash,
)
from oncall.orchestrator import (
    Deps,
    InvestigationOutcome,
    ProposalRejection,
    ProposalRequest,
    derive_final_status,
    run_incident,
)

NOW = datetime(2026, 10, 23, 10, 5, 0, tzinfo=timezone.utc)

VALID_INPUT = {"incident_id": "INC-1", "title": "Checkout failing", "reported_service": "frontend"}


def make_evidence() -> Evidence:
    return Evidence(
        id="ev-001",
        source="prometheus",
        tool="get_service_health",
        query="q",
        service="svc-a",
        collected_at=NOW,
        summary="error rate 47%",
        data={"error_rate": 0.47},
    )


def make_diagnosis(status="diagnosed", confidence="high", root="svc-a") -> Diagnosis:
    if status == "insufficient_evidence":
        return Diagnosis(status=DiagnosisStatus.insufficient_evidence, confidence=Confidence(confidence))
    return Diagnosis(
        status=DiagnosisStatus.diagnosed,
        suspected_root_cause_service=root,
        root_cause_hypothesis="hypothesis",
        facts=[{"statement": "errors", "evidence_ids": ["ev-001"]}],
        alternative_hypotheses=[{"hypothesis": "other", "why_less_likely": "reason"}],
        confidence=Confidence(confidence),
        recommended_runbook_id="RB-X",
    )


def make_outcome(**kwargs) -> InvestigationOutcome:
    diagnosis = kwargs.pop("diagnosis", make_diagnosis())
    request = kwargs.pop("proposal_request", ProposalRequest(runbook_id="RB-X", params={"k": "v"}, rationale="because"))
    return InvestigationOutcome(
        evidence=[make_evidence()],
        diagnosis=diagnosis,
        proposal_request=request,
        tool_calls=4,
        llm_calls=3,
        **kwargs,
    )


def make_proposal(simulated=False) -> RemediationProposal:
    adapter = "simulated" if simulated else "organizer"
    return RemediationProposal(
        proposal_id="p-abc1",
        incident_id="INC-1",
        runbook_id="RB-X",
        action_type="do_thing",
        target_service="svc-a",
        params={"k": "v"},
        rationale="because",
        expected_effect="recovers",
        risk="low",
        adapter=adapter,
        simulated=simulated,
        success_criteria={"services": ["svc-a"], "max_error_rate": 0.02, "min_requests": 20, "wait_seconds": 0, "window_seconds": 60},
        proposal_hash=compute_proposal_hash(
            incident_id="INC-1",
            runbook_id="RB-X",
            action_type="do_thing",
            target_service="svc-a",
            params={"k": "v"},
            adapter=adapter,
        ),
        created_at=NOW,
    )


def make_approval(proposal: RemediationProposal, decision="approved") -> ApprovalRecord:
    return ApprovalRecord(
        approval_id="apr-1",
        proposal_id=proposal.proposal_id,
        proposal_hash=proposal.proposal_hash,
        decision=ApprovalDecision(decision),
        approver="tester",
        channel="test",
        decided_at=NOW,
        expires_at=NOW,
    )


def make_action(status="succeeded", simulated=False, reason=None) -> ActionResult:
    return ActionResult(
        action_id="act-1",
        status=ActionStatus(status),
        rejection_reason=RejectionReason(reason) if reason else None,
        adapter="simulated" if simulated else "organizer",
        simulated=simulated,
        started_at=NOW,
        finished_at=NOW,
    )


def make_verification(status="recovered") -> VerificationResult:
    return VerificationResult(status=VerificationStatus(status), checked_at=NOW)


class Harness:
    def __init__(self, **overrides):
        self.calls: list[str] = []
        self.reports: list[IncidentReport] = []
        self.kill = False
        self.simulated = overrides.pop("simulated", False)
        self.outcome = overrides.pop("outcome", None)
        self.proposal_result = overrides.pop("proposal_result", None)
        self.decision = overrides.pop("decision", "approved")
        self.action = overrides.pop("action", None)
        self.verification = overrides.pop("verification", None)
        self.raises: dict[str, Exception] = overrides.pop("raises", {})
        self.kill_after: str | None = overrides.pop("kill_after", None)
        self.preflight = overrides.pop("preflight", None)
        assert not overrides, overrides

    def _step(self, name):
        self.calls.append(name)
        if name in self.raises:
            raise self.raises[name]
        if self.kill_after == name:
            self.kill = True

    def investigate(self, incident):
        self._step("investigate")
        return self.outcome or make_outcome()

    def build_proposal(self, incident, diagnosis, request):
        self._step("build_proposal")
        return self.proposal_result or make_proposal(self.simulated)

    def request_approval(self, proposal):
        self._step("request_approval")
        return make_approval(proposal, self.decision)

    def execute(self, proposal, approval):
        self._step("execute")
        return self.action or make_action(simulated=self.simulated)

    def verify(self, proposal, action):
        self._step("verify")
        return self.verification or make_verification()

    def report(self, report):
        self._step("report")
        self.reports.append(report)

    def deps(self) -> Deps:
        return Deps(
            settings=load_settings({"LLM_PROVIDER": "anthropic", "LLM_MODEL": "test-model"}),
            investigate_fn=self.investigate,
            build_proposal_fn=self.build_proposal,
            request_approval_fn=self.request_approval,
            execute_fn=self.execute,
            verify_fn=self.verify,
            report_fn=self.report,
            killswitch_fn=lambda: self.kill,
            clock=lambda: NOW,
            preflight_fn=(lambda: self.preflight) if self.preflight is not None else None,
        )


def run(harness: Harness, raw=VALID_INPUT) -> IncidentReport:
    return run_incident(raw, harness.deps())


def states(report: IncidentReport) -> list[str]:
    return [entry.state for entry in report.timeline]


def test_real_action_recovered_is_resolved():
    h = Harness()
    report = run(h)
    assert report.final_status == FinalStatus.resolved
    assert h.calls == ["investigate", "build_proposal", "request_approval", "execute", "verify", "report"]
    assert states(report) == [
        "RECEIVED", "VALIDATED", "INVESTIGATING", "DIAGNOSED",
        "PROPOSED", "AWAITING_APPROVAL", "APPROVED", "EXECUTING", "ACTION_SUCCEEDED", "VERIFYING", "VERIFIED", "REPORTED",
    ]
    assert report.agent_meta.tool_calls == 4 and report.agent_meta.llm_calls == 3
    assert report.agent_meta.model == "test-model"
    assert report.limitations == []
    assert h.reports == [report]


def test_real_action_not_recovered_is_not_resolved():
    h = Harness(verification=make_verification("not_recovered"))
    assert run(h).final_status == FinalStatus.not_resolved


def test_real_action_unverified_is_mitigation_unverified():
    h = Harness(verification=make_verification("unverified"))
    assert run(h).final_status == FinalStatus.mitigation_unverified


def test_simulated_action_can_never_be_resolved():
    h = Harness(simulated=True, verification=make_verification("recovered"))
    report = run(h)
    assert report.final_status == FinalStatus.mitigation_unverified
    assert any("simulated" in text.lower() for text in report.limitations)


def test_simulated_action_not_recovered_is_not_resolved():
    h = Harness(simulated=True, verification=make_verification("not_recovered"))
    assert run(h).final_status == FinalStatus.not_resolved


def test_invalid_input_is_rejected_without_investigating():
    h = Harness()
    report = run(h, {"incident_id": "bad id", "title": ""})
    assert report.final_status == FinalStatus.rejected_input
    assert report.incident is None
    assert h.calls == ["report"]
    assert any("incident_id" in text for text in report.limitations)
    assert any("title" in text for text in report.limitations)


@pytest.mark.parametrize("raw", [None, "not json", 42, [], {"unknown": 1}])
def test_non_object_or_unknown_input_is_rejected_not_crashed(raw):
    h = Harness()
    assert run(h, raw).final_status == FinalStatus.rejected_input
    assert "investigate" not in h.calls


def test_insufficient_evidence_takes_no_action():
    h = Harness(outcome=make_outcome(diagnosis=make_diagnosis("insufficient_evidence", "low"), proposal_request=None))
    report = run(h)
    assert report.final_status == FinalStatus.no_action_taken
    assert "build_proposal" not in h.calls and "execute" not in h.calls
    assert report.proposal is None


def test_low_confidence_blocks_proposal_even_if_a_runbook_was_requested():
    h = Harness(outcome=make_outcome(diagnosis=make_diagnosis(confidence="low")))
    report = run(h)
    assert report.final_status == FinalStatus.no_action_taken
    assert "build_proposal" not in h.calls
    assert any("below medium" in text for text in report.limitations)


def test_missing_root_cause_blocks_proposal():
    h = Harness(outcome=make_outcome(diagnosis=make_diagnosis(root=None)))
    assert run(h).final_status == FinalStatus.no_action_taken
    assert "build_proposal" not in h.calls


def test_no_runbook_requested_takes_no_action():
    h = Harness(outcome=make_outcome(proposal_request=None))
    assert run(h).final_status == FinalStatus.no_action_taken


def test_proposal_rejection_takes_no_action_and_says_why():
    h = Harness(proposal_result=ProposalRejection(reason="unknown runbook RB-X"))
    report = run(h)
    assert report.final_status == FinalStatus.no_action_taken
    assert any("unknown runbook RB-X" in text for text in report.limitations)
    assert "request_approval" not in h.calls


@pytest.mark.parametrize("decision", ["denied", "expired"])
def test_denied_or_expired_approval_never_executes(decision):
    h = Harness(decision=decision)
    report = run(h)
    assert report.final_status == FinalStatus.no_action_taken
    assert "execute" not in h.calls
    assert report.approval is not None and report.approval.decision.value == decision


def test_approval_for_a_different_proposal_hash_is_treated_as_denial():
    h = Harness()
    original = h.request_approval

    def wrong_hash(proposal):
        record = original(proposal)
        return record.model_copy(update={"proposal_hash": "sha256:" + "0" * 64})

    h.request_approval = wrong_hash
    report = run(h)
    assert report.final_status == FinalStatus.no_action_taken
    assert "execute" not in h.calls


@pytest.mark.parametrize("status,reason", [("failed", None), ("timeout", None), ("rejected", "DUPLICATE"), ("rejected", "HASH_MISMATCH")])
def test_failed_rejected_or_timed_out_action_is_action_failed_and_skips_verification(status, reason):
    h = Harness(action=make_action(status, reason=reason))
    report = run(h)
    assert report.final_status == FinalStatus.action_failed
    assert "verify" not in h.calls
    assert report.verification is None


def test_kill_switch_before_investigation_aborts_everything():
    h = Harness()
    h.kill = True
    report = run(h)
    assert report.final_status == FinalStatus.aborted
    assert h.calls == ["report"]


@pytest.mark.parametrize("after", ["investigate", "build_proposal"])
def test_kill_switch_between_steps_stops_before_the_next_step(after):
    h = Harness(kill_after=after)
    report = run(h)
    assert report.final_status == FinalStatus.aborted
    assert "request_approval" not in h.calls and "execute" not in h.calls


def test_kill_switch_flipped_while_waiting_for_approval_is_blocked_by_the_executor():
    h = Harness(kill_after="request_approval", action=make_action("rejected", reason="KILL_SWITCH_ACTIVE"))
    report = run(h)
    assert h.calls.count("execute") == 1
    assert report.action is not None and report.action.rejection_reason == RejectionReason.KILL_SWITCH_ACTIVE
    assert report.final_status == FinalStatus.aborted
    assert "verify" not in h.calls


def test_kill_switch_check_that_raises_fails_closed():
    h = Harness()
    deps = h.deps()

    def broken():
        raise RuntimeError("cannot read state")

    deps.killswitch_fn = broken
    report = run_incident(VALID_INPUT, deps)
    assert report.final_status == FinalStatus.aborted
    assert "investigate" not in h.calls
    assert any("treated as ON" in text for text in report.limitations)


def test_investigation_crash_becomes_an_aborted_report_not_an_exception():
    h = Harness(raises={"investigate": RuntimeError("llm exploded")})
    report = run(h)
    assert report.final_status == FinalStatus.aborted
    assert any("llm exploded" in text for text in report.limitations)
    assert h.reports == [report]


def test_proposal_builder_crash_is_aborted():
    h = Harness(raises={"build_proposal": ValueError("bad yaml")})
    assert run(h).final_status == FinalStatus.aborted


def test_approval_step_crash_is_treated_as_denial():
    h = Harness(raises={"request_approval": EOFError()})
    report = run(h)
    assert report.final_status == FinalStatus.no_action_taken
    assert "execute" not in h.calls


def test_executor_crash_is_action_failed():
    h = Harness(raises={"execute": RuntimeError("adapter blew up")})
    report = run(h)
    assert report.final_status == FinalStatus.action_failed
    assert report.action is None


def test_verifier_crash_leaves_recovery_unverified():
    h = Harness(raises={"verify": TimeoutError("prometheus")})
    report = run(h)
    assert report.final_status == FinalStatus.mitigation_unverified
    assert any("unverified" in text for text in report.limitations)


def test_preflight_findings_become_limitations_and_a_crash_does_not_stop_the_run():
    h = Harness(preflight=["opensearch is unreachable; falling back to docker logs"])
    report = run(h)
    assert "opensearch is unreachable; falling back to docker logs" in report.limitations
    assert report.final_status == FinalStatus.resolved

    h2 = Harness()
    deps = h2.deps()

    def broken():
        raise RuntimeError("probe failed")

    deps.preflight_fn = broken
    report2 = run_incident(VALID_INPUT, deps)
    assert report2.final_status == FinalStatus.resolved
    assert any("Preflight failed" in text for text in report2.limitations)


def test_report_writer_crash_still_returns_the_report():
    h = Harness(raises={"report": OSError("disk full")})
    report = run(h)
    assert report.final_status == FinalStatus.resolved
    assert any("Writing the report failed" in text for text in report.limitations)


def test_incident_input_object_is_accepted_as_well_as_a_dict():
    from oncall.contracts import IncidentInput

    h = Harness()
    report = run(h, IncidentInput.model_validate(VALID_INPUT))
    assert report.final_status == FinalStatus.resolved


def test_summary_is_generated_from_fields_and_mentions_status():
    report = run(Harness())
    assert "Checkout failing" in report.summary
    assert "svc-a" in report.summary
    assert report.summary.endswith("Final status: resolved.")


@pytest.mark.parametrize(
    "action,verification,expected",
    [
        (None, None, FinalStatus.no_action_taken),
        (make_action("failed"), None, FinalStatus.action_failed),
        (make_action("timeout"), None, FinalStatus.action_failed),
        (make_action("rejected", reason="COOLDOWN"), None, FinalStatus.action_failed),
        (make_action("rejected", reason="KILL_SWITCH_ACTIVE"), None, FinalStatus.aborted),
        (make_action(), make_verification("recovered"), FinalStatus.resolved),
        (make_action(), make_verification("not_recovered"), FinalStatus.not_resolved),
        (make_action(), make_verification("unverified"), FinalStatus.mitigation_unverified),
        (make_action(), None, FinalStatus.mitigation_unverified),
        (make_action(simulated=True), make_verification("recovered"), FinalStatus.mitigation_unverified),
        (make_action(simulated=True), make_verification("unverified"), FinalStatus.mitigation_unverified),
        (make_action(simulated=True), make_verification("not_recovered"), FinalStatus.not_resolved),
    ],
)
def test_final_status_table_from_plan_13_9(action, verification, expected):
    assert derive_final_status(action, verification) == expected
