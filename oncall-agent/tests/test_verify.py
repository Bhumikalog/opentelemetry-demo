import re
import time
from datetime import datetime, timedelta, timezone

import httpx
import pytest

from oncall import verify as verify_module
from oncall.config import load_settings
from oncall.contracts import (
    ActionResult,
    ActionStatus,
    FinalStatus,
    RejectionReason,
    RemediationProposal,
    Risk,
    SuccessCriteria,
    VerificationResult,
    VerificationStatus,
    compute_proposal_hash,
)
from oncall.orchestrator import derive_final_status
from oncall.telemetry import TelemetryTools, fixture_tools
from oncall.verify import verify

FINISHED = datetime(2026, 1, 15, 10, 0, 0, tzinfo=timezone.utc)
SETTINGS = load_settings({})


class Clock:
    def __init__(self, start=FINISHED):
        self.now = start
        self.sleeps = []

    def __call__(self):
        return self.now

    def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.now += timedelta(seconds=seconds)


def make_proposal(services=("payment",), max_error_rate=0.05, min_requests=100, wait=90, window=120, simulated=False):
    fields = dict(
        incident_id="inc-1",
        runbook_id="rb-test",
        action_type="restart",
        target_service="payment",
        params={},
        adapter="organizer",
    )
    return RemediationProposal(
        proposal_id="p-abc123",
        rationale="test",
        expected_effect="test",
        risk=Risk.low,
        simulated=simulated,
        success_criteria=SuccessCriteria(
            services=list(services),
            max_error_rate=max_error_rate,
            min_requests=min_requests,
            wait_seconds=wait,
            window_seconds=window,
        ),
        proposal_hash=compute_proposal_hash(**fields),
        **fields,
    )


def make_action(status=ActionStatus.succeeded, simulated=False, finished=FINISHED):
    reason = RejectionReason.NO_APPROVAL if status == ActionStatus.rejected else None
    return ActionResult(
        action_id="act-1",
        status=status,
        rejection_reason=reason,
        adapter="organizer",
        simulated=simulated,
        started_at=finished - timedelta(seconds=2),
        finished_at=finished,
    )


class Backend:
    def __init__(self, clock, numbers=None, stamp_offset=0):
        self.clock = clock
        self.numbers = numbers or {}
        self.stamp_offset = stamp_offset
        self.requests = []

    def __call__(self, request):
        self.requests.append(request)
        query = request.url.params["query"]
        service = re.search(r'service_name="([a-z-]+)"', query).group(1)
        entry = self.numbers.get(service)
        if isinstance(entry, Exception):
            raise entry
        if entry is None:
            rows = []
        else:
            requests, errors = entry
            value = errors if "STATUS_CODE_ERROR" in query else requests
            rows = [{"metric": {}, "value": [self.clock.now.timestamp() + self.stamp_offset, str(value)]}] if value else []
        return httpx.Response(200, json={"status": "success", "data": {"resultType": "vector", "result": rows}})

    def tools(self):
        return TelemetryTools(SETTINGS, transport=httpx.MockTransport(self), clock=self.clock)


def run(numbers, proposal=None, action=None, clock=None, **backend_kwargs):
    clock = clock or Clock()
    backend = Backend(clock, numbers, **backend_kwargs)
    result = verify(
        proposal or make_proposal(),
        action or make_action(),
        SETTINGS,
        tools=backend.tools(),
        sleep=clock.sleep,
        clock=clock,
    )
    return result, backend, clock


class TestOutcomes:
    def test_recovered(self):
        result, backend, clock = run({"payment": (400, 2)})
        assert result.status == VerificationStatus.recovered
        entry = result.per_service[0]
        assert (entry.service, entry.requests, entry.passed) == ("payment", 400, True)
        assert entry.error_rate == 0.005
        assert result.criteria.max_error_rate == 0.05
        assert result.criteria.min_requests == 100
        assert result.window.start >= FINISHED
        assert result.window.end == result.checked_at
        assert any("no pre-action baseline" in note for note in result.notes)
        VerificationResult.model_validate_json(result.model_dump_json())

    def test_not_recovered_on_continued_failure(self):
        result, _, _ = run({"payment": (410, 200)})
        assert result.status == VerificationStatus.not_recovered
        assert result.per_service[0].passed is False
        assert any("still exceed the allowed error rate" in note for note in result.notes)

    def test_error_rate_exactly_at_the_limit_passes(self):
        result, _, _ = run({"payment": (200, 10)}, proposal=make_proposal(max_error_rate=0.05))
        assert result.status == VerificationStatus.recovered

    def test_error_rate_just_above_the_limit_fails(self):
        result, _, _ = run({"payment": (200, 11)}, proposal=make_proposal(max_error_rate=0.05))
        assert result.status == VerificationStatus.not_recovered

    def test_requests_exactly_at_the_minimum_count(self):
        assert run({"payment": (100, 0)})[0].status == VerificationStatus.recovered

    def test_requests_just_below_the_minimum_are_unverified(self):
        result, _, _ = run({"payment": (99, 0)})
        assert result.status == VerificationStatus.unverified
        assert any("only 99 requests" in note for note in result.notes)

    def test_zero_traffic_is_never_recovered(self):
        result, _, _ = run({})
        assert result.status == VerificationStatus.unverified
        assert result.per_service[0].requests == 0
        assert result.per_service[0].passed is False
        assert any("no span-metric data" in note for note in result.notes)

    def test_failure_with_too_little_traffic_is_not_called_not_recovered(self):
        result, _, _ = run({"payment": (5, 5)})
        assert result.status == VerificationStatus.unverified

    def test_all_services_must_pass(self):
        proposal = make_proposal(services=("payment", "checkout"))
        assert run({"payment": (400, 1), "checkout": (400, 1)}, proposal=proposal)[0].status == VerificationStatus.recovered
        assert run({"payment": (400, 1), "checkout": (50, 0)}, proposal=proposal)[0].status == VerificationStatus.unverified

    def test_mixed_failing_and_thin_traffic_is_not_recovered(self):
        proposal = make_proposal(services=("payment", "checkout"))
        result, _, _ = run({"payment": (400, 200), "checkout": (3, 0)}, proposal=proposal)
        assert result.status == VerificationStatus.not_recovered

    def test_failure_stands_even_if_another_service_query_fails(self):
        proposal = make_proposal(services=("payment", "checkout"))
        numbers = {"payment": (400, 200), "checkout": httpx.ReadTimeout("slow")}
        result, _, _ = run(numbers, proposal=proposal)
        assert result.status == VerificationStatus.not_recovered
        assert any("checkout: telemetry query failed (TIMEOUT" in note for note in result.notes)

    def test_duplicate_services_are_checked_once(self):
        proposal = make_proposal(services=("payment", "payment"))
        result, backend, _ = run({"payment": (400, 1)}, proposal=proposal)
        assert result.status == VerificationStatus.recovered
        assert len(result.per_service) == 1
        assert len(backend.requests) == 2


class TestNoQueryWithoutSuccess:
    @pytest.mark.parametrize(
        "status", [ActionStatus.failed, ActionStatus.rejected, ActionStatus.timeout]
    )
    def test_unsuccessful_action_is_unverified_without_telemetry(self, status):
        result, backend, clock = run({"payment": (400, 0)}, action=make_action(status))
        assert result.status == VerificationStatus.unverified
        assert backend.requests == []
        assert clock.sleeps == []
        assert status.value in result.notes[0]
        assert result.criteria is not None

    def test_action_finishing_in_the_future_is_unusable(self):
        action = make_action(finished=FINISHED + timedelta(minutes=5))
        result, backend, clock = run({"payment": (400, 0)}, action=action)
        assert result.status == VerificationStatus.unverified
        assert backend.requests == []
        assert clock.sleeps == []
        assert "clocks disagree" in result.notes[0]

    def test_no_window_after_the_action(self):
        result, backend, _ = run({"payment": (400, 0)}, proposal=make_proposal(wait=0))
        assert result.status == VerificationStatus.unverified
        assert backend.requests == []
        assert "No telemetry window" in result.notes[0]


class TestFailuresAndStaleness:
    @pytest.mark.parametrize(
        "error,code",
        [
            (httpx.ReadTimeout("slow"), "TIMEOUT"),
            (httpx.ConnectError("refused"), "UNAVAILABLE"),
        ],
    )
    def test_backend_failure_is_unverified(self, error, code):
        result, _, _ = run({"payment": error})
        assert result.status == VerificationStatus.unverified
        assert result.per_service == []
        assert any(code in note for note in result.notes)

    def test_malformed_backend_response_is_unverified(self):
        clock = Clock()
        tools = TelemetryTools(
            SETTINGS, transport=httpx.MockTransport(lambda request: httpx.Response(200, text="nope")), clock=clock
        )
        result = verify(make_proposal(), make_action(), SETTINGS, tools=tools, sleep=clock.sleep, clock=clock)
        assert result.status == VerificationStatus.unverified
        assert any("BACKEND_ERROR" in note for note in result.notes)

    def test_stale_samples_are_unverified(self):
        result, _, _ = run({"payment": (400, 0)}, stamp_offset=-3600)
        assert result.status == VerificationStatus.unverified
        assert any("stale" in note for note in result.notes)

    def test_contradictory_metrics_are_unverified(self):
        result, _, _ = run({"payment": (100, 500)})
        assert result.status == VerificationStatus.unverified
        assert any("contradict" in note for note in result.notes)

    def test_unknown_target_service_is_unverified(self):
        proposal = make_proposal(services=("payment", "not-a-known-service"))
        result, _, _ = run({"payment": (400, 0)}, proposal=proposal)
        assert result.status == VerificationStatus.unverified
        assert any("BAD_ARGS" in note for note in result.notes)

    def test_verify_never_raises(self):
        clock = Clock()

        def broken_sleep(seconds):
            raise RuntimeError("sleep failed")

        tools = Backend(clock, {"payment": (400, 0)}).tools()
        result = verify(make_proposal(), make_action(), SETTINGS, tools=tools, sleep=broken_sleep, clock=clock)
        assert result.status == VerificationStatus.unverified
        assert "RuntimeError" in result.notes[0]

    def test_a_broken_clock_still_yields_a_result(self):
        def clock():
            raise RuntimeError("no clock")

        result = verify(make_proposal(), make_action(), SETTINGS, tools=Backend(Clock()).tools(), clock=clock)
        assert result.status == VerificationStatus.unverified


class TestTimeHandling:
    def test_sleeps_once_for_the_configured_wait(self):
        started = time.monotonic()
        result, _, clock = run({"payment": (400, 0)}, proposal=make_proposal(wait=90))
        assert clock.sleeps == [90]
        assert time.monotonic() - started < 2
        assert result.checked_at == FINISHED + timedelta(seconds=90)

    def test_only_the_remaining_wait_is_slept(self):
        clock = Clock(FINISHED + timedelta(seconds=30))
        _, _, clock = run({"payment": (400, 0)}, clock=clock)
        assert clock.sleeps == [60]

    def test_no_sleep_if_the_wait_already_passed(self):
        clock = Clock(FINISHED + timedelta(seconds=200))
        _, _, clock = run({"payment": (400, 0)}, clock=clock)
        assert clock.sleeps == []

    def test_window_is_capped_at_the_criteria_window(self):
        clock = Clock(FINISHED + timedelta(seconds=600))
        result, backend, _ = run({"payment": (400, 0)}, proposal=make_proposal(wait=0, window=120), clock=clock)
        assert all("[120s]" in request.url.params["query"] for request in backend.requests)
        assert result.window.start == clock.now - timedelta(seconds=120)
        assert result.window.start >= FINISHED

    def test_window_never_reaches_before_the_action_finished(self):
        result, backend, _ = run({"payment": (400, 0)}, proposal=make_proposal(wait=45, window=300))
        assert all("[45s]" in request.url.params["query"] for request in backend.requests)
        assert result.window.start == FINISHED

    def test_queries_ask_for_fresh_increase_not_lifetime_counters(self):
        _, backend, _ = run({"payment": (400, 0)})
        for request in backend.requests:
            assert "increase(" in request.url.params["query"]
            assert request.method == "GET"


class TestHonesty:
    @pytest.mark.parametrize(
        "phase,computed",
        [
            ("after_recovered", "recovered"),
            ("after_still_failing", "not_recovered"),
            ("after_no_traffic", "unverified"),
        ],
    )
    def test_synthetic_fixture_telemetry_is_always_unverified(self, phase, computed):
        clock = Clock()
        tools = fixture_tools(SETTINGS, "payment_errors", phase, clock=clock)
        proposal = make_proposal(services=("checkout", "payment"))
        result = verify(proposal, make_action(), SETTINGS, tools=tools, sleep=clock.sleep, clock=clock)
        assert result.status == VerificationStatus.unverified
        assert any("synthetic fixture" in note and f"'{computed}'" in note for note in result.notes)
        assert {entry.service for entry in result.per_service} == {"checkout", "payment"}

    def test_fixture_numbers_are_kept_for_explanation(self):
        clock = Clock()
        tools = fixture_tools(SETTINGS, "payment_errors", "after_recovered", clock=clock)
        result = verify(make_proposal(), make_action(), SETTINGS, tools=tools, sleep=clock.sleep, clock=clock)
        assert result.per_service[0].requests == 400
        assert result.per_service[0].passed is True

    def test_recorded_telemetry_is_not_live_either(self):
        clock = Clock()
        tools = Backend(clock, {"payment": (400, 0)}).tools()
        tools.data_origin = "recorded_fixture"
        result = verify(make_proposal(), make_action(), SETTINGS, tools=tools, sleep=clock.sleep, clock=clock)
        assert result.status == VerificationStatus.unverified
        assert any("recorded fixture" in note for note in result.notes)

    def test_simulated_action_is_flagged_and_final_status_is_capped(self):
        result, _, _ = run({"payment": (400, 0)}, action=make_action(simulated=True), proposal=make_proposal(simulated=True))
        assert result.status == VerificationStatus.recovered
        assert any("action was simulated" in note for note in result.notes)
        assert derive_final_status(make_action(simulated=True), result) == FinalStatus.mitigation_unverified

    def test_real_action_with_live_recovery_can_resolve(self):
        result, _, _ = run({"payment": (400, 0)})
        assert derive_final_status(make_action(), result) == FinalStatus.resolved

    def test_failure_maps_to_not_resolved(self):
        result, _, _ = run({"payment": (400, 300)})
        assert derive_final_status(make_action(), result) == FinalStatus.not_resolved

    def test_unverified_maps_to_mitigation_unverified(self):
        result, _, _ = run({})
        assert derive_final_status(make_action(), result) == FinalStatus.mitigation_unverified

    def test_fixture_recovery_can_never_resolve(self):
        clock = Clock()
        tools = fixture_tools(SETTINGS, "payment_errors", "after_recovered", clock=clock)
        result = verify(make_proposal(), make_action(), SETTINGS, tools=tools, sleep=clock.sleep, clock=clock)
        assert derive_final_status(make_action(), result) != FinalStatus.resolved


class TestWiring:
    def test_default_tools_are_built_from_settings_and_closed(self, monkeypatch):
        clock = Clock()
        built = []

        class Spy(TelemetryTools):
            def __init__(self, settings, **kwargs):
                built.append(settings)
                super().__init__(
                    settings, transport=httpx.MockTransport(Backend(clock, {"payment": (400, 0)})), **kwargs
                )
                self.closed = False

            def close(self):
                self.closed = True
                super().close()

        holder = []
        monkeypatch.setattr(verify_module, "TelemetryTools", lambda settings, **kw: holder.append(Spy(settings, **kw)) or holder[-1])
        result = verify(make_proposal(), make_action(), SETTINGS, sleep=clock.sleep, clock=clock)
        assert result.status == VerificationStatus.recovered
        assert built == [SETTINGS]
        assert holder[0].closed is True

    def test_supplied_tools_are_not_closed(self):
        clock = Clock()
        tools = Backend(clock, {"payment": (400, 0)}).tools()
        verify(make_proposal(), make_action(), SETTINGS, tools=tools, sleep=clock.sleep, clock=clock)
        assert tools._http.is_closed is False

    def test_binds_to_the_orchestrator_signature(self):
        from functools import partial

        clock = Clock()
        tools = Backend(clock, {"payment": (400, 0)}).tools()
        verify_fn = partial(verify, settings=SETTINGS, tools=tools, sleep=clock.sleep, clock=clock)
        result = verify_fn(make_proposal(), make_action())
        assert result.status == VerificationStatus.recovered
