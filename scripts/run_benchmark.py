"""Run the benchmark questions against the live system and grade the answers.

    python scripts/run_benchmark.py                 # all 50
    python scripts/run_benchmark.py --limit 5       # first 5, to smoke-test
    python scripts/run_benchmark.py --start 20      # resume partway

Reads ``docs/BENCHMARK_QUESTIONS.md``, asks each question through the real
pipeline, and writes ``docs/BENCHMARK_RESULTS.md``.

Grading
-------
Answers are graded automatically **only where that is honest**. When the
expected answer contains a number, the check is whether that number appears in
the response - a strict test, since the whole design exists to make the figures
exact. Where the expected answer is a judgement ("declines politely", "explains
the skew"), the case is marked for review rather than guessed at. A grader that
pretended to score those would produce a number that looks like a result and
is not one.

Pacing
------
The free tier allows 8,000 tokens per minute, and a question costs roughly
1,600. Running fifty back to back would spend the budget in under two minutes
and spend the rest of the run being rate limited, so the runner paces itself
and backs off when the provider asks it to.
"""

from __future__ import annotations

import argparse
import re
import sys
import time
from dataclasses import dataclass
from pathlib import Path

# A ten-minute run is unreadable if its progress sits in a buffer until the
# end. stdout is block-buffered whenever it is not a terminal, so line
# buffering is requested explicitly.
sys.stdout.reconfigure(line_buffering=True)

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from app.data import build_database  # noqa: E402
from app.llm import (  # noqa: E402
    LlmError,
    LlmRateLimitedError,
    TicketQueryService,
    build_chat_client,
)

QUESTIONS_PATH = REPO_ROOT / "docs" / "BENCHMARK_QUESTIONS.md"
RESULTS_PATH = REPO_ROOT / "docs" / "BENCHMARK_RESULTS.md"

# Roughly 1,600 tokens per question against an 8,000-per-minute ceiling allows
# about five a minute. Twelve seconds keeps a margin for the reasoning tokens,
# which vary with the question.
SECONDS_BETWEEN_QUESTIONS = 12.0

# Extra pause when the provider reports a rate limit without saying how long.
RATE_LIMIT_BACKOFF_SECONDS = 45.0

PASS, FAIL, REVIEW, ERROR = "PASS", "FAIL", "REVIEW", "ERROR"


@dataclass
class Case:
    """One benchmark question parsed from the document.

    Attributes:
        number: Question number.
        category: Capability being exercised.
        difficulty: Easy, Medium or Hard.
        question: The question text.
        expected: The expected answer.
        checks: What a correct response must contain.
        auto_gradable: Whether a machine can judge this answer.
    """

    number: int
    category: str
    difficulty: str
    question: str
    expected: str
    checks: str
    auto_gradable: bool


@dataclass
class Result:
    """The outcome of asking one question.

    Attributes:
        case: The question that was asked.
        verdict: PASS, FAIL, REVIEW or ERROR.
        answer: What the system replied.
        sql: The SQL it generated, if any.
        tool: The tool it selected.
        rows: Number of rows returned.
        elapsed_ms: Round-trip time.
        detail: Why the verdict was reached.
    """

    case: Case
    verdict: str
    answer: str
    sql: str | None
    tool: str | None
    rows: int
    elapsed_ms: int
    detail: str


def parse_questions() -> list[Case]:
    """Read the benchmark document.

    Returns:
        Every question, in document order.

    Raises:
        SystemExit: If the document is missing or unparseable, since running
            against a partial set would give a misleadingly good score.
    """
    if not QUESTIONS_PATH.exists():
        raise SystemExit(
            f"{QUESTIONS_PATH} not found. Run scripts/generate_benchmark.py first."
        )

    text = QUESTIONS_PATH.read_text(encoding="utf-8")
    pattern = re.compile(
        r"## (?P<category>[^\n]+)\n\n(?=###)|"
        r"### (?P<number>\d+)\. (?P<question>[^\n]+)\n\n"
        r"\*\*Difficulty:\*\* (?P<difficulty>\w+)\n\n"
        r"\*\*Expected answer:\*\* (?P<expected>.*?)\n\n"
        r"\*\*Checks:\*\* (?P<checks>.*?)\n\n"
        r"\*\*Graded:\*\* (?P<graded>\w+)\n",
        re.S,
    )

    cases: list[Case] = []
    category = "Uncategorised"
    for match in pattern.finditer(text):
        if match.group("category"):
            category = match.group("category").strip()
            continue
        cases.append(
            Case(
                number=int(match.group("number")),
                category=category,
                difficulty=match.group("difficulty"),
                question=match.group("question").strip(),
                expected=match.group("expected").strip(),
                checks=match.group("checks").strip(),
                auto_gradable=match.group("graded") == "automatic",
            )
        )

    if not cases:
        raise SystemExit("No questions parsed - the document format may have changed.")
    return cases


def expected_numbers(expected: str) -> list[str]:
    """Extract the figures a correct answer must contain.

    Args:
        expected: The expected answer text.

    Returns:
        Numbers found, as strings. Empty when the answer is a judgement rather
        than a figure.
    """
    # Four-digit years and bare dates are context, not the answer itself.
    cleaned = re.sub(r"\b(19|20)\d{2}-\d{2}-\d{2}\b", " ", expected)
    cleaned = re.sub(r"\b(19|20)\d{2}\b", " ", cleaned)
    return re.findall(r"\d+\.\d+|\d+", cleaned)


def grade(case: Case, answer: str) -> tuple[str, str]:
    """Judge a response against its expected answer.

    Numeric answers are graded strictly: the figure must appear. A decimal is
    also accepted in its trimmed form, so 3.480 matches an answer saying 3.48.

    Args:
        case: The question asked.
        answer: The system's reply.

    Returns:
        A ``(verdict, detail)`` pair.
    """
    if not case.auto_gradable:
        # Declared by the question, not guessed from its text. Inferring this
        # from "does the answer contain a digit" misfired: "ratings run from 1
        # to 5" holds numbers but is not a numeric answer, so a correct reply
        # was being failed for omitting a figure that was never the answer.
        return REVIEW, "Judgement required - review this answer by hand"

    wanted = expected_numbers(case.expected)

    if not wanted:
        return REVIEW, "No figure in the expected answer to match against"

    normalised = answer.replace(",", "")

    def present(number: str) -> bool:
        """Report whether a figure appears in the answer.

        Args:
            number: The figure to look for.

        Returns:
            True when the answer contains it, allowing for trailing zeros.
        """
        variants = {number}
        if "." in number:
            variants.add(number.rstrip("0").rstrip("."))
            variants.add(f"{float(number):.2f}")
        return any(variant in normalised for variant in variants)

    # The first figure is the answer; any others are supporting detail. Asking
    # "how many are unresolved" and receiving "173" is correct, even though the
    # expected answer also breaks that down as 111 Open plus 62 Escalated.
    # Demanding the breakdown would fail a right answer, and a benchmark that
    # fails correct behaviour is worse than none.
    primary, *supporting = wanted

    if not present(primary):
        return FAIL, f"Missing the key figure {primary}"

    absent = [number for number in supporting if not present(number)]
    if absent:
        return PASS, f"Correct ({primary}); omitted detail: {', '.join(absent)}"
    return PASS, f"Contains {', '.join(wanted)}"


def run_case(service: TicketQueryService, case: Case) -> Result:
    """Ask one question and grade the reply.

    Args:
        service: The live query service.
        case: The question to ask.

    Returns:
        The graded result.
    """
    try:
        outcome = service.answer(case.question)
    except LlmRateLimitedError as exc:
        wait = exc.retry_after or RATE_LIMIT_BACKOFF_SECONDS
        print(f"    rate limited; waiting {wait:.0f}s and retrying once")
        time.sleep(wait)
        try:
            outcome = service.answer(case.question)
        except LlmError as retry_exc:
            return Result(case, ERROR, str(retry_exc), None, None, 0, 0, "Provider error")
    except LlmError as exc:
        return Result(case, ERROR, str(exc), None, None, 0, 0, "Provider error")

    verdict, detail = grade(case, outcome.answer)
    return Result(
        case=case,
        verdict=verdict,
        answer=outcome.answer,
        sql=outcome.sql,
        tool=outcome.tool,
        rows=outcome.row_count,
        elapsed_ms=outcome.elapsed_ms,
        detail=detail,
    )


def render(results: list[Result]) -> str:
    """Render the results as markdown.

    Args:
        results: Every graded result.

    Returns:
        The report document.
    """
    counts = {verdict: 0 for verdict in (PASS, FAIL, REVIEW, ERROR)}
    for result in results:
        counts[result.verdict] += 1

    graded = counts[PASS] + counts[FAIL]
    rate = f"{100 * counts[PASS] / graded:.0f}%" if graded else "n/a"

    lines = [
        "# Benchmark Results",
        "",
        f"Ran {len(results)} of the questions in `BENCHMARK_QUESTIONS.md` "
        "against the live system.",
        "",
        "| Verdict | Count |",
        "|---|---|",
        f"| Passed | {counts[PASS]} |",
        f"| Failed | {counts[FAIL]} |",
        f"| Needs review | {counts[REVIEW]} |",
        f"| Provider error | {counts[ERROR]} |",
        "",
        f"**Automatic pass rate: {rate}** of the {graded} questions with a "
        "numeric answer.",
        "",
        "Questions whose expected answer is a judgement - a refusal, an "
        "explanation, an honest \"there is no relationship\" - are marked "
        "**needs review** rather than scored. Guessing at those would produce "
        "a number that looks like a result and is not one.",
        "",
    ]

    failures = [r for r in results if r.verdict == FAIL]
    if failures:
        lines += ["## Failures", "", "| # | Question | Expected | Got |", "|---|---|---|---|"]
        for result in failures:
            answer = result.answer.replace("|", "/")[:70]
            lines.append(
                f"| {result.case.number} | {result.case.question[:52]} | "
                f"{result.case.expected[:34]} | {answer} |"
            )
        lines.append("")

    lines += ["## Every result", ""]
    for result in results:
        icon = {PASS: "PASS", FAIL: "**FAIL**", REVIEW: "REVIEW", ERROR: "**ERROR**"}[
            result.verdict
        ]
        lines += [
            f"### {result.case.number}. {result.case.question}",
            "",
            f"- **Verdict:** {icon} - {result.detail}",
            f"- **Expected:** {result.case.expected}",
            f"- **Answer:** {result.answer}",
            f"- **Tool:** {result.tool or 'none'} | **Rows:** {result.rows} | "
            f"**Time:** {result.elapsed_ms / 1000:.1f}s",
        ]
        if result.sql:
            lines += ["", "```sql", result.sql, "```"]
        lines.append("")

    return "\n".join(lines)


def main() -> int:
    """Run the benchmark.

    Returns:
        A shell exit code: 0 if nothing failed, 1 otherwise.
    """
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--limit", type=int, help="Run only the first N questions")
    parser.add_argument("--start", type=int, default=1, help="Start at question N")
    parser.add_argument(
        "--pace",
        type=float,
        default=SECONDS_BETWEEN_QUESTIONS,
        help="Seconds between questions, to stay inside the token budget",
    )
    args = parser.parse_args()

    cases = [c for c in parse_questions() if c.number >= args.start]
    if args.limit:
        cases = cases[: args.limit]

    database = build_database()
    service = TicketQueryService(
        client=build_chat_client(),
        db_path=database.path,
        as_of=database.as_of,
        row_count=database.row_count,
    )

    print(f"Running {len(cases)} questions at {args.pace:.0f}s intervals")
    print(f"Estimated duration: {len(cases) * args.pace / 60:.0f} minutes\n")

    results: list[Result] = []
    for index, case in enumerate(cases, start=1):
        result = run_case(service, case)
        results.append(result)

        marker = {PASS: "  ok  ", FAIL: " FAIL ", REVIEW: "review", ERROR: "ERROR "}[
            result.verdict
        ]
        print(
            f"[{index:>2}/{len(cases)}] {marker} Q{case.number:<3} "
            f"{case.question[:54]}"
        )
        if result.verdict == FAIL:
            print(f"           expected {case.expected[:44]}")
            print(f"           got      {result.answer[:60]}")

        if index < len(cases):
            time.sleep(args.pace)

    RESULTS_PATH.write_text(render(results), encoding="utf-8")

    counts = {v: sum(1 for r in results if r.verdict == v) for v in (PASS, FAIL, REVIEW, ERROR)}
    print(
        f"\npassed {counts[PASS]} | failed {counts[FAIL]} | "
        f"review {counts[REVIEW]} | errors {counts[ERROR]}"
    )
    print(f"Report: {RESULTS_PATH.relative_to(REPO_ROOT)}")

    return 1 if counts[FAIL] or counts[ERROR] else 0


if __name__ == "__main__":
    sys.exit(main())
