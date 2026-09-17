# Changelog

All notable changes to this project are documented in this file.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and versioning follows [Semantic Versioning](https://semver.org/).

## [0.3.0] - 2026-09-17

The natural-language layer: questions in, grounded answers out. All five of the
brief's sample questions now answer correctly against the live model.

### Added

- A bounded two-step pipeline. The model is forced to call one of two tools,
  the result is computed locally, and a second call narrates the real figures.
  Exactly two model calls per question, plus at most one repair attempt, giving
  a predictable ceiling of roughly 1,500 tokens.
- A `ChatClient` protocol separating orchestration from the Groq SDK. Only one
  class imports Groq; the test suite injects a scripted fake and runs offline
  with no API key and no cost.
- A system prompt carrying the schema, enum values, null semantics, the time
  anchor and worked date arithmetic — measured at roughly 670 tokens, with a
  test that fails if it grows past its ceiling.
- A one-shot repair retry. SQL rejected by the guard, or failing against the
  database, is returned to the model with the specific error so it can correct
  itself. A second failure is reported honestly rather than retried further.
- Typed provider failures: rate limits carry the provider's `Retry-After`
  value, and a missing API key raises at the point of use rather than at
  import, so the deterministic endpoints keep working without credentials.
- Result transparency. Every answer returns the generated SQL, the full row
  set, row count, token usage and elapsed time alongside the prose.
- 51 further tests covering the repair path, row capping, refusals, malformed
  tool calls and provider failures — paths a live model would reach only
  occasionally.

### Fixed

Found by running the pipeline against the live model; none were reachable
through the mocked tests, since all three concern how a model reads text.

- A query returning 34 correct rows was narrated as "No tickets matched". The
  column held mostly NULLs, being unresolved tickets, and the model read that
  as absent data. Results now state their row count explicitly and the prompt
  defines NULL as "not applicable".
- Averages were reported at full floating-point precision
  (`3.7403846153846154`). Generated SQL now rounds them.
- The model echoed the result metadata into its answer as prose ("1 rows
  matched. The average is..."). That line is now bracketed so it reads as
  machine annotation rather than a sentence.

### Decided

- Bounded the pipeline at two calls rather than using an open-ended agent loop.
  On an 8,000 token-per-minute free tier, an unbounded loop lets one confused
  question exhaust the budget for every subsequent one.
- Set `reasoning_effort` to low. Measured at 12 reasoning tokens against 35 for
  the default, with identical SQL — single-table aggregation is not a hard
  reasoning problem.
- Capped the rows shown to the model at 20, while still returning every row to
  the caller. Sending 500 rows would cost roughly 15,000 tokens and exceed the
  per-minute ceiling on a single question.
- Omitted the 26 distinct issue summaries from the prompt. Including them would
  add roughly 200 tokens to every call to spare the model an occasional `LIKE`.
- Chose not to test whether the model writes good SQL. That is a property of
  the model and the prompt, not of this code; asserting it would make the suite
  slow, costly and dependent on network access. It is verified by hand against
  the sample questions instead.


## [0.2.0] - 2026-09-17

The deterministic core: data ingestion, query safety, and anomaly detection.
Every component in this release runs without an LLM, without network access,
and without an API key.

### Added

- Typed application settings loaded once from the environment, validated at
  startup so a malformed value fails immediately with an actionable message
  rather than surfacing mid-query.
- CSV ingestion into SQLite with explicit type coercion. Missing values for
  unresolved tickets are stored as real SQL `NULL`, never `0.0` or `NaN` —
  a `0.0` resolution time would silently corrupt every average computed over
  the column.
- An `AS_OF` time anchor, defaulting to the dataset's own latest timestamp
  (2024-03-30 18:06). Relative questions such as "this week" resolve against
  the data rather than the wall clock, which would otherwise match nothing.
- Read-only database access enforced by SQLite itself through a `mode=ro`
  connection, plus a context manager guaranteeing connections are closed.
- Validation of model-generated SQL: single read-only statements only,
  rejecting stacked statements, schema and data modification, engine control,
  and `load_extension`. Analysis runs on a sanitised copy with comments
  stripped and string literals emptied, so a legitimate
  `LIKE '%delete%'` is not mistaken for a `DELETE`.
- Two anomaly detectors behind a shared protocol and registry, so further
  detectors can be added without modifying existing ones:
  - Resolution-time outliers via Tukey's interquartile fence.
  - SLA breaches for unresolved High and Critical tickets beyond an agreed age.
- Every flagged ticket carries its own justification, stating the measured
  value and the threshold it crossed.
- 135 unit tests covering all of the above, running in under two seconds with
  no network access and no credentials configured.

### Fixed

- A required API key field prevented the application from starting without
  credentials, which would have broken both the anomaly endpoints and the test
  suite. Absent, blank and placeholder keys now degrade the system to its
  deterministic subset instead of blocking startup.
- A blank `AS_OF=` in the environment was read as an empty string rather than
  an absent value, so the documented "leave blank to auto-anchor" behaviour
  crashed on startup.
- Anomaly thresholds were recomputed inside the requested time window, so a
  quiet week raised the outlier fence from 48.1 to 80.5 hours and would have
  excused genuinely slow resolutions. Thresholds now derive from the full
  history while only the windowed tickets are evaluated against them.
- Empty result sets raised errors instead of reporting no anomalies, and an
  empty selection could materialise a phantom all-null row through index
  alignment.

### Decided

- Chose Tukey's interquartile fence over z-scores for outlier detection.
  Resolution time is right-skewed (mean 19.2 hours against a median of 12.0),
  and a z-score assumes a normality the data does not have: it flags 7 of 327
  resolved tickets where the interquartile fence flags 21.
- Added no detector for response time. It is bounded between 0.2 and 5.0 hours
  across all 500 rows with no outliers by either method, so a detector there
  could never fire.
- Used the standard library's CSV reader rather than pandas for ingestion.
  Pandas' implicit type inference is a common source of the null-handling bug
  this layer exists to prevent. Pandas is still used for the anomaly
  statistics, where vectorised quantiles genuinely earn the dependency.
- Kept SQL text validation and read-only connections as two independent
  barriers. Text validation of SQL is inherently approximate; the read-only
  connection is what makes writes impossible.


## [0.1.0] - 2026-09-16

Initial project scaffolding for the DOTMappers AI Engineer assessment.

### Added

- Project structure and dependency management, with every package version
  pinned so the evaluator's environment resolves to the exact set this
  system was built and tested against.
- `scripts/check_groq.py`, a diagnostic that verifies the Groq backend
  before any application code is written: API key validity, model
  availability, tool-calling support, and the `tool_choice` modes the
  free-tier model actually accepts.
- Measured, not assumed, that `openai/gpt-oss-120b` supports
  `tool_choice="required"` on the free tier, and that `reasoning_effort="low"`
  costs 9% fewer tokens than the default with no loss of output quality on
  this schema.
- `.env.example` documenting every configuration value the system needs,
  including the `AS_OF` time anchor used to make relative-time questions
  ("this week", "this month") meaningful against a static 2024 dataset.
- `app/__init__.py` establishing the package and its version.

### Decided

- Confirmed Groq's free tier no longer includes any Llama model; selected
  `openai/gpt-oss-120b` as the primary model after checking current
  documentation rather than relying on prior assumptions.
- Chosen a `python run.py` launcher over Docker Compose, since Docker is
  not installed on the development machine and the brief explicitly
  accepts a plain `uvicorn` startup.
