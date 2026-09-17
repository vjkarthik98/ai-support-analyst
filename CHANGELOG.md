# Changelog

All notable changes to this project are documented in this file.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and versioning follows [Semantic Versioning](https://semver.org/).

## [0.6.0] - 2026-09-17

Single-command startup. The system now runs with `python run.py`.

### Added

- A launcher that starts the API and the interface together, in the right
  order, and stops both cleanly.
- A pre-flight port check, run before anything is started, so a conflict is
  reported as one sentence naming the port and the likely cause rather than a
  traceback from inside a server's socket setup. Both ports are checked in the
  same pass, so two conflicts can be fixed without restarting in between.
- Health-gated startup. The interface is launched only once the API answers
  `/health` with a 200. The API must parse the CSV and build its database
  first, so starting both at once would show "cannot reach the API" as the
  first thing anyone sees. The wait polls rather than sleeping a fixed
  interval, so it is as fast as the machine allows, and watches the API process
  so a crash is reported immediately instead of after the full timeout.
- Shutdown that escalates from `terminate()` to `kill()` after a grace period,
  running on every exit path including Ctrl+C, and stopping the interface
  before the API it depends on.
- A startup notice when no API key is configured, so running in deterministic
  mode is an obvious state rather than a silent one.
- 8 tests covering the port-availability logic.

### Fixed

- Progress output and error messages could appear out of order, because stdout
  is block-buffered when it is not a terminal while stderr never is. An error
  surfaced above the line printed before it, making the checks look as though
  they had run in the wrong order.

### Decided

- Tested the port logic and deliberately not the process orchestration. Testing
  the latter means mocking `subprocess.Popen`, at which point the test asserts
  that the mock behaves like the mock — a test that `start_api` builds a
  particular argument list is the source code written twice, and passes whether
  or not the server starts. The orchestration was verified by running it
  instead: happy path, port conflict, clean shutdown with no orphaned
  processes, and startup with no API key. For a launcher, whose failure modes
  are environmental rather than logical, that evidence is stronger than a mock
  can provide.
- Set `SO_REUSEADDR` on the port probe. Without it a port left in `TIME_WAIT`
  by a run seconds earlier reads as occupied, and the launcher refuses to start
  over a conflict that does not exist — while restarting immediately after
  stopping is the most common thing anyone does.
- Stopped child processes with `terminate()` rather than signals. On Windows
  `CTRL_C_EVENT` propagates to the whole process group and is awkward to
  target; calling `terminate()` on each child is simpler and behaves
  identically on every platform.
- Passed Streamlit's server options on the command line as well as setting them
  in `.streamlit/config.toml`. The config file is their documented home, but a
  user with a conflicting global Streamlit config would otherwise override it
  and hit the first-run email prompt, which blocks startup entirely.


## [0.5.0] - 2026-09-17

The user interface: a Streamlit dashboard that is a thin client of the API.

### Added

- A question tab with the brief's six sample questions as one-click buttons, so
  the system can be exercised without typing anything.
- Every answer displayed with its evidence: rows returned, tool used, elapsed
  time, token cost, the generated SQL in an expander, and the complete result
  set as a table.
- An anomalies dashboard with detector and time-window filters, showing each
  report's method, threshold and tickets considered alongside the findings.
- Severity charts drawn with Altair: horizontal bars sorted by measured value,
  with the threshold marked as a dashed reference line, so how far past the
  limit a ticket sits is legible at a glance.
- A sidebar reporting service status, tickets loaded, the model in use and the
  reference date, with an explanation of why that date is not today.
- Failure states written for a person. An unreachable API says "start it with
  `python run.py`", not `ConnectionRefusedError`.

### Fixed

Both found by exercising the interface rather than by reading the code.

- Ranking queries rounded the aggregate before ordering by it. AGT-08 (3.4800)
  and AGT-11 (3.4828) both round to 3.48, so the two tied and SQLite chose
  between them arbitrarily — returning the correct agent by luck rather than
  by logic. Ranking now orders by the unrounded value and displays the rounded
  one.
- Answers that listed individual tickets gave a partial list with no indication
  it was partial, so 13 of 34 matching tickets read as though 34 did not exist.
  A truncated list now states the total first.
- Sample question buttons were laid out in three columns, so a longer question
  wrapped to two lines and knocked the grid out of alignment. Now two columns,
  ordered by length, with every label on one line.
- Charts used Streamlit's native bar chart, which sorts a categorical axis
  alphabetically — the table led with the worst offender while the chart led
  with whichever ticket id sorted first.
- Replaced `use_container_width`, deprecated and slated for removal, so the
  app runs without deprecation warnings.

### Decided

- Kept the UI a pure HTTP client with no business logic. It never opens the
  database, builds a prompt or computes an anomaly. Importing the service layer
  directly would be marginally faster and would create a second code path that
  only the UI exercises — which is how a UI and an API drift apart. The brief
  requires both interfaces; this way they cannot disagree.
- Declared `altair` in requirements despite it shipping with Streamlit. It is
  imported directly, and relying on a transitive dependency means a future
  Streamlit release could remove it and break the app with no visible cause.
- Surfaced the reference date in the sidebar rather than hiding it in
  configuration. It is the decision that makes relative-time questions work at
  all, and an operator seeing empty results should be able to check it
  immediately.
- Showed the generated SQL beside every answer. An answer a reviewer can verify
  is worth more than one they must trust, and it is the clearest demonstration
  that figures come from the database rather than from the model.


## [0.4.0] - 2026-09-17

The HTTP layer: four endpoints, a typed contract, and honest failure reporting.

### Added

- `GET /health` reporting readiness, the dataset row count, the time anchor,
  the running version and whether natural-language querying is available.
  Deliberately richer than a status flag, so the two most likely
  misconfigurations — an empty dataset, or a missing key — are diagnosable
  from a single request.
- `POST /query` answering a natural-language question, returning the prose
  answer together with the generated SQL, the complete result set, row count,
  token usage and elapsed time. The evidence travels with the answer so a
  figure can be verified rather than trusted.
- `GET /anomalies` running the statistical detectors, with optional detector
  selection and time window. No language model is involved, so it serves with
  no API key configured.
- `GET /schema` describing the table, its permitted values and the available
  detectors, making the API self-describing.
- Pydantic schemas at every boundary, generating the OpenAPI document served
  at `/docs`. Field descriptions are written for whoever reads that page.
- Interactive API documentation at `/docs`, exercising all four endpoints.
- Failure mapped to meaningful status codes rather than a blanket 500:
  429 with a `Retry-After` header for provider rate limits, 502 when the
  provider is unreachable, 503 when no API key is configured, and 422 for a
  malformed question or an unknown detector name. The distinction matters to a
  caller: one should be retried after a wait, one may be retried immediately,
  and one will never succeed until an operator intervenes.
- CORS restricted to the configured UI origin rather than opened to all.
- 30 further tests covering the contract, the failure paths and graceful
  degradation, all running offline against a scripted model.

### Fixed

- Unknown detector names were reported with `KeyError`'s repr quoting, so the
  message reached the caller wrapped in stray apostrophes.
- `QueryRequest` declared `model_config` twice, the second silently discarding
  the first and disabling whitespace stripping.

### Decided

- Built the database once at startup rather than per request, holding state on
  the application instance rather than in module-level globals. The app can
  therefore be constructed more than once, which is what lets the test suite
  spin up isolated instances.
- Kept a missing API key as a startup downgrade rather than a startup failure.
  The service logs the downgrade, `/health` reports it, and the deterministic
  endpoints continue to serve. A system that refuses to start because one
  optional capability is unconfigured is harder to operate, not safer.
- Returned the full result set from `/query` even when the model was shown only
  a sample. Capping protects the token budget, but allowing it to truncate the
  API response would leave a client believing twenty rows were the whole
  answer — wrong, silently, with no error raised.
- Wrote field descriptions for the reader of `/docs` rather than for the
  codebase. That page is the API documentation during the walkthrough, so it is
  a deliverable rather than a convenience.


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
