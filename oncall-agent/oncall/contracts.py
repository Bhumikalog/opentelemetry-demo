from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Annotated, Any

from pydantic import (
    AfterValidator,
    BaseModel,
    ConfigDict,
    Field,
    PlainSerializer,
    model_validator,
)


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _to_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _format_utc(value: datetime) -> str:
    return _to_utc(value).strftime("%Y-%m-%dT%H:%M:%SZ")


UtcDatetime = Annotated[
    datetime,
    AfterValidator(_to_utc),
    PlainSerializer(_format_utc, return_type=str, when_used="json"),
]

EvidenceId = Annotated[str, Field(pattern=r"^ev-\d{3,}$")]
IncidentId = Annotated[str, Field(min_length=1, max_length=64, pattern=r"^[A-Za-z0-9_.-]+$")]
ProposalId = Annotated[str, Field(pattern=r"^p-[A-Za-z0-9]+$")]
ApprovalId = Annotated[str, Field(pattern=r"^apr-[A-Za-z0-9]+$")]
ActionId = Annotated[str, Field(pattern=r"^act-[A-Za-z0-9]+$")]

EVIDENCE_DATA_MAX_CHARS = 4000


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class SymptomType(str, Enum):
    errors = "errors"
    latency = "latency"
    unavailable = "unavailable"
    resource = "resource"
    other = "other"


class Severity(str, Enum):
    SEV1 = "SEV1"
    SEV2 = "SEV2"
    SEV3 = "SEV3"
    SEV4 = "SEV4"


class IncidentSource(str, Enum):
    manual = "manual"
    alert = "alert"
    harness = "harness"


class EvidenceSource(str, Enum):
    prometheus = "prometheus"
    jaeger = "jaeger"
    opensearch = "opensearch"
    docker_logs = "docker_logs"
    docker_inspect = "docker_inspect"
    static_config = "static_config"
    verification = "verification"


class ToolErrorCode(str, Enum):
    TIMEOUT = "TIMEOUT"
    UNAVAILABLE = "UNAVAILABLE"
    BAD_ARGS = "BAD_ARGS"
    NOT_FOUND = "NOT_FOUND"
    BACKEND_ERROR = "BACKEND_ERROR"
    BUDGET_EXCEEDED = "BUDGET_EXCEEDED"


RETRYABLE_TOOL_ERRORS = frozenset({ToolErrorCode.TIMEOUT, ToolErrorCode.UNAVAILABLE})


class DiagnosisStatus(str, Enum):
    diagnosed = "diagnosed"
    insufficient_evidence = "insufficient_evidence"


class Confidence(str, Enum):
    low = "low"
    medium = "medium"
    high = "high"


CONFIDENCE_RANK = {Confidence.low: 0, Confidence.medium: 1, Confidence.high: 2}


class Risk(str, Enum):
    low = "low"
    medium = "medium"
    high = "high"


class ApprovalState(str, Enum):
    PROPOSED = "PROPOSED"
    AWAITING_APPROVAL = "AWAITING_APPROVAL"
    APPROVED = "APPROVED"
    DENIED = "DENIED"
    EXPIRED = "EXPIRED"
    CONSUMED = "CONSUMED"


class ApprovalDecision(str, Enum):
    approved = "approved"
    denied = "denied"
    expired = "expired"


class ActionStatus(str, Enum):
    succeeded = "succeeded"
    failed = "failed"
    rejected = "rejected"
    timeout = "timeout"


class RejectionReason(str, Enum):
    NO_APPROVAL = "NO_APPROVAL"
    APPROVAL_DENIED = "APPROVAL_DENIED"
    APPROVAL_EXPIRED = "APPROVAL_EXPIRED"
    APPROVAL_ALREADY_USED = "APPROVAL_ALREADY_USED"
    HASH_MISMATCH = "HASH_MISMATCH"
    DUPLICATE = "DUPLICATE"
    COOLDOWN = "COOLDOWN"
    KILL_SWITCH_ACTIVE = "KILL_SWITCH_ACTIVE"
    UNSUPPORTED_ACTION = "UNSUPPORTED_ACTION"
    INVALID_PARAMS = "INVALID_PARAMS"
    DRAFT_RUNBOOK = "DRAFT_RUNBOOK"


class VerificationStatus(str, Enum):
    recovered = "recovered"
    not_recovered = "not_recovered"
    unverified = "unverified"


class FinalStatus(str, Enum):
    resolved = "resolved"
    not_resolved = "not_resolved"
    mitigation_unverified = "mitigation_unverified"
    no_action_taken = "no_action_taken"
    action_failed = "action_failed"
    rejected_input = "rejected_input"
    aborted = "aborted"


class TimeRange(Strict):
    start: UtcDatetime
    end: UtcDatetime

    @model_validator(mode="after")
    def _ordered(self) -> TimeRange:
        if self.end < self.start:
            raise ValueError("end must not be before start")
        return self


class IncidentInput(Strict):
    incident_id: IncidentId
    title: str = Field(min_length=1, max_length=200)
    description: str | None = Field(default=None, max_length=4000)
    reported_service: str | None = None
    symptom_type: SymptomType = SymptomType.other
    detected_at: UtcDatetime = Field(default_factory=utcnow)
    severity: Severity | None = None
    source: IncidentSource | None = None
    labels: dict[str, str] = Field(default_factory=dict)


class Evidence(Strict):
    id: EvidenceId
    source: EvidenceSource
    tool: str = Field(min_length=1)
    query: str
    service: str | None = None
    time_range: TimeRange | None = None
    collected_at: UtcDatetime
    summary: str = Field(max_length=500)
    data: Any = None
    truncated: bool = False

    @model_validator(mode="after")
    def _bounded_data(self) -> Evidence:
        size = len(json.dumps(self.data, default=str))
        if size > EVIDENCE_DATA_MAX_CHARS:
            raise ValueError(
                f"data is {size} chars when serialized; the limit is {EVIDENCE_DATA_MAX_CHARS}"
            )
        return self


class ToolCall(Strict):
    name: str = Field(min_length=1)
    args: dict[str, Any] = Field(default_factory=dict)


class ToolError(Strict):
    code: ToolErrorCode
    message: str
    retryable: bool

    @classmethod
    def of(cls, code: ToolErrorCode, message: str) -> ToolError:
        return cls(code=code, message=message, retryable=code in RETRYABLE_TOOL_ERRORS)


class ToolResult(Strict):
    ok: bool
    tool: str = Field(min_length=1)
    evidence: list[Evidence] = Field(default_factory=list)
    error: ToolError | None = None
    duration_ms: int = Field(default=0, ge=0)
    cached: bool = False

    @model_validator(mode="after")
    def _ok_matches_error(self) -> ToolResult:
        if self.ok and self.error is not None:
            raise ValueError("ok=true must not carry an error")
        if not self.ok and self.error is None:
            raise ValueError("ok=false requires an error")
        return self


class Fact(Strict):
    statement: str = Field(min_length=1)
    evidence_ids: list[EvidenceId] = Field(min_length=1)


class AlternativeHypothesis(Strict):
    hypothesis: str = Field(min_length=1)
    why_less_likely: str = Field(min_length=1)


class Contradiction(Strict):
    description: str = Field(min_length=1)
    evidence_ids: list[EvidenceId] = Field(default_factory=list)


class Diagnosis(Strict):
    status: DiagnosisStatus
    affected_services: list[str] = Field(default_factory=list)
    suspected_root_cause_service: str | None = None
    root_cause_hypothesis: str = Field(default="", max_length=800)
    facts: list[Fact] = Field(default_factory=list)
    assumptions: list[str] = Field(default_factory=list)
    alternative_hypotheses: list[AlternativeHypothesis] = Field(default_factory=list)
    contradictions: list[Contradiction] = Field(default_factory=list)
    missing_evidence: list[str] = Field(default_factory=list)
    confidence: Confidence
    llm_confidence: Confidence | None = None
    recommended_runbook_id: str | None = None
    checker_notes: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def _diagnosed_needs_alternatives(self) -> Diagnosis:
        if self.status == DiagnosisStatus.diagnosed and not self.alternative_hypotheses:
            raise ValueError("a diagnosed result needs at least one alternative hypothesis")
        return self


class SuccessCriteria(Strict):
    services: list[str] = Field(min_length=1)
    max_error_rate: float = Field(ge=0, le=1)
    min_requests: int = Field(ge=1)
    wait_seconds: int = Field(ge=0)
    window_seconds: int = Field(ge=1)


def compute_proposal_hash(
    *,
    incident_id: str,
    runbook_id: str,
    action_type: str,
    target_service: str,
    params: dict[str, Any],
    adapter: str,
) -> str:
    canonical = json.dumps(
        {
            "incident_id": incident_id,
            "runbook_id": runbook_id,
            "action_type": action_type,
            "target_service": target_service,
            "params": params,
            "adapter": adapter,
        },
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )
    return "sha256:" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()


class RemediationProposal(Strict):
    proposal_id: ProposalId
    incident_id: IncidentId
    runbook_id: str = Field(min_length=1)
    action_type: str = Field(min_length=1)
    target_service: str = Field(min_length=1)
    params: dict[str, Any] = Field(default_factory=dict)
    rationale: str
    expected_effect: str
    risk: Risk
    adapter: str = Field(min_length=1)
    simulated: bool
    success_criteria: SuccessCriteria
    proposal_hash: str = Field(pattern=r"^sha256:")
    created_at: UtcDatetime = Field(default_factory=utcnow)

    def compute_hash(self) -> str:
        return compute_proposal_hash(
            incident_id=self.incident_id,
            runbook_id=self.runbook_id,
            action_type=self.action_type,
            target_service=self.target_service,
            params=self.params,
            adapter=self.adapter,
        )

    def hash_is_valid(self) -> bool:
        return self.proposal_hash == self.compute_hash()


class ApprovalRecord(Strict):
    approval_id: ApprovalId
    proposal_id: ProposalId
    proposal_hash: str = Field(pattern=r"^sha256:")
    decision: ApprovalDecision
    approver: str = Field(min_length=1)
    channel: str = Field(min_length=1)
    decided_at: UtcDatetime
    expires_at: UtcDatetime
    consumed_at: UtcDatetime | None = None
    reason: str | None = None


class ActionRequest(Strict):
    action_id: ActionId
    idempotency_key: str = Field(min_length=1)
    proposal_id: ProposalId
    approval_id: ApprovalId
    action_type: str = Field(min_length=1)
    target_service: str = Field(min_length=1)
    params: dict[str, Any] = Field(default_factory=dict)


class ActionResult(Strict):
    action_id: ActionId
    status: ActionStatus
    rejection_reason: RejectionReason | None = None
    adapter: str = Field(min_length=1)
    simulated: bool
    started_at: UtcDatetime
    finished_at: UtcDatetime
    api_status_code: int | None = None
    response_summary: str = Field(default="", max_length=500)
    error: str | None = None

    @model_validator(mode="after")
    def _reason_matches_status(self) -> ActionResult:
        if self.status == ActionStatus.rejected and self.rejection_reason is None:
            raise ValueError("a rejected action needs a rejection_reason")
        if self.status != ActionStatus.rejected and self.rejection_reason is not None:
            raise ValueError("rejection_reason is only valid when status is rejected")
        return self


class VerificationCriteria(Strict):
    max_error_rate: float = Field(ge=0, le=1)
    min_requests: int = Field(ge=1)


class ServiceVerification(Strict):
    service: str = Field(min_length=1)
    requests: int = Field(ge=0)
    error_rate: float = Field(ge=0, le=1)
    passed: bool
    evidence_id: EvidenceId | None = None


class VerificationResult(Strict):
    status: VerificationStatus
    checked_at: UtcDatetime
    window: TimeRange | None = None
    criteria: VerificationCriteria | None = None
    per_service: list[ServiceVerification] = Field(default_factory=list)
    baseline_before: dict[str, float] = Field(default_factory=dict)
    notes: list[str] = Field(default_factory=list)


class TimelineEntry(Strict):
    at: UtcDatetime
    state: str = Field(min_length=1)
    detail: str = ""


class AgentMeta(Strict):
    model: str = ""
    provider: str = ""
    tool_calls: int = Field(default=0, ge=0)
    llm_calls: int = Field(default=0, ge=0)
    duration_s: float = Field(default=0.0, ge=0)
    adapter: str = ""
    version: str = ""


REPORT_SCHEMA_ID = "oncall-agent/proposed/v1"


class IncidentReport(Strict):
    report_schema: str = REPORT_SCHEMA_ID
    incident: IncidentInput | None = None
    final_status: FinalStatus
    summary: str
    diagnosis: Diagnosis | None = None
    prevention: list[str] = Field(default_factory=list)
    evidence: list[Evidence] = Field(default_factory=list)
    proposal: RemediationProposal | None = None
    approval: ApprovalRecord | None = None
    action: ActionResult | None = None
    verification: VerificationResult | None = None
    timeline: list[TimelineEntry] = Field(default_factory=list)
    limitations: list[str] = Field(default_factory=list)
    agent_meta: AgentMeta = Field(default_factory=AgentMeta)
    generated_at: UtcDatetime = Field(default_factory=utcnow)


SCHEMA_MODELS: dict[str, type[BaseModel]] = {
    "incident_input": IncidentInput,
    "evidence": Evidence,
    "tool_call": ToolCall,
    "tool_result": ToolResult,
    "diagnosis": Diagnosis,
    "remediation_proposal": RemediationProposal,
    "approval_record": ApprovalRecord,
    "action_request": ActionRequest,
    "action_result": ActionResult,
    "verification_result": VerificationResult,
    "incident_report.proposed": IncidentReport,
}

PROPOSED_REPORT_TITLE = "PROPOSED by team (not official)"


def write_json_schemas(out_dir: str | Path) -> list[Path]:
    target = Path(out_dir)
    target.mkdir(parents=True, exist_ok=True)
    written: list[Path] = []
    for name, model in SCHEMA_MODELS.items():
        schema = model.model_json_schema()
        if name == "incident_report.proposed":
            schema["title"] = PROPOSED_REPORT_TITLE
        path = target / f"{name}.schema.json"
        path.write_text(json.dumps(schema, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        written.append(path)
    return written


if __name__ == "__main__":
    schemas_dir = Path(__file__).resolve().parent.parent / "schemas"
    for written_path in write_json_schemas(schemas_dir):
        print(written_path)
