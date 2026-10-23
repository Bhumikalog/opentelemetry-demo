from __future__ import annotations

import time
from datetime import datetime, timedelta
from typing import Callable

from oncall.config import Settings
from oncall.contracts import (
    ActionResult,
    ActionStatus,
    RemediationProposal,
    ServiceVerification,
    TimeRange,
    VerificationCriteria,
    VerificationResult,
    VerificationStatus,
    utcnow,
)
from oncall.telemetry import TelemetryTools

CONTRADICTION_SLACK = 1.1
CONTRADICTION_ABSOLUTE = 1.0


def _unverified(
    now: datetime,
    notes: list[str],
    criteria: VerificationCriteria | None = None,
    window: TimeRange | None = None,
    per_service: list[ServiceVerification] | None = None,
) -> VerificationResult:
    return VerificationResult(
        status=VerificationStatus.unverified,
        checked_at=now,
        window=window,
        criteria=criteria,
        per_service=per_service or [],
        notes=notes,
    )


def verify(
    proposal: RemediationProposal,
    action: ActionResult,
    settings: Settings,
    *,
    tools: TelemetryTools | None = None,
    sleep: Callable[[float], None] = time.sleep,
    clock: Callable[[], datetime] = utcnow,
) -> VerificationResult:
    try:
        return _verify(proposal, action, settings, tools, sleep, clock)
    except Exception as exc:
        return _unverified(
            _safe_now(clock),
            [f"Verification could not be completed ({type(exc).__name__}); recovery is unverified."],
        )


def _safe_now(clock: Callable[[], datetime]) -> datetime:
    try:
        return clock()
    except Exception:
        return utcnow()


def _verify(
    proposal: RemediationProposal,
    action: ActionResult,
    settings: Settings,
    tools: TelemetryTools | None,
    sleep: Callable[[float], None],
    clock: Callable[[], datetime],
) -> VerificationResult:
    success = proposal.success_criteria
    criteria = VerificationCriteria(max_error_rate=success.max_error_rate, min_requests=success.min_requests)
    now = clock()
    if action.status != ActionStatus.succeeded:
        return _unverified(
            now,
            [f"The action status was '{action.status.value}', so no recovery check was run and no telemetry was queried."],
            criteria,
        )

    own_tools = tools is None
    tools = tools or TelemetryTools(settings, clock=clock)
    try:
        return _check(proposal, action, criteria, tools, sleep, clock)
    finally:
        if own_tools:
            tools.close()


def _check(
    proposal: RemediationProposal,
    action: ActionResult,
    criteria: VerificationCriteria,
    tools: TelemetryTools,
    sleep: Callable[[float], None],
    clock: Callable[[], datetime],
) -> VerificationResult:
    success = proposal.success_criteria
    now = clock()
    if action.finished_at > now:
        return _unverified(
            now,
            ["The action finished after the verifier's clock time; the clocks disagree, so the window is unusable."],
            criteria,
        )
    remaining = success.wait_seconds - (now - action.finished_at).total_seconds()
    if remaining > 0:
        sleep(remaining)
    now = clock()
    available = int((now - action.finished_at).total_seconds())
    window_seconds = min(success.window_seconds, available)
    if window_seconds < 1:
        return _unverified(now, ["No telemetry window exists after the action finished; recovery is unverified."], criteria)
    window = TimeRange(start=now - timedelta(seconds=window_seconds), end=now)

    notes: list[str] = []
    per_service: list[ServiceVerification] = []
    failing: list[str] = []
    passing: list[str] = []
    problems: list[str] = []
    seen: set[str] = set()
    for service in success.services:
        if service in seen:
            continue
        seen.add(service)
        outcome = _check_service(tools, service, window_seconds, window.start, criteria, notes)
        if outcome is None:
            problems.append(service)
            continue
        entry, verdict = outcome
        per_service.append(entry)
        if verdict == "failing":
            failing.append(service)
        elif verdict == "passing":
            passing.append(service)
        else:
            problems.append(service)

    if failing:
        computed = VerificationStatus.not_recovered
    elif problems or not passing or len(passing) != len(seen):
        computed = VerificationStatus.unverified
    else:
        computed = VerificationStatus.recovered

    notes.insert(0, f"Measured the {window_seconds} s after the action finished, ending at the check time.")
    if failing:
        notes.append("Continued failure: " + ", ".join(failing) + " still exceed the allowed error rate.")
    if problems and not failing:
        notes.append("Not every service could be confirmed: " + ", ".join(problems) + ".")
    if computed == VerificationStatus.recovered:
        notes.append("Recovered means post-action telemetry meets the criteria; no pre-action baseline was compared.")

    status = computed
    if tools.data_origin != "live":
        status = VerificationStatus.unverified
        notes.append(
            f"The telemetry came from a {tools.data_origin.replace('_', ' ')}, not the running system, so it cannot "
            f"establish real recovery; the computed result would have been '{computed.value}'."
        )
    if action.simulated:
        notes.append("The action was simulated: no real change was made, so telemetry does not show an effect of the action.")

    return VerificationResult(
        status=status,
        checked_at=now,
        window=window,
        criteria=criteria,
        per_service=per_service,
        notes=notes,
    )


def _check_service(
    tools: TelemetryTools,
    service: str,
    window_seconds: int,
    window_start: datetime,
    criteria: VerificationCriteria,
    notes: list[str],
) -> tuple[ServiceVerification, str] | None:
    result = tools.service_window(service, window_seconds)
    if not result.ok or not result.evidence or not isinstance(result.evidence[0].data, dict):
        error = result.error
        detail = f"{error.code.value}: {error.message}" if error else "no usable evidence"
        notes.append(f"{service}: telemetry query failed ({detail}).")
        return None
    data = result.evidence[0].data
    requests, errors = data.get("requests"), data.get("errors")
    if requests is None or errors is None:
        notes.append(f"{service}: no span-metric data in the window.")
        return ServiceVerification(service=service, requests=0, error_rate=0.0, passed=False), "problem"
    stamp = data.get("sample_timestamp")
    if isinstance(stamp, (int, float)) and stamp < window_start.timestamp():
        notes.append(f"{service}: the newest sample is older than the verification window, so it is stale.")
        return None
    if errors > requests * CONTRADICTION_SLACK + CONTRADICTION_ABSOLUTE:
        notes.append(f"{service}: {errors} errors exceed {requests} requests; the metrics contradict each other.")
        return None
    rate = data.get("error_rate") or 0.0
    enough = requests >= criteria.min_requests
    healthy = enough and rate <= criteria.max_error_rate
    entry = ServiceVerification(service=service, requests=requests, error_rate=rate, passed=healthy)
    if not enough:
        notes.append(f"{service}: only {requests} requests in the window; at least {criteria.min_requests} are needed.")
        return entry, "problem"
    if not healthy:
        notes.append(f"{service}: error rate {rate:.4f} is above the allowed {criteria.max_error_rate}.")
        return entry, "failing"
    return entry, "passing"
