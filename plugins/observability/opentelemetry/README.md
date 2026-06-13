# OpenTelemetry Observability Plugin

Optional OpenTelemetry-compatible trace/metric export for Hermes. Local-first,
opt-in, and redaction-safe.

## Enable

```bash
# 1. Enable the plugin (required before any config takes effect)
hermes plugins enable observability/opentelemetry

# 2. Turn telemetry on in config.yaml or via env
export HERMES_OTEL_ENABLED=1
```

## Configure

Either edit `~/.hermes/config.yaml`:

```yaml
observability:
  opentelemetry:
    enabled: true
    exporter: jsonl          # or otlp
    output_directory: telemetry   # relative to HERMES_HOME; used by jsonl exporter
    endpoint: "http://localhost:4318"   # required for exporter: otlp
    headers:
      Authorization: "Bearer ${OTLP_TOKEN}"
    service_name: hermes-agent
    service_version: "0.0.0"
    environment: local
    sample_rate: 1.0
    max_field_length: 8192
    redact: true
```

Or use environment variables:

```bash
HERMES_OTEL_ENABLED=1
HERMES_OTEL_EXPORTER=jsonl          # or otlp
HERMES_OTEL_ENDPOINT=http://localhost:4318
HERMES_OTEL_AUTH_HEADER="Bearer ..."
HERMES_OTEL_SERVICE_NAME=hermes-agent
HERMES_OTEL_SERVICE_VERSION=0.0.0
HERMES_OTEL_ENVIRONMENT=local
HERMES_OTEL_SAMPLE_RATE=1.0
HERMES_OTEL_MAX_FIELD_LENGTH=8192
HERMES_OTEL_OUTPUT_DIR=telemetry
```

## What is exported

- Session start/end spans
- Turn spans (`pre_llm_call` / `post_llm_call`)
- LLM API request spans (`pre_api_request` / `post_api_request` / `api_request_error`)
- Tool spans (`pre_tool_call` / `post_tool_call`)
- Approval request/response events
- Subagent start/stop spans with parent linkage

All events include duration, status, retry count, token counts (when available),
and provider/model/platform metadata where provided by the core hook contract.

## Redaction

When `redact: true` (default):

- Prompt text is truncated, never echoed in full.
- Tool arguments and results are scanned for secret-shaped keys and bearer tokens.
- File payloads (`read_file`, etc.) keep structural metadata but drop `content`/`lines`.
- Paths under `~/.hermes/`, `~/.ssh/`, and `.env` files are replaced with `<redacted path>`.

## Trace IDs in reports

Other Hermes subsystems can safely call:

```python
from plugins.observability.opentelemetry import get_current_trace_id, add_event, format_trace_link

trace_id = get_current_trace_id(session_id="...", task_id="...")
add_event("evolution.implementation", {"issue": 167})
```

These no-op when the plugin is disabled or no trace is active.

## Verify

```bash
hermes plugins list                  # observability/opentelemetry enabled
hermes chat -q "hello"               # run a turn
cat ~/.hermes/telemetry/hermes-otel-$(date +%Y-%m-%d).jsonl
```

## Disable

```bash
hermes plugins disable observability/opentelemetry
```
