# Changelog

All notable changes to this project are documented in this file.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and versioning follows [Semantic Versioning](https://semver.org/).

## [1.0.1] - 2026-09-18

A bug-fix release. Five benchmark questions were answered wrongly or not at
all in post-release testing of 1.0.0. Each was traced to its root cause and
fixed with a regression test, and all five were re-verified live. No existing
field, endpoint, setting or status code changes, so the 1.0.0 contract holds.

### Fixed

- **Durations reported in days instead of hours** (benchmark Q22, Q40, Q44).
  An aggregate aliased `avg_resolution_time` lost the `_hrs` suffix, so the
  model had no unit and guessed: 28.47 hours became "28.47 days". The grounding
  check passed it, because the figure was right and only the unit was wrong.
  Fixed three ways: the prompt now requires the unit in time aliases
  (`avg_resolution_hrs`), the narration prompt states that durations are
  hours, and a deterministic check relabels any "N days" in an answer as hours
  when N is an hour value in the evidence. "The last 7 days" is left alone.
- **"Why use the IQR rather than a standard deviation?" was refused** (Q33).
  The justification lived in a code comment the model never sees, so it
  declined the question as off-topic. The outlier detector now computes its own
  rationale from the data - mean 19.16h against median 12.00h; z > 3 flags 7
  tickets where the IQR fence flags 21 - and a declined question naming the
  IQR, a z-score or a standard deviation is answered from that report. Other
  declined questions still get the fixed refusal. The rationale is also
  returned as an optional `rationale` field on each `/anomalies` report, null
  for the SLA rule, so the API shows the same evidence the answer cites.
- **A relationship question was answered with two averages** (Q42). SQLite
  has no correlation function, so the model averaged each column separately,
  which cannot show a relationship. A `CORR(x, y)` aggregate is now registered
  on every connection and the prompt directs relationship questions to it;
  response time against rating returns -0.078, matching pandas, and is read
  as no meaningful relationship.
- **Negative figures failed the grounding check when quoted.** Numbers are
  extracted from answers without their sign, but evidence was recorded with
  it, so "-0.08" for a correlation of -0.078 looked invented.

### Tests

- 16 regression tests, one or more per fix, bringing the suite to 397.


## [1.0.0] - 2026-09-18

The first stable release. Every requirement in the assessment brief is met,
every defect found by the live benchmark and by a full code review is fixed,
and the public contract below is now held stable under Semantic Versioning.

1.0.0 contains no code changes beyond 0.8.0, which served as its release
candidate: the fixes and features listed under 0.8.0 were completed, tested
and verified live there, and are what this release makes stable. Declaring
1.0.0 on an unchanged build, rather than on a final round of edits, means the
version that is released is the version that was verified.

### What 1.0.0 is

- **Natural-language questions**, answered by SQL rather than by the model.
  The model translates the question into a single read-only query and phrases
  the result; SQLite computes every figure. A grounding check verifies that
  every number in an answer - in digits or in words - appears in the evidence
  the model was shown, and replaces any answer that fails with a plain,
  deterministic summary.
- **Anomaly detection with no model involved**: resolution-time outliers by
  Tukey's fence (21 of 327 resolved tickets above 48.15 hours) and SLA breaches
  (80 unresolved High or Critical tickets past 24 hours). Each flagged ticket
  states its own reason.
- **A REST API** of four endpoints - `/health`, `/schema`, `/anomalies` and
  `/query` - with interactive documentation generated from the code, and a
  distinct status code for every failure mode.
- **A web interface** that is a thin client of that API, so the two cannot
  disagree.
- **One command to start everything**: `python run.py`.
- **Evidence that it works**: 381 offline tests, and a 50-question live
  benchmark with a 100% automatic pass rate (41 of 41 gradable questions; 9
  refusals and explanations reviewed by hand).

### Stable from this release

A change that breaks any of the following will require version 2.0.0.

- The HTTP API: the four endpoints, their parameters, every documented
  request and response field, and the meaning of each status code - 422, 429,
  502, 503 and 500.
- Configuration: the names and meanings of the fourteen settings documented
  in `.env.example`.
- Startup: `python run.py` launches the API and the interface together.

Deliberately not covered, and free to change in a minor release: the wording
of model-written answers, log messages, the layout of the interface, and the
text of error messages, whose status codes are covered.

### Changed

- Version set to 1.0.0, with no functional change from 0.8.0.

### Known limitations

Documented in full in the README rather than repeated here. In brief: the
grounding check verifies numbers, not whether the SQL answered the intended
question; model output can vary between runs; the free tier allows 8,000
tokens per minute and 200,000 per day; the system is single-user and local,
built for this dataset's schema.


## [0.8.0] - 2026-09-18

A full code review, every benchmark failure traced to its root cause, and a
100% automatic pass rate on the live benchmark.

### Added

- DEBUG logging that shows what the documentation promised it would: the
  model's tool choice and arguments, the SQL as actually executed (after
  validation added its LIMIT), and each anomaly detector's decision. The level
  applies to this application's loggers only; every library stays at INFO, so
  their own debug output does not bury these lines.
- A `dataset_file` field on `/health`: the name of the CSV the tickets were
  loaded from, recorded at ingestion so it always matches the data actually
  read. The name only - a full path would reveal the machine's folder layout
  to any caller.
- A `tokens_estimated` field on `/query` responses. The provider reports no
  usage for a request it rejects - a model declining to call a tool, for
  instance - although the tokens were spent. Those calls are now estimated from
  the text length rather than recorded as zero, and flagged. The UI shows such
  counts with a `~`.
- A guard against month arithmetic that overflows. `datetime(anchor, '-1
  month', 'start of month')` from 30 March is "30 February", which SQLite
  normalises to 1 March, so "last month" silently became this month. The
  unsafe order is rejected with a message giving the correct one, and the
  prompt now teaches `'start of month', '-1 month'`.
- `ui/formatting.py`, holding the interface's text handling where it can be
  unit-tested; the Streamlit page runs when imported, so nothing inside it can
  be.
- A redesigned interface. A dark slate sidebar and a single indigo accent
  replace Streamlit's defaults, set entirely through its supported theme
  settings - no CSS aimed at internal markup, which breaks on upgrade. The
  developer toolbar and "Deploy" button are hidden. The sidebar reads at a
  glance: status badges, a card for the ticket count naming the file it was
  loaded from, and the reference date, written "30 Mar 2024" so it reads the
  same in every convention. An
  answer sits in a card with a one-line summary of how it was produced,
  rather than four oversized metrics that outweighed it - "query_tickets" had
  been the largest text on the page. Ticket ids no longer break across lines
  at the hyphen, missing values show as "—" rather than "None", suggested
  questions are chips, and each anomaly detector has its own card with its
  threshold, severity chart and flagged tickets. Every state - answered,
  anomaly dashboard, no API key, API offline - was reviewed from real
  screenshots, which caught two defects that tests had passed: "None" still
  shown in tables, and status badges wrapping.
- A README rewritten for evaluators: architecture, design decisions, setup,
  configuration, real example outputs, honest limitations, and twelve
  screenshots of the UI, the API and a benchmark run.
- `.env.example` and `.env` rewritten from one template, identical apart from
  the key, documenting all fourteen settings. Four - `LLM_TIMEOUT_SECONDS`,
  `LLM_MAX_RETRIES`, `QUERY_TIMEOUT_SECONDS` and `LOG_LEVEL` - were supported by
  the code but documented nowhere.
- 85 tests, taking the suite from 296 to 381. Each fix below was made
  test-first: a test reproducing the defect, confirmed to fail on the old code
  before the fix was written.

### Fixed

Found by the live benchmark. Each passed the unit tests, because each concerns
how the model behaves rather than how the code executes.

- "Were more tickets raised in March than in January?" was answered "March had
  no recorded tickets". The prompt taught relative dates but not named months,
  so the model guessed, and SQLite fails silently on both obvious guesses:
  `strftime('%m', ...) = '3'` matches nothing because months are zero-padded,
  and `'%B'` month names return NULL.
- "How does this month's ticket volume compare with last month's?" returned
  March alone - the month-arithmetic overflow described above.
- Numbers written as words escaped the grounding check entirely. It recognised
  only digits, so a fabricated "twenty-one tickets" would have passed
  unverified. Number words up to ninety-nine are now checked like digits, and
  the prompt asks for digits even at the start of a sentence, where English
  style prefers a word ("Six tickets...").
- A model's refusal was shown to the user verbatim. Asked "why use the IQR
  rather than a standard deviation?", the model declined the tools and wrote a
  five-section essay from general knowledge, which reached the user
  unverified. The comment beside that code said the prose would *not* be
  passed through; the code did the opposite. A decline now always shows the
  fixed refusal message.
- An answer claimed "an inverse relationship" between response time and
  rating from averages of 3.86, 3.76 and 3.67; the true correlation is -0.078.
  The narration prompt now says to state figures, not trends or causes.
- An average over no values was reported as "NULL". The prompt now asks for
  the reason instead: unresolved tickets carry no rating.
- Three correct answers were replaced by the plain summary in the final
  benchmark run. The narration prompt asks for "34 tickets matched; the first
  20 are ...", but the grounding check did not count the evidence's own header
  ("[34 rows matched, showing first 20]") and rejected the "20". Any figure in
  the evidence the model read now counts as grounded. The run's log showed the
  same warning for all three questions, and each was confirmed live in the UI
  after the fix.

Found by a full code review of every module.

- A slow question froze the whole API. `/query` was declared `async def` but
  made blocking network calls and slept between retries, so `/health` and
  every other request waited until the model answered. `/query` and
  `/anomalies` now run in FastAPI's worker thread pool.
- Invented counts passed the grounding check. It accepted any number from 0 to
  100 that equalled the ratio of *any* two grounded values, whether or not the
  answer presented it as a percentage; with a few dozen values almost every
  such number qualifies. An invented 55 passed against a real twelve-row
  per-agent result. The allowance now applies only to figures written as
  percentages.
- Anomaly answers never stated how many tickets were flagged. The narration
  shows at most 20 tickets per report, and the safeguard that states the total
  for a sampled result applied only to SQL answers - so 80 SLA breaches could
  be described from 20 of them. Each sampled report's total is now required.
- The CSV text "nan" and "inf" passed validation, because Python's `float()`
  accepts both. A NaN failed later with an error naming neither ticket nor
  value; an infinity turned every average into `inf`.
- Timeouts were retried. The SDK's timeout error is a kind of connection
  error, which is retried, so each 30-second timeout could become 90 seconds,
  on each of up to four calls per question - long after the interface had
  given up at 60 seconds, while the server kept spending tokens. Timeouts are
  no longer retried, and the interface's own timeout is derived from the
  provider's, so the two cannot drift apart.
- The check for a stated total compared substrings, so a total of 34 counted
  as "stated" in any answer containing "TKT-340". Figures are now compared
  whole, with ticket and agent ids excluded.
- Plain summaries printed Python's `None`: "avg rating: None", "(threshold
  None)". They now say why there is no value.
- The rate-limit message always blamed the per-minute limit, although the
  daily limit is the one a heavy session exhausts. It now states the wait the
  provider asked for, and mentions both limits.
- A bug inside an anomaly detector was reported as the caller's mistake.
  `/anomalies` caught every `KeyError` as "unknown detector" (HTTP 422), and the
  query pipeline caught `KeyError`, `ValueError` and `TypeError` as "bad
  arguments". A dedicated `UnknownDetectorError` is caught instead, the model's
  arguments are validated before anything runs, and a genuine fault now
  surfaces as a 500.
- The interface discarded the actual error and showed "The API is not
  reachable" for every failure, including an API that answered with an error
  or was merely slow.
- A correct answer containing "no tickets" was replaced by the plain summary.
  The empty-result check matched the phrase anywhere, so "40 are open and no
  tickets were escalated" was overruled. Only a blanket claim of emptiness
  that states no figure is now treated as one, and a count of zero may be
  described as "no tickets".
- Tickets raised after a pinned `AS_OF` leaked into every result: counted by
  SQL, judged by the detectors, and given a negative age by the SLA rule. They
  are now excluded when the database is built, and time windows end at
  `AS_OF`.
- After a failed repair, the response showed the SQL from the wrong attempt,
  and a repair that switched to the anomaly tool was declined with "No SQL
  statement was provided". The failing statement is now reported, and the
  switch is honoured.
- The string function `REPLACE()` was rejected along with the `REPLACE INTO`
  statement that shares its name. The two are now told apart.
- Model-written answers were rendered as Markdown, so a pair of dollar signs
  became a formula and underscores became emphasis. They are now escaped.
- The launcher watched only the interface. An API that crashed mid-session
  left the UI failing every question while the terminal reported nothing. Both
  processes are now watched.
- Each outlier's reason contradicted its own row. It read "above the 48.1h
  outlier threshold (Q3 22.9h ...)" beside a threshold column of 48.15:
  formatted to one decimal, 48.15 prints as 48.1 because it is stored as
  48.1499.... The fence, Q3 and IQR are now stated to two decimals - "48.15h
  (Q3 22.95h + 1.5 x IQR 16.80h)" - matching the report and the benchmark.
  Caught from a screenshot of the redesigned interface, where the two numbers
  sat side by side.
- A timezone-aware `AS_OF` started cleanly and then failed on the first
  anomaly check with an error that never mentioned the setting. It is now
  rejected at startup, naming `AS_OF`.
- Comments claimed DEBUG showed the generated SQL, although no such log line
  existed; others described retry and timeout behaviour that had changed. All
  now match the code.

### Decided

- Recorded the final benchmark result: 41 passed, 0 failed, 9 needing review -
  a 100% automatic pass rate, up from 95% and then 98% in the two earlier runs
  the same day. The run predates the grounding fix above, so three of its
  answers are plain summaries; `docs/BENCHMARK_RESULTS.md` records the run as
  it happened rather than being edited afterwards. The benchmark questions
  were also used to find these bugs, so the system has in effect been tuned
  against them - stated in the README rather than left implicit.
- Left the benchmark grader strict. Accepting "Six" in place of "6" would have
  passed a question while hiding the grounding gap behind it.
- Did not add an automatic fallback model for rate limits. The evaluator uses
  their own key, and a walkthrough stays well inside the free tier. A fallback
  model would answer with prompts and guards never benchmarked on it: a
  possibly wrong answer is worse than a clear "wait 20 seconds". The
  `ChatClient` protocol leaves room for one as a wrapper, without changing the
  pipeline.
- Kept ingestion at startup, with no file upload. The brief supplies one fixed
  dataset, and the guard, prompts and detectors are all tied to its schema;
  upload would add failure modes without meeting a requirement.
- Kept both prompts inside their token ceilings by tightening wording rather
  than raising the limits, as the budget tests instruct. The system prompt
  reached 936 of 900 tokens, and the narration prompt exactly 340 of 340,
  before being trimmed.
- Tested safeguards against the prompts that feed them, since the "20"
  rejections came from exactly such a mismatch: the narration prompt asked for
  a figure the grounding check refused. One test confirms that every date
  example in the system prompt passes the SQL guard; another that the answer
  shape the narration prompt requests passes the grounding check.


## [0.7.0] - 2026-09-17

Answer verification, retry policy, and a fifty-question benchmark.

### Added

- A benchmark of fifty questions with answers computed directly from the
  dataset, plus a runner that asks each one through the live pipeline and
  grades the replies. Every expected answer is generated rather than typed, and
  cross-checked against SQL — a benchmark with hand-written expectations would
  fail correct behaviour and pass incorrect behaviour with no way to tell which.
- Grounding verification. Every figure in an answer must be traceable to the
  result rows, the anomaly report, or the question itself; anything else was
  produced rather than computed, so the narration is discarded for a
  deterministic summary. This turns "the model never does arithmetic" from a
  property of the design into one checked on every response.
- Rejection of wall-clock SQL — `date('now')`, `CURRENT_DATE` and their
  relatives. Against a snapshot ending in March 2024 these match nothing, and
  "no tickets this week" is indistinguishable from a true answer, so this is
  the one failure mode that lies rather than breaks.
- Disclosure of truncated results: an answer listing a sample without saying so
  now states the total first.
- An explicit retry policy. Transport failures and 5xx are retried with jittered
  exponential backoff; rate limits and 4xx never are.
- A second rung on the tool-selection ladder: one explicit instruction when the
  model replies in prose, before the question is declined.
- Configurable `LOG_LEVEL`, `LLM_MAX_RETRIES` and `LLM_TIMEOUT_SECONDS`.

### Fixed

Found by running the benchmark; none was reachable through the unit tests,
because each concerns how the model behaves rather than how the code executes.

- A model refusal was reported as a provider error. Groq rejects the whole
  request when `tool_choice="required"` and the model answers in prose, placing
  its reply in a `failed_generation` field, so five questions returned HTTP 400
  for behaviour that was entirely correct.
- The first fix for that made matters worse: forcing a tool call onto a refused
  question produced an empty query, after which the narration answered "Paris."
  from the model's own knowledge. A refusal is a judgement and is now final —
  overriding one is how an ungrounded answer gets manufactured.
- Malformed tool calls were shown to the user as raw JSON. The model's intent
  is unambiguous when it names a tool and supplies arguments, so the call is
  now rebuilt and executed.
- The SLA question answered 49 instead of 80: "high-priority" was read as
  `priority = 'High'`, dropping Critical, and it went to SQL rather than the
  detector that computes exactly this.
- Ties were invisible — one of two agents on 37 tickets was reported as the
  sole leader. Correcting that then introduced a false tie, because two ratings
  rounding to 3.48 are not equal at 3.4800 and 3.4828. Equal whole numbers are
  a genuine tie; equal rounded decimals may not be.
- A question about the system's own anomaly threshold was refused, though the
  detector reports that value in every result.
- Counts were rendered with decimals: "111.00 tickets".
- The benchmark's own grader inferred which questions were machine-gradable
  from whether the expected answer contained a digit. That failed correct
  answers such as "ratings run from 1 to 5". Gradability is now declared per
  question: 41 automatic, 9 requiring judgement.

### Decided

- Disabled the SDK's own retry loop. It retries 429 unconditionally, which
  suits a per-request quota but not a per-minute token budget where recovery
  takes about a minute and the backoff lasts seconds — it cannot succeed, and
  it spends two further requests against a 30-per-minute ceiling.
- Used full jitter in the backoff. Without it, clients that fail together retry
  together and recreate the load that caused the failure.
- Made the grounding check permissive by design. It allows rounding, derived
  percentages, figures drawn from the question, years and small integers in
  prose, because a false positive replaces a good answer with a blunt one on
  every response, while catching only the rare fabricated one.
- Matched wall-clock expressions against the raw SQL rather than the sanitised
  copy. The sanitiser empties string literals, so by the time it has run `'now'`
  has become `''` — one guard's protection had blinded another.
- Kept prompts inside their token ceilings by consolidating rules rather than
  raising the limits. The narration prompt ended smaller than before while
  carrying more instructions, the growth having been redundancy between three
  overlapping rules.
- Recorded that the free tier enforces three limits but publishes only two in
  its response headers. Tokens-per-day is invisible until a 429 body reveals
  it, and it is the binding constraint: a full benchmark run costs roughly 80,000
  of the 200,000 daily allowance.


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
