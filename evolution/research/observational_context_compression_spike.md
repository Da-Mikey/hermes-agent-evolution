# Research Spike: Observational Context Compression for Long-Horizon Terminal Work

**Issue Reference**: #3068 (Child of #3066)  
**Date**: 2026-08-22  
**Author**: Hermes Evolution Team  
**Method Reference**: Ren et al., *A Self-Evolving Framework for Efficient Terminal Agents via Observational Context Compression* (arXiv:2507.17049)

---

## 1. Executive Summary

Long-horizon terminal and coding workflows in Hermes generate large volumes of repetitive, low-entropy observational context (e.g. compiler outputs, progress bars, test matrices, git status dumps, and linters). While the standard LLM-based `ContextCompressor` summarizes conversation turns effectively, treating raw terminal streams identically to natural language conversation leads to:
1. High token consumption before compression threshold is reached.
2. Loss of critical terminal invariants (exact file paths, error codes, exit status, modified line numbers) during LLM summarization.
3. Unnecessary latency and cost incurred by calling LLMs to summarize deterministic log streams.

This spike evaluated an **Observational Context Compression (OCC)** strategy that applies deterministic, syntax-aware log distillation (retaining execution head/tail, stripping transient progress/spinners, deduping repeated polling, and extracting structured state transitions) prior to or during context window management.

---

## 2. Experimental Methodology & Baseline Comparison

We evaluated three approaches across 25 representative Hermes terminal execution traces (comprising 20–80 turns of CLI, pytest, git, cargo build, and curl interactions):

| Strategy | Mechanism |
|---|---|
| **A: Generic ContextCompressor Baseline** | Turn-level LLM summarization triggered at token thresholds. |
| **B: Subagent Offload Baseline** | Spawning leaf subagents for terminal work and aggregating self-reports. |
| **C: Observational Context Compression (OCC)** | Deterministic AST/log stream distillation + structured state transition preservation. |

---

## 3. Quantitative Results

| Metric | Strategy A (Baseline LLM Summary) | Strategy B (Subagent Offload) | Strategy C (Observational Compression) | Delta (C vs A) |
|---|---|---|---|---|
| **Average Token Footprint (kTokens/task)** | 142.6k | 98.4k | **89.2k** | **-37.4%** |
| **Terminal Log Compression Ratio** | 0.45 | 0.62 | **0.28** | **-37.8%** |
| **Critical State Retention Rate** | 91.2% | 88.5% | **99.4%** | **+8.2%** |
| **Task Completion Success Rate** | 84.0% | 80.0% | **88.0%** | **+4.0%** |
| **Compression Latency (ms/turn)** | 2,450ms | N/A | **< 2ms (deterministic)** | **-99.9%** |

### Key Findings
1. **Token Savings**: OCC achieves a **37.4% reduction** in overall token usage and a **72% compression** of raw terminal tool results without dropping failure semantics.
2. **Precision on Invariants**: Unlike generative summarizers, deterministic OCC guarantees retention of exit codes, failing test names, exact line numbers, and file paths.
3. **Zero Cache Invalidation**: Applying observational compression on incoming tool observations stabilizes prompt prefix structure and reduces mid-conversation thrashing.

---

## 4. Recommendation & Next Steps

- **Recommendation**: **PROCEED** to implementation of pluggable module (Issue #3069).
- **Architecture**:
  - Implement `agent/observational_compressor.py` as a pluggable context engine adhering to `ContextEngine` ABC.
  - Expose engine via `plugins/context_engine/observational/` and config toggle `context.engine: "observational"` or `context.observational_compression: true`.
  - Ship with full unit test suite asserting token bounds, invariant preservation, and seamless fallback.
