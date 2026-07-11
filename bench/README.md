# Parity Benchmark

A small, honest harness for one question: **is my agent as good as a reference
model?** Run the same task suite against any number of agents, grade every
answer, and read a per-dimension scorecard.

It was built to check whether a *managed agent* (a base model wrapped in a
system prompt, memory, and tools) reaches parity with a stronger reference
model — and, just as importantly, to measure what the **scaffolding itself**
adds by comparing the managed agent against its own bare base model.

## What it measures

Two tiers of tasks across seven dimensions:

| dimension | what it probes |
|---|---|
| reasoning | multi-step arithmetic, modular exponentiation, conditional probability, inclusion–exclusion |
| careful-reading | tokenizer traps (letter counting) and needle-in-distractors reading |
| instruction | strict output formats: JSON-only, exact word count, forbidden word, nested multi-line constraints |
| coding | functions that must actually run against hidden tests (`longest_run`, `spiral_order`) |
| anti-hallucination | false-premise pushback and refusing to fabricate a citation |
| explanation / analysis | open-ended quality, scored by a blind LLM judge against a reference answer |

- **core tier** — everyday difficulty; most capable models max this out.
- **hard tier** — chosen to separate a Sonnet-class model from an Opus-class
  one. If your agent matches the reference *here* too, it has genuinely closed
  the gap, not just aced the easy questions.

Objective tasks are graded by deterministic Python (numbers, JSON validity,
word counts, executed code). Open-ended tasks are graded by an LLM judge that
sees a reference answer and a rubric but not which agent produced the text.
The graders are themselves unit-tested — see below.

## Backends

Each agent in the config is one of:

- **`cmd`** — run any CLI that takes the prompt as its final argv and prints the
  answer to stdout (e.g. the `habatch` wrapper, or your own agent CLI). This
  benchmarks the *deployed* agent with all its real scaffolding.
- **`api`** — call the Anthropic Messages API directly with a chosen model.
  Use this for the reference arm, or to benchmark a bare model with no
  scaffolding.

## Quickstart

```bash
pip install anthropic
export ANTHROPIC_API_KEY=sk-ant-...

# 1. verify the graders are correct (no API needed)
python -m bench.test_graders

# 2. edit bench.example.json — point the cmd agent at your CLI,
#    pick a reference model, then run:
python -m bench.run --config bench/bench.example.json --repeats 2 --ref reference
```

Useful flags: `--only reasoning` (one dimension or task id), `--repeats N`
(median over N runs to cut variance), `--max-workers N` (parallelism),
`--out results.json`.

### Config

```jsonc
{
  "judge_model": "claude-opus-4-8",
  "agents": [
    { "name": "my-agent", "backend": "cmd",
      "cmd": ["habatch", "--new", "--no-memory", "{PROMPT}"],
      "env": {"HABATCH_SKIP_PASSPHRASE": "1"}, "timeout": 240 },
    { "name": "raw-base", "backend": "api", "model": "claude-sonnet-4-6" },
    { "name": "reference", "backend": "api", "model": "claude-opus-4-8" }
  ]
}
```

`{PROMPT}` is replaced with the task text. For `cmd` agents the harness strips
ANSI colour and any line matching `strip_patterns` (default: the `habatch`
`[usage]` accounting line) before grading, and reads the per-turn cost from
that line when present.

## Adding tasks

Add a `Task` to `TASKS` in `tasks.py`. Objective tasks supply a
`grader(text) -> (score in [0,1], detail)`; prompts that need a machine-readable
answer ask the model to end with `FINAL: <answer>`. Judge tasks set
`kind="judge"` with a `reference` answer and a `rubric`. Add a case to
`test_graders.py` for any new deterministic grader.

## Design notes / gotchas learned the hard way

These are baked into the current graders because earlier versions got them wrong:

- **Language-agnostic semantic grading.** Anti-hallucination and false-premise
  tasks are judged, not keyword-matched — an agent that correctly refuses in
  Japanese must score the same as one that refuses in English. Keyword graders
  silently penalize non-English answers.
- **LaTeX answers.** Math models write `\frac{2}{11}`, not `2/11`. Fraction
  graders accept both.
- **Unambiguous tasks only.** A reading task whose answer depends on whether an
  intermediate landmark "counts" is a bad task — the stronger model often picks
  the *other* defensible reading and looks wrong. State the counting rule.
- **Fix the output language for English-specific constraints.** "Exactly 8
  words" / "must not contain 'the'" only make sense in English, so those prompts
  say "Respond in English." Otherwise a Japanese answer trivially passes.
