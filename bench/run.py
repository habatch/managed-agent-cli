#!/usr/bin/env python3
"""Parity benchmark runner: is agent B as good as reference agent A?

Runs the same task suite against one or more agents, grades every answer
(deterministic graders + a blind LLM judge for open-ended tasks), and prints a
per-dimension scorecard plus cost and latency. Results are written to JSON for
later inspection / regression tracking.

Config is a small JSON file (see ``bench.example.json``). Each agent entry is
either a ``cmd`` backend (run an external CLI such as ``habatch``) or an ``api``
backend (call the Anthropic API with a chosen model directly).

Usage::

    python -m bench.run --config bench.example.json
    python -m bench.run --config bench.example.json --repeats 3 --only reasoning
"""

from __future__ import annotations

import argparse
import json
import statistics
import sys
import time
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from .agents import ApiAgent, CmdAgent
from .judge import Judge
from .tasks import TASKS, Task


def build_agent(spec: dict):
    kind = spec["backend"]
    if kind == "cmd":
        return CmdAgent(
            name=spec["name"], cmd=spec["cmd"], env=spec.get("env"),
            timeout=spec.get("timeout", 240),
            strip_patterns=spec.get("strip_patterns"),
        )
    if kind == "api":
        return ApiAgent(
            name=spec["name"], model=spec["model"], system=spec.get("system"),
            max_tokens=spec.get("max_tokens", 2048),
            thinking_budget=spec.get("thinking_budget"),
        )
    raise ValueError(f"unknown backend: {kind}")


def grade(task: Task, text: str, judge: Judge | None) -> tuple[float, str]:
    if task.kind == "objective":
        return task.grader(text)
    if judge is None:
        return 0.0, "no judge configured"
    return judge.score(task.prompt, task.reference, task.rubric, text)


def run(config_path: str, repeats: int, only: str | None, max_workers: int) -> dict:
    cfg = json.loads(Path(config_path).read_text())
    agents = [build_agent(a) for a in cfg["agents"]]
    judge_model = cfg.get("judge_model", "claude-opus-4-8")
    tasks = [t for t in TASKS if not only or t.dim == only or t.id == only]
    # The tools tier needs a tool-capable agent, so it is opt-in: excluded from
    # the default run, included only when --only explicitly targets it.
    if not only:
        tasks = [t for t in tasks if t.tier != "tools"]
    if not tasks:
        sys.exit(f"no tasks match --only {only!r}")

    needs_judge = any(t.kind == "judge" for t in tasks)
    judge = Judge(judge_model) if needs_judge else None

    # Build the work list: (agent, task, repeat_index). Tool tasks require a
    # tool-capable (cmd) agent; skip them for bare api agents.
    jobs = [(ag, t, r) for ag in agents for t in tasks for r in range(repeats)
            if not (t.tier == "tools" and not isinstance(ag, CmdAgent))]
    print(f"Running {len(tasks)} tasks x {repeats} repeats x {len(agents)} agents "
          f"= {len(jobs)} calls (up to {max_workers} in parallel)\n", flush=True)

    raw: dict[str, list] = defaultdict(list)

    def do(job):
        ag, task, r = job
        if task.setup:
            task.setup()
        res = ag.ask(task.prompt)
        if not res.ok:
            return ag.name, task, 0.0, f"[error] {res.error}", res
        score, detail = grade(task, res.text, judge)
        return ag.name, task, score, detail, res

    t0 = time.time()
    done = 0
    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        futs = {pool.submit(do, j): j for j in jobs}
        for fut in as_completed(futs):
            name, task, score, detail, res = fut.result()
            raw[name].append({
                "task": task.id, "dim": task.dim, "score": score, "detail": detail,
                "latency_s": round(res.latency_s, 1), "cost_usd": res.cost_usd,
                "ok": res.ok, "answer": res.text[:600],
            })
            done += 1
            flag = "OK " if score >= 0.999 else ("~~ " if score > 0 else "XX ")
            print(f"  [{done}/{len(jobs)}] {flag}{name:<12} {task.id:<18} "
                  f"{score:.2f}  {detail[:50]}", flush=True)
    wall = time.time() - t0

    report = summarize(raw, tasks, wall)
    return report


def summarize(raw: dict, tasks: list[Task], wall: float) -> dict:
    dims = sorted({t.dim for t in tasks})
    tier_of = {t.id: t.tier for t in tasks}
    tiers = sorted({t.tier for t in tasks})
    agents = list(raw.keys())
    report = {"agents": {}, "dims": dims, "tiers": tiers, "wall_s": round(wall, 1)}
    for name in agents:
        rows = raw[name]
        by_dim = defaultdict(list)
        by_task = defaultdict(list)
        by_tier = defaultdict(list)
        for row in rows:
            by_dim[row["dim"]].append(row["score"])
            by_task[row["task"]].append(row["score"])
            by_tier[tier_of.get(row["task"], "core")].append(row["score"])
        report["agents"][name] = {
            "overall": round(statistics.mean(r["score"] for r in rows), 3),
            "by_dim": {d: round(statistics.mean(by_dim[d]), 3) for d in dims},
            "by_tier": {t: round(statistics.mean(by_tier[t]), 3) for t in tiers if by_tier[t]},
            "by_task": {t: round(statistics.mean(s), 3) for t, s in by_task.items()},
            "total_cost_usd": round(sum(r["cost_usd"] for r in rows), 4),
            "avg_latency_s": round(statistics.mean(r["latency_s"] for r in rows), 1),
            "rows": rows,
        }
    return report


def print_scorecard(report: dict, ref_name: str | None) -> None:
    agents = list(report["agents"].keys())
    dims = report["dims"]
    ref = ref_name if ref_name in agents else agents[0]
    w = 22
    print("\n" + "=" * (w + 12 * len(agents)))
    print("PARITY SCORECARD  (score 0.00-1.00, higher = better)")
    print("=" * (w + 12 * len(agents)))
    header = f"{'dimension':<{w}}" + "".join(f"{a[:11]:>12}" for a in agents)
    print(header)
    print("-" * len(header))
    for d in dims:
        line = f"{d:<{w}}" + "".join(
            f"{report['agents'][a]['by_dim'][d]:>12.2f}" for a in agents)
        print(line)
    print("-" * len(header))
    if len(report.get("tiers", [])) > 1:
        print("-" * len(header))
        for t in report["tiers"]:
            print(f"{'tier: '+t:<{w}}" + "".join(
                f"{report['agents'][a]['by_tier'].get(t, float('nan')):>12.2f}" for a in agents))
    print("-" * len(header))
    print(f"{'OVERALL':<{w}}" + "".join(
        f"{report['agents'][a]['overall']:>12.2f}" for a in agents))
    print(f"{'parity vs '+ref[:11]:<{w}}" + "".join(
        f"{report['agents'][a]['overall']/max(report['agents'][ref]['overall'],1e-9):>11.0%}"
        for a in agents))
    print(f"{'cost (USD)':<{w}}" + "".join(
        f"{report['agents'][a]['total_cost_usd']:>12.3f}" for a in agents))
    print(f"{'avg latency (s)':<{w}}" + "".join(
        f"{report['agents'][a]['avg_latency_s']:>12.1f}" for a in agents))
    print("=" * len(header))


def main():
    ap = argparse.ArgumentParser(description="Managed-agent parity benchmark")
    ap.add_argument("--config", required=True)
    ap.add_argument("--repeats", type=int, default=1, help="runs per task (median of scores)")
    ap.add_argument("--only", help="filter to a dimension or task id")
    ap.add_argument("--max-workers", type=int, default=4)
    ap.add_argument("--out", default="bench_result.json")
    ap.add_argument("--ref", help="reference agent name for the parity row")
    args = ap.parse_args()

    report = run(args.config, args.repeats, args.only, args.max_workers)
    print_scorecard(report, args.ref)
    Path(args.out).write_text(json.dumps(report, ensure_ascii=False, indent=2))
    print(f"\nFull results -> {args.out}")


if __name__ == "__main__":
    main()
