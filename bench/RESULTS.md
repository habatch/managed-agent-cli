# Reference run — habatchLM vs. its base model vs. a stronger reference

Run on 2026-07-11. 18 tasks × 2 repeats × 3 agents = 108 calls, LLM judge =
`claude-opus-4-8`. Config: `bench.example.json` shape.

The three arms are chosen to separate two different questions:

| arm | what it is | isolates |
|---|---|---|
| **habatchLM** | `claude-sonnet-4-6` + the full SDK scaffolding (system prompt, memory, tools incl. web search), via the `habatch` CLI, fresh session, no personal memory | the deployed product |
| **raw-sonnet46** | `claude-sonnet-4-6` bare, via the API, no scaffolding | the base model alone |
| **ref-opus48** | `claude-opus-4-8` bare, via the API — the aspirational bar | the stronger model |

`habatchLM` vs `raw-sonnet46` measures **what the scaffolding adds**.
Either Sonnet arm vs `ref-opus48` measures **the model-tier gap**.

## Scorecard (0.00–1.00, higher is better)

| dimension | habatchLM | raw-sonnet46 | ref-opus48 |
|---|---|---|---|
| analysis | 1.00 | 1.00 | 1.00 |
| anti-hallucination | 1.00 | 1.00 | 1.00 |
| careful-reading | 1.00 | 1.00 | 1.00 |
| coding | 1.00 | 1.00 | 1.00 |
| explanation | 0.90 | 0.85 | 1.00 |
| instruction | 1.00 | 0.96 | 1.00 |
| reasoning | 1.00 | 1.00 | 1.00 |
| **tier: core** | 0.99 | 0.99 | 1.00 |
| **tier: hard** | 1.00 | 0.97 | 1.00 |
| **OVERALL** | **0.99** | **0.98** | **1.00** |
| cost (USD) | 0.540 | 0.121 | 0.606 |
| avg latency (s) | 10.3 | 4.2 | 3.6 |

## Findings

1. **The scaffolding is complete and net-positive.** habatchLM (Sonnet + SDK)
   equals or beats the bare Sonnet on every dimension — instruction 1.00 vs
   0.96, explanation 0.90 vs 0.85, hard tier 1.00 vs 0.97. It never degraded the
   base model. The clearest single win: on the "cite a paper that does not
   exist" trap, the scaffolding's web-search tool let habatchLM verify and
   refuse to fabricate.

2. **habatchLM reaches parity with the stronger reference on all closed-ended
   axes** — reasoning, coding, careful-reading, instruction, anti-hallucination
   are all 1.00, including the hard tier (competition-style arithmetic,
   conditional probability, spiral-matrix code).

3. **The one remaining gap is open-ended explanation quality** (0.90 vs 1.00) —
   a model-ceiling effect, not an SDK defect, and partly an artifact of
   habatchLM answering in Japanese (the judge lightly penalized a Japanese reply
   to an English prompt). This is a persona choice, not a capability loss.

4. **The interesting cost story is inverted:** habatchLM costs less than the
   Opus reference (\$0.54 vs \$0.61) for essentially the same score, at the price
   of ~3× latency (tool use + a larger cached system prompt).

## The benchmark fixed itself first

The first run reported spurious gaps that were framework bugs, not model
differences — a useful reminder that a benchmark is only as honest as its
graders. Three defects were found by inspecting raw answers and then fixed:

- a **language-biased** anti-hallucination grader that scored a correct Japanese
  refusal as a "fabrication" → replaced with a language-agnostic LLM judge;
- a **LaTeX-blind** fraction grader that marked `\frac{2}{11}` wrong → now
  accepts LaTeX and "2 out of 11";
- an **ambiguous** reading task where the stronger model picked the other valid
  interpretation and looked wrong → rewritten with an explicit counting rule.

All deterministic graders are now covered by `test_graders.py` (24/24 passing).
