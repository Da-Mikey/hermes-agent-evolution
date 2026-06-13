# OpenTelemetry Export

Hermes ships an optional OpenTelemetry observability plugin at
`plugins/observability/opentelemetry/`. It converts the existing Hermes observer
hook contract into OpenTelemetry-style traces and emits them to a local JSONL
file or an OTLP/HTTP endpoint.

The plugin is **opt-in and disabled by default**. It adds no required
dependencies and uses only the Python standard library for network export.

For setup and configuration, see the plugin's README:
[plugins/observability/opentelemetry/README.md](../../../plugins/observability/opentelemetry/README.md)

## Integration with Evolution reports

Cron jobs and Evolution pipeline stages can attach trace IDs to generated
issues/reports so users can inspect the run that produced them:

```python
from plugins.observability.opentelemetry import get_current_trace_id

trace_id = get_current_trace_id(session_id="...", task_id="...")
```

The helper returns `None` when telemetry is disabled, so callers do not need
to gate the call.
