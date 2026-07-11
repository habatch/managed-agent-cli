"""LLM-as-judge for open-ended tasks.

A strong reference model scores a candidate answer 0-10 against the task's
reference answer and rubric, returning strict JSON. The judge sees neither
which agent produced the answer nor the other agents' answers, so scoring is
blind and independent per candidate.
"""

from __future__ import annotations

import json
import re

JUDGE_SYSTEM = (
    "You are a strict, fair grader. You are given a QUESTION, a REFERENCE answer, "
    "a RUBRIC, and a CANDIDATE answer. Score the candidate from 0 to 10 on how well "
    "it satisfies the rubric and matches the correctness of the reference. Be "
    "calibrated: 10 = as good as or better than the reference; 5 = partially "
    "correct or ignores a stated constraint; 0 = wrong or empty. Reply with ONLY "
    'a JSON object: {"score": <int 0-10>, "reason": "<one sentence>"}.'
)


class Judge:
    def __init__(self, model: str = "claude-opus-4-8"):
        from anthropic import Anthropic
        self.model = model
        self._client = Anthropic()

    def score(self, question: str, reference: str, rubric: str, candidate: str) -> tuple[float, str]:
        if not candidate.strip():
            return 0.0, "empty answer"
        user = (
            f"QUESTION:\n{question}\n\nREFERENCE:\n{reference}\n\nRUBRIC:\n{rubric}\n\n"
            f"CANDIDATE:\n{candidate}"
        )
        try:
            resp = self._client.messages.create(
                model=self.model, max_tokens=300, system=JUDGE_SYSTEM,
                messages=[{"role": "user", "content": user}],
            )
        except Exception as e:  # noqa: BLE001
            return 0.0, f"judge error: {e}"
        text = "".join(b.text for b in resp.content if getattr(b, "type", None) == "text")
        m = re.search(r"\{.*\}", text, re.DOTALL)
        if not m:
            return 0.0, f"judge gave no JSON: {text[:80]}"
        try:
            obj = json.loads(m.group(0))
            return max(0.0, min(10.0, float(obj["score"]))) / 10.0, str(obj.get("reason", ""))
        except Exception:  # noqa: BLE001
            return 0.0, f"judge JSON parse fail: {text[:80]}"
