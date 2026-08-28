"""Tests for symptom-driven failure classification and intervention (Issue #3284)."""

from agent.symptom_intervention import (
    SymptomCategory,
    apply_symptom_intervention,
    classify_failure_symptom,
    plan_symptom_intervention,
)


def test_classify_syntax_error():
    err = '''  File "agent/my_module.py", line 42
    def bad_syntax(:
                  ^
SyntaxError: invalid syntax'''
    symptom = classify_failure_symptom(err)
    assert symptom.category == SymptomCategory.syntax_error
    assert symptom.target_file == "agent/my_module.py"
    assert symptom.target_line == 42
    assert symptom.is_deterministic is True


def test_classify_import_error():
    err = "ModuleNotFoundError: No module named 'nonexistent_package'"
    symptom = classify_failure_symptom(err)
    assert symptom.category == SymptomCategory.import_error
    assert symptom.failing_symbol == "nonexistent_package"


def test_classify_assertion_failure():
    err = """FAILED tests/test_math.py:15: AssertionError: assert 2 + 2 == 5
E       AssertionError: assert 4 == 5"""
    symptom = classify_failure_symptom(err)
    assert symptom.category == SymptomCategory.assertion_failure
    assert symptom.target_file == "tests/test_math.py"
    assert symptom.target_line == 15


def test_classify_file_not_found():
    err = "FileNotFoundError: [Errno 2] No such file or directory: 'config/missing.yaml'"
    symptom = classify_failure_symptom(err)
    assert symptom.category == SymptomCategory.file_not_found
    assert symptom.target_file == "config/missing.yaml"


def test_classify_rate_limit_and_timeout():
    rate_err = "HTTP 429 Too Many Requests: Rate limit exceeded"
    assert classify_failure_symptom(rate_err).category == SymptomCategory.rate_limit_stall

    timeout_err = "stale timeout after 90.0s waiting for LLM stream response"
    assert classify_failure_symptom(timeout_err).category == SymptomCategory.timeout_stall


def test_plan_symptom_intervention():
    symptom = classify_failure_symptom('SyntaxError: invalid syntax')
    plan = plan_symptom_intervention(symptom)
    assert plan.prevent_blind_retry is True
    assert "Do NOT rerun unchanged code" in plan.directive


def test_apply_symptom_intervention():
    raw_error = "ImportError: cannot import name 'foo' from 'bar'"
    result = apply_symptom_intervention(raw_error)
    assert "[SYMPTOM INTERVENTION: IMPORT_ERROR]" in result
    assert "cannot import name 'foo'" in result
    assert "Verify import paths" in result
