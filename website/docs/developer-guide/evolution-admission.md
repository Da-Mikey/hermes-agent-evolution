# Evolution admission receipts

Autonomous `evolution/issue-*` PRs must pass class-specific admission in
`scripts/evolution_merge_gate.py`. Other PRs retain their existing policy.

The gate classifies changed paths as memory/skill documents, tools, control
logic, or plugin composition. Mixed changes require the union of their checks.
All classes need the successful CI aggregate; tool/plugin changes additionally
need Python E2E, and control changes also need the Windows footgun check.
The policy table is `CLASS_GATES` in `scripts/evolution_admission.py`.

Evidence comes directly from the repository's GitHub Actions API, for the exact
reviewed SHA, the latest pull-request run of `.github/workflows/ci.yaml`, and its
current attempt. Local scorecards cannot substitute for these receipts. Missing,
failed, skipped, malformed, future-dated, or older-than-seven-day evidence blocks
admission with a diagnostic. Workflow/action changes still require human review;
a candidate cannot change its own verifier and attest to itself.

Immediately before the atomic merge call, the gate records the receipt in
`<HERMES_HOME>/evolution/admission.sqlite3`. The cycle is the issue branch; the
repository, cycle, and SHA form an idempotency key. A SQLite immediate transaction
serializes admission accounting. Rechecking or retrying a SHA never spends its
budget twice. A failed merge leaves the admission receipt, not a claim that the
merge succeeded. Each record includes its classes and verifier run/attempt.

The autonomous regression budget is zero: successful independent CI attests to
zero observed CI regressions, not proof that no untested behavior regressed.
Existing example-level flip gates remain additional requirements. Unlike GRASP's
improvement-oriented scoring, admission never expands its budget to tolerate
baseline failures. The existing line cap, high-risk path checks, and atomic
head-SHA merge protection remain in force.

Validation data independence is checked separately from execution provenance:
the run's immutable PR baseline is compared to the candidate. Existing baseline
tests, fixtures and evals cannot be edited, renamed or deleted by a self-admitted
change; test runners, configuration, hooks and workflow machinery cannot be
changed either. Added tests supplement the baseline but never replace it. All
baseline Python test slices must be present, and their actual execution steps
must succeed. Class-specific validation steps cannot be skipped under a green
job. This deliberately routes baseline-test changes and docs-only runs that skip
the baseline suite to human review, with a missing-evidence diagnostic. It does
not infer a successful replay from candidate-authored narrative reports.
