"""opentelemetry — optional Hermes plugin for OpenTelemetry-style traces.

Emits spans/events for session lifecycle, LLM turns, provider API calls, tool
calls, approvals, and subagent delegation. Designed to be opt-in, local-first,
and redaction-safe:

  * Disabled unless the plugin is enabled AND `observability.opentelemetry.enabled`
    is true (or `HERMES_OTEL_ENABLED=1`).
  * Default exporter is a local JSONL file in `~/.hermes/telemetry/`.
  * Network OTLP export requires explicit `endpoint` configuration and is
    implemented with stdlib-only HTTP to avoid adding a required dependency.
  * Prompt text, tool arguments, secrets, file paths marked sensitive, and raw
    external content are redacted by default.

The plugin uses the existing Hermes observer hook contract and fails open: any
exception in a hook callback is caught and logged, never propagated.

Issue: #167
"""

from __future__ import annotations

import base64
import json
import logging
import os
import re
import threading
import time
import urllib.error
import urllib.request
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

from hermes_constants import get_hermes_home
from hermes_cli.config import cfg_get, load_config

logger = logging.getLogger(__name__)

# Sentinel marking a failed initialization so we don't retry on every hook call.
_INIT_FAILED = object()

_LOCK = threading.RLock()
_TRACER: "_Tracer | object | None" = None

# Schema version surfaced in every exported event for consumers.
_SCHEMA_VERSION = "hermes.opentelemetry.v1"

# ---------------------------------------------------------------------------
# Public helpers for other subsystems (cron, evolution, cli summaries)
# ---------------------------------------------------------------------------


def get_current_trace_id(session_id: str = "", task_id: str = "") -> Optional[str]:
    """Return the trace ID for the active session/task, if telemetry is on."""
    tracer = _get_tracer()
    if tracer is None:
        return None
    return tracer.get_trace_id(session_id=session_id, task_id=task_id)


def add_event(
    name: str, attributes: Optional[Dict[str, Any]] = None, **kwargs: Any
) -> None:
    """Add a custom event to the current session/task trace, if any.

    Safe to call from anywhere; no-ops when telemetry is disabled or no
    active trace exists.
    """
    tracer = _get_tracer()
    if tracer is None:
        return
    session_id = kwargs.get("session_id", "")
    task_id = kwargs.get("task_id", "")
    trace = tracer.get_trace(session_id=session_id, task_id=task_id)
    if trace is None:
        return
    try:
        trace.add_event(name, _redact_payload(attributes or {}))
    except Exception as exc:
        logger.debug("otel add_event failed: %s", exc)


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------


def _env(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


def _env_bool(*names: str) -> bool:
    for name in names:
        value = _env(name).lower()
        if value in {"1", "true", "yes", "on"}:
            return True
        if value in {"0", "false", "no", "off", "disabled"}:
            return False
    return None  # type: ignore[return-value]


def _load_config() -> Dict[str, Any]:
    try:
        return load_config() or {}
    except Exception:
        return {}


def _cfg_otel(config: Dict[str, Any]) -> Dict[str, Any]:
    obs = config.get("observability")
    if isinstance(obs, dict):
        otel = obs.get("opentelemetry")
        if isinstance(otel, dict):
            return otel
    return {}


def _is_enabled() -> bool:
    env = _env_bool("HERMES_OTEL_ENABLED")
    if env is not None:
        return env
    otel = _cfg_otel(_load_config())
    enabled = otel.get("enabled")
    return bool(enabled) if enabled is not None else False


def _settings() -> "_Settings":
    otel = _cfg_otel(_load_config())

    exporter = (
        (otel.get("exporter") or _env("HERMES_OTEL_EXPORTER") or "jsonl")
        .lower()
        .strip()
    )
    endpoint = otel.get("endpoint") or _env("HERMES_OTEL_ENDPOINT") or ""
    headers = otel.get("headers") or {}
    if not isinstance(headers, dict):
        headers = {}
    auth_header = _env("HERMES_OTEL_AUTH_HEADER")
    if auth_header and "Authorization" not in headers:
        headers["Authorization"] = auth_header

    service_name = (
        otel.get("service_name") or _env("HERMES_OTEL_SERVICE_NAME") or "hermes-agent"
    )
    service_version = (
        otel.get("service_version") or _env("HERMES_OTEL_SERVICE_VERSION") or "unknown"
    )
    env_tag = otel.get("environment") or _env("HERMES_OTEL_ENVIRONMENT") or ""

    sample_rate = otel.get("sample_rate")
    if sample_rate is None:
        sample_rate = _env("HERMES_OTEL_SAMPLE_RATE")
    try:
        sample_rate = float(sample_rate) if sample_rate is not None else 1.0
    except Exception:
        sample_rate = 1.0

    max_field_len = otel.get("max_field_length")
    if max_field_len is None:
        max_field_len = _env("HERMES_OTEL_MAX_FIELD_LENGTH")
    try:
        max_field_len = int(max_field_len) if max_field_len is not None else 8192
    except Exception:
        max_field_len = 8192

    redact = otel.get("redact", True)
    if not isinstance(redact, bool):
        redact = str(redact).lower() in {"1", "true", "yes", "on"}

    # Directory for JSONL exporter; relative paths resolve under HERMES_HOME.
    raw_dir = (
        otel.get("output_directory") or _env("HERMES_OTEL_OUTPUT_DIR") or "telemetry"
    )
    output_dir = Path(raw_dir)
    if not output_dir.is_absolute():
        output_dir = get_hermes_home() / output_dir

    return _Settings(
        enabled=True,
        exporter=exporter,
        endpoint=endpoint,
        headers=headers,
        service_name=service_name,
        service_version=service_version,
        environment=env_tag,
        sample_rate=max(0.0, min(1.0, sample_rate)),
        max_field_length=max(64, max_field_len),
        redact=redact,
        output_dir=output_dir,
    )


@dataclass
class _Settings:
    enabled: bool = False
    exporter: str = "jsonl"
    endpoint: str = ""
    headers: Dict[str, str] = field(default_factory=dict)
    service_name: str = "hermes-agent"
    service_version: str = "unknown"
    environment: str = ""
    sample_rate: float = 1.0
    max_field_length: int = 8192
    redact: bool = True
    output_dir: Path = field(default_factory=lambda: get_hermes_home() / "telemetry")


# ---------------------------------------------------------------------------
# Redaction
# ---------------------------------------------------------------------------

_SECRET_RE = re.compile(
    r"([A-Za-z_][A-Za-z0-9_]*(?:key|token|secret|password|credential|api_key|auth|private|signature))\s*[:=]\s*[\"']?[^\s\"']+",
    re.IGNORECASE,
)
_BEARER_RE = re.compile(r"(?i)bearer\s+[a-z0-9_\-\.]{20,}")
_PATH_RE = re.compile(
    r"(~?/[^\s\"']*\.env|~?/\.ssh/[^\s\"']*|~?/\.hermes/[^\s\"']*)", re.IGNORECASE
)
_HEX_RE = re.compile(r"\b[0-9a-f]{32,}\b", re.IGNORECASE)


def _is_secret_key(key: str) -> bool:
    low = key.lower().replace("-", "_")
    return any(
        token in low
        for token in {
            "key",
            "token",
            "secret",
            "password",
            "credential",
            "auth",
            "private",
            "signature",
        }
    )


def _truncate(value: Any, max_len: int) -> Any:
    if isinstance(value, str) and len(value) > max_len:
        return value[:max_len] + f"...[{len(value) - max_len} chars omitted]"
    return value


def _redact_string(value: str, max_len: int) -> str:
    if not value:
        return value
    value = _truncate(value, max_len)
    value = _BEARER_RE.sub("<redacted bearer>", value)
    value = _SECRET_RE.sub(r"\1=<redacted>", value)
    value = _PATH_RE.sub("<redacted path>", value)
    value = _HEX_RE.sub("<redacted hex>", value)
    return value


def _looks_like_file_payload(value: Any) -> bool:
    return isinstance(value, dict) and any(
        k in value
        for k in {
            "content",
            "path",
            "lines",
            "total_lines",
            "file_size",
            "is_binary",
            "is_image",
        }
    )


def _redact_payload(value: Any, max_len: int = 8192) -> Any:
    if isinstance(value, str):
        return _redact_string(value, max_len)
    if isinstance(value, (int, float, bool)) or value is None:
        return value
    if isinstance(value, bytes):
        return f"<bytes:{len(value)}>"
    if isinstance(value, list):
        return [_redact_payload(item, max_len) for item in value]
    if isinstance(value, dict):
        out: Dict[str, Any] = {}
        for k, v in value.items():
            if _is_secret_key(k):
                out[k] = "<redacted>"
                continue
            if _looks_like_file_payload(v):
                out[k] = _redact_file_payload(v, max_len)
                continue
            out[k] = _redact_payload(v, max_len)
        return out
    return _redact_string(str(value), max_len)


def _redact_file_payload(value: Dict[str, Any], max_len: int) -> Dict[str, Any]:
    out = dict(value)
    # Drop raw file content; keep structural metadata so the trace is useful.
    out.pop("content", None)
    out.pop("lines", None)
    out.pop("text", None)
    if "path" in out and isinstance(out["path"], str):
        out["path"] = _redact_string(out["path"], max_len)
    out["_redacted"] = "file content omitted"
    return out


# ---------------------------------------------------------------------------
# Trace model
# ---------------------------------------------------------------------------


class _Span:
    def __init__(
        self,
        span_id: str,
        parent_id: Optional[str],
        name: str,
        start_time_ns: int,
        attributes: Dict[str, Any],
    ) -> None:
        self.span_id = span_id
        self.parent_id = parent_id
        self.name = name
        self.start_time_ns = start_time_ns
        self.attributes = attributes
        self.events: List[Dict[str, Any]] = []
        self.status = "unset"
        self.status_message = ""
        self.end_time_ns: Optional[int] = None
        self.children: Dict[str, "_Span"] = {}

    def add_event(self, name: str, attributes: Dict[str, Any]) -> None:
        self.events.append({
            "name": name,
            "timestamp_ns": time.time_ns(),
            "attributes": _redact_payload(attributes),
        })

    def set_status(self, status: str, message: str = "") -> None:
        self.status = status
        if message:
            self.status_message = message

    def end(
        self,
        end_time_ns: Optional[int] = None,
        attributes: Optional[Dict[str, Any]] = None,
    ) -> None:
        self.end_time_ns = end_time_ns or time.time_ns()
        if attributes:
            self.attributes.update(attributes)

    def to_dict(self) -> Dict[str, Any]:
        out: Dict[str, Any] = {
            "span_id": self.span_id,
            "parent_id": self.parent_id,
            "name": self.name,
            "start_time_ns": self.start_time_ns,
            "end_time_ns": self.end_time_ns,
            "attributes": _redact_payload(self.attributes),
            "events": self.events,
            "status": self.status,
        }
        if self.status_message:
            out["status_message"] = self.status_message
        return out


class _Trace:
    def __init__(
        self, trace_id: str, session_id: str, task_id: str, settings: _Settings
    ) -> None:
        self.trace_id = trace_id
        self.session_id = session_id
        self.task_id = task_id
        self.settings = settings
        self.root_span_id = _new_span_id()
        self.spans: Dict[str, _Span] = {}
        self._closed = False
        root = _Span(
            span_id=self.root_span_id,
            parent_id=None,
            name="hermes.session",
            start_time_ns=time.time_ns(),
            attributes={"session_id": session_id, "task_id": task_id},
        )
        self.spans[self.root_span_id] = root

    @property
    def root(self) -> _Span:
        return self.spans[self.root_span_id]

    def start_span(
        self,
        name: str,
        parent_id: Optional[str] = None,
        attributes: Optional[Dict[str, Any]] = None,
    ) -> _Span:
        span_id = _new_span_id()
        span = _Span(
            span_id=span_id,
            parent_id=parent_id or self.root_span_id,
            name=name,
            start_time_ns=time.time_ns(),
            attributes=_redact_payload(attributes or {}),
        )
        self.spans[span_id] = span
        parent = self.spans.get(span.parent_id)
        if parent is not None:
            parent.children[span_id] = span
        return span

    def get_span(self, span_id: str) -> Optional[_Span]:
        return self.spans.get(span_id)

    def add_event(self, name: str, attributes: Dict[str, Any]) -> None:
        self.root.add_event(name, attributes)

    def close(self, reason: str = "") -> None:
        if self._closed:
            return
        self._closed = True
        now = time.time_ns()
        for span in self.spans.values():
            if span.end_time_ns is None:
                span.end(now, {"close_reason": reason or "orphaned"})

    def to_dict(self) -> Dict[str, Any]:
        return {
            "schema_version": _SCHEMA_VERSION,
            "trace_id": self.trace_id,
            "session_id": self.session_id,
            "task_id": self.task_id,
            "service_name": self.settings.service_name,
            "service_version": self.settings.service_version,
            "environment": self.settings.environment,
            "exported_at_ns": time.time_ns(),
            "spans": [span.to_dict() for span in self.spans.values()],
        }


# ---------------------------------------------------------------------------
# Trace state management
# ---------------------------------------------------------------------------


class _Tracer:
    def __init__(self, settings: _Settings) -> None:
        self.settings = settings
        self._traces: Dict[str, _Trace] = {}
        self._lock = threading.RLock()
        self._exporter: "_Exporter" = _build_exporter(settings)
        self._sampler = _Sampler(settings.sample_rate)
        self._last_flush = 0.0

    def _key(self, session_id: str, task_id: str) -> str:
        if task_id:
            return f"task:{task_id}"
        if session_id:
            return f"session:{session_id}"
        return f"thread:{threading.get_ident()}"

    def get_trace_id(self, session_id: str = "", task_id: str = "") -> Optional[str]:
        key = self._key(session_id, task_id)
        with self._lock:
            trace = self._traces.get(key)
            return trace.trace_id if trace else None

    def get_trace(self, session_id: str = "", task_id: str = "") -> Optional[_Trace]:
        key = self._key(session_id, task_id)
        with self._lock:
            return self._traces.get(key)

    def start_trace(
        self,
        session_id: str,
        task_id: str,
        attributes: Optional[Dict[str, Any]] = None,
    ) -> _Trace:
        key = self._key(session_id, task_id)
        with self._lock:
            existing = self._traces.get(key)
            if existing is not None:
                if attributes:
                    existing.root.attributes.update(_redact_payload(attributes))
                return existing
            trace_id = _new_trace_id()
            trace = _Trace(trace_id, session_id, task_id, self.settings)
            if attributes:
                trace.root.attributes.update(_redact_payload(attributes))
            self._traces[key] = trace
            return trace

    def end_trace(self, session_id: str, task_id: str, reason: str = "") -> None:
        key = self._key(session_id, task_id)
        with self._lock:
            trace = self._traces.pop(key, None)
        if trace is None:
            return
        trace.close(reason)
        self._export(trace)

    def start_span(
        self,
        session_id: str,
        task_id: str,
        name: str,
        parent_id: Optional[str] = None,
        attributes: Optional[Dict[str, Any]] = None,
    ) -> Optional[_Span]:
        trace = self.start_trace(session_id, task_id)
        return trace.start_span(name, parent_id=parent_id, attributes=attributes)

    def end_span(
        self,
        span_id: str,
        session_id: str,
        task_id: str,
        attributes: Optional[Dict[str, Any]] = None,
    ) -> None:
        trace = self.get_trace(session_id, task_id)
        if trace is None:
            return
        span = trace.get_span(span_id)
        if span is None:
            return
        span.end(attributes=_redact_payload(attributes or {}))

    def _export(self, trace: _Trace) -> None:
        if not self._sampler.sample(trace.trace_id):
            return
        try:
            self._exporter.export(trace)
        except Exception as exc:
            logger.debug("otel export failed: %s", exc)

        # Cheap periodic flush for OTLP batches.
        if isinstance(self._exporter, _OtlpHttpExporter):
            now = time.time()
            if now - self._last_flush > 5.0:
                try:
                    self._exporter.flush()
                    self._last_flush = now
                except Exception as exc:
                    logger.debug("otel OTLP flush failed: %s", exc)

    def flush(self) -> None:
        if isinstance(self._exporter, _OtlpHttpExporter):
            try:
                self._exporter.flush()
            except Exception as exc:
                logger.debug("otel flush failed: %s", exc)


# ---------------------------------------------------------------------------
# Sampling
# ---------------------------------------------------------------------------


class _Sampler:
    def __init__(self, rate: float) -> None:
        self.rate = rate

    def sample(self, trace_id: str) -> bool:
        if self.rate >= 1.0:
            return True
        if self.rate <= 0.0:
            return False
        # Deterministic sampling based on trace_id (UUID hex). Stable per trace.
        try:
            value = int(trace_id.replace("-", "")[:16], 16)
        except Exception:
            value = hash(trace_id)
        return (value % 10000) < int(self.rate * 10000)


# ---------------------------------------------------------------------------
# Exporters
# ---------------------------------------------------------------------------


class _Exporter:
    def export(self, trace: _Trace) -> None:
        raise NotImplementedError


class _JsonlExporter(_Exporter):
    def __init__(self, settings: _Settings) -> None:
        self.settings = settings
        self.output_dir = settings.output_dir
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()

    def export(self, trace: _Trace) -> None:
        path = self.output_dir / f"hermes-otel-{time.strftime('%Y-%m-%d')}.jsonl"
        line = json.dumps(trace.to_dict(), default=_json_default, ensure_ascii=False)
        with self._lock:
            with path.open("a", encoding="utf-8") as f:
                f.write(line + "\n")


class _OtlpHttpExporter(_Exporter):
    """Minimal OTLP trace exporter using stdlib HTTP.

    Implements the protobuf-JSON encoding subset needed for spans with
    attributes and events. No external dependencies required.
    """

    def __init__(self, settings: _Settings) -> None:
        self.endpoint = settings.endpoint.rstrip("/")
        if "/v1/traces" not in self.endpoint:
            self.endpoint = self.endpoint + "/v1/traces"
        self.headers = dict(settings.headers)
        self.headers.setdefault("Content-Type", "application/json")
        self._queue: List[Dict[str, Any]] = []
        self._lock = threading.Lock()
        self._max_queue = 100

    def export(self, trace: _Trace) -> None:
        resource_spans = _trace_to_otlp(trace)
        with self._lock:
            self._queue.append(resource_spans)
            if len(self._queue) > self._max_queue:
                self._queue.pop(0)
        # Synchronous send for immediate feedback; batching happens in flush().
        self._send([resource_spans])

    def flush(self) -> None:
        with self._lock:
            batch = self._queue
            self._queue = []
        if batch:
            self._send(batch)

    def _send(self, resource_spans_list: List[Dict[str, Any]]) -> None:
        payload = {"resourceSpans": resource_spans_list}
        body = json.dumps(payload, default=_json_default, ensure_ascii=False).encode(
            "utf-8"
        )
        req = urllib.request.Request(
            url=self.endpoint,
            data=body,
            headers=self.headers,
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                resp.read()
        except urllib.error.HTTPError as exc:
            logger.warning("otel OTLP HTTP %s: %s", exc.code, exc.reason)
        except Exception as exc:
            logger.warning("otel OTLP send failed: %s", exc)


def _build_exporter(settings: _Settings) -> _Exporter:
    if settings.exporter == "otlp" or settings.endpoint:
        if not settings.endpoint:
            logger.warning(
                "otel exporter=otlp but no endpoint configured; falling back to jsonl"
            )
            return _JsonlExporter(settings)
        return _OtlpHttpExporter(settings)
    return _JsonlExporter(settings)


def _trace_to_otlp(trace: _Trace) -> Dict[str, Any]:
    """Convert a _Trace to OTLP ResourceSpans JSON structure."""
    resource_attrs: List[Dict[str, Any]] = [
        {"key": "service.name", "value": {"stringValue": trace.settings.service_name}},
        {
            "key": "service.version",
            "value": {"stringValue": trace.settings.service_version},
        },
    ]
    if trace.settings.environment:
        resource_attrs.append({
            "key": "deployment.environment",
            "value": {"stringValue": trace.settings.environment},
        })

    scope_spans = [
        {
            "scope": {"name": "hermes.opentelemetry", "version": _SCHEMA_VERSION},
            "spans": [],
        }
    ]
    for span in trace.spans.values():
        sdict = {
            "traceId": _hex16(trace.trace_id),
            "spanId": _hex8(span.span_id),
            "parentSpanId": _hex8(span.parent_id) if span.parent_id else "",
            "name": span.name,
            "kind": 1,  # SPAN_KIND_INTERNAL
            "startTimeUnixNano": str(span.start_time_ns),
            "endTimeUnixNano": str(span.end_time_ns or time.time_ns()),
            "attributes": _attrs_to_otlp(span.attributes),
            "events": [
                {
                    "name": ev["name"],
                    "timeUnixNano": str(ev["timestamp_ns"]),
                    "attributes": _attrs_to_otlp(ev["attributes"]),
                }
                for ev in span.events
            ],
            "status": {
                "code": _status_to_otlp(span.status),
                "message": span.status_message,
            },
        }
        scope_spans[0]["spans"].append(sdict)

    return {"resource": {"attributes": resource_attrs}, "scopeSpans": scope_spans}


def _attrs_to_otlp(value: Any) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    if isinstance(value, dict):
        for k, v in value.items():
            out.append({"key": str(k), "value": _value_to_otlp(v)})
    return out


def _value_to_otlp(value: Any) -> Dict[str, Any]:
    if isinstance(value, bool):
        return {"boolValue": value}
    if isinstance(value, int):
        return {"intValue": str(value)}
    if isinstance(value, float):
        return {"doubleValue": value}
    if isinstance(value, list):
        return {"arrayValue": {"values": [_value_to_otlp(v) for v in value]}}
    return {"stringValue": _truncate(str(value), 8192)}


def _status_to_otlp(status: str) -> int:
    mapping = {"ok": 1, "unset": 0, "error": 2}
    return mapping.get(status, 0)


def _hex16(value: str) -> str:
    """Convert a UUID-like string to a 32-hex-char trace ID."""
    hex_part = value.replace("-", "")
    if len(hex_part) >= 32:
        return hex_part[:32]
    return (hex_part + "0" * 32)[:32]


def _hex8(value: str) -> str:
    hex_part = value.replace("-", "")
    if len(hex_part) >= 16:
        return hex_part[:16]
    return (hex_part + "0" * 16)[:16]


def _json_default(obj: Any) -> Any:
    if isinstance(obj, Path):
        return str(obj)
    raise TypeError(f"Object of type {type(obj).__name__} is not JSON serializable")


# ---------------------------------------------------------------------------
# Initialization
# ---------------------------------------------------------------------------


def _get_tracer() -> Optional[_Tracer]:
    global _TRACER
    if _TRACER is _INIT_FAILED:
        return None
    if _TRACER is not None:
        return _TRACER  # type: ignore[return-type]
    if not _is_enabled():
        _TRACER = _INIT_FAILED
        return None
    settings = _settings()
    if not settings.enabled:
        _TRACER = _INIT_FAILED
        return None
    _TRACER = _Tracer(settings)
    return _TRACER


def _new_trace_id() -> str:
    return str(uuid.uuid4())


def _new_span_id() -> str:
    return str(uuid.uuid4())


# ---------------------------------------------------------------------------
# Hook callbacks
# ---------------------------------------------------------------------------


def on_session_start(
    *,
    session_id: str = "",
    task_id: str = "",
    platform: str = "",
    model: str = "",
    provider: str = "",
    **_: Any,
) -> None:
    tracer = _get_tracer()
    if tracer is None:
        return
    tracer.start_trace(
        session_id,
        task_id,
        attributes={
            "event": "session_start",
            "platform": platform,
            "model": model,
            "provider": provider,
        },
    )


def on_session_end(
    *,
    session_id: str = "",
    task_id: str = "",
    completed: bool = False,
    interrupted: bool = False,
    reason: str = "",
    **_: Any,
) -> None:
    tracer = _get_tracer()
    if tracer is None:
        return
    trace = tracer.get_trace(session_id, task_id)
    if trace is not None:
        trace.root.add_event(
            "session_end",
            {"completed": completed, "interrupted": interrupted, "reason": reason},
        )
    tracer.end_trace(session_id, task_id, reason=reason or "session_end")


def on_session_finalize(*, session_id: str = "", task_id: str = "", **_: Any) -> None:
    on_session_end(session_id=session_id, task_id=task_id, reason="finalize")


def on_session_reset(
    *, old_session_id: str = "", new_session_id: str = "", **_: Any
) -> None:
    tracer = _get_tracer()
    if tracer is None:
        return
    if old_session_id:
        tracer.end_trace(old_session_id, "", reason="reset")
    if new_session_id:
        tracer.start_trace(
            new_session_id,
            "",
            attributes={"event": "session_reset", "old_session_id": old_session_id},
        )


def pre_llm_call(
    *,
    session_id: str = "",
    task_id: str = "",
    turn_id: str = "",
    user_message: Any = None,
    conversation_history: Any = None,
    model: str = "",
    platform: str = "",
    **_: Any,
) -> None:
    tracer = _get_tracer()
    if tracer is None:
        return
    trace = tracer.start_trace(session_id, task_id)
    span = trace.start_span(
        "hermes.turn",
        attributes={"turn_id": turn_id, "model": model, "platform": platform},
    )
    # Keep a weak mapping from turn_id to span id for API call parentage.
    if turn_id:
        trace.root.attributes[f"_turn_span:{turn_id}"] = span.span_id
    span.add_event(
        "turn_start",
        {
            "user_message": _truncate(str(user_message or ""), 240),
            "history_length": len(conversation_history)
            if isinstance(conversation_history, list)
            else 0,
        },
    )


def post_llm_call(
    *,
    session_id: str = "",
    task_id: str = "",
    turn_id: str = "",
    assistant_response: Any = None,
    **_: Any,
) -> None:
    tracer = _get_tracer()
    if tracer is None:
        return
    trace = tracer.get_trace(session_id, task_id)
    if trace is None:
        return
    span_id = trace.root.attributes.get(f"_turn_span:{turn_id}") if turn_id else None
    span = trace.get_span(span_id) if span_id else trace.root
    if span is None:
        return
    span.add_event(
        "turn_end",
        {
            "assistant_response_chars": len(assistant_response)
            if isinstance(assistant_response, str)
            else 0
        },
    )
    span.end()


def pre_api_request(
    *,
    session_id: str = "",
    task_id: str = "",
    turn_id: str = "",
    api_request_id: str = "",
    api_call_count: int = 0,
    model: str = "",
    provider: str = "",
    platform: str = "",
    base_url: str = "",
    api_mode: str = "",
    approx_input_tokens: int = 0,
    **_: Any,
) -> None:
    tracer = _get_tracer()
    if tracer is None:
        return
    trace = tracer.start_trace(session_id, task_id)
    parent_id = trace.root.attributes.get(f"_turn_span:{turn_id}") if turn_id else None
    span = trace.start_span(
        "hermes.llm_request",
        parent_id=parent_id,
        attributes={
            "api_request_id": api_request_id,
            "api_call_count": api_call_count,
            "model": model,
            "provider": provider,
            "platform": platform,
            "base_url": base_url,
            "api_mode": api_mode,
            "approx_input_tokens": approx_input_tokens,
        },
    )
    if api_request_id:
        trace.root.attributes[f"_api_span:{api_request_id}"] = span.span_id


def post_api_request(
    *,
    session_id: str = "",
    task_id: str = "",
    turn_id: str = "",
    api_request_id: str = "",
    api_duration: float = 0.0,
    usage: Any = None,
    finish_reason: str = "",
    **_: Any,
) -> None:
    tracer = _get_tracer()
    if tracer is None:
        return
    trace = tracer.get_trace(session_id, task_id)
    if trace is None:
        return
    span_id = (
        trace.root.attributes.get(f"_api_span:{api_request_id}")
        if api_request_id
        else None
    )
    span = trace.get_span(span_id) if span_id else None
    if span is None:
        return
    attrs: Dict[str, Any] = {
        "api_duration_s": round(api_duration, 3),
        "finish_reason": finish_reason,
    }
    if isinstance(usage, dict):
        for key in (
            "input_tokens",
            "output_tokens",
            "cache_read_tokens",
            "cache_write_tokens",
            "reasoning_tokens",
        ):
            value = usage.get(key)
            if value is not None:
                attrs[key] = value
    span.end(attributes=attrs)


def api_request_error(
    *,
    session_id: str = "",
    task_id: str = "",
    api_request_id: str = "",
    api_duration: float = 0.0,
    retry_count: int = 0,
    reason: str = "",
    error: Any = None,
    **_: Any,
) -> None:
    tracer = _get_tracer()
    if tracer is None:
        return
    trace = tracer.get_trace(session_id, task_id)
    if trace is None:
        return
    span_id = (
        trace.root.attributes.get(f"_api_span:{api_request_id}")
        if api_request_id
        else None
    )
    span = trace.get_span(span_id) if span_id else None
    if span is None:
        return
    span.set_status("error", str(reason or error or "api error"))
    span.add_event(
        "api_error",
        {"retry_count": retry_count, "error": str(error) if error else reason},
    )
    span.end(attributes={"api_duration_s": round(api_duration, 3)})


def pre_tool_call(
    *,
    tool_name: str = "",
    args: Any = None,
    session_id: str = "",
    task_id: str = "",
    turn_id: str = "",
    tool_call_id: str = "",
    api_request_id: str = "",
    **_: Any,
) -> None:
    tracer = _get_tracer()
    if tracer is None:
        return
    trace = tracer.start_trace(session_id, task_id)
    parent_id = (
        trace.root.attributes.get(f"_api_span:{api_request_id}")
        if api_request_id
        else None
    )
    if parent_id is None and turn_id:
        parent_id = trace.root.attributes.get(f"_turn_span:{turn_id}")
    span = trace.start_span(
        f"hermes.tool:{tool_name}",
        parent_id=parent_id,
        attributes={"tool_name": tool_name, "tool_call_id": tool_call_id},
    )
    span.add_event("tool_start", {"args": args})
    if tool_call_id:
        trace.root.attributes[f"_tool_span:{tool_call_id}"] = span.span_id


def post_tool_call(
    *,
    tool_name: str = "",
    args: Any = None,
    result: Any = None,
    session_id: str = "",
    task_id: str = "",
    tool_call_id: str = "",
    duration_ms: float = 0.0,
    status: str = "",
    **_: Any,
) -> None:
    tracer = _get_tracer()
    if tracer is None:
        return
    trace = tracer.get_trace(session_id, task_id)
    if trace is None:
        return
    span_id = (
        trace.root.attributes.get(f"_tool_span:{tool_call_id}")
        if tool_call_id
        else None
    )
    span = trace.get_span(span_id) if span_id else None
    if span is None:
        # Fall back to finding an open tool span by name.
        for candidate in reversed(list(trace.spans.values())):
            if (
                candidate.name == f"hermes.tool:{tool_name}"
                and candidate.end_time_ns is None
            ):
                span = candidate
                break
    if span is None:
        return
    status = status or "ok"
    span.set_status(status)
    span.add_event(
        "tool_end",
        {"status": status, "duration_ms": round(duration_ms, 2), "result": result},
    )
    span.end()


def pre_approval_request(
    *,
    command: str = "",
    description: str = "",
    session_key: str = "",
    **_: Any,
) -> None:
    _add_event_to_root(
        "approval_request",
        {"command": command, "description": description, "session_key": session_key},
        session_id=session_key,
    )


def post_approval_response(
    *,
    command: str = "",
    choice: str = "",
    session_key: str = "",
    **_: Any,
) -> None:
    _add_event_to_root(
        "approval_response",
        {"command": command, "choice": choice, "session_key": session_key},
        session_id=session_key,
    )


def subagent_start(
    *,
    parent_session_id: str = "",
    child_session_id: str = "",
    task_id: str = "",
    goal: str = "",
    **_: Any,
) -> None:
    tracer = _get_tracer()
    if tracer is None:
        return
    # Link child trace to parent context.
    parent_trace = tracer.get_trace(parent_session_id, task_id)
    parent_trace_id = parent_trace.trace_id if parent_trace else ""
    trace = tracer.start_trace(child_session_id, task_id)
    trace.root.attributes["parent_session_id"] = parent_session_id
    trace.root.attributes["parent_trace_id"] = parent_trace_id
    trace.root.attributes["subagent_goal"] = _truncate(str(goal or ""), 240)
    trace.root.add_event("subagent_start", {"goal": goal})


def subagent_stop(
    *,
    child_session_id: str = "",
    task_id: str = "",
    reason: str = "",
    **_: Any,
) -> None:
    tracer = _get_tracer()
    if tracer is None:
        return
    trace = tracer.get_trace(child_session_id, task_id)
    if trace is not None:
        trace.root.add_event("subagent_stop", {"reason": reason})
    tracer.end_trace(child_session_id, task_id, reason=reason or "subagent_stop")


def _add_event_to_root(
    name: str, attributes: Dict[str, Any], session_id: str = "", task_id: str = ""
) -> None:
    tracer = _get_tracer()
    if tracer is None:
        return
    trace = tracer.get_trace(session_id, task_id)
    if trace is None:
        return
    trace.root.add_event(name, attributes)


# ---------------------------------------------------------------------------
# Plugin registration
# ---------------------------------------------------------------------------


def register(ctx) -> None:
    ctx.register_hook("on_session_start", on_session_start)
    ctx.register_hook("on_session_end", on_session_end)
    ctx.register_hook("on_session_finalize", on_session_finalize)
    ctx.register_hook("on_session_reset", on_session_reset)
    ctx.register_hook("pre_llm_call", pre_llm_call)
    ctx.register_hook("post_llm_call", post_llm_call)
    ctx.register_hook("pre_api_request", pre_api_request)
    ctx.register_hook("post_api_request", post_api_request)
    ctx.register_hook("api_request_error", api_request_error)
    ctx.register_hook("pre_tool_call", pre_tool_call)
    ctx.register_hook("post_tool_call", post_tool_call)
    ctx.register_hook("pre_approval_request", pre_approval_request)
    ctx.register_hook("post_approval_response", post_approval_response)
    ctx.register_hook("subagent_start", subagent_start)
    ctx.register_hook("subagent_stop", subagent_stop)


# ---------------------------------------------------------------------------
# Entry-point helpers (CLI / cron / evolution reports can call these safely)
# ---------------------------------------------------------------------------


def format_trace_link(trace_id: str, base_url: str = "") -> str:
    """Return a human-readable trace reference string."""
    if base_url:
        return f"{base_url.rstrip('/')}/traces/{trace_id}"
    return trace_id
