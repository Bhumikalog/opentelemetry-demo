from __future__ import annotations

import json
import math
import re
import subprocess
import time
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable

import httpx

from oncall.config import PROJECT_ROOT, Settings
from oncall.contracts import (
    EVIDENCE_DATA_MAX_CHARS,
    Evidence,
    EvidenceSource,
    TimeRange,
    ToolError,
    ToolErrorCode,
    ToolResult,
    utcnow,
)

SHOP_SERVICES = frozenset(
    {
        "accounting",
        "ad",
        "cart",
        "checkout",
        "currency",
        "email",
        "fraud-detection",
        "frontend",
        "frontend-proxy",
        "frontend-web",
        "image-provider",
        "kafka",
        "load-generator",
        "payment",
        "product-catalog",
        "quote",
        "recommendation",
        "shipping",
    }
)

FIXTURE_ROOT = PROJECT_ROOT / "fixtures" / "telemetry"

MIN_MINUTES = 1
MAX_MINUTES = 60
MIN_LIMIT = 1
MAX_LIMIT = 50
TOP_SERVICES = 10
TRACE_FETCH_MULTIPLIER = 3
TRACE_FETCH_CAP = 60
MAX_SPANS_PARSED = 20_000
MAX_DEPTH_STEPS = 100
MAX_DOCKER_OUTPUT_CHARS = 2_000
LOG_SEVERITIES = ("ERROR", "Error", "error", "FATAL", "Fatal", "fatal")

DATA_ORIGINS = ("live", "synthetic_fixture", "recorded_fixture")
ORIGIN_LABELS = {
    "synthetic_fixture": "[SYNTHETIC FIXTURE] ",
    "recorded_fixture": "[RECORDED FIXTURE] ",
}

UNAVAILABLE_HTTP_STATUSES = frozenset({502, 503, 504})
DOCKER_INSPECT_FORMAT = (
    '{"status":{{json .State.Status}},'
    '"health":{{if .State.Health}}{{json .State.Health.Status}}{{else}}null{{end}},'
    '"restart_count":{{json .RestartCount}},'
    '"started_at":{{json .State.StartedAt}},'
    '"oom_killed":{{json .State.OOMKilled}}}'
)
DOCKER_FIELDS = ("status", "health", "restart_count", "started_at", "oom_killed")
DOCKER_TIMEOUT_S = 8.0

# Allowed characters of a service name, scenario or phase: lowercase letters, digits, underscore, hyphen.
_SAFE_NAME = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")
# Any ASCII control character; replaced by a space so backend text cannot break out of one line.
_CONTROL_CHARS = re.compile(r"[\x00-\x1f\x7f]+")
# Pulls the service name out of a PromQL matcher such as service_name="payment".
_PROM_SERVICE = re.compile(r'service_name="([a-z0-9-]+)"')
# Pulls the service name out of the PPL filter resource.service.name = 'payment'.
_PPL_SERVICE = re.compile(r"resource\.service\.name = '([a-z0-9-]+)'")

DockerRunner = Callable[[list[str], float], "subprocess.CompletedProcess[str]"]


class FixtureError(ValueError):
    pass


class FixtureMissing(Exception):
    pass


class _Failure(Exception):
    def __init__(self, code: ToolErrorCode, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass
class _Draft:
    source: EvidenceSource
    query: str
    summary: str
    data: dict[str, Any]
    service: str | None = None
    window_seconds: int | None = None
    truncated: bool = False


@dataclass
class _Sample:
    labels: dict[str, str]
    value: float | None
    timestamp: float | None


class EvidenceIds:
    def __init__(self, start: int = 1):
        self._last = start - 1

    def __call__(self) -> str:
        self._last += 1
        return f"ev-{self._last:03d}"


def _clip(text: Any, limit: int) -> str:
    cleaned = _CONTROL_CHARS.sub(" ", str(text)).strip()
    if len(cleaned) <= limit:
        return cleaned
    return cleaned[: max(limit - 1, 0)] + "…"


def _number(raw: Any) -> float | None:
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return None
    if math.isnan(value) or math.isinf(value):
        return None
    return value


def _ratio(errors: float | None, requests: float | None) -> float | None:
    if requests is None or requests <= 0:
        return None
    return round(min((errors or 0.0) / requests, 1.0), 4)


def _plural(count: int, noun: str) -> str:
    return f"{count} {noun}" if count == 1 else f"{count} {noun}s"


def _is_error_status(status: Any) -> bool:
    if not isinstance(status, dict):
        return False
    code = status.get("code")
    return code == 2 or code == "STATUS_CODE_ERROR"


def _resource_service(resource: Any) -> str | None:
    if not isinstance(resource, dict):
        return None
    for attribute in resource.get("attributes") or []:
        if isinstance(attribute, dict) and attribute.get("key") == "service.name":
            value = attribute.get("value")
            if isinstance(value, dict) and isinstance(value.get("stringValue"), str):
                return value["stringValue"]
    return None


def _fit_data(data: dict[str, Any], cap: int) -> tuple[dict[str, Any], bool]:
    trimmed = False
    data = dict(data)
    while len(json.dumps(data, default=str)) > cap:
        longest = max(
            (key for key, value in data.items() if isinstance(value, list) and value),
            key=lambda key: len(json.dumps(data[key], default=str)),
            default=None,
        )
        if longest is None:
            return {"dropped": "payload exceeded the evidence size limit"}, True
        data[longest] = data[longest][:-1]
        trimmed = True
    return data, trimmed


def _parse_timestamp(raw: Any) -> datetime | None:
    if not isinstance(raw, str):
        return None
    try:
        parsed = datetime.fromisoformat(raw.strip().replace(" ", "T"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _run_docker(args: list[str], timeout: float) -> "subprocess.CompletedProcess[str]":
    return subprocess.run(args, capture_output=True, text=True, timeout=timeout, check=False, shell=False)


class TelemetryTools:
    def __init__(
        self,
        settings: Settings,
        *,
        transport: httpx.BaseTransport | None = None,
        services: frozenset[str] | set[str] = SHOP_SERVICES,
        allow_docker_inspect: bool = False,
        docker_runner: DockerRunner | None = None,
        data_origin: str = "live",
        clock: Callable[[], datetime] = utcnow,
        next_evidence_id: Callable[[], str] | None = None,
    ):
        if data_origin not in DATA_ORIGINS:
            raise ValueError(f"data_origin must be one of {DATA_ORIGINS}")
        bad = sorted(name for name in services if not _SAFE_NAME.match(name))
        if bad:
            raise ValueError(f"unsafe service names in the allowlist: {bad}")
        self.settings = settings
        self.services = frozenset(services)
        self.allow_docker_inspect = allow_docker_inspect
        self.data_origin = data_origin
        self._clock = clock
        self._next_id = next_evidence_id or EvidenceIds()
        self._docker_runner = docker_runner or _run_docker
        limits = settings.limits
        self._http = httpx.Client(
            transport=transport,
            timeout=httpx.Timeout(
                connect=limits.http_connect_timeout_s,
                read=limits.http_read_timeout_s,
                write=limits.http_read_timeout_s,
                pool=limits.http_connect_timeout_s,
            ),
            follow_redirects=False,
            trust_env=False,
        )

    def close(self) -> None:
        self._http.close()

    def __enter__(self) -> "TelemetryTools":
        return self

    def __exit__(self, *exc_info: Any) -> None:
        self.close()

    def top_error_services(self, minutes: Any) -> ToolResult:
        return self._execute("top_error_services", lambda: self._top_error_services(minutes))

    def service_health(self, svc: Any, minutes: Any) -> ToolResult:
        return self._execute("service_health", lambda: self._service_health(svc, minutes))

    def error_traces(self, svc: Any, minutes: Any, limit: Any) -> ToolResult:
        return self._execute("error_traces", lambda: self._error_traces(svc, minutes, limit))

    def error_logs(self, svc: Any, minutes: Any, limit: Any) -> ToolResult:
        return self._execute("error_logs", lambda: self._error_logs(svc, minutes, limit))

    def container_health(self, svc: Any) -> ToolResult:
        return self._execute("container_health", lambda: self._container_health(svc))

    def service_window(self, svc: Any, window_seconds: Any) -> ToolResult:
        return self._execute("service_window", lambda: self._service_window(svc, window_seconds))

    def _execute(self, tool: str, produce: Callable[[], list[_Draft]]) -> ToolResult:
        started = time.monotonic()
        try:
            drafts = produce()
            evidence = [self._finalize(tool, draft) for draft in drafts]
            return ToolResult(ok=True, tool=tool, evidence=evidence, duration_ms=self._elapsed_ms(started))
        except _Failure as failure:
            return self._failed(tool, failure.code, failure.message, started)
        except Exception as exc:
            return self._failed(
                tool, ToolErrorCode.BACKEND_ERROR, f"unexpected internal error ({type(exc).__name__})", started
            )

    @staticmethod
    def _elapsed_ms(started: float) -> int:
        return max(int((time.monotonic() - started) * 1000), 0)

    def _failed(self, tool: str, code: ToolErrorCode, message: str, started: float) -> ToolResult:
        return ToolResult(
            ok=False,
            tool=tool,
            error=ToolError.of(code, _clip(message, 300)),
            duration_ms=self._elapsed_ms(started),
        )

    def _finalize(self, tool: str, draft: _Draft) -> Evidence:
        now = self._clock()
        cap = min(EVIDENCE_DATA_MAX_CHARS, self.settings.limits.tool_output_max_chars)
        data = {**draft.data, "data_origin": self.data_origin}
        data, trimmed = _fit_data(data, cap)
        time_range = None
        if draft.window_seconds:
            time_range = TimeRange(start=now - timedelta(seconds=draft.window_seconds), end=now)
        return Evidence(
            id=self._next_id(),
            source=draft.source,
            tool=tool,
            query=draft.query,
            service=draft.service,
            time_range=time_range,
            collected_at=now,
            summary=_clip(ORIGIN_LABELS.get(self.data_origin, "") + draft.summary, 500),
            data=data,
            truncated=draft.truncated or trimmed,
        )

    def _check_minutes(self, minutes: Any) -> int:
        if isinstance(minutes, bool) or not isinstance(minutes, int) or not MIN_MINUTES <= minutes <= MAX_MINUTES:
            raise _Failure(ToolErrorCode.BAD_ARGS, f"minutes must be an integer from {MIN_MINUTES} to {MAX_MINUTES}")
        return minutes

    def _check_limit(self, limit: Any) -> int:
        if isinstance(limit, bool) or not isinstance(limit, int) or not MIN_LIMIT <= limit <= MAX_LIMIT:
            raise _Failure(ToolErrorCode.BAD_ARGS, f"limit must be an integer from {MIN_LIMIT} to {MAX_LIMIT}")
        return limit

    def _check_service(self, svc: Any) -> str:
        if not isinstance(svc, str) or svc not in self.services:
            raise _Failure(ToolErrorCode.BAD_ARGS, "svc is not one of the known services")
        return svc

    def _check_window_seconds(self, window_seconds: Any) -> int:
        limit = MAX_MINUTES * 60
        if (
            isinstance(window_seconds, bool)
            or not isinstance(window_seconds, int)
            or not 1 <= window_seconds <= limit
        ):
            raise _Failure(ToolErrorCode.BAD_ARGS, f"window_seconds must be an integer from 1 to {limit}")
        return window_seconds

    def _http_json(
        self,
        backend: str,
        method: str,
        url: str,
        *,
        params: dict[str, Any] | None = None,
        body: dict[str, Any] | None = None,
    ) -> Any:
        max_bytes = self.settings.limits.http_max_body_bytes
        chunks: list[bytes] = []
        try:
            with self._http.stream(
                method, url, params=params, json=body, headers={"Accept": "application/json"}
            ) as response:
                status = response.status_code
                if status in UNAVAILABLE_HTTP_STATUSES:
                    raise _Failure(ToolErrorCode.UNAVAILABLE, f"{backend} returned HTTP {status}")
                if not 200 <= status < 300:
                    raise _Failure(ToolErrorCode.BACKEND_ERROR, f"{backend} returned HTTP {status}")
                declared = response.headers.get("content-length", "")
                if declared.isdigit() and int(declared) > max_bytes:
                    raise _Failure(ToolErrorCode.BACKEND_ERROR, f"{backend} response is larger than {max_bytes} bytes")
                size = 0
                for chunk in response.iter_bytes():
                    size += len(chunk)
                    if size > max_bytes:
                        raise _Failure(
                            ToolErrorCode.BACKEND_ERROR, f"{backend} response is larger than {max_bytes} bytes"
                        )
                    chunks.append(chunk)
        except _Failure:
            raise
        except FixtureMissing as missing:
            raise _Failure(ToolErrorCode.UNAVAILABLE, str(missing)) from None
        except httpx.TimeoutException:
            raise _Failure(ToolErrorCode.TIMEOUT, f"{backend} did not answer in time") from None
        except httpx.HTTPError as exc:
            raise _Failure(ToolErrorCode.UNAVAILABLE, f"{backend} is unreachable ({type(exc).__name__})") from None
        try:
            return json.loads(b"".join(chunks))
        except (ValueError, RecursionError):
            raise _Failure(ToolErrorCode.BACKEND_ERROR, f"{backend} returned invalid JSON") from None

    def _prom_vector(self, query: str) -> list[_Sample]:
        payload = self._http_json(
            "prometheus", "GET", f"{self.settings.prometheus_url}/api/v1/query", params={"query": query}
        )
        if not isinstance(payload, dict):
            raise _Failure(ToolErrorCode.BACKEND_ERROR, "prometheus returned an unexpected response shape")
        if payload.get("status") != "success":
            kind = _clip(payload.get("errorType", "unknown"), 40)
            raise _Failure(ToolErrorCode.BACKEND_ERROR, f"prometheus reported an error ({kind})")
        data = payload.get("data")
        if not isinstance(data, dict) or data.get("resultType") != "vector" or not isinstance(data.get("result"), list):
            raise _Failure(ToolErrorCode.BACKEND_ERROR, "prometheus returned an unexpected response shape")
        samples: list[_Sample] = []
        for item in data["result"]:
            metric = item.get("metric") if isinstance(item, dict) else None
            value = item.get("value") if isinstance(item, dict) else None
            if not isinstance(metric, dict) or not isinstance(value, list) or len(value) != 2:
                raise _Failure(ToolErrorCode.BACKEND_ERROR, "prometheus returned an unexpected response shape")
            labels = {str(key): str(val) for key, val in metric.items()}
            samples.append(_Sample(labels=labels, value=_number(value[1]), timestamp=_number(value[0])))
        return samples

    def _service_matcher(self) -> str:
        return "|".join(sorted(self.services))

    def _calls_by_service(self, window_seconds: int, *, errors_only: bool) -> str:
        status = ',status_code="STATUS_CODE_ERROR"' if errors_only else ""
        return (
            "sum by (service_name) (increase(traces_span_metrics_calls_total"
            f'{{service_name=~"{self._service_matcher()}"{status}}}[{window_seconds}s]))'
        )

    @staticmethod
    def _calls_for(svc: str, window_seconds: int, *, errors_only: bool) -> str:
        status = ',status_code="STATUS_CODE_ERROR"' if errors_only else ""
        return (
            "sum(increase(traces_span_metrics_calls_total"
            f'{{service_name="{svc}"{status}}}[{window_seconds}s]))'
        )

    @staticmethod
    def _p95_for(svc: str, window_seconds: int) -> str:
        return (
            "histogram_quantile(0.95, sum by (le) (rate(traces_span_metrics_duration_milliseconds_bucket"
            f'{{service_name="{svc}"}}[{window_seconds}s])))'
        )

    def _top_error_services(self, minutes: Any) -> list[_Draft]:
        minutes = self._check_minutes(minutes)
        window = minutes * 60
        error_query = self._calls_by_service(window, errors_only=True)
        call_query = self._calls_by_service(window, errors_only=False)
        errors = self._prom_vector(error_query)
        calls = self._prom_vector(call_query)
        requests_by_service = {
            sample.labels.get("service_name", ""): sample.value or 0.0 for sample in calls if sample.value is not None
        }
        errors_by_service = {
            sample.labels.get("service_name", ""): sample.value or 0.0 for sample in errors if sample.value is not None
        }
        known = [name for name in requests_by_service if name in self.services]
        rows = []
        for name in sorted(set(known) | {n for n in errors_by_service if n in self.services}):
            error_count = errors_by_service.get(name, 0.0)
            if error_count <= 0:
                continue
            request_count = requests_by_service.get(name)
            rows.append(
                {
                    "service": name,
                    "errors": int(round(error_count)),
                    "requests": None if request_count is None else int(round(request_count)),
                    "error_rate": _ratio(error_count, request_count),
                }
            )
        rows.sort(key=lambda row: (-row["errors"], row["service"]))
        rows = rows[:TOP_SERVICES]
        if not known:
            summary = f"No span-metric data was returned for the last {minutes} min, so error services cannot be ranked."
        elif not rows:
            summary = f"No error spans in the last {minutes} min across {_plural(len(known), 'service')} with traffic."
        else:
            parts = [self._row_text(row) for row in rows[:5]]
            summary = f"Services with error spans in the last {minutes} min: " + "; ".join(parts) + "."
        data = {
            "window_minutes": minutes,
            "services_with_traffic": len(known),
            "services": rows,
        }
        return [
            _Draft(
                source=EvidenceSource.prometheus,
                query=f"{error_query} ; {call_query}",
                summary=summary,
                data=data,
                window_seconds=window,
            )
        ]

    @staticmethod
    def _row_text(row: dict[str, Any]) -> str:
        rate = "n/a" if row["error_rate"] is None else f"{row['error_rate'] * 100:.1f}%"
        return f"{row['service']} {_plural(row['errors'], 'error')} ({rate} of {row['requests']} spans)"

    def _window_numbers(self, svc: str, window: int) -> tuple[float | None, float | None, float | None, str]:
        call_query = self._calls_for(svc, window, errors_only=False)
        error_query = self._calls_for(svc, window, errors_only=True)
        calls = self._prom_vector(call_query)
        errors = self._prom_vector(error_query)
        requests = calls[0].value if calls else None
        timestamp = calls[0].timestamp if calls else None
        error_count = (errors[0].value if errors else 0.0) if requests is not None else None
        return requests, error_count, timestamp, f"{call_query} ; {error_query}"

    def _health_data(self, svc: str, window: int) -> tuple[dict[str, Any], str, list[str]]:
        requests, error_count, timestamp, query = self._window_numbers(svc, window)
        notes: list[str] = []
        data = {
            "window_seconds": window,
            "requests": None if requests is None else int(round(requests)),
            "errors": None if error_count is None else int(round(error_count)),
            "error_rate": _ratio(error_count, requests),
            "requests_per_second": None if requests is None else round(requests / window, 4),
            "sample_timestamp": timestamp,
        }
        return data, query, notes

    def _service_health(self, svc: Any, minutes: Any) -> list[_Draft]:
        svc = self._check_service(svc)
        minutes = self._check_minutes(minutes)
        window = minutes * 60
        data, query, notes = self._health_data(svc, window)
        p95_query = self._p95_for(svc, window)
        p95 = None
        try:
            samples = self._prom_vector(p95_query)
            p95 = samples[0].value if samples else None
        except _Failure as failure:
            notes.append(f"p95 latency unavailable: {failure.message}")
        data["p95_ms"] = None if p95 is None else round(p95, 1)
        data["window_minutes"] = minutes
        data["notes"] = notes
        if data["requests"] is None:
            summary = f"No span-metric data for {svc} in the last {minutes} min."
        else:
            rate = "n/a" if data["error_rate"] is None else f"{data['error_rate'] * 100:.1f}%"
            latency = "p95 n/a" if data["p95_ms"] is None else f"p95 {data['p95_ms']} ms"
            summary = (
                f"{svc} in the last {minutes} min: {data['requests']} spans, {data['errors']} errors "
                f"({rate}), {latency}."
            )
        return [
            _Draft(
                source=EvidenceSource.prometheus,
                query=f"{query} ; {p95_query}",
                summary=summary,
                data=data,
                service=svc,
                window_seconds=window,
            )
        ]

    def _service_window(self, svc: Any, window_seconds: Any) -> list[_Draft]:
        svc = self._check_service(svc)
        window = self._check_window_seconds(window_seconds)
        data, query, _ = self._health_data(svc, window)
        if data["requests"] is None:
            summary = f"No span-metric data for {svc} in the last {window} s."
        else:
            rate = "n/a" if data["error_rate"] is None else f"{data['error_rate'] * 100:.1f}%"
            summary = f"{svc} in the last {window} s: {data['requests']} spans, {data['errors']} errors ({rate})."
        return [
            _Draft(
                source=EvidenceSource.prometheus,
                query=query,
                summary=summary,
                data=data,
                service=svc,
                window_seconds=window,
            )
        ]

    def _error_traces(self, svc: Any, minutes: Any, limit: Any) -> list[_Draft]:
        svc = self._check_service(svc)
        minutes = self._check_minutes(minutes)
        limit = self._check_limit(limit)
        fetch = min(limit * TRACE_FETCH_MULTIPLIER, TRACE_FETCH_CAP)
        now = self._clock()
        params = {
            "query.service_name": svc,
            "query.num_traces": fetch,
            "query.start_time_min": (now - timedelta(minutes=minutes)).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "query.start_time_max": (now + timedelta(seconds=60)).strftime("%Y-%m-%dT%H:%M:%SZ"),
        }
        payload = self._http_json("jaeger", "GET", f"{self.settings.jaeger_url}/api/v3/traces", params=params)
        if not isinstance(payload, dict) or not isinstance(payload.get("result", {}), dict):
            raise _Failure(ToolErrorCode.BACKEND_ERROR, "jaeger returned an unexpected response shape")
        resource_spans = (payload.get("result") or {}).get("resourceSpans") or []
        if not isinstance(resource_spans, list):
            raise _Failure(ToolErrorCode.BACKEND_ERROR, "jaeger returned an unexpected response shape")
        spans = self._collect_spans(resource_spans)
        traces: dict[str, list[dict[str, Any]]] = {}
        for span in spans:
            traces.setdefault(span["trace_id"], []).append(span)
        found, ignored = [], 0
        for trace_id, members in traces.items():
            by_id = {member["span_id"]: member for member in members}
            errored = [m for m in members if m["is_error"] and m["service"] in self.services]
            ignored += sum(1 for m in members if m["is_error"] and m["service"] not in self.services)
            if not errored:
                continue
            deepest = max(errored, key=lambda m: (self._depth(m, by_id), m["start"]))
            found.append(
                {
                    "trace_id": _clip(trace_id, 32),
                    "error_spans": len(errored),
                    "deepest_service": deepest["service"],
                    "deepest_span": _clip(deepest["name"], 80),
                    "status_message": _clip(deepest["message"], 160),
                    "start": min(m["start"] for m in members),
                }
            )
        found.sort(key=lambda row: (-row["start"], row["trace_id"]))
        shown = found[:limit]
        for row in shown:
            row.pop("start")
        counts: dict[str, int] = {}
        for row in found:
            counts[row["deepest_service"]] = counts.get(row["deepest_service"], 0) + 1
        if not traces:
            summary = f"No traces for {svc} in the last {minutes} min."
        elif not found:
            summary = f"No error spans in {_plural(len(traces), 'trace')} for {svc} in the last {minutes} min."
        else:
            ranked = sorted(counts.items(), key=lambda item: (-item[1], item[0]))
            deepest_text = ", ".join(f"{name} ({count})" for name, count in ranked[:3])
            summary = (
                f"{len(found)} of {_plural(len(traces), 'trace')} for {svc} in the last {minutes} min "
                f"contain error spans; deepest erroring service: {deepest_text}."
            )
        data = {
            "window_minutes": minutes,
            "traces_examined": len(traces),
            "error_trace_count": len(found),
            "deepest_service_counts": dict(sorted(counts.items())),
            "ignored_error_spans_in_other_services": ignored,
            "error_traces": shown,
        }
        query = "GET /api/v3/traces?" + "&".join(f"{key}={value}" for key, value in params.items())
        return [
            _Draft(
                source=EvidenceSource.jaeger,
                query=query,
                summary=summary,
                data=data,
                service=svc,
                window_seconds=minutes * 60,
                truncated=len(found) > len(shown),
            )
        ]

    def _collect_spans(self, resource_spans: list[Any]) -> list[dict[str, Any]]:
        spans: list[dict[str, Any]] = []
        for resource_span in resource_spans:
            if not isinstance(resource_span, dict):
                raise _Failure(ToolErrorCode.BACKEND_ERROR, "jaeger returned an unexpected response shape")
            service = _resource_service(resource_span.get("resource")) or ""
            for scope in resource_span.get("scopeSpans") or []:
                for span in (scope.get("spans") or []) if isinstance(scope, dict) else []:
                    if not isinstance(span, dict):
                        raise _Failure(ToolErrorCode.BACKEND_ERROR, "jaeger returned an unexpected response shape")
                    if len(spans) >= MAX_SPANS_PARSED:
                        return spans
                    status = span.get("status")
                    spans.append(
                        {
                            "trace_id": str(span.get("traceId", "")),
                            "span_id": str(span.get("spanId", "")),
                            "parent_id": str(span.get("parentSpanId", "")),
                            "service": service,
                            "name": span.get("name", ""),
                            "is_error": _is_error_status(status),
                            "message": status.get("message", "") if isinstance(status, dict) else "",
                            "start": int(_number(span.get("startTimeUnixNano")) or 0),
                        }
                    )
        return spans

    @staticmethod
    def _depth(span: dict[str, Any], by_id: dict[str, dict[str, Any]]) -> int:
        depth, current = 0, span
        while current["parent_id"] and depth < MAX_DEPTH_STEPS:
            parent = by_id.get(current["parent_id"])
            if parent is None:
                break
            depth += 1
            current = parent
        return depth

    def _error_logs(self, svc: Any, minutes: Any, limit: Any) -> list[_Draft]:
        svc = self._check_service(svc)
        minutes = self._check_minutes(minutes)
        limit = self._check_limit(limit)
        if not self.settings.opensearch_url:
            raise _Failure(ToolErrorCode.UNAVAILABLE, "OPENSEARCH_URL is not configured, so logs cannot be queried")
        severities = ", ".join(f"'{name}'" for name in LOG_SEVERITIES)
        ppl = (
            f"source=otel-logs-* | where resource.service.name = '{svc}' and severity.text in ({severities}) "
            f"| sort - @timestamp | head {limit} | fields @timestamp, severity.text, body, traceId"
        )
        payload = self._http_json(
            "opensearch", "POST", f"{self.settings.opensearch_url}/_plugins/_ppl", body={"query": ppl}
        )
        if not isinstance(payload, dict) or not isinstance(payload.get("schema"), list):
            raise _Failure(ToolErrorCode.BACKEND_ERROR, "opensearch returned an unexpected response shape")
        rows = payload.get("datarows")
        if not isinstance(rows, list):
            raise _Failure(ToolErrorCode.BACKEND_ERROR, "opensearch returned an unexpected response shape")
        names = [column.get("name") if isinstance(column, dict) else None for column in payload["schema"]]
        wanted = {"@timestamp": "timestamp", "severity.text": "severity", "body": "message", "traceId": "trace_id"}
        if not set(wanted) <= set(names):
            raise _Failure(ToolErrorCode.BACKEND_ERROR, "opensearch returned an unexpected response shape")
        cutoff = self._clock() - timedelta(minutes=minutes)
        logs, stale, unparsed = [], 0, 0
        for row in rows:
            if not isinstance(row, list) or len(row) != len(names):
                raise _Failure(ToolErrorCode.BACKEND_ERROR, "opensearch returned an unexpected response shape")
            record = dict(zip(names, row))
            moment = _parse_timestamp(record["@timestamp"])
            if moment is None:
                unparsed += 1
            elif moment < cutoff:
                stale += 1
                continue
            message = record["body"]
            logs.append(
                {
                    "timestamp": _clip(record["@timestamp"], 40),
                    "severity": _clip(record["severity.text"], 16),
                    "message": _clip(message if isinstance(message, str) else json.dumps(message, default=str), 200),
                    "trace_id": _clip(record["traceId"] or "", 32),
                }
            )
        if not logs:
            summary = f"No error logs for {svc} in the last {minutes} min."
            if stale:
                summary += f" {_plural(stale, 'older error log')} fell outside the window."
        else:
            summary = f"{_plural(len(logs), 'error log')} for {svc} in the last {minutes} min. Latest: {logs[0]['message']}"
        data = {
            "window_minutes": minutes,
            "returned": len(logs),
            "outside_window": stale,
            "unparsed_timestamps": unparsed,
            "logs": logs,
        }
        return [
            _Draft(
                source=EvidenceSource.opensearch,
                query=ppl,
                summary=summary,
                data=data,
                service=svc,
                window_seconds=minutes * 60,
            )
        ]

    def _container_health(self, svc: Any) -> list[_Draft]:
        svc = self._check_service(svc)
        if not self.allow_docker_inspect:
            raise _Failure(
                ToolErrorCode.UNAVAILABLE,
                "container inspection is disabled until the organizers confirm docker inspect is permitted",
            )
        args = ["docker", "inspect", "--format", DOCKER_INSPECT_FORMAT, svc]
        try:
            completed = self._docker_runner(args, DOCKER_TIMEOUT_S)
        except subprocess.TimeoutExpired:
            raise _Failure(ToolErrorCode.TIMEOUT, "docker inspect did not finish in time") from None
        except FixtureMissing as missing:
            raise _Failure(ToolErrorCode.UNAVAILABLE, str(missing)) from None
        except OSError:
            raise _Failure(ToolErrorCode.UNAVAILABLE, "the docker CLI is not available") from None
        if completed.returncode != 0:
            if "no such" in (completed.stderr or "").lower():
                raise _Failure(ToolErrorCode.NOT_FOUND, f"no container named {svc} was found")
            raise _Failure(ToolErrorCode.UNAVAILABLE, "docker inspect failed; the docker daemon may be unreachable")
        output = completed.stdout or ""
        if len(output) > MAX_DOCKER_OUTPUT_CHARS:
            raise _Failure(ToolErrorCode.BACKEND_ERROR, "docker inspect output is larger than expected")
        try:
            parsed = json.loads(output)
        except (ValueError, RecursionError):
            raise _Failure(ToolErrorCode.BACKEND_ERROR, "docker inspect returned invalid JSON") from None
        if not isinstance(parsed, dict) or set(parsed) != set(DOCKER_FIELDS):
            raise _Failure(ToolErrorCode.BACKEND_ERROR, "docker inspect returned an unexpected shape")
        state = {
            "status": _clip(parsed["status"], 32),
            "health": None if parsed["health"] is None else _clip(parsed["health"], 32),
            "restart_count": parsed["restart_count"],
            "started_at": _clip(parsed["started_at"], 40),
            "oom_killed": parsed["oom_killed"],
        }
        if not isinstance(state["restart_count"], int) or not isinstance(state["oom_killed"], bool):
            raise _Failure(ToolErrorCode.BACKEND_ERROR, "docker inspect returned an unexpected shape")
        health = state["health"] or "no healthcheck"
        summary = (
            f"Container {svc}: {state['status']}, health {health}, {state['restart_count']} restarts, "
            f"OOM-killed {'yes' if state['oom_killed'] else 'no'}, started {state['started_at']}."
        )
        return [
            _Draft(
                source=EvidenceSource.docker_inspect,
                query="docker inspect --format <fixed state fields> " + svc,
                summary=summary,
                data=state,
                service=svc,
            )
        ]


@dataclass(frozen=True)
class FixtureSet:
    root: Path
    scenario: str
    phase: str
    synthetic: bool
    based_on: str
    expected_missing: tuple[str, ...] = field(default_factory=tuple)

    @property
    def directory(self) -> Path:
        return self.root / self.scenario / self.phase

    @property
    def data_origin(self) -> str:
        return "synthetic_fixture" if self.synthetic else "recorded_fixture"

    def relative(self, name: str) -> str:
        return f"fixtures/telemetry/{self.scenario}/{self.phase}/{name}"

    def read(self, name: str, tool_hint: str | None = None) -> bytes:
        path = self.directory / name
        if not path.is_file():
            declared = " (the scenario declares this signal as missing)" if tool_hint in self.expected_missing else ""
            raise FixtureMissing(f"fixture not found: {self.relative(name)}{declared}")
        return path.read_bytes()


_FILE_TOOL = {"opensearch.logs": "error_logs", "jaeger.traces": "error_traces", "docker": "container_health"}


def load_fixture_set(scenario: str, phase: str = "before", *, root: Path = FIXTURE_ROOT) -> FixtureSet:
    for label, value in (("scenario", scenario), ("phase", phase)):
        if not isinstance(value, str) or not _SAFE_NAME.match(value):
            raise FixtureError(f"{label} must match the safe name pattern")
    resolved_root = root.resolve()
    directory = (resolved_root / scenario / phase).resolve()
    if resolved_root not in directory.parents:
        raise FixtureError("fixture path escapes the fixture root")
    if not directory.is_dir():
        raise FixtureError(f"fixture directory not found: fixtures/telemetry/{scenario}/{phase}")
    meta_path = resolved_root / scenario / "_meta.json"
    if not meta_path.is_file():
        raise FixtureError(f"fixtures/telemetry/{scenario}/_meta.json is missing")
    try:
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
    except ValueError:
        raise FixtureError(f"fixtures/telemetry/{scenario}/_meta.json is not valid JSON") from None
    if not isinstance(meta, dict) or not isinstance(meta.get("synthetic"), bool) or not isinstance(
        meta.get("based_on"), str
    ):
        raise FixtureError(f"fixtures/telemetry/{scenario}/_meta.json needs boolean 'synthetic' and string 'based_on'")
    missing = meta.get("expected_missing", [])
    if not isinstance(missing, list) or not all(isinstance(item, str) for item in missing):
        raise FixtureError(f"fixtures/telemetry/{scenario}/_meta.json 'expected_missing' must be a list of strings")
    return FixtureSet(
        root=resolved_root,
        scenario=scenario,
        phase=phase,
        synthetic=meta["synthetic"],
        based_on=meta["based_on"],
        expected_missing=tuple(missing),
    )


def _tool_hint(name: str) -> str | None:
    for prefix, tool in _FILE_TOOL.items():
        if name.startswith(prefix):
            return tool
    return None


def fixture_transport(fixtures: FixtureSet) -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.endswith("/api/v1/query"):
            query = request.url.params.get("query", "")
            found = _PROM_SERVICE.search(query)
            role = "latency" if "histogram_quantile" in query else (
                "errors" if 'status_code="STATUS_CODE_ERROR"' in query else "calls"
            )
            name = f"prometheus.{role}.{found.group(1) if found else 'all'}.json"
        elif path.endswith("/api/v3/traces"):
            service = request.url.params.get("query.service_name", "")
            name = f"jaeger.traces.{service}.json" if _SAFE_NAME.match(service) else "jaeger.traces.invalid.json"
        elif path.endswith("/_plugins/_ppl"):
            found = _PPL_SERVICE.search(json.loads(request.content or b"{}").get("query", ""))
            name = f"opensearch.logs.{found.group(1) if found else 'invalid'}.json"
        else:
            return httpx.Response(404, json={"error": "no fixture route for this path"})
        return httpx.Response(200, content=fixtures.read(name, _tool_hint(name)), headers={"content-type": "application/json"})

    return httpx.MockTransport(handler)


def fixture_docker_runner(fixtures: FixtureSet) -> DockerRunner:
    def runner(args: list[str], timeout: float) -> "subprocess.CompletedProcess[str]":
        name = f"docker.{args[-1]}.json"
        output = fixtures.read(name, "container_health").decode("utf-8")
        return subprocess.CompletedProcess(args=args, returncode=0, stdout=output, stderr="")

    return runner


def fixture_tools(
    settings: Settings,
    scenario: str,
    phase: str = "before",
    *,
    root: Path = FIXTURE_ROOT,
    **kwargs: Any,
) -> TelemetryTools:
    fixtures = load_fixture_set(scenario, phase, root=root)
    offline = replace(
        settings,
        prometheus_url="http://prometheus.fixture.invalid",
        jaeger_url="http://jaeger.fixture.invalid/jaeger/ui",
        opensearch_url="http://opensearch.fixture.invalid",
    )
    return TelemetryTools(
        offline,
        transport=fixture_transport(fixtures),
        allow_docker_inspect=True,
        docker_runner=fixture_docker_runner(fixtures),
        data_origin=fixtures.data_origin,
        **kwargs,
    )
