# Telemetry and Docker status (P2)

Written 2026-10-23. Every statement below is either backed by a command listed in "What was actually run" or is marked as an assumption or `NOT RUN`.

**No live backend was contacted while this was written.** The demo was not started, no HTTP request left the machine from the tools, and no `docker` command was run. Everything that passed, passed offline against mocked HTTP responses and synthetic fixtures.

## Fixture data is synthetic and cannot prove recovery

- Everything under `fixtures/telemetry/` is hand-written with invented numbers, fake IDs and generic messages. None of it was recorded from a real system.
- Evidence built from a fixture has a summary starting `[SYNTHETIC FIXTURE]` and `data.data_origin = "synthetic_fixture"`. A recorded fixture would be `[RECORDED FIXTURE]`.
- `verify()` returns `unverified` whenever the telemetry did not come from the live backends (`TelemetryTools.data_origin != "live"`). It still records the computed numbers and says in `notes` what the status would have been, so a reader can follow the reasoning. A fixture run therefore can never reach `resolved`.
- A passing test using `MockTransport` shows that the parsing and decision logic are correct for the response shapes assumed here. It does not show that the real backends return those shapes.

## Configured endpoints

The URLs come from P1's `Settings` (`oncall/config.py`). The tools read no environment variables themselves.

| Backend | Settings field | Environment variable | Default | Read-only request the tools send |
|---|---|---|---|---|
| Prometheus | `prometheus_url` | `PROMETHEUS_URL` | `http://localhost:9090` | `GET {url}/api/v1/query?query=<PromQL>` |
| Jaeger v3 | `jaeger_url` | `JAEGER_URL` | `http://localhost:8080/jaeger/ui` | `GET {url}/api/v3/traces?query.service_name=...&query.num_traces=...&query.start_time_min=...&query.start_time_max=...` |
| OpenSearch | `opensearch_url` | `OPENSEARCH_URL` | empty, so `error_logs` returns `UNAVAILABLE` without any request | `POST {url}/_plugins/_ppl` with a PPL `source=otel-logs-* \| where ...` query (a read-only query; the repository's own log test uses the same call) |
| Docker | none | none | disabled | `docker inspect --format <fixed state fields> <container>`, list-form, no shell |

Limits come from `Settings.limits`: connect 3 s, read 8 s, response body at most 200,000 bytes (enforced while streaming), evidence `data` at most `min(4000, tool_output_max_chars)` characters. Oversized data is trimmed list by list and the evidence is marked `truncated`.

`OPENSEARCH_URL` has no stable default because the container's host port is chosen by Docker. Per `.env.example` it is `http://localhost:<port from docker port opensearch 9200>`.

## Tools and what each one queries

| Tool | Backend | What it reports |
|---|---|---|
| `top_error_services(minutes)` | Prometheus | `traces_span_metrics_calls_total` increase over the window, grouped by `service_name`, with and without `status_code="STATUS_CODE_ERROR"`; top 10 by error count |
| `service_health(svc, minutes)` | Prometheus | span count, error count, error ratio, spans per second, and p95 from `traces_span_metrics_duration_milliseconds_bucket` |
| `service_window(svc, window_seconds)` | Prometheus | same counts over an exact number of seconds; used by `verify` |
| `error_traces(svc, minutes, limit)` | Jaeger | traces that contain error spans, the deepest erroring service per trace, and counts per deepest service |
| `error_logs(svc, minutes, limit)` | OpenSearch | newest error-severity log lines for the service, inside the window |
| `container_health(svc)` | Docker | status, health status, restart count, started-at, OOM-killed; nothing else |

The metric names, label names, Jaeger paths and OpenSearch fields (`resource.service.name`, `severity.text`, `body`, `traceId`, `@timestamp`) are taken from the repository's Grafana provisioning and `test/telemetry/*`. Service names are checked against an allowlist copied from `test/telemetry/services.py` (the 18 shop services). Anything else, including infrastructure names, gets `BAD_ARGS` before any request is made, and the allowlist is also placed in the Prometheus ranking queries so series of other services are never fetched.

## Run the tests without starting the demo

From `oncall-agent/`, with the dependencies from P1's `requirements.txt` (no others were added):

```bash
python3 -m venv .venv && source .venv/bin/activate
python -m pip install -r requirements.txt
python -m pytest -q tests/test_telemetry.py tests/test_verify.py   # P2 tests only
python -m pytest -q                                                 # whole suite
```

No test needs the network, Docker, an LLM, or a running demo. Tests inject a fake clock and a recording sleep, so none waits.

## Using the tools from code

```python
from oncall.config import load_settings
from oncall.telemetry import TelemetryTools, fixture_tools
from oncall.verify import verify

settings = load_settings()
tools = TelemetryTools(settings)                      # live backends from PROMETHEUS_URL, JAEGER_URL, OPENSEARCH_URL
tools = TelemetryTools(settings, allow_docker_inspect=True)   # only after the organizers confirm (see below)
tools = fixture_tools(settings, "payment_errors")     # offline, labelled synthetic

result = tools.service_health("payment", 15)          # always a ToolResult, never raises
verify_fn = functools.partial(verify, settings=settings, tools=tools)   # matches the orchestrator's VerifyFn
```

Evidence IDs default to `ev-001`, `ev-002`, ... per `TelemetryTools` instance. Pass `next_evidence_id=<callable returning "ev-NNN">` to share one sequence across a run.

## Enabling live adapters safely

1. Start the demo the normal team way and confirm each backend answers a read-only query yourself.
2. Put the URLs in `oncall-agent/.env` (gitignored): `PROMETHEUS_URL`, `JAEGER_URL`, `OPENSEARCH_URL`.
3. Construct `TelemetryTools(settings)`. Nothing else is needed for Prometheus, Jaeger and OpenSearch.
4. Docker inspection stays off. There is no `Settings` field or environment variable for it, and none was invented. It is a constructor argument (`allow_docker_inspect=True`) until P1 decides how to configure it.

## Docker: what needs organizer confirmation

Not confirmed by anyone, so Docker inspection is opt-in and off by default.

- Whether the Docker CLI is permitted and present in the judged environment at all.
- Whether `docker inspect` of the demo's containers is permitted. If yes, only the fixed state fields above are read; `Config.Env` and the rest of the inspect output are never requested or stored.
- Container logs (`docker logs`) are not used anywhere and there is no fallback to them. The only log source is OpenSearch.
- The tools assume a container is named like its service. Per `P2.md`, `frontend-web` has no `container_name` in the compose files; that was not re-checked here. A missing container gives `NOT_FOUND`, a missing CLI or unreachable daemon gives `UNAVAILABLE`.

## Verification of recovery

`verify(proposal, action, settings, *, tools=None, sleep=time.sleep, clock=utcnow)` returns P1's `VerificationResult`. Criteria come only from `proposal.success_criteria` (`services`, `max_error_rate`, `min_requests`, `wait_seconds`, `window_seconds`).

- Action not `succeeded`: `unverified`, no telemetry queried, no waiting.
- Waits for whatever is left of `wait_seconds` after `action.finished_at`, then measures the most recent `min(window_seconds, time since the action finished)` seconds, so the window never reaches back before the action.
- `recovered` only if every listed service has at least `min_requests` spans and an error ratio at or below `max_error_rate`.
- `not_recovered` if any listed service has enough spans and an error ratio above the limit, even if another service has too little traffic or could not be queried.
- Everything else is `unverified`: zero or too little traffic, a failed or timed-out query, a malformed response, a stale sample, errors exceeding requests, an unknown service, a window of under one second, or a clock that disagrees with `finished_at`.
- A simulated action does not change the telemetry-based status but adds a note; the orchestrator's `derive_final_status` caps it at `mitigation_unverified`.
- `recovered` means post-action telemetry meets the criteria. No pre-action baseline is compared (`baseline_before` stays empty).

## What was actually run

All from `oncall-agent/` on 2026-10-23 under WSL2, Python 3.12.3, pydantic 2.14.1, httpx 0.28.1, pytest 9.1.1.

| Check | Command | Result |
|---|---|---|
| P1 tests before any change | `python -m pytest -q` | PASS, 112 passed |
| P2 tests | `python -m pytest -q tests/test_telemetry.py tests/test_verify.py` | PASS, 229 passed |
| Whole suite | `python -m pytest -q` | PASS, 341 passed |
| Mutation check: 9 deliberate code breaks, each run against the P2 tests | local script, not committed | the first run left 1 of 9 undetected (streamed-body size cap); the test was strengthened, and that break is now caught. The other 8 were caught |
| Live Prometheus query | `curl -s -G http://localhost:9090/api/v1/query --data-urlencode 'query=...'` | NOT RUN |
| Live Jaeger `/api/v3/services` and `/api/v3/traces` | `curl -s http://localhost:8080/jaeger/ui/api/v3/services` | NOT RUN |
| Live OpenSearch PPL | `docker port opensearch 9200`, then the PPL call | NOT RUN |
| `docker inspect` with the fixed format | `docker inspect --format ...` | NOT RUN (permission unclear) |
| Any tool against a running demo | n/a | NOT RUN |

## Known limitations and unverified assumptions

Nothing in this list was checked against a running system.

- **Span counts, not requests.** Prometheus counts every span with the service's `service_name`, including internal and client spans. The ratio is a span error ratio. The `span_kind` label values were not confirmed, so no span-kind filter is applied; the repository's own error-rate dashboards do not filter on it either.
- **Absent error series means zero.** Prometheus only exposes the error series after the first error, so an empty error result with traffic present is read as zero errors.
- **`increase()` extrapolates.** Counts are rounded and can differ slightly between series. `verify` allows 10% plus one span of slack before calling the metrics contradictory.
- **Jaeger error filtering is client side.** The v3 error-tag query parameter was not confirmed, so `error_traces` fetches up to `min(3 * limit, 60)` recent traces and filters them. It can under-report when errors are rare. A large response is rejected at 200,000 bytes rather than truncated.
- **Jaeger status and span shape.** Parsing accepts status code `2` or `"STATUS_CODE_ERROR"` and reads `resourceSpans[].scopeSpans[].spans[]` with `traceId`, `spanId`, `parentSpanId`, `startTimeUnixNano`. The span fields come from the repository's trace edge test; the status format and the error message field come from the OTLP JSON format, not from a captured response.
- **Log severity values.** The PPL filter uses `severity.text in ('ERROR','Error','error','FATAL','Fatal','fatal')`. The `severity.text`, `body`, `traceId` and `@timestamp` field names come from Grafana provisioning. The actual values the demo's services emit were not checked.
- **Log time window is applied after the query.** The PPL has no time predicate (no time function was confirmed), so it takes the newest N error lines and drops those older than the window. If the newest N are all old, the result is empty.
- **PPL response shape.** `schema` and `datarows` are used as in the repository's log test; the other columns were not seen in a real response.
- **Docker format template.** `DOCKER_INSPECT_FORMAT` is a Go template that was not run against a real Docker daemon.
- **Clocks.** Prometheus is queried at its own "now" while `verify` measures from the local `finished_at`; skew between the two is not corrected. A sample timestamp older than the measured window is treated as stale.
- **Fixture coverage.** Four services (`frontend`, `checkout`, `payment`, `cart`) are covered per scenario. Others give `UNAVAILABLE` naming the missing fixture file.

## Open questions for P1 and the team

These are not answered by anything in the repository, and the contracts and configuration were left untouched.

1. **No `fixture` evidence source and no telemetry-mode setting.** `EvidenceSource` has no value for fixture data and `Settings` has no mode field. Fixture evidence is therefore tagged with its real backend kind in `source` (for example `prometheus`), and its synthetic origin is carried in the summary prefix and `data.data_origin`. Anything that counts distinct `source` values for confidence cannot tell fixture from live evidence.
2. **Evidence ID allocation.** Each `TelemetryTools` numbers its own evidence unless given `next_evidence_id`. P1's registry (`Deps.registry`, not defined yet) should supply one shared allocator.
3. **Orchestrator signature.** `VerifyFn` takes `(proposal, action)` only; `verify` also needs settings, so bind it with `functools.partial` as above. How the tool registry will call the five tools is not defined (`registry: Any`).
4. **Where telemetry lives.** `docs/ARCHITECTURE.md` lists telemetry under `oncall/tools/*` and the verifier under `oncall/safety/*` (P4), while `MVP_CHECKLIST.md` and `PLAN.md` section 14 assign `oncall/telemetry.py` and `oncall/verify.py` to P2. P2 files follow the latter and no other person's file was touched.
5. **Docker log fallback.** `docs/ARCHITECTURE.md` says OpenSearch logs have a `docker logs` fallback. The team rules in `P2.md` forbid `docker logs`, and none is implemented.
6. **Fixture-mode verification.** `verify` returns `unverified` for fixture telemetry (the safest reading of the brief). If the team wants computed `not_recovered` to survive for fixture runs, that is a single branch in `verify.py` and needs an explicit decision.
7. **Docker opt-in wiring.** Whether Docker inspection becomes a `Settings` field is P1's call once the organizers answer.
8. **Chosen bounds, not agreed ones:** `minutes` 1 to 60, `limit` 1 to 50, `window_seconds` 1 to 3600, all real integers (booleans rejected).
