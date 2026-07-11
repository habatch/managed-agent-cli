"""Agent backends for the parity benchmark.

Two ways to obtain an answer from a target:

- ``CmdAgent``  — run an external command (e.g. the ``habatch`` wrapper or any
  CLI that takes a prompt as its final argv and prints the answer to stdout).
  Use this to benchmark a *deployed* managed agent, with all of its real
  scaffolding (system prompt, memory, tools).

- ``ApiAgent``  — call the Anthropic Messages API directly with a chosen model.
  Use this for the reference "raw Claude" arm, or to benchmark any bare model.

Both return an :class:`AgentResult` so the runner can treat them uniformly.
"""

from __future__ import annotations

import os
import re
import subprocess
import time
from dataclasses import dataclass, field

# Strip ANSI colour codes and the habatch "[usage] ..." accounting line so the
# grader sees only the model's actual answer.
_ANSI = re.compile(r"\x1b\[[0-9;]*m")


@dataclass
class AgentResult:
    text: str            # cleaned answer text
    raw: str             # unmodified stdout (for debugging)
    latency_s: float
    cost_usd: float = 0.0
    ok: bool = True      # False if the call errored / timed out
    error: str = ""


def _clean(text: str, strip_patterns: list[str]) -> str:
    text = _ANSI.sub("", text)
    kept = []
    for line in text.splitlines():
        if any(re.search(p, line) for p in strip_patterns):
            continue
        kept.append(line)
    return "\n".join(kept).strip()


# habatch prints a line like: "[usage] in=3 out=5 ... この turn: $0.0534 ..."
_HABATCH_COST = re.compile(r"この turn:\s*\$([0-9.]+)")


class CmdAgent:
    """Runs an external command; the prompt replaces the ``{PROMPT}`` token.

    Example (habatch, fresh session, no personal memory)::

        CmdAgent(
            name="habatchLM",
            cmd=["habatch", "--new", "--no-memory", "{PROMPT}"],
            env={"HABATCH_SKIP_PASSPHRASE": "1"},
        )
    """

    def __init__(
        self,
        name: str,
        cmd: list[str],
        env: dict[str, str] | None = None,
        timeout: int = 240,
        strip_patterns: list[str] | None = None,
        cost_pattern: str | None = None,
    ):
        self.name = name
        self.cmd = cmd
        self.env = env or {}
        self.timeout = timeout
        # By default strip habatch's usage line.
        self.strip_patterns = strip_patterns if strip_patterns is not None else [r"\[usage\]"]
        self.cost_pattern = cost_pattern if cost_pattern is not None else _HABATCH_COST.pattern

    def ask(self, prompt: str) -> AgentResult:
        argv = [prompt if tok == "{PROMPT}" else tok for tok in self.cmd]
        env = {**os.environ, **self.env}
        t0 = time.time()
        try:
            proc = subprocess.run(
                argv, capture_output=True, text=True, timeout=self.timeout, env=env
            )
        except subprocess.TimeoutExpired:
            return AgentResult("", "", time.time() - t0, ok=False,
                               error=f"timeout after {self.timeout}s")
        latency = time.time() - t0
        raw = proc.stdout + ("\n" + proc.stderr if proc.stderr else "")
        cost = 0.0
        if self.cost_pattern:
            m = re.search(self.cost_pattern, raw)
            if m:
                cost = float(m.group(1))
        if proc.returncode != 0 and not proc.stdout.strip():
            return AgentResult("", raw, latency, cost, ok=False,
                               error=f"exit {proc.returncode}: {proc.stderr[:300]}")
        return AgentResult(_clean(proc.stdout, self.strip_patterns), raw, latency, cost)


# Rough Anthropic list prices (USD per 1M tokens) for cost estimation only.
_PRICES = {
    "claude-opus-4-8": (15.0, 75.0),
    "claude-opus-4-1": (15.0, 75.0),
    "claude-sonnet-4-6": (3.0, 15.0),
    "claude-sonnet-4-5": (3.0, 15.0),
    "claude-haiku-4-5": (1.0, 5.0),
}


def _price_for(model: str) -> tuple[float, float]:
    for key, price in _PRICES.items():
        if model.startswith(key):
            return price
    return (0.0, 0.0)


class ApiAgent:
    """Calls the Anthropic Messages API directly with a fixed model."""

    def __init__(
        self,
        name: str,
        model: str,
        system: str | None = None,
        max_tokens: int = 2048,
        thinking_budget: int | None = None,
    ):
        from anthropic import Anthropic  # imported lazily so cmd-only runs need no key
        self.name = name
        self.model = model
        self.system = system
        self.max_tokens = max_tokens
        self.thinking_budget = thinking_budget
        self._client = Anthropic()

    def ask(self, prompt: str) -> AgentResult:
        kwargs: dict = {
            "model": self.model,
            "max_tokens": self.max_tokens,
            "messages": [{"role": "user", "content": prompt}],
        }
        if self.system:
            kwargs["system"] = self.system
        if self.thinking_budget:
            kwargs["thinking"] = {"type": "enabled", "budget_tokens": self.thinking_budget}
            kwargs["max_tokens"] = max(self.max_tokens, self.thinking_budget + 512)
        t0 = time.time()
        try:
            resp = self._client.messages.create(**kwargs)
        except Exception as e:  # noqa: BLE001 — surface any API failure as a failed result
            return AgentResult("", "", time.time() - t0, ok=False, error=str(e)[:300])
        latency = time.time() - t0
        text = "".join(
            b.text for b in resp.content if getattr(b, "type", None) == "text"
        ).strip()
        pin, pout = _price_for(self.model)
        cost = (resp.usage.input_tokens * pin + resp.usage.output_tokens * pout) / 1e6
        return AgentResult(text, text, latency, cost)
