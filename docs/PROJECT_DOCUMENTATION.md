# Project Documentation: AI Support Ticket Analyst

A retrospective on how this project was built, for the record now that it is finished. The [README](../README.md) documents the system as it stands for anyone using or evaluating it; this document covers how it got there — the assessment it answers, the decisions locked in early, the traps that shaped the design, and what the finished build actually proves.

---

## 1. The Assessment

Built for the **AI Engineer internship assessment at DOTMappers IT Pvt. Ltd.**, delivered as a GitHub repository.

The task: take a 500-row customer support ticket CSV and build a system that answers natural-language questions about it and flags operational anomalies, exposed through both a REST API and a UI, using an LLM at zero cost, startable with a single command.

The brief's own scoring weights made the priority explicit — Architecture & Design carried 25%, and its closing section stated that candidates are judged on "the reasoning behind your choices as much as the working code." That shaped everything below: every non-obvious decision in this system has a written reason, in the [README's Design Decisions](../README.md#design-decisions) and in the [CHANGELOG](../CHANGELOG.md), not just a working implementation.

---

## 2. Reading the Brief Before Writing Code

Three things in the brief and data did not match each other, and finding them before building avoided a demo that would have quietly failed in front of an evaluator:

- **Groq's free tier no longer includes any Llama model.** `llama-3.3-70b-versatile`, the obvious choice, is Enterprise-only. `openai/gpt-oss-120b` was confirmed as the largest model actually on the free tier by querying the API directly rather than trusting documentation that could be stale — see `scripts/check_groq.py`, written before any application code, as a standing diagnostic.
- **The brief contradicted itself on required interfaces.** Section 2 bolded "both required" for the API and UI; Section 4 phrased it as a looser "or". Built both, since the stricter reading costs nothing extra and the looser one risks a scoring deduction.
- **The dataset does not match its own schema preview.** The brief's sample schema implied `Escalated` tickets carry resolution time and rating like any other row; the real CSV leaves both null on `Escalated` rows. Trusting the preview over the data would have produced ingestion code that crashed or silently miscounted on 62 real rows.
- **The data is frozen on 2024-03-30, but the sample questions ask about "this week" and "this month".** Resolved against the real clock, every relative-time question returns zero rows — a working system that looks broken in the one demo an evaluator is guaranteed to try. This is why `AS_OF` exists: a time anchor pinned to the dataset's own last timestamp, not to wall-clock time.

---

## 3. Architecture Decided Up Front, and Held

The core architecture was locked after this analysis and not revisited:

> **The LLM is used for natural-language understanding and orchestration only. It never performs arithmetic.**

Concretely: Groq free tier (`openai/gpt-oss-120b`) → a bounded, two-step native tool-calling pipeline → LLM-generated SQL validated as SELECT-only → executed against a read-only SQLite connection → the LLM narrates the real rows that come back. FastAPI serves four endpoints; Streamlit is a thin HTTP client of that API, never a second implementation. Anomaly detection is deterministic pandas with no model in its path at all. Everything starts with `python run.py`.

Holding this decision throughout the build — rather than reaching for an open-ended agent loop or letting the model estimate a figure directly — is what let every later fix be a *tightening* of the design instead of a rewrite of it. The full reasoning behind each piece is in the [README's Architecture](../README.md#architecture) and [Design Decisions](../README.md#design-decisions) sections.

---

## 4. Traps Found During the Build

Several defects were non-obvious enough that they are worth recording separately from the CHANGELOG's line-by-line fixes, because each one is a *class* of mistake likely to recur in future work, not a one-off bug:

- **A blank environment variable is an empty string, not a missing key.** `AS_OF=` in `.env` reached pydantic as `""`, not `None`, so the documented "leave blank to auto-anchor" behavior crashed at startup instead of applying its default. Any optional setting documented as "leave blank" needs an explicit blank-to-`None` normalisation; the type system will not do it for free.
- **A required credential breaks graceful degradation, even when nothing seems to read it directly.** Declaring `groq_api_key` as required made the entire settings import fail without a key — which broke `/anomalies`, `/health`, and the whole test suite's ability to collect, since all of them transitively import settings. Credentials in a system that promises to degrade without them must be optional fields, validated at the point of use, never at import.
- **Windows file URIs need `Path.as_uri()`, never string formatting.** The project path contains a space (`AI Assessment`), which breaks a hand-built `f"file:{path}?mode=ro"` in ways that are easy to miss until SQLite's read-only mode is tested on this exact machine.
- **NaN and NULL are not the same fact, and pandas will blur them without asking.** Unresolved tickets must store SQL `NULL` for resolution time and rating, not `0.0` or `NaN` — either would silently corrupt every average computed over the column. This is why ingestion uses the standard library's `csv` module rather than pandas, despite pandas being the more obvious tool, and why pandas is still used downstream in `anomalies.py`, where its vectorised quantiles genuinely earn the dependency.
- **A reasoning model can return empty content if given too small a token budget** — it spends output tokens thinking before it writes, so a tight `max_tokens` looks like a broken model but is actually a budgeting mistake. Measuring `reasoning_effort="low"` against the default showed prompt size, not reasoning, is what actually dominates the token budget on this schema.

---

## 5. How Correctness Was Actually Verified

Two independent forms of evidence back the finished system, deliberately kept separate:

1. **397 offline unit and integration tests**, running in under 10 seconds with no API key and no network access, using a `ChatClient` protocol so a scripted fake model exercises every code path — including ones a live model would reach only occasionally, such as the repair-retry loop and malformed tool calls.
2. **A 50-question live benchmark against the real model**, with expected answers generated from the data and cross-checked against SQL rather than typed by hand. This is what actually found the bugs that mattered: named months read incorrectly, month arithmetic that silently turned "last month" into "this month", numbers spelled out in words evading the grounding check, and a model refusal once rendered to the user as an unverified five-section essay. None of these were reachable through the mocked unit tests, because each concerns how the model behaves with real language, not how the code executes.

The benchmark pass rate climbed from 95% to 98% to 100% (41 of 41 machine-gradable questions) across three runs, each failure traced to a root cause and closed with a test reproducing it before the fix was written — never a prompt tweak accepted on faith. The full detail, including the one grounding fix made after the final run and independently re-verified live in the UI, is in the [README's Testing and Evaluation section](../README.md#testing-and-evaluation) and [docs/BENCHMARK_RESULTS.md](BENCHMARK_RESULTS.md).

---

## 6. Outcome

**Release 1.0.1** meets every requirement in the brief:

| Requirement | Delivered as |
|---|---|
| Ingest and query the CSV in natural language | SQLite ingestion with strict NULL handling; LLM-generated, guarded, read-only SQL |
| Detect and flag anomalies | Deterministic Tukey-fence and SLA-breach detectors, no LLM involved |
| REST API and UI | FastAPI (4 endpoints, interactive docs) + Streamlit as a thin client |
| Zero-cost LLM | Groq free tier, `openai/gpt-oss-120b` |
| Single-command startup | `python run.py` |

The HTTP API, configuration surface, and startup command are held stable under Semantic Versioning from this release forward, so a future change to any of them is a 2.0.0 decision, not a silent one. What is *not* held stable — model-written answer wording, log messages, UI layout — is stated explicitly, so evaluators and future maintainers know exactly which surface is a contract and which is free to change.

Ten version increments (0.1.0 → 1.0.1) took the project from scaffolding to a stable release, the last fixing five answers found wrong in post-release testing, each one documented in the [CHANGELOG](../CHANGELOG.md) with what changed, what broke, and why the fix was correct rather than merely different.

---

## 7. What Would Change With More Time

Recorded honestly rather than left implicit — the full list, with reasoning, is in the README's [Known Limitations](../README.md#known-limitations) and [Future Improvements](../README.md#future-improvements). The two most worth naming here: the grounding check proves every number in an answer came from real data, but it cannot prove the SQL answered the *intended* question if the model misreads it — that remains the design's most important open limitation. And the 100% benchmark pass rate is one run of fifty questions written by the same person who found and fixed bugs using them, so the system has, in effect, been tuned against its own test — a genuinely independent evaluator's questions are the real test this project has not yet faced.

---

**Author:** VIJAYA KARTHIK 
