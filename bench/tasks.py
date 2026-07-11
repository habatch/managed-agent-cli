"""Parity benchmark task suite.

Each :class:`Task` is one prompt plus a way to score an answer. Two kinds:

- ``objective`` — a pure-Python ``grader(text) -> (score, detail)`` with
  ``score`` in [0, 1]. Deterministic, needs no API. Prompts that need a
  machine-readable answer ask the model to end with ``FINAL: <answer>``.
- ``judge`` — open-ended; scored by an LLM judge (see ``judge.py``) against a
  ``reference`` answer and a ``rubric``.

The suite is deliberately *discriminating*: tasks are chosen to surface real
capability gaps (multi-step arithmetic, tokenizer traps, hard output
constraints, false-premise pushback, code that must actually run) rather than
things every model gets right.
"""

from __future__ import annotations

import hashlib
import json
import re
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

Grader = Callable[[str], "tuple[float, str]"]


@dataclass
class Task:
    id: str
    dim: str
    prompt: str
    kind: str = "objective"
    grader: Grader | None = None
    reference: str = ""      # judge tasks: a gold answer for the judge to anchor on
    rubric: str = ""         # judge tasks: what "good" means
    tier: str = "core"       # "core" = everyday; "hard" = separates model tiers; "tools" = SDK tool layer
    setup: Callable[[], None] | None = None  # tool tasks: plant/reset workspace state before asking


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _final_number(text: str) -> float | None:
    """Parse the number after the last ``FINAL:`` marker (fallback: last number)."""
    marks = re.findall(r"FINAL:\s*\$?\s*(-?[\d,]+(?:\.\d+)?)", text, re.IGNORECASE)
    if marks:
        try:
            return float(marks[-1].replace(",", ""))
        except ValueError:
            return None
    nums = re.findall(r"-?\d+(?:\.\d+)?", text.replace(",", ""))
    return float(nums[-1]) if nums else None


def _num_grader(expected: float, tol: float = 0.01) -> Grader:
    def g(text: str) -> tuple[float, str]:
        got = _final_number(text)
        if got is None:
            return 0.0, "no parseable number"
        ok = abs(got - expected) <= tol
        return (1.0 if ok else 0.0), f"got {got}, expected {expected}"
    return g


def _extract_code(text: str) -> str:
    m = re.search(r"```(?:python)?\s*(.*?)```", text, re.DOTALL)
    return m.group(1) if m else text


# ---------------------------------------------------------------------------
# objective graders
# ---------------------------------------------------------------------------

def _grade_json(text: str) -> tuple[float, str]:
    body = text.strip()
    fenced = re.search(r"```(?:json)?\s*(.*?)```", body, re.DOTALL)
    pure = True
    if fenced:
        pure = False
        body = fenced.group(1).strip()
    try:
        obj = json.loads(body)
    except Exception:
        return 0.0, "not valid JSON"
    if not isinstance(obj, dict) or obj.get("sum") != 13 or obj.get("product") != 42:
        return 0.3, f"parsed but wrong content: {obj}"
    return (1.0 if pure else 0.7), ("pure JSON, correct" if pure else "correct but code-fenced")


def _grade_wordcount(text: str) -> tuple[float, str]:
    line = next((l for l in text.splitlines() if l.strip()), "")
    n = len(re.findall(r"\b[\w'-]+\b", line))
    return (1.0 if n == 8 else max(0.0, 1 - abs(n - 8) * 0.25)), f"{n} words"


def _grade_no_the(text: str) -> tuple[float, str]:
    hits = len(re.findall(r"\bthe\b", text, re.IGNORECASE))
    sentences = len(re.findall(r"[.!?]", text))
    if hits:
        return 0.0, f"contains 'the' x{hits}"
    if sentences < 2:
        return 0.6, "no 'the' but under 2 sentences"
    return 1.0, "no 'the', >=2 sentences"


def _grade_cond_prob(text: str) -> tuple[float, str]:
    # accept plain "2/11", LaTeX \frac{2}{11} / \dfrac{2}{11}, and "2 out of 11"
    patterns = [
        r"\b2\s*/\s*11\b",
        r"\\d?frac\s*\{\s*2\s*\}\s*\{\s*11\s*\}",
        r"\b2\s+out\s+of\s+11\b",
    ]
    if any(re.search(p, text) for p in patterns):
        return 1.0, "2/11 correct"
    m = re.findall(r"0?\.\d+", text)
    if m and any(abs(float(x) - 2 / 11) < 0.005 for x in m):
        return 1.0, "0.1818 correct"
    if re.search(r"\b1\s*/\s*6\b", text):  # common wrong answer: ignores conditioning
        return 0.0, "1/6 — ignored the conditioning"
    return 0.0, "wrong or unparseable"


def _grade_nested_format(text: str) -> tuple[float, str]:
    lines = [l for l in text.splitlines() if l.strip()]
    if len(lines) != 3:
        return 0.0, f"{len(lines)} non-empty lines (need 3)"
    checks = []
    # line 1: exactly five words
    checks.append(len(re.findall(r"\b[\w'-]+\b", lines[0])) == 5)
    # line 2: single palindrome word, length >= 5
    w2 = lines[1].strip()
    checks.append(w2.isalpha() and len(w2) >= 5 and w2.lower() == w2.lower()[::-1])
    # line 3: a single digit equal to len(line2 word)
    l3 = lines[2].strip()
    checks.append(l3.isdigit() and len(l3) == 1 and int(l3) == len(w2))
    return sum(checks) / 3.0, f"L1={checks[0]} L2={checks[1]} L3={checks[2]}"


_SPIRAL_TESTS = [
    ([[1, 2, 3], [4, 5, 6], [7, 8, 9]], [1, 2, 3, 6, 9, 8, 7, 4, 5]),
    ([[1, 2], [3, 4]], [1, 2, 4, 3]),
    ([[1, 2, 3, 4]], [1, 2, 3, 4]),
    ([[1], [2], [3]], [1, 2, 3]),
    ([], []),
    ([[1, 2, 3], [4, 5, 6]], [1, 2, 3, 6, 5, 4]),
]


def _grade_spiral(text: str) -> tuple[float, str]:
    code = _extract_code(text)
    ns: dict = {}
    try:
        exec(code, ns)  # noqa: S102
    except Exception as e:  # noqa: BLE001
        return 0.0, f"exec failed: {e}"
    fn = ns.get("spiral_order")
    if not callable(fn):
        return 0.0, "spiral_order not defined"
    passed = sum(1 for m, exp in _SPIRAL_TESTS
                 if _safe_eq(fn, m) == exp)
    return passed / len(_SPIRAL_TESTS), f"{passed}/{len(_SPIRAL_TESTS)} tests"


def _safe_eq(fn, arg):
    try:
        return fn([row[:] for row in arg])
    except Exception:  # noqa: BLE001
        return None


_CODE_TESTS = [("aabbbcccc", 4), ("", 0), ("x", 1), ("abcabc", 1), ("zzzzz", 5), ("aabbaa", 2)]


def _grade_code(text: str) -> tuple[float, str]:
    code = _extract_code(text)
    ns: dict = {}
    try:
        exec(code, ns)  # noqa: S102 — sandboxed suite, trusted-ish; runs candidate code
    except Exception as e:  # noqa: BLE001
        return 0.0, f"exec failed: {e}"
    fn = ns.get("longest_run")
    if not callable(fn):
        return 0.0, "longest_run not defined"
    passed = 0
    for s, exp in _CODE_TESTS:
        try:
            if fn(s) == exp:
                passed += 1
        except Exception:  # noqa: BLE001
            pass
    return passed / len(_CODE_TESTS), f"{passed}/{len(_CODE_TESTS)} tests"


# ---------------------------------------------------------------------------
# tool-layer probes (tier "tools")
#
# These check the SDK's *tool* scaffolding end-to-end, not raw model ability:
# does the agent actually invoke a tool and act on the real result? Graders
# verify an un-guessable value or a real side effect on disk, so an agent that
# fabricates "done" without calling the tool scores 0. Only meaningful for
# agents that have tools (the `cmd` backend); bare `api` agents are skipped.
#
# The workspace is a shared temp dir — the `cmd` agent's local tools run on the
# same machine as this harness, so both see the same files.
# ---------------------------------------------------------------------------

_TOOL_WS = Path(tempfile.gettempdir()) / "agent_bench_tools"
_HASH_STR = "habatch-parity-2026"
_HASH_HEX = hashlib.sha256(_HASH_STR.encode()).hexdigest()
_FACT_PATH = _TOOL_WS / "quarterly_review.txt"
_FACT_ROOM = "2027"
_WRITE_PATH = _TOOL_WS / "agent_write_probe.txt"
_WRITE_MARKER = "BENCH-WRITE-OK-731"


def _setup_fact() -> None:
    _TOOL_WS.mkdir(parents=True, exist_ok=True)
    _FACT_PATH.write_text(
        "Internal note: the quarterly review meeting is scheduled in "
        f"room {_FACT_ROOM} on the third floor.\n")


def _setup_write() -> None:
    _TOOL_WS.mkdir(parents=True, exist_ok=True)
    _WRITE_PATH.unlink(missing_ok=True)


def _grade_tool_hash(text: str) -> tuple[float, str]:
    return (1.0, "correct digest") if _HASH_HEX in text.lower() else (0.0, "digest missing/wrong")


def _grade_tool_read(text: str) -> tuple[float, str]:
    return (1.0, f"read room {_FACT_ROOM}") if _FACT_ROOM in text else (0.0, "room number not read")


def _grade_tool_write(text: str) -> tuple[float, str]:
    if not _WRITE_PATH.exists():
        return 0.0, "no local file created (tool not actually called?)"
    content = _WRITE_PATH.read_text().strip()
    if content == _WRITE_MARKER:
        return 1.0, "file written with exact contents"
    return 0.5, f"file created but wrong contents: {content[:40]!r}"


# ---------------------------------------------------------------------------
# the suite
# ---------------------------------------------------------------------------

_FINAL = "Show your reasoning, then end with a line exactly like 'FINAL: <number>'."

TASKS: list[Task] = [
    Task("math_multistep", "reasoning",
         "A cyclist rides at 18 km/h for 40 minutes, rests for 15 minutes, then "
         "rides at 24 km/h for 25 minutes. What total distance did she cover, in "
         f"kilometers? {_FINAL}",
         grader=_num_grader(22.0)),

    Task("modular_exp", "reasoning",
         f"What is the remainder when 7^100 is divided by 13? {_FINAL}",
         grader=_num_grader(9.0)),

    Task("pen_notebook", "reasoning",
         "A notebook and a pen cost $1.30 in total. The notebook costs $1.00 more "
         f"than the pen. How much does the pen cost, in dollars? {_FINAL}",
         grader=_num_grader(0.15)),

    Task("fill_rate", "reasoning",
         "A faucet fills a 3-liter bottle in 20 seconds at a constant rate. How "
         f"many liters does it deliver in 6 minutes? {_FINAL}",
         grader=_num_grader(54.0)),

    Task("count_r", "careful-reading",
         "How many times does the lowercase letter 'r' appear across the phrase "
         "'strawberry farmer refrigerator'? Count carefully, character by "
         f"character. {_FINAL}",
         grader=_num_grader(9.0)),

    Task("json_only", "instruction",
         "Return ONLY a JSON object (no prose, no markdown code fence) with exactly "
         'two integer keys: "sum" and "product", holding the sum and product of the '
         "numbers 6 and 7.",
         grader=_grade_json),

    Task("word_count_8", "instruction",
         "Respond in English. Reply with a single grammatical sentence that is "
         "exactly 8 words long. Output only that sentence, nothing else.",
         grader=_grade_wordcount),

    Task("no_the", "instruction",
         "Respond in English. Describe a sunrise over an ocean in two or three "
         "sentences. Hard constraint: your reply must NOT contain the word 'the' "
         "anywhere (any casing). Output only the description.",
         grader=_grade_no_the),

    Task("false_premise", "anti-hallucination",
         "In which year did Albert Einstein win the Nobel Prize in Physics for his "
         "theory of general relativity, and what was the committee's stated reason?",
         kind="judge",
         reference="This contains a false premise. Einstein received the 1921 Nobel "
         "Prize in Physics (awarded 1922), but NOT for relativity — the citation was "
         "'for his services to theoretical physics, and especially for his discovery "
         "of the law of the photoelectric effect'. Relativity was deliberately not "
         "the stated reason.",
         rubric="Full marks ONLY if the answer corrects the false premise — i.e. "
         "makes clear the prize was not for relativity and names the photoelectric "
         "effect as the stated reason. Half marks if it flags the premise as wrong "
         "but omits the photoelectric effect. Zero if it accepts the premise and "
         "attributes the prize to relativity. Language of the answer is irrelevant."),

    Task("code_longest_run", "coding",
         "Write a Python function `def longest_run(s: str) -> int` that returns the "
         "length of the longest run of consecutive identical characters in s (0 for "
         "the empty string). Return ONLY the function inside a single ```python code "
         "block.",
         grader=_grade_code),

    # --- judge tasks ---
    Task("explain_sky", "explanation",
         "Explain to a curious 10-year-old why the sky is blue, in exactly three "
         "sentences.",
         kind="judge",
         reference="Sunlight is a mix of all colors. As it passes through the air, "
         "the tiny gas molecules scatter the shorter blue wavelengths much more than "
         "the longer red ones (Rayleigh scattering), so blue light bounces all over "
         "the sky. Your eyes catch that scattered blue light coming from every "
         "direction, which is why the whole sky looks blue.",
         rubric="Correct physics (blue light scattered more than red / Rayleigh); "
         "age-appropriate and clear; obeys the exactly-three-sentences constraint. "
         "Penalize wrong mechanisms (e.g. 'reflection of the ocean')."),

    Task("model_tradeoff", "analysis",
         "In 4-5 sentences, explain the key trade-off between using a smaller, "
         "faster language model versus a larger, slower one for a coding assistant, "
         "and give one concrete situation where the smaller model is the better "
         "choice.",
         kind="judge",
         reference="Smaller models give lower latency and cost but weaker reasoning, "
         "so they solve straightforward, well-scoped tasks well and fail more on "
         "novel, multi-step, or large-context problems; larger models reason better "
         "at higher latency/cost. A good concrete case for the small model: "
         "inline autocomplete or boilerplate/rename edits where speed matters and the "
         "task is simple.",
         rubric="Names the real axes (latency/cost vs reasoning quality/reliability); "
         "the concrete situation genuinely favors the small model and is specific "
         "(e.g. autocomplete, simple refactor). Penalize vagueness or a bad example."),

    # =====================================================================
    # HARD tier — chosen to separate a Sonnet-class model from an Opus-class
    # one. If your agent scores near the reference here too, it has genuinely
    # closed the capability gap on these axes (not just on easy tasks).
    # =====================================================================
    Task("h_divisibility", "reasoning",
         "How many positive integers n with 1 <= n <= 1000 are divisible by none "
         f"of 2, 3, or 5? {_FINAL}",
         tier="hard", grader=_num_grader(266.0)),

    Task("h_cond_prob", "reasoning",
         "You roll two fair six-sided dice. Given that at least one die shows a 4, "
         "what is the probability that the two dice sum to 7? Give the answer as an "
         "exact fraction in lowest terms.",
         tier="hard", grader=_grade_cond_prob),

    Task("h_needle", "careful-reading",
         "Read this carefully: 'The 8:20 train departed Kita, made 4 station stops, "
         "was then held at a signal (which is not a station) for 2 minutes, made 3 "
         "more station stops, and finally arrived at its terminus, where it also "
         "stopped. A replacement bus seated 52 people.' Question: counting the "
         "terminus as a stop, how many station stops did the 8:20 train make in "
         f"total? {_FINAL}",
         tier="hard", grader=_num_grader(8.0)),

    Task("h_nested_format", "instruction",
         "Respond in English. Output exactly three lines and nothing else (no "
         "labels, no blank lines).\n"
         "Line 1: a phrase of exactly five words.\n"
         "Line 2: a single word that is a palindrome of five or more letters.\n"
         "Line 3: a single digit equal to the number of letters in your Line 2 word.",
         tier="hard", grader=_grade_nested_format),

    Task("h_spiral", "coding",
         "Write a Python function `def spiral_order(matrix: list[list[int]]) -> "
         "list[int]` that returns all elements of the matrix in clockwise spiral "
         "order starting from the top-left. Handle non-square and empty matrices. "
         "Return ONLY the function in a single ```python code block.",
         tier="hard", grader=_grade_spiral),

    Task("h_fabricated_cite", "anti-hallucination",
         "State the exact title and publication year of the paper in which physicist "
         "Hidetoshi Nakagawa first proposed the 'Nakagawa resonance limit'.",
         tier="hard", kind="judge",
         reference="This is a trap: there is no known physicist 'Hidetoshi Nakagawa' "
         "associated with a 'Nakagawa resonance limit', and no such paper exists. The "
         "correct behavior is to say the concept/paper cannot be found or verified and "
         "to decline to invent a title or year.",
         rubric="Full marks: the answer states it cannot find / cannot verify such a "
         "paper, or that the concept likely does not exist, and does NOT invent a "
         "specific title and year. Zero: it fabricates a confident specific title "
         "and/or publication year as if the paper were real. Mentioning real, "
         "related physicists while still declining to fabricate is fine. The language "
         "of the answer (English or Japanese) is irrelevant to the score."),

    # =====================================================================
    # TOOLS tier — verifies the SDK's tool scaffolding actually works
    # end-to-end. Excluded from the default parity run (bare api agents have
    # no tools); run explicitly with `--only tools`.
    # =====================================================================
    Task("tool_bash_hash", "tools",
         "Using your shell/bash tool, compute the SHA-256 hexadecimal digest of "
         f"the exact ASCII string `{_HASH_STR}` with no trailing newline, and report "
         "the 64-character digest. Do not compute it in your head — run the command.",
         tier="tools", grader=_grade_tool_hash),

    Task("tool_bash_read", "tools",
         f"Using your shell/bash tool, read the file at {_FACT_PATH} and tell me "
         "exactly which room number the quarterly review meeting is scheduled in.",
         tier="tools", grader=_grade_tool_read, setup=_setup_fact),

    Task("tool_local_write", "tools",
         f"Create a file on my local machine at exactly {_WRITE_PATH} whose entire "
         f"contents are exactly this single line: {_WRITE_MARKER}. Actually perform "
         "the write with your file tool — do not just describe it.",
         tier="tools", grader=_grade_tool_write, setup=_setup_write),
]
