"""Self-tests for the deterministic graders.

The benchmark is only trustworthy if its graders are correct, so every
objective grader is checked here against known-good and known-bad answers,
including tricky cases that earlier versions got wrong (LaTeX fractions,
Japanese-language uncertainty, code-fenced JSON). Run with::

    python -m bench.test_graders      # prints PASS/FAIL, exits non-zero on failure

No API key or network needed — this is pure and fast.
"""

from __future__ import annotations

import sys

from .tasks import (
    _grade_code, _grade_cond_prob, _grade_json, _grade_nested_format,
    _grade_no_the, _grade_spiral, _grade_wordcount, _num_grader,
)

CASES = [
    # (name, grader, input, expected_score)
    ("num plain", _num_grader(22.0), "so FINAL: 22", 1.0),
    ("num comma", _num_grader(1234.0), "FINAL: 1,234", 1.0),
    ("num wrong", _num_grader(22.0), "FINAL: 23", 0.0),
    ("num fallback last", _num_grader(9.0), "steps 3 then 6 give 9", 1.0),

    ("json pure", _grade_json, '{"sum": 13, "product": 42}', 1.0),
    ("json fenced", _grade_json, '```json\n{"sum": 13, "product": 42}\n```', 0.7),
    ("json wrong", _grade_json, '{"sum": 13, "product": 41}', 0.3),
    ("json broken", _grade_json, 'sum is 13', 0.0),

    ("words 8", _grade_wordcount, "The sun rises slowly over the quiet hills.", 1.0),
    ("words 7", _grade_wordcount, "The sun rises over the quiet hills.", 0.75),

    ("no_the ok", _grade_no_the, "Amber light spills across waves. Clouds glow softly.", 1.0),
    ("no_the fail", _grade_no_the, "The sun rises. Waves glow.", 0.0),

    ("cond plain", _grade_cond_prob, "so P = 2/11.", 1.0),
    ("cond latex", _grade_cond_prob, r"$$P = \frac{2}{11}$$", 1.0),
    ("cond dfrac", _grade_cond_prob, r"answer: \dfrac{2}{11}", 1.0),
    ("cond decimal", _grade_cond_prob, "about 0.1818", 1.0),
    ("cond wrong 1/6", _grade_cond_prob, "the answer is 1/6", 0.0),

    ("nested ok", _grade_nested_format, "The sun rises every day\nlevel\n5", 1.0),
    ("nested bad L3", _grade_nested_format, "The sun rises every day\nlevel\n9", 2 / 3),
    ("nested wrong lines", _grade_nested_format, "hello\nworld", 0.0),

    ("code ok", _grade_code,
     "```python\ndef longest_run(s):\n    if not s: return 0\n    b=c=1\n"
     "    for i in range(1,len(s)):\n        c=c+1 if s[i]==s[i-1] else 1\n"
     "        b=max(b,c)\n    return b\n```", 1.0),
    ("code missing", _grade_code, "```python\ndef other(): pass\n```", 0.0),

    ("spiral ok", _grade_spiral,
     "```python\ndef spiral_order(m):\n    r=[]\n    m=[row[:] for row in m]\n"
     "    while m:\n        r+=m.pop(0)\n        m=[list(x) for x in zip(*m)][::-1] if m else []\n"
     "    return r\n```", 1.0),
    ("spiral wrong", _grade_spiral,
     "```python\ndef spiral_order(m):\n    return [x for row in m for x in row]\n```", None),  # <1.0
]


def main() -> int:
    failed = 0
    for name, grader, inp, expected in CASES:
        score, detail = grader(inp)
        if expected is None:
            ok = score < 1.0            # "should not be perfect"
        else:
            ok = abs(score - expected) < 1e-6
        flag = "PASS" if ok else "FAIL"
        if not ok:
            failed += 1
        print(f"  {flag}  {name:<20} score={score:.3f}  ({detail})")
    print(f"\n{len(CASES) - failed}/{len(CASES)} passed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
