import json
import re
import subprocess
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path

import httpx
import pytest

from oncall import telemetry
from oncall.config import Limits, load_settings
from oncall.contracts import EVIDENCE_DATA_MAX_CHARS, EvidenceSource, ToolErrorCode, ToolResult
from oncall.telemetry import (
    DOCKER_INSPECT_FORMAT,
    FIXTURE_ROOT,
    SHOP_SERVICES,
    EvidenceIds,
    FixtureError,
    TelemetryTools,
    fixture_tools,
    load_fixture_set,
)

PACKAGE = Path(__file__).resolve().parent.parent / "oncall"
NOW = datetime(2026, 1, 15, 10, 0, 0, tzinfo=timezone.utc)
SETTINGS = load_settings({"OPENSEARCH_URL": "http://opensearch.test:9200"})
SCENARIOS = ["healthy", "payment_errors", "payment_unreachable", "conflicting", "logs_missing"]
FIXTURE_SERVICES = ["frontend", "checkout", "payment", "cart"]
BAD_SERVICES = [
    "flagd",
    "flagd-ui",
    "jaeger",
    "prometheus",
    "opensearch",
    "grafana",
    "otel-collector",
    "",
    " payment",
    "PAYMENT",
    "payment; drop",
    "payment' OR 1=1",
    "../payment",
    None,
    5,
    ["payment"],
]
BAD_MINUTES = [0, -1, 61, 10_000, True, "5", 5.0, None]
BAD_LIMITS = [0, -1, 51, True, "5", 2.5, None]


def fx(scenario, phase="before", **kwargs):
    kwargs.setdefault("clock", lambda: NOW)
    return fixture_tools(SETTINGS, scenario, phase, **kwargs)


def live(handler, settings=SETTINGS, **kwargs):
    calls = []

    def recording(request):
        calls.append(request)
        return handler(request)

    tools = TelemetryTools(settings, transport=httpx.MockTransport(recording), clock=lambda: NOW, **kwargs)
    return tools, calls


def prom_body(rows):
    return {"status": "success", "data": {"resultType": "vector", "result": rows}}


def prom_row(value, service=None, ts=1768471200):
    return {"metric": {"service_name": service} if service else {}, "value": [ts, str(value)]}


def assert_error(result, code):
    assert result.ok is False
    assert result.error is not None
    assert result.error.code == code
    assert result.evidence == []
    ToolResult.model_validate_json(result.model_dump_json())


def assert_valid(result):
    assert result.ok is True, result.error
    assert result.evidence
    ToolResult.model_validate_json(result.model_dump_json())
    for item in result.evidence:
        assert len(item.summary) <= 500
        assert len(json.dumps(item.data, default=str)) <= EVIDENCE_DATA_MAX_CHARS


def run_all(tools, svc="payment"):
    return [
        tools.top_error_services(15),
        tools.service_health(svc, 15),
        tools.error_traces(svc, 15, 5),
        tools.error_logs(svc, 15, 5),
        tools.container_health(svc),
    ]


class TestSuccessfulParsing:
    def test_every_tool_returns_valid_evidence_from_fixtures(self):
        tools = fx("payment_errors")
        results = run_all(tools)
        assert [r.tool for r in results] == [
            "top_error_services",
            "service_health",
            "error_traces",
            "error_logs",
            "container_health",
        ]
        for result in results:
            assert_valid(result)

    def test_evidence_sources_and_ids(self):
        results = run_all(fx("payment_errors"))
        sources = [r.evidence[0].source for r in results]
        assert sources == [
            EvidenceSource.prometheus,
            EvidenceSource.prometheus,
            EvidenceSource.jaeger,
            EvidenceSource.opensearch,
            EvidenceSource.docker_inspect,
        ]
        ids = [r.evidence[0].id for r in results]
        assert ids == ["ev-001", "ev-002", "ev-003", "ev-004", "ev-005"]

    def test_custom_evidence_id_allocator_is_used(self):
        tools = fx("payment_errors", next_evidence_id=EvidenceIds(start=40))
        assert tools.top_error_services(15).evidence[0].id == "ev-040"
        assert tools.service_health("payment", 15).evidence[0].id == "ev-041"

    def test_top_error_services_ranks_by_error_count(self):
        data = fx("payment_errors").top_error_services(15).evidence[0].data
        assert [row["service"] for row in data["services"]] == ["payment", "checkout", "frontend", "cart"]
        assert data["services"][0] == {"service": "payment", "errors": 290, "requests": 580, "error_rate": 0.5}

    def test_service_health_numbers(self):
        evidence = fx("payment_errors").service_health("payment", 15).evidence[0]
        assert evidence.service == "payment"
        assert evidence.data["requests"] == 580
        assert evidence.data["errors"] == 290
        assert evidence.data["error_rate"] == 0.5
        assert evidence.data["p95_ms"] == 60.0
        assert evidence.data["requests_per_second"] == round(580 / 900, 4)
        assert evidence.time_range is not None
        assert (evidence.time_range.end - evidence.time_range.start).total_seconds() == 900

    def test_absent_error_series_means_zero_errors(self):
        data = fx("healthy").service_health("cart", 15).evidence[0].data
        assert data["requests"] == 900
        assert data["errors"] == 0
        assert data["error_rate"] == 0.0

    def test_root_cause_service_is_the_deepest_erroring_service(self):
        data = fx("payment_errors").error_traces("frontend", 15, 10).evidence[0].data
        assert data["deepest_service_counts"] == {"payment": 3}
        assert data["error_trace_count"] == 3
        assert {row["deepest_service"] for row in data["error_traces"]} == {"payment"}
        assert all(row["error_spans"] == 3 for row in data["error_traces"])

    def test_symptom_is_not_always_the_cause(self):
        tools = fx("payment_unreachable")
        health = tools.service_health("payment", 15).evidence[0].data
        traces = tools.error_traces("frontend", 15, 10).evidence[0].data
        assert health["error_rate"] < 0.01
        assert traces["deepest_service_counts"] == {"checkout": 2}
        assert "connection to downstream failed" in traces["error_traces"][0]["status_message"]

    def test_healthy_scenario_reports_no_errors_without_inventing_a_cause(self):
        tools = fx("healthy")
        top = tools.top_error_services(15).evidence[0]
        assert "cart" not in [row["service"] for row in top.data["services"]]
        traces = tools.error_traces("frontend", 15, 10).evidence[0]
        assert traces.data["error_trace_count"] == 0
        assert "No error spans" in traces.summary
        logs = tools.error_logs("payment", 15, 10).evidence[0]
        assert logs.data["returned"] == 0

    def test_error_logs_parse_rows_inside_the_window(self):
        evidence = fx("payment_errors").error_logs("payment", 15, 5).evidence[0]
        assert evidence.data["returned"] == 2
        assert evidence.data["logs"][0]["severity"] == "ERROR"
        assert evidence.data["logs"][0]["message"] == "synthetic: request handling failed"
        assert evidence.data["outside_window"] == 0

    def test_error_logs_drop_rows_older_than_the_window(self):
        late = fx("payment_errors", clock=lambda: NOW.replace(hour=12))
        evidence = late.error_logs("payment", 15, 5).evidence[0]
        assert evidence.data["returned"] == 0
        assert evidence.data["outside_window"] == 2
        assert "fell outside the window" in evidence.summary

    def test_container_health_reads_only_the_fixed_fields(self):
        evidence = fx("payment_errors").container_health("payment").evidence[0]
        assert set(evidence.data) == {"status", "health", "restart_count", "started_at", "oom_killed", "data_origin"}
        assert evidence.data["status"] == "running"

    def test_all_scenarios_load_and_every_fixture_service_answers(self):
        for scenario in SCENARIOS:
            tools = fx(scenario)
            assert_valid(tools.top_error_services(15))
            for service in FIXTURE_SERVICES:
                assert_valid(tools.service_health(service, 15))
                assert_valid(tools.error_traces(service, 15, 5))
                assert_valid(tools.container_health(service))
                if scenario != "logs_missing":
                    assert_valid(tools.error_logs(service, 15, 5))

    def test_service_window_uses_seconds(self):
        tools, calls = live(
            lambda request: httpx.Response(200, json=prom_body([prom_row(250)])),
        )
        result = tools.service_window("payment", 95)
        assert_valid(result)
        assert all("[95s]" in call.url.params["query"] for call in calls)
        assert result.evidence[0].data["window_seconds"] == 95


class TestEmptyResults:
    def test_prometheus_empty_vector_is_not_an_error(self):
        tools, _ = live(lambda request: httpx.Response(200, json=prom_body([])))
        top = tools.top_error_services(10)
        assert_valid(top)
        assert "No span-metric data" in top.evidence[0].summary
        health = tools.service_health("payment", 10)
        assert_valid(health)
        assert "No span-metric data" in health.evidence[0].summary
        assert health.evidence[0].data["requests"] is None

    def test_traffic_without_errors_is_reported_as_such(self):
        def handler(request):
            query = request.url.params["query"]
            return httpx.Response(200, json=prom_body([] if "STATUS_CODE_ERROR" in query else [prom_row(50, "payment")]))

        tools, _ = live(handler)
        result = tools.top_error_services(10)
        assert_valid(result)
        assert "No error spans" in result.evidence[0].summary

    def test_jaeger_without_traces(self):
        tools, _ = live(lambda request: httpx.Response(200, json={"result": {}}))
        result = tools.error_traces("payment", 10, 5)
        assert_valid(result)
        assert "No traces" in result.evidence[0].summary

    def test_opensearch_without_rows(self):
        payload = {"schema": _log_schema(), "datarows": [], "total": 0, "size": 0}
        tools, _ = live(lambda request: httpx.Response(200, json=payload))
        result = tools.error_logs("payment", 10, 5)
        assert_valid(result)
        assert "No error logs" in result.evidence[0].summary


def _log_schema():
    return [
        {"name": "@timestamp", "type": "timestamp"},
        {"name": "severity.text", "type": "string"},
        {"name": "body", "type": "string"},
        {"name": "traceId", "type": "string"},
    ]


class TestArgumentValidation:
    @pytest.mark.parametrize("bad", BAD_SERVICES)
    def test_unknown_service_is_rejected_before_any_access(self, bad):
        docker_calls = []

        def runner(args, timeout):
            docker_calls.append(args)
            raise AssertionError("docker must not run")

        tools, calls = live(
            lambda request: pytest.fail("no request expected"),
            allow_docker_inspect=True,
            docker_runner=runner,
        )
        for result in (
            tools.service_health(bad, 10),
            tools.error_traces(bad, 10, 5),
            tools.error_logs(bad, 10, 5),
            tools.container_health(bad),
            tools.service_window(bad, 60),
        ):
            assert_error(result, ToolErrorCode.BAD_ARGS)
        assert calls == []
        assert docker_calls == []

    @pytest.mark.parametrize("bad", BAD_MINUTES)
    def test_bad_minutes(self, bad):
        tools, calls = live(lambda request: pytest.fail("no request expected"))
        assert_error(tools.top_error_services(bad), ToolErrorCode.BAD_ARGS)
        assert_error(tools.service_health("payment", bad), ToolErrorCode.BAD_ARGS)
        assert_error(tools.error_traces("payment", bad, 5), ToolErrorCode.BAD_ARGS)
        assert_error(tools.error_logs("payment", bad, 5), ToolErrorCode.BAD_ARGS)
        assert calls == []

    @pytest.mark.parametrize("bad", BAD_LIMITS)
    def test_bad_limit(self, bad):
        tools, calls = live(lambda request: pytest.fail("no request expected"))
        assert_error(tools.error_traces("payment", 10, bad), ToolErrorCode.BAD_ARGS)
        assert_error(tools.error_logs("payment", 10, bad), ToolErrorCode.BAD_ARGS)
        assert calls == []

    @pytest.mark.parametrize("bad", [0, -5, 3601, True, "60", 1.5, None])
    def test_bad_window_seconds(self, bad):
        tools, calls = live(lambda request: pytest.fail("no request expected"))
        assert_error(tools.service_window("payment", bad), ToolErrorCode.BAD_ARGS)
        assert calls == []

    def test_error_message_does_not_echo_the_bad_value(self):
        tools, _ = live(lambda request: pytest.fail("no request expected"))
        result = tools.service_health("payment' OR 1=1", 10)
        assert "OR 1=1" not in result.error.message

    def test_custom_allowlist_is_enforced_and_must_be_safe(self):
        tools, _ = live(lambda request: httpx.Response(200, json=prom_body([])), services={"payment"})
        assert_error(tools.service_health("checkout", 10), ToolErrorCode.BAD_ARGS)
        assert_valid(tools.service_health("payment", 10))
        with pytest.raises(ValueError):
            TelemetryTools(SETTINGS, services={'pay"ment'})

    def test_allowlist_has_no_infrastructure_names(self):
        assert not {"jaeger", "prometheus", "opensearch", "grafana", "otel-collector"} & SHOP_SERVICES
        assert not [name for name in SHOP_SERVICES if "flag" in name]
        assert len(SHOP_SERVICES) == 18

    def test_services_outside_the_allowlist_never_appear_in_a_query(self):
        tools, calls = live(lambda request: httpx.Response(200, json=prom_body([])))
        tools.top_error_services(10)
        matcher = re.search(r'service_name=~"([^"]+)"', calls[0].url.params["query"]).group(1)
        assert matcher.split("|") == sorted(SHOP_SERVICES)

    def test_unlisted_services_in_a_response_are_ignored(self):
        def handler(request):
            rows = [prom_row(900, "unlisted-service"), prom_row(40, "payment")]
            return httpx.Response(200, json=prom_body(rows))

        tools, _ = live(handler)
        data = tools.top_error_services(10).evidence[0].data
        assert [row["service"] for row in data["services"]] == ["payment"]


class TestFailureMapping:
    HTTP_CALLS = [
        lambda t: t.top_error_services(10),
        lambda t: t.service_health("payment", 10),
        lambda t: t.error_traces("payment", 10, 5),
        lambda t: t.error_logs("payment", 10, 5),
        lambda t: t.service_window("payment", 60),
    ]

    @pytest.mark.parametrize("call", HTTP_CALLS)
    def test_timeout(self, call):
        def handler(request):
            raise httpx.ReadTimeout("slow", request=request)

        tools, _ = live(handler)
        assert_error(call(tools), ToolErrorCode.TIMEOUT)

    @pytest.mark.parametrize("call", HTTP_CALLS)
    def test_connection_failure(self, call):
        def handler(request):
            raise httpx.ConnectError("refused", request=request)

        tools, _ = live(handler)
        result = call(tools)
        assert_error(result, ToolErrorCode.UNAVAILABLE)
        assert result.error.retryable is True

    @pytest.mark.parametrize("status", [502, 503, 504])
    def test_gateway_errors_are_unavailable(self, status):
        tools, _ = live(lambda request: httpx.Response(status))
        assert_error(tools.service_health("payment", 10), ToolErrorCode.UNAVAILABLE)

    @pytest.mark.parametrize("call", HTTP_CALLS)
    @pytest.mark.parametrize("status", [400, 404, 500])
    def test_http_errors_are_backend_errors(self, call, status):
        tools, _ = live(lambda request: httpx.Response(status, text="boom"))
        result = call(tools)
        assert_error(result, ToolErrorCode.BACKEND_ERROR)
        assert result.error.retryable is False
        assert "boom" not in result.error.message

    @pytest.mark.parametrize("call", HTTP_CALLS)
    def test_invalid_json(self, call):
        tools, _ = live(lambda request: httpx.Response(200, text="<html>not json</html>"))
        assert_error(call(tools), ToolErrorCode.BACKEND_ERROR)

    @pytest.mark.parametrize("body", [[], "text", 7, None, {"status": "success"}, {"status": "error", "errorType": "bad_data"}])
    def test_prometheus_wrong_shape(self, body):
        tools, _ = live(lambda request: httpx.Response(200, json=body))
        assert_error(tools.service_health("payment", 10), ToolErrorCode.BACKEND_ERROR)
        assert_error(tools.top_error_services(10), ToolErrorCode.BACKEND_ERROR)

    @pytest.mark.parametrize(
        "rows",
        [
            ["not-a-dict"],
            [{"metric": {}, "value": [1]}],
            [{"metric": "x", "value": [1, "2"]}],
            [{"value": [1, "2"]}],
        ],
    )
    def test_prometheus_bad_rows(self, rows):
        tools, _ = live(lambda request: httpx.Response(200, json=prom_body(rows)))
        assert_error(tools.service_health("payment", 10), ToolErrorCode.BACKEND_ERROR)

    @pytest.mark.parametrize(
        "body",
        [[], {"result": []}, {"result": {"resourceSpans": "x"}}, {"result": {"resourceSpans": ["x"]}},
         {"result": {"resourceSpans": [{"scopeSpans": [{"spans": ["x"]}]}]}}],
    )
    def test_jaeger_wrong_shape(self, body):
        tools, _ = live(lambda request: httpx.Response(200, json=body))
        assert_error(tools.error_traces("payment", 10, 5), ToolErrorCode.BACKEND_ERROR)

    @pytest.mark.parametrize(
        "body",
        [
            [],
            {"datarows": []},
            {"schema": "x", "datarows": []},
            {"schema": _log_schema()},
            {"schema": [{"name": "other"}], "datarows": []},
            {"schema": _log_schema(), "datarows": [["too", "short"]]},
            {"schema": _log_schema(), "datarows": ["x"]},
        ],
    )
    def test_opensearch_wrong_shape(self, body):
        tools, _ = live(lambda request: httpx.Response(200, json=body))
        assert_error(tools.error_logs("payment", 10, 5), ToolErrorCode.BACKEND_ERROR)

    def test_oversized_body_with_content_length(self):
        tools, _ = live(lambda request: httpx.Response(200, content=b"{" + b" " * 250_000 + b"}"))
        result = tools.service_health("payment", 10)
        assert_error(result, ToolErrorCode.BACKEND_ERROR)
        assert "larger than" in result.error.message

    def test_oversized_streamed_body_without_content_length(self):
        valid = json.dumps(prom_body([prom_row(5)])).encode()

        def body():
            yield valid
            for _ in range(30):
                yield b" " * 10_000

        request_seen = []

        def handler(request):
            response = httpx.Response(200, content=body())
            request_seen.append("content-length" in response.headers)
            return response

        tools, _ = live(handler)
        result = tools.service_health("payment", 10)
        assert request_seen and not any(request_seen)
        assert_error(result, ToolErrorCode.BACKEND_ERROR)
        assert "larger than" in result.error.message

    def test_body_limit_comes_from_settings(self):
        small = replace(SETTINGS, limits=Limits(http_max_body_bytes=100))
        tools, _ = live(lambda request: httpx.Response(200, json=prom_body([prom_row(5)] * 20)), settings=small)
        assert_error(tools.service_health("payment", 10), ToolErrorCode.BACKEND_ERROR)

    def test_opensearch_not_configured(self):
        tools, calls = live(
            lambda request: pytest.fail("no request expected"), settings=load_settings({})
        )
        result = tools.error_logs("payment", 10, 5)
        assert_error(result, ToolErrorCode.UNAVAILABLE)
        assert "OPENSEARCH_URL" in result.error.message
        assert calls == []

    def test_latency_failure_does_not_hide_request_numbers(self):
        def handler(request):
            if "histogram_quantile" in request.url.params["query"]:
                return httpx.Response(500)
            return httpx.Response(200, json=prom_body([prom_row(100)]))

        tools, _ = live(handler)
        result = tools.service_health("payment", 10)
        assert_valid(result)
        assert result.evidence[0].data["p95_ms"] is None
        assert "p95 latency unavailable" in result.evidence[0].data["notes"][0]

    def test_nan_latency_becomes_none(self):
        def handler(request):
            if "histogram_quantile" in request.url.params["query"]:
                return httpx.Response(200, json=prom_body([prom_row("NaN")]))
            return httpx.Response(200, json=prom_body([prom_row(100)]))

        tools, _ = live(handler)
        assert tools.service_health("payment", 10).evidence[0].data["p95_ms"] is None

    def test_errors_do_not_leak_credentials_or_paths(self):
        secret_settings = load_settings(
            {"PROMETHEUS_URL": "http://user:hunter2@prom.test:9090", "OPENSEARCH_URL": "http://admin:hunter2@os.test"}
        )

        def handler(request):
            raise httpx.ConnectError("connect to http://user:hunter2@prom.test failed", request=request)

        tools, _ = live(handler, settings=secret_settings)
        for result in (tools.service_health("payment", 10), tools.error_logs("payment", 10, 5)):
            assert "hunter2" not in result.error.message
            assert "/home/" not in result.error.message

    def test_evidence_query_has_no_credentials(self):
        secret_settings = load_settings({"PROMETHEUS_URL": "http://user:hunter2@prom.test:9090"})
        tools, _ = live(lambda request: httpx.Response(200, json=prom_body([prom_row(5)])), settings=secret_settings)
        assert "hunter2" not in tools.service_health("payment", 10).evidence[0].query

    def test_unexpected_exceptions_are_contained(self, monkeypatch):
        tools, _ = live(lambda request: httpx.Response(200, json=prom_body([])))
        monkeypatch.setattr(tools, "_prom_vector", lambda query: 1 / 0)
        result = tools.service_health("payment", 10)
        assert_error(result, ToolErrorCode.BACKEND_ERROR)
        assert "ZeroDivisionError" in result.error.message


class TestPayloadCap:
    def _noisy_traces(self, count):
        spans = [
            {
                "traceId": f"{n:032x}",
                "spanId": f"{n:016x}",
                "parentSpanId": "",
                "name": "x" * 500,
                "startTimeUnixNano": str(n),
                "status": {"code": 2, "message": "m" * 500},
            }
            for n in range(1, count + 1)
        ]
        resource = {"attributes": [{"key": "service.name", "value": {"stringValue": "payment"}}]}
        return {"result": {"resourceSpans": [{"resource": resource, "scopeSpans": [{"spans": spans}]}]}}

    def test_large_trace_response_is_trimmed_and_flagged(self):
        body = self._noisy_traces(50)
        tools, _ = live(lambda request: httpx.Response(200, json=body))
        result = tools.error_traces("payment", 10, 50)
        assert_valid(result)
        evidence = result.evidence[0]
        assert evidence.truncated is True
        assert len(json.dumps(evidence.data, default=str)) <= EVIDENCE_DATA_MAX_CHARS
        assert evidence.data["error_trace_count"] == 50
        assert len(evidence.data["error_traces"]) < 50

    def test_limit_is_respected_and_flagged(self):
        body = self._noisy_traces(40)
        tools, _ = live(lambda request: httpx.Response(200, json=body))
        evidence = tools.error_traces("payment", 10, 3).evidence[0]
        assert len(evidence.data["error_traces"]) == 3
        assert evidence.truncated is True

    def test_settings_can_lower_the_cap(self):
        small = replace(SETTINGS, limits=Limits(tool_output_max_chars=900))
        tools, _ = live(lambda request: httpx.Response(200, json=self._noisy_traces(50)), settings=small)
        evidence = tools.error_traces("payment", 10, 50).evidence[0]
        assert len(json.dumps(evidence.data, default=str)) <= 900
        assert evidence.truncated is True

    def test_log_messages_are_clipped_and_stripped(self):
        row = ["2026-01-15 09:59:00.000000", "ERROR", "line1\nline2\x1b[31m" + "z" * 600, "t"]
        payload = {"schema": _log_schema(), "datarows": [row], "total": 1, "size": 1}
        tools, _ = live(lambda request: httpx.Response(200, json=payload))
        message = tools.error_logs("payment", 10, 5).evidence[0].data["logs"][0]["message"]
        assert len(message) <= 200
        assert "\n" not in message
        assert "\x1b" not in message

    def test_log_with_unparsable_timestamp_is_kept_and_counted(self):
        rows = [["yesterday", "ERROR", "odd", "t"], [None, "ERROR", "odder", "t"], ["2026-01-15T09:59:30Z", "FATAL", {"k": 1}, None]]
        payload = {"schema": _log_schema(), "datarows": rows, "total": 3, "size": 3}
        tools, _ = live(lambda request: httpx.Response(200, json=payload))
        data = tools.error_logs("payment", 10, 5).evidence[0].data
        assert data["returned"] == 3
        assert data["unparsed_timestamps"] == 2
        assert data["logs"][2]["message"] == '{"k": 1}'


class TestTraceParsing:
    def _body(self, spans_by_service):
        resource_spans = []
        for service, spans in spans_by_service.items():
            resource = {"attributes": [{"key": "service.name", "value": {"stringValue": service}}]}
            resource_spans.append({"resource": resource, "scopeSpans": [{"spans": spans}]})
        return {"result": {"resourceSpans": resource_spans}}

    @staticmethod
    def _span(span_id, parent, error, start=1, trace="a" * 32, message="boom"):
        status = {}
        if error == "int":
            status = {"code": 2, "message": message}
        if error == "str":
            status = {"code": "STATUS_CODE_ERROR", "message": message}
        if error == "ok":
            status = {"code": 1}
        return {
            "traceId": trace,
            "spanId": span_id,
            "parentSpanId": parent,
            "name": f"op-{span_id}",
            "startTimeUnixNano": str(start),
            "status": status,
        }

    def test_status_may_be_int_or_string(self):
        body = self._body(
            {
                "frontend": [self._span("1", "", "int", trace="1" * 32), self._span("9", "", "ok", trace="2" * 32)],
                "payment": [self._span("2", "1", "str", trace="1" * 32)],
            }
        )
        tools, _ = live(lambda request: httpx.Response(200, json=body))
        data = tools.error_traces("frontend", 10, 5).evidence[0].data
        assert data["traces_examined"] == 2
        assert data["error_trace_count"] == 1
        assert data["error_traces"][0]["deepest_service"] == "payment"
        assert data["error_traces"][0]["error_spans"] == 2

    def test_errors_in_unlisted_services_are_counted_but_not_blamed(self):
        body = self._body(
            {
                "frontend": [self._span("1", "", "int")],
                "unlisted-service": [self._span("2", "1", "int")],
            }
        )
        tools, _ = live(lambda request: httpx.Response(200, json=body))
        data = tools.error_traces("frontend", 10, 5).evidence[0].data
        assert data["deepest_service_counts"] == {"frontend": 1}
        assert data["ignored_error_spans_in_other_services"] == 1

    def test_parent_cycles_terminate(self):
        body = self._body({"frontend": [self._span("1", "2", "int"), self._span("2", "1", "int")]})
        tools, _ = live(lambda request: httpx.Response(200, json=body))
        assert_valid(tools.error_traces("frontend", 10, 5))

    def test_request_window_and_parameters(self):
        tools, calls = live(lambda request: httpx.Response(200, json={"result": {}}))
        tools.error_traces("payment", 30, 7)
        request = calls[0]
        assert request.method == "GET"
        assert request.url.path == "/jaeger/ui/api/v3/traces"
        params = request.url.params
        assert params["query.service_name"] == "payment"
        assert params["query.num_traces"] == "21"
        assert params["query.start_time_min"] == "2026-01-15T09:30:00Z"
        assert params["query.start_time_max"] == "2026-01-15T10:01:00Z"


class TestDocker:
    @staticmethod
    def _completed(stdout="", stderr="", code=0):
        return subprocess.CompletedProcess(args=[], returncode=code, stdout=stdout, stderr=stderr)

    GOOD = json.dumps(
        {"status": "running", "health": None, "restart_count": 2, "started_at": "2026-01-15T08:00:00Z", "oom_killed": True}
    )

    def tools(self, runner, **kwargs):
        return TelemetryTools(SETTINGS, allow_docker_inspect=True, docker_runner=runner, clock=lambda: NOW, **kwargs)

    def test_disabled_by_default_and_never_runs(self):
        calls = []
        tools = TelemetryTools(SETTINGS, docker_runner=lambda a, t: calls.append(a), clock=lambda: NOW)
        result = tools.container_health("payment")
        assert_error(result, ToolErrorCode.UNAVAILABLE)
        assert "organizers" in result.error.message
        assert calls == []

    def test_exact_command_is_list_form_with_fixed_format(self):
        seen = []

        def runner(args, timeout):
            seen.append((args, timeout))
            return self._completed(self.GOOD)

        result = self.tools(runner).container_health("payment")
        assert_valid(result)
        args, timeout = seen[0]
        assert args == ["docker", "inspect", "--format", DOCKER_INSPECT_FORMAT, "payment"]
        assert timeout > 0
        assert "Env" not in DOCKER_INSPECT_FORMAT
        assert "Config" not in DOCKER_INSPECT_FORMAT
        assert result.evidence[0].data["oom_killed"] is True
        assert result.evidence[0].data["health"] is None
        assert "no healthcheck" in result.evidence[0].summary

    def test_timeout(self):
        def runner(args, timeout):
            raise subprocess.TimeoutExpired(args, timeout)

        assert_error(self.tools(runner).container_health("payment"), ToolErrorCode.TIMEOUT)

    def test_cli_missing(self):
        def runner(args, timeout):
            raise FileNotFoundError("docker")

        assert_error(self.tools(runner).container_health("payment"), ToolErrorCode.UNAVAILABLE)

    def test_container_not_found(self):
        runner = lambda a, t: self._completed(stderr="Error: No such object: payment", code=1)
        result = self.tools(runner).container_health("payment")
        assert_error(result, ToolErrorCode.NOT_FOUND)

    def test_daemon_unreachable(self):
        runner = lambda a, t: self._completed(stderr="Cannot connect to the Docker daemon at unix:///var/run/docker.sock", code=1)
        result = self.tools(runner).container_health("payment")
        assert_error(result, ToolErrorCode.UNAVAILABLE)
        assert "docker.sock" not in result.error.message

    @pytest.mark.parametrize(
        "stdout",
        [
            "not json",
            "[]",
            json.dumps({"status": "running"}),
            json.dumps({"status": "running", "health": None, "restart_count": "x", "started_at": "t", "oom_killed": False}),
            json.dumps({"status": "running", "health": None, "restart_count": 1, "started_at": "t", "oom_killed": "no"}),
            json.dumps({"status": "running", "health": None, "restart_count": 1, "started_at": "t", "oom_killed": False, "env": ["SECRET=1"]}),
            "x" * 5000,
        ],
    )
    def test_malformed_output(self, stdout):
        assert_error(self.tools(lambda a, t: self._completed(stdout)).container_health("payment"), ToolErrorCode.BACKEND_ERROR)

    def test_default_runner_never_uses_a_shell(self, monkeypatch):
        captured = {}

        def fake_run(args, **kwargs):
            captured.update(kwargs, args=args)
            return self._completed(self.GOOD)

        monkeypatch.setattr(telemetry.subprocess, "run", fake_run)
        tools = TelemetryTools(SETTINGS, allow_docker_inspect=True, clock=lambda: NOW)
        assert_valid(tools.container_health("payment"))
        assert captured["shell"] is False
        assert isinstance(captured["args"], list)
        assert captured["timeout"] > 0


class TestFixtures:
    def test_fixture_evidence_is_labelled_synthetic(self):
        for result in run_all(fx("payment_errors")):
            evidence = result.evidence[0]
            assert evidence.summary.startswith("[SYNTHETIC FIXTURE] ")
            assert evidence.data["data_origin"] == "synthetic_fixture"

    def test_live_evidence_is_not_labelled(self):
        tools, _ = live(lambda request: httpx.Response(200, json=prom_body([prom_row(5)])))
        evidence = tools.service_health("payment", 10).evidence[0]
        assert not evidence.summary.startswith("[")
        assert evidence.data["data_origin"] == "live"

    def test_recorded_fixture_gets_its_own_label(self, tmp_path):
        scenario = tmp_path / "recorded" / "before"
        scenario.mkdir(parents=True)
        (tmp_path / "recorded" / "_meta.json").write_text(json.dumps({"synthetic": False, "based_on": "test"}))
        for role, rows in (("calls", [prom_row(10)]), ("errors", []), ("latency", [prom_row(5)])):
            (scenario / f"prometheus.{role}.payment.json").write_text(json.dumps(prom_body(rows)))
        tools = fixture_tools(SETTINGS, "recorded", root=tmp_path, clock=lambda: NOW)
        evidence = tools.service_health("payment", 10).evidence[0]
        assert evidence.summary.startswith("[RECORDED FIXTURE] ")
        assert tools.data_origin == "recorded_fixture"

    def test_fixture_tools_never_point_at_a_real_host(self):
        tools = fx("healthy")
        for url in (tools.settings.prometheus_url, tools.settings.jaeger_url, tools.settings.opensearch_url):
            assert url.endswith(".invalid") or ".fixture.invalid" in url

    def test_missing_signal_gives_unavailable_with_a_relative_path(self):
        result = fx("logs_missing").error_logs("payment", 15, 5)
        assert_error(result, ToolErrorCode.UNAVAILABLE)
        assert "fixtures/telemetry/logs_missing/before/opensearch.logs.payment.json" in result.error.message
        assert "declares this signal as missing" in result.error.message
        assert str(FIXTURE_ROOT) not in result.error.message

    def test_logs_missing_still_returns_metrics_and_traces(self):
        tools = fx("logs_missing")
        assert_valid(tools.top_error_services(15))
        assert_valid(tools.service_health("payment", 15))
        assert_valid(tools.error_traces("payment", 15, 5))

    def test_forgotten_file_is_not_reported_as_deliberate(self, tmp_path):
        base = tmp_path / "gap"
        (base / "before").mkdir(parents=True)
        (base / "_meta.json").write_text(json.dumps({"synthetic": True, "based_on": "test"}))
        tools = fixture_tools(SETTINGS, "gap", root=tmp_path, clock=lambda: NOW)
        result = tools.error_logs("payment", 15, 5)
        assert_error(result, ToolErrorCode.UNAVAILABLE)
        assert "declares" not in result.error.message

    def test_unknown_container_fixture_is_unavailable(self):
        tools = fx("healthy")
        assert_error(tools.container_health("kafka"), ToolErrorCode.UNAVAILABLE)

    def test_unknown_scenario_and_phase(self):
        with pytest.raises(FixtureError):
            load_fixture_set("does_not_exist")
        with pytest.raises(FixtureError):
            load_fixture_set("healthy", "after_recovered")

    def test_missing_meta_is_an_error(self, tmp_path):
        (tmp_path / "nometa" / "before").mkdir(parents=True)
        with pytest.raises(FixtureError, match="_meta.json is missing"):
            load_fixture_set("nometa", root=tmp_path)

    @pytest.mark.parametrize(
        "meta",
        ["not json", "[]", "{}", '{"synthetic": "yes", "based_on": "x"}', '{"synthetic": true}',
         '{"synthetic": true, "based_on": "x", "expected_missing": "logs"}'],
    )
    def test_bad_meta_is_an_error(self, tmp_path, meta):
        (tmp_path / "bad" / "before").mkdir(parents=True)
        (tmp_path / "bad" / "_meta.json").write_text(meta)
        with pytest.raises(FixtureError):
            load_fixture_set("bad", root=tmp_path)

    @pytest.mark.parametrize(
        "scenario,phase",
        [("../x", "before"), ("healthy", ".."), ("/etc", "before"), ("healthy", "/tmp"), ("a/b", "before"),
         ("", "before"), ("healthy", ""), ("HEALTHY", "before"), ("healthy\x00", "before"), (None, "before"), ("healthy", 3)],
    )
    def test_path_traversal_is_rejected_before_any_read(self, scenario, phase, monkeypatch):
        monkeypatch.setattr(Path, "read_text", lambda *a, **k: pytest.fail("read attempted"))
        monkeypatch.setattr(Path, "read_bytes", lambda *a, **k: pytest.fail("read attempted"))
        with pytest.raises(FixtureError):
            load_fixture_set(scenario, phase)

    def test_every_shipped_scenario_declares_itself_synthetic(self):
        for scenario in SCENARIOS:
            meta = json.loads((FIXTURE_ROOT / scenario / "_meta.json").read_text())
            assert meta["synthetic"] is True
            assert "invented" in meta["based_on"]

    def test_fixture_files_are_small_and_valid_json(self):
        files = [path for path in FIXTURE_ROOT.rglob("*.json")]
        assert files
        for path in files:
            assert path.stat().st_size < 20_000
            json.loads(path.read_text())

    def test_fixtures_carry_no_real_looking_identifiers(self):
        pattern = re.compile(r"(\d{1,3}\.){3}\d{1,3}|https?://|@[a-z0-9-]+\.[a-z]{2,}|api[_-]?key|token|secret", re.I)
        for path in FIXTURE_ROOT.rglob("*"):
            if path.is_file():
                assert not pattern.search(path.read_text()), path

    def test_fixture_and_live_paths_parse_identically(self):
        directory = FIXTURE_ROOT / "payment_errors" / "before"

        def raw(name):
            return json.loads((directory / name).read_text())

        def handler(request):
            path = request.url.path
            if path.endswith("/api/v1/query"):
                query = request.url.params["query"]
                found = re.search(r'service_name="([a-z-]+)"', query)
                role = "latency" if "histogram_quantile" in query else ("errors" if "STATUS_CODE_ERROR" in query else "calls")
                return httpx.Response(200, json=raw(f"prometheus.{role}.{found.group(1) if found else 'all'}.json"))
            if path.endswith("/api/v3/traces"):
                return httpx.Response(200, json=raw(f"jaeger.traces.{request.url.params['query.service_name']}.json"))
            service = re.search(r"service.name = '([a-z-]+)'", json.loads(request.content)["query"]).group(1)
            return httpx.Response(200, json=raw(f"opensearch.logs.{service}.json"))

        live_tools, _ = live(handler)
        fixture = fx("payment_errors")
        for call in (
            lambda t: t.top_error_services(15),
            lambda t: t.service_health("payment", 15),
            lambda t: t.error_traces("frontend", 15, 5),
            lambda t: t.error_logs("payment", 15, 5),
        ):
            from_live, from_fixture = call(live_tools).evidence[0], call(fixture).evidence[0]
            assert {k: v for k, v in from_live.data.items() if k != "data_origin"} == {
                k: v for k, v in from_fixture.data.items() if k != "data_origin"
            }
            assert from_fixture.summary == "[SYNTHETIC FIXTURE] " + from_live.summary
            assert from_live.query == from_fixture.query

    def test_conflicting_signals_are_both_returned_unmerged(self):
        tools = fx("conflicting")
        health = tools.service_health("payment", 15).evidence[0]
        traces = tools.error_traces("payment", 15, 5).evidence[0]
        assert health.data["errors"] == 0
        assert traces.data["error_trace_count"] == 2
        assert traces.data["deepest_service_counts"] == {"payment": 2}
        assert health.source != traces.source


class TestSafetyProperties:
    def test_only_read_only_requests_to_known_paths(self):
        recorded = []

        def handler(request):
            recorded.append(request)
            path = request.url.path
            if path == "/_plugins/_ppl":
                return httpx.Response(200, json={"schema": _log_schema(), "datarows": []})
            if path.endswith("/api/v3/traces"):
                return httpx.Response(200, json={"result": {}})
            return httpx.Response(200, json=prom_body([]))

        tools, _ = live(handler)
        tools.top_error_services(10)
        tools.service_health("payment", 10)
        tools.error_traces("payment", 10, 5)
        tools.error_logs("payment", 10, 5)
        tools.service_window("payment", 60)
        assert recorded
        for request in recorded:
            assert request.method in {"GET", "POST"}
            if request.method == "POST":
                assert request.url.path == "/_plugins/_ppl"
                assert request.url.host == "opensearch.test"
            else:
                assert request.url.path in {"/api/v1/query", "/jaeger/ui/api/v3/traces"}
                assert request.url.host in {"localhost"}
        paths = " ".join(str(request.url) for request in recorded).lower()
        assert "feature" not in paths

    def test_ppl_query_is_built_from_allowlisted_values_only(self):
        tools, calls = live(lambda request: httpx.Response(200, json={"schema": _log_schema(), "datarows": []}))
        tools.error_logs("payment", 10, 5)
        query = json.loads(calls[0].content)["query"]
        assert "resource.service.name = 'payment'" in query
        assert query.endswith("fields @timestamp, severity.text, body, traceId")
        assert "head 5" in query

    def test_tools_never_raise_on_garbage(self):
        tools, _ = live(lambda request: httpx.Response(200, json=prom_body([])), allow_docker_inspect=True)
        garbage = [None, object(), [], {}, 1e308, float("nan"), "x" * 10_000, b"bytes"]
        for value in garbage:
            for result in (
                tools.top_error_services(value),
                tools.service_health(value, value),
                tools.error_traces(value, value, value),
                tools.error_logs(value, value, value),
                tools.container_health(value),
                tools.service_window(value, value),
            ):
                assert isinstance(result, ToolResult)
                assert result.ok is False

    def test_modules_contain_no_prohibited_sources(self):
        prohibited = ["/feature/", "flagd", "flag_config", "FLAGS_READ", "docker logs", "docker exec", "shell=True", "os.environ"]
        for name in ("telemetry.py", "verify.py"):
            text = (PACKAGE / name).read_text(encoding="utf-8")
            for needle in prohibited:
                assert needle.lower() not in text.lower(), f"{name} contains {needle!r}"

    def test_docker_is_never_invoked_by_import_or_construction(self, monkeypatch):
        monkeypatch.setattr(telemetry.subprocess, "run", lambda *a, **k: pytest.fail("subprocess used"))
        TelemetryTools(SETTINGS)
        fx("healthy")
