# Synthetic telemetry fixtures

Every file here is hand-written with invented numbers, fake IDs and generic messages. None of it was recorded from a real system, and none of it is derived from service fault code, configuration of any kind, incidents or scoring material.

Each file is the raw body a backend would return, so the same parsers read live and fixture data. File names: `prometheus.{calls,errors,latency}.<service|all>.json`, `jaeger.traces.<service>.json`, `opensearch.logs.<service>.json`, `docker.<service>.json` (the fixed state fields only).

Evidence built from these files is labelled `[SYNTHETIC FIXTURE]` and can never establish real recovery. A scenario's `_meta.json` must say `synthetic` and `based_on`; `expected_missing` lists signals that are absent on purpose.
