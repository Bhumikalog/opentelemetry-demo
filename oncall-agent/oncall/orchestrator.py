from __future__ import annotations

import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Callable

from pydantic import ValidationError

from oncall import __version__
from oncall.config import Settings
from oncall.contracts import (
    CONFIDENCE_RANK,
    ActionResult,
    ActionStatus,
    AgentMeta,
    ApprovalDecision,
    ApprovalRecord,
    Confidence,
    Diagnosis,
    DiagnosisStatus,
    Evidence,
    FinalStatus,
    IncidentInput,
    IncidentReport,
    RejectionReason,
    RemediationProposal,
    TimelineEntry,
    VerificationResult,
    VerificationStatus,
    utcnow,
)


@dataclass
class ProposalRequest:
    runbook_id: str
    params: dict[str, Any] = field(default_factory=dict)
    rationale: str = ""


@dataclass
class ProposalRejection:
    reason: str


@dataclass
class InvestigationOutcome:
    evidence: list[Evidence]
    diagnosis: Diagnosis
    proposal_request: ProposalRequest | None = None
    tool_calls: int = 0
    llm_calls: int = 0
    limitations: list[str] = field(default_factory=list)


InvestigateFn = Callable[[IncidentInput], InvestigationOutcome]
BuildProposalFn = Callable[
    [IncidentInput, Diagnosis, ProposalRequest], RemediationProposal | ProposalRejection
]
RequestApprovalFn = Callable[[RemediationProposal], ApprovalRecord]
ExecuteFn = Callable[[RemediationProposal, ApprovalRecord], ActionResult]
VerifyFn = Callable[[RemediationProposal, ActionResult], VerificationResult]
ReportFn = Callable[[IncidentReport], Any]
PreflightFn = Callable[[], list[str]]


@dataclass
class Deps:
    settings: Settings
    investigate_fn: InvestigateFn
    build_proposal_fn: BuildProposalFn
    request_approval_fn: RequestApprovalFn
    execute_fn: ExecuteFn
    verify_fn: VerifyFn
    report_fn: ReportFn
    killswitch_fn: Callable[[], bool]
    clock: Callable[[], datetime] = utcnow
    preflight_fn: PreflightFn | None = None
    registry: Any = None
    llm: Any = None


class _Stop(Exception):
    def __init__(self, status: FinalStatus):
        super().__init__(status.value)
        self.status = status


def derive_final_status(action: ActionResult | None, verification: VerificationResult | None) -> FinalStatus:
    if action is None:
        return FinalStatus.no_action_taken
    if action.rejection_reason == RejectionReason.KILL_SWITCH_ACTIVE:
        return FinalStatus.aborted
    if action.status != ActionStatus.succeeded:
        return FinalStatus.action_failed
    verification_status = verification.status if verification is not None else VerificationStatus.unverified
    if verification_status == VerificationStatus.not_recovered:
        return FinalStatus.not_resolved
    if action.simulated:
        return FinalStatus.mitigation_unverified
    if verification_status == VerificationStatus.recovered:
        return FinalStatus.resolved
    return FinalStatus.mitigation_unverified


def _proposal_allowed(diagnosis: Diagnosis, request: ProposalRequest | None) -> str | None:
    if diagnosis.status != DiagnosisStatus.diagnosed:
        return "The diagnosis has insufficient evidence, so no remediation was proposed."
    if not diagnosis.suspected_root_cause_service:
        return "No root-cause service was identified, so no remediation was proposed."
    if CONFIDENCE_RANK[diagnosis.confidence] < CONFIDENCE_RANK[Confidence.medium]:
        return "Diagnosis confidence is below medium, so no remediation was proposed."
    if request is None:
        return "The investigation did not request a runbook, so no remediation was proposed."
    return None


def build_summary(
    final_status: FinalStatus,
    incident: IncidentInput | None,
    diagnosis: Diagnosis | None,
    proposal: RemediationProposal | None,
    approval: ApprovalRecord | None,
    action: ActionResult | None,
    verification: VerificationResult | None,
) -> str:
    title = incident.title if incident else "the submitted input"
    parts: list[str] = []
    if final_status == FinalStatus.rejected_input:
        return f"The incident input was rejected as invalid, so no investigation was run for {title}."
    if diagnosis is None:
        parts.append(f"Incident: {title}. No diagnosis was produced.")
    elif diagnosis.status == DiagnosisStatus.diagnosed:
        service = diagnosis.suspected_root_cause_service or "an unidentified service"
        parts.append(
            f"Incident: {title}. Suspected root cause: {service} "
            f"(confidence {diagnosis.confidence.value})."
        )
    else:
        parts.append(f"Incident: {title}. The evidence was insufficient for a confident diagnosis.")
    if proposal is not None:
        simulated = " (simulated)" if proposal.simulated else ""
        parts.append(
            f"Proposed {proposal.action_type} on {proposal.target_service}{simulated}; "
            f"approval: {approval.decision.value if approval else 'not requested'}."
        )
    if action is not None:
        detail = action.rejection_reason.value if action.rejection_reason else action.status.value
        parts.append(f"Action result: {detail}.")
    if verification is not None:
        parts.append(f"Verification: {verification.status.value}.")
    parts.append(f"Final status: {final_status.value}.")
    return " ".join(parts)


class _Run:
    def __init__(self, deps: Deps):
        self.deps = deps
        self.started = time.monotonic()
        self.timeline: list[TimelineEntry] = []
        self.limitations: list[str] = []
        self.incident: IncidentInput | None = None
        self.evidence: list[Evidence] = []
        self.diagnosis: Diagnosis | None = None
        self.proposal: RemediationProposal | None = None
        self.approval: ApprovalRecord | None = None
        self.action: ActionResult | None = None
        self.verification: VerificationResult | None = None
        self.tool_calls = 0
        self.llm_calls = 0
        self._proposal_request: ProposalRequest | None = None

    def mark(self, state: str, detail: str = "") -> None:
        try:
            now = self.deps.clock()
        except Exception:
            now = utcnow()
        self.timeline.append(TimelineEntry(at=now, state=state, detail=detail))

    def limit(self, text: str) -> None:
        if text not in self.limitations:
            self.limitations.append(text)

    def check_kill(self, where: str) -> None:
        try:
            active = bool(self.deps.killswitch_fn())
        except Exception as exc:
            active = True
            self.limit(f"Kill switch check failed ({exc}); treated as ON.")
        if active:
            self.mark("ABORTED", f"Kill switch active before {where}.")
            self.limit("Kill switch was active; the run was stopped before completion.")
            raise _Stop(FinalStatus.aborted)

    def investigate(self) -> None:
        assert self.incident is not None
        self.mark("INVESTIGATING")
        try:
            outcome = self.deps.investigate_fn(self.incident)
        except Exception as exc:
            self.mark("ABORTED", f"Investigation failed: {type(exc).__name__}: {exc}")
            self.limit(f"Investigation failed with {type(exc).__name__}: {exc}")
            raise _Stop(FinalStatus.aborted) from exc
        self.evidence = list(outcome.evidence)
        self.diagnosis = outcome.diagnosis
        self.tool_calls = outcome.tool_calls
        self.llm_calls = outcome.llm_calls
        for text in outcome.limitations:
            self.limit(text)
        self.mark(
            "DIAGNOSED",
            f"status={outcome.diagnosis.status.value}, confidence={outcome.diagnosis.confidence.value}",
        )
        self._proposal_request = outcome.proposal_request

    def propose(self) -> None:
        assert self.incident is not None and self.diagnosis is not None
        blocked = _proposal_allowed(self.diagnosis, self._proposal_request)
        if blocked:
            self.limit(blocked)
            self.mark("NO_PROPOSAL", blocked)
            raise _Stop(FinalStatus.no_action_taken)
        try:
            result = self.deps.build_proposal_fn(self.incident, self.diagnosis, self._proposal_request)
        except Exception as exc:
            self.mark("ABORTED", f"Building the proposal failed: {type(exc).__name__}: {exc}")
            self.limit(f"Building the proposal failed with {type(exc).__name__}: {exc}")
            raise _Stop(FinalStatus.aborted) from exc
        if isinstance(result, ProposalRejection):
            self.limit(f"No proposal: {result.reason}")
            self.mark("NO_PROPOSAL", result.reason)
            raise _Stop(FinalStatus.no_action_taken)
        self.proposal = result
        if result.simulated:
            self.limit("The action adapter is simulated: no real system was changed.")
        self.mark("PROPOSED", f"{result.runbook_id} -> {result.action_type} on {result.target_service}")

    def approve(self) -> None:
        assert self.proposal is not None
        self.mark("AWAITING_APPROVAL", self.proposal.proposal_id)
        try:
            record = self.deps.request_approval_fn(self.proposal)
        except Exception as exc:
            self.mark("DENIED", f"Approval step failed, treated as denial: {type(exc).__name__}: {exc}")
            self.limit(f"Approval step failed with {type(exc).__name__}: {exc}; treated as a denial.")
            raise _Stop(FinalStatus.no_action_taken) from exc
        self.approval = record
        if record.decision != ApprovalDecision.approved:
            self.mark(record.decision.value.upper(), record.reason or "")
            raise _Stop(FinalStatus.no_action_taken)
        if record.proposal_id != self.proposal.proposal_id or record.proposal_hash != self.proposal.proposal_hash:
            self.mark("DENIED", "Approval does not match the proposal.")
            self.limit("The approval record did not match the proposal; no action was taken.")
            raise _Stop(FinalStatus.no_action_taken)
        self.mark("APPROVED", record.approver)

    def execute(self) -> None:
        assert self.proposal is not None and self.approval is not None
        self.mark("EXECUTING", self.proposal.action_type)
        try:
            self.action = self.deps.execute_fn(self.proposal, self.approval)
        except Exception as exc:
            self.limit(f"Executor raised {type(exc).__name__}: {exc}; no action result was recorded.")
            self.mark("ACTION_FAILED", f"{type(exc).__name__}: {exc}")
            raise _Stop(FinalStatus.action_failed) from exc
        detail = self.action.rejection_reason.value if self.action.rejection_reason else self.action.status.value
        self.mark("ACTION_" + self.action.status.value.upper(), detail)
        if self.action.simulated:
            self.limit("The action was simulated: no real system was changed.")
        if self.action.rejection_reason == RejectionReason.KILL_SWITCH_ACTIVE:
            self.limit("The kill switch blocked the action; nothing was changed.")

    def verify(self) -> None:
        assert self.proposal is not None and self.action is not None
        if self.action.status != ActionStatus.succeeded:
            return
        self.mark("VERIFYING")
        try:
            self.verification = self.deps.verify_fn(self.proposal, self.action)
        except Exception as exc:
            self.limit(f"Verification failed with {type(exc).__name__}: {exc}; recovery is unverified.")
            self.mark("VERIFIED", "unverified (verification error)")
            return
        self.mark("VERIFIED", self.verification.status.value)

    def preflight(self) -> None:
        if self.deps.preflight_fn is None:
            return
        self.mark("PREFLIGHT")
        try:
            for text in self.deps.preflight_fn():
                self.limit(text)
        except Exception as exc:
            self.limit(f"Preflight failed with {type(exc).__name__}: {exc}")

    def build_report(self, final_status: FinalStatus) -> IncidentReport:
        settings = self.deps.settings
        adapter = self.proposal.adapter if self.proposal else settings.adapter
        return IncidentReport(
            incident=self.incident,
            final_status=final_status,
            summary=build_summary(
                final_status,
                self.incident,
                self.diagnosis,
                self.proposal,
                self.approval,
                self.action,
                self.verification,
            ),
            diagnosis=self.diagnosis,
            evidence=self.evidence,
            proposal=self.proposal,
            approval=self.approval,
            action=self.action,
            verification=self.verification,
            timeline=self.timeline,
            limitations=self.limitations,
            agent_meta=AgentMeta(
                model=settings.llm_model,
                provider=settings.llm_provider,
                tool_calls=self.tool_calls,
                llm_calls=self.llm_calls,
                duration_s=round(time.monotonic() - self.started, 3),
                adapter=adapter,
                version=__version__,
            ),
        )


def _validate_incident(run: _Run, raw_input: Any) -> bool:
    if isinstance(raw_input, IncidentInput):
        run.incident = raw_input
        return True
    try:
        run.incident = IncidentInput.model_validate(raw_input)
        return True
    except ValidationError as exc:
        for error in exc.errors():
            location = ".".join(str(part) for part in error["loc"]) or "input"
            run.limit(f"Invalid input at {location}: {error['msg']}")
    except Exception as exc:
        run.limit(f"Invalid input: {type(exc).__name__}: {exc}")
    return False


def _write_report(run: _Run, report: IncidentReport) -> None:
    try:
        run.deps.report_fn(report)
    except Exception as exc:
        report.limitations.append(f"Writing the report failed with {type(exc).__name__}: {exc}")


def run_incident(raw_input: Any, deps: Deps) -> IncidentReport:
    run = _Run(deps)
    final_status = FinalStatus.aborted
    try:
        run.mark("RECEIVED")
        if not _validate_incident(run, raw_input):
            run.mark("REJECTED_INPUT")
            final_status = FinalStatus.rejected_input
        else:
            assert run.incident is not None
            run.mark("VALIDATED", run.incident.incident_id)
            try:
                run.check_kill("preflight")
                run.preflight()
                run.check_kill("investigation")
                run.investigate()
                run.check_kill("proposal")
                run.propose()
                run.check_kill("approval")
                run.approve()
                run.execute()
                run.verify()
                final_status = derive_final_status(run.action, run.verification)
            except _Stop as stop:
                final_status = stop.status
    except Exception as exc:
        run.limit(f"Unexpected error: {type(exc).__name__}: {exc}")
        run.mark("ABORTED", f"Unexpected error: {type(exc).__name__}")
        final_status = FinalStatus.aborted
    run.mark("REPORTED", final_status.value)
    report = run.build_report(final_status)
    _write_report(run, report)
    return report
