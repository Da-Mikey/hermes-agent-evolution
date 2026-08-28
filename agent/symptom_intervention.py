"""Symptom-driven intervention engine for failure diagnosis and repair.

Implements #3284: Replaces unguided rerun/resample loops with deterministic,
symptom-anchored interventions based on empirical failure classification.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional


class SymptomCategory(str, Enum):
    syntax_error = "syntax_error"
    import_error = "import_error"
    assertion_failure = "assertion_failure"
    file_not_found = "file_not_found"
    permission_denied = "permission_denied"
    rate_limit_stall = "rate_limit_stall"
    timeout_stall = "timeout_stall"
    schema_violation = "schema_violation"
    unknown = "unknown"


@dataclass(frozen=True)
class SymptomClassification:
    category: SymptomCategory
    target_file: Optional[str] = None
    target_line: Optional[int] = None
    failing_symbol: Optional[str] = None
    root_cause_snippet: str = ""
    is_deterministic: bool = True


@dataclass(frozen=True)
class InterventionPlan:
    symptom: SymptomClassification
    directive: str
    suggested_action: str
    prevent_blind_retry: bool = True

    def render_markdown(self) -> str:
        lines = [
            f"[SYMPTOM INTERVENTION: {self.symptom.category.value.upper()}]",
            f"• Root Cause: {self.symptom.root_cause_snippet or self.symptom.category.value}",
        ]
        if self.symptom.target_file:
            loc = self.symptom.target_file
            if self.symptom.target_line:
                loc += f":{self.symptom.target_line}"
            lines.append(f"• Location: {loc}")
        if self.symptom.failing_symbol:
            lines.append(f"• Symbol: {self.symptom.failing_symbol}")
        lines.append(f"• Directive: {self.directive}")
        lines.append(f"• Action: {self.suggested_action}")
        return "\n".join(lines)


_SYNTAX_RE = re.compile(
    r'(?:SyntaxError|IndentationError|TabError):\s*([^\n]+)|(?:File\s+"([^"]+)",\s+line\s+(\d+))',
    re.IGNORECASE,
)
_IMPORT_RE = re.compile(
    r'(?:ModuleNotFoundError|ImportError):\s*(?:No module named\s+[\'"]([^\'"]+)[\'"]|cannot import name\s+[\'"]([^\'"]+)[\'"])',
    re.IGNORECASE,
)
_ASSERT_RE = re.compile(
    r'(?:AssertionError|assert\s+[^\n]+|E\s+AssertionError:\s*([^\n]+))',
    re.IGNORECASE,
)
_FNF_RE = re.compile(
    r'(?:FileNotFoundError|NoSuchFileException|No such file or directory).*?:\s*[\'"]?([^\n\'"]+)[\'"]?',
    re.IGNORECASE,
)
_PERM_RE = re.compile(
    r'(?:PermissionError|AccessDeniedException|Permission denied)',
    re.IGNORECASE,
)
_RATE_LIMIT_RE = re.compile(
    r'(?:429\s+Too\s+Many\s+Requests|rate_limit_exceeded|QuotaExceeded|rate limit reached)',
    re.IGNORECASE,
)
_TIMEOUT_RE = re.compile(
    r'(?:TimeoutError|Timed out after|stale timeout|deadline exceeded)',
    re.IGNORECASE,
)
_SCHEMA_RE = re.compile(
    r'(?:ValidationError|SchemaError|JSONDecodeError|invalid JSON|schema validation failed)',
    re.IGNORECASE,
)


def classify_failure_symptom(
    error_text: str, tool_name: Optional[str] = None
) -> SymptomClassification:
    """Classify a raw error output or trace failure into a structured symptom."""
    text = (error_text or "").strip()
    if not text:
        return SymptomClassification(
            category=SymptomCategory.unknown,
            root_cause_snippet="empty error output",
            is_deterministic=False,
        )

    # 1. Syntax / Indentation Error
    m_syntax = _SYNTAX_RE.search(text)
    if m_syntax:
        # Extract file / line if present
        file_matches = re.findall(r'File\s+"([^"]+)",\s+line\s+(\d+)', text)
        target_file = file_matches[-1][0] if file_matches else None
        target_line = int(file_matches[-1][1]) if file_matches else None
        snippet = m_syntax.group(1) or m_syntax.group(0)
        return SymptomClassification(
            category=SymptomCategory.syntax_error,
            target_file=target_file,
            target_line=target_line,
            root_cause_snippet=snippet.strip(),
            is_deterministic=True,
        )

    # 2. Module / Import Error
    m_import = _IMPORT_RE.search(text)
    if m_import:
        symbol = m_import.group(1) or m_import.group(2) or ""
        return SymptomClassification(
            category=SymptomCategory.import_error,
            failing_symbol=symbol.strip(),
            root_cause_snippet=m_import.group(0).strip(),
            is_deterministic=True,
        )

    # 3. Assertion Failure
    m_assert = _ASSERT_RE.search(text)
    if m_assert:
        # Extract test file/line if present
        test_file_match = re.search(r'([\w\-\./]+test[\w\-\.]*\.py):(\d+):', text)
        t_file = test_file_match.group(1) if test_file_match else None
        t_line = int(test_file_match.group(2)) if test_file_match else None
        return SymptomClassification(
            category=SymptomCategory.assertion_failure,
            target_file=t_file,
            target_line=t_line,
            root_cause_snippet=m_assert.group(0).strip(),
            is_deterministic=True,
        )

    # 4. File Not Found
    if "filenotfounderror" in text.lower() or "no such file or directory" in text.lower():
        path_match = re.search(
            r'(?:No such file or directory|FileNotFoundError).*?[\'"]([^\n\'"]+)[\'"]',
            text,
        )
        missing_path = path_match.group(1) if path_match else None
        return SymptomClassification(
            category=SymptomCategory.file_not_found,
            target_file=missing_path.strip() if missing_path else None,
            root_cause_snippet="File or directory not found",
            is_deterministic=True,
        )

    # 5. Permission Denied
    if _PERM_RE.search(text):
        return SymptomClassification(
            category=SymptomCategory.permission_denied,
            root_cause_snippet="Permission denied or insufficient access rights",
            is_deterministic=True,
        )

    # 6. Rate Limit
    if _RATE_LIMIT_RE.search(text):
        return SymptomClassification(
            category=SymptomCategory.rate_limit_stall,
            root_cause_snippet="Rate limit / quota exceeded on remote provider",
            is_deterministic=False,
        )

    # 7. Timeout
    if _TIMEOUT_RE.search(text):
        return SymptomClassification(
            category=SymptomCategory.timeout_stall,
            root_cause_snippet="Execution or network request timed out",
            is_deterministic=False,
        )

    # 8. Schema Violation
    m_schema = _SCHEMA_RE.search(text)
    if m_schema:
        return SymptomClassification(
            category=SymptomCategory.schema_violation,
            root_cause_snippet=m_schema.group(0).strip(),
            is_deterministic=True,
        )

    return SymptomClassification(
        category=SymptomCategory.unknown,
        root_cause_snippet=text.split("\n")[0][:100],
        is_deterministic=False,
    )


def plan_symptom_intervention(
    symptom: SymptomClassification,
) -> InterventionPlan:
    """Generate a deterministic intervention plan to repair the diagnosed symptom."""
    cat = symptom.category
    if cat == SymptomCategory.syntax_error:
        loc = f" at {symptom.target_file}:{symptom.target_line}" if symptom.target_file else ""
        return InterventionPlan(
            symptom=symptom,
            directive=f"Do NOT rerun unchanged code. Fix syntax error{loc} first.",
            suggested_action="Read the exact lines around the error and apply targeted patch.",
            prevent_blind_retry=True,
        )

    if cat == SymptomCategory.import_error:
        sym = f" '{symptom.failing_symbol}'" if symptom.failing_symbol else ""
        return InterventionPlan(
            symptom=symptom,
            directive=f"Missing module or import{sym}. Verify import paths or requirements.",
            suggested_action="Check package installation via package manager or correct the module path.",
            prevent_blind_retry=True,
        )

    if cat == SymptomCategory.assertion_failure:
        return InterventionPlan(
            symptom=symptom,
            directive="Test assertion failed. Inspect the expected invariant versus actual output.",
            suggested_action="Review test logic, check actual variable states, and update implementation.",
            prevent_blind_retry=True,
        )

    if cat == SymptomCategory.file_not_found:
        f = f" '{symptom.target_file}'" if symptom.target_file else ""
        return InterventionPlan(
            symptom=symptom,
            directive=f"Target file{f} not found. Check relative vs absolute path and verify directory tree.",
            suggested_action="Use search_files or list_dir to locate the target path before accessing.",
            prevent_blind_retry=True,
        )

    if cat == SymptomCategory.permission_denied:
        return InterventionPlan(
            symptom=symptom,
            directive="Action blocked by OS permission or security policy.",
            suggested_action="Verify file write permissions or use a permitted workspace location.",
            prevent_blind_retry=True,
        )

    if cat == SymptomCategory.rate_limit_stall:
        return InterventionPlan(
            symptom=symptom,
            directive="Provider rate limited. Exponential backoff or provider fallback required.",
            suggested_action="Wait for cooldown or switch to fallback model/provider.",
            prevent_blind_retry=False,
        )

    if cat == SymptomCategory.timeout_stall:
        return InterventionPlan(
            symptom=symptom,
            directive="Operation timed out. Simplify workload or extend operation timeout.",
            suggested_action="Break task into smaller sub-tasks or increase timeout configuration.",
            prevent_blind_retry=False,
        )

    if cat == SymptomCategory.schema_violation:
        return InterventionPlan(
            symptom=symptom,
            directive="Output failed schema validation. Format output strictly per required schema.",
            suggested_action="Validate JSON keys and data types against schema before submitting.",
            prevent_blind_retry=True,
        )

    return InterventionPlan(
        symptom=symptom,
        directive="Unknown error encountered. Diagnose logs before proceeding.",
        suggested_action="Inspect full failure stack trace and verify prerequisites.",
        prevent_blind_retry=False,
    )


def apply_symptom_intervention(
    error_text: str, tool_name: Optional[str] = None
) -> str:
    """Classify and append symptom intervention directive to error text."""
    symptom = classify_failure_symptom(error_text, tool_name)
    plan = plan_symptom_intervention(symptom)
    rendered = plan.render_markdown()
    if error_text and str(error_text).strip():
        return f"{str(error_text).strip()}\n\n{rendered}"
    return rendered
