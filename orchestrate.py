#!/usr/bin/env python3
"""Fan-out orchestrator — the Claude-Code-style decomposition pattern, made explicit.

This is the reimplementation of the orchestration spec (手段B): it does, against
your own Managed Agent, what Claude Code does internally — and it PRINTS its
decomposition + parallel execution so running it doubles as an observation of the
pattern (手段A).

Pipeline:
    1. plan   — one call asks the agent to decompose the task into a JSON DAG
                (independent subtasks + dependency edges).
    2. exec   — topological layers; within a layer, subtasks run in PARALLEL,
                each in its own fresh, narrow-context session. Dependent subtasks
                receive their upstream results as context.
    3. merge  — one synthesis call integrates the sub-results into a final answer.

Everything here is commodity: DAG, topological sort, bounded parallel map, context
isolation, retry/fallback. No proprietary anything. Rate-limit handling and usage
accounting are inherited from cli.py.

Usage:
    set -a && source ~/.config/managed-agent-cli/anthropic.env && set +a
    python orchestrate.py "your decomposable task" \
        --config ~/.config/managed-agent-cli/config.json --workers 3

    # no API key? watch the fan-out trace offline (canned results):
    AGENT_CLI_MOCK=1 python orchestrate.py "your decomposable task"
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import cli  # noqa: E402

from anthropic import Anthropic  # noqa: E402

PLAN_INSTRUCTIONS = (
    "You are a task decomposer for a parallel agent runner. Break the TASK below "
    "into 3-6 subtasks. Return ONLY a JSON object, no prose, no code fences:\n"
    '{"subtasks":[{"id":"t1","prompt":"<self-contained instruction>","deps":[]}]}\n'
    "Rules:\n"
    "- Maximize subtasks with deps:[] so they run in parallel.\n"
    "- Use deps ONLY when a subtask genuinely needs another's output.\n"
    "- Each prompt must be fully self-contained (a fresh agent sees only it + dep results).\n"
    "TASK:\n"
)


def load_cfg(path: str | None) -> dict:
    if cli.mock_enabled() and not path:
        return {"name": "MockAgent", "model": cli.DEFAULT_MODEL, "agent_id": "mock", "environment_id": "mock"}
    cfg = json.loads(Path(path).expanduser().read_text()) if path else cli.load_config()
    for key in ("agent_id", "environment_id"):
        if not cfg.get(key):
            sys.exit(f"config missing '{key}' — pass --config to a provisioned agent's json")
    return cfg


def run_turn(client: Anthropic, cfg: dict, prompt: str, label: str) -> dict:
    """One turn in a fresh, narrow-context session. Collects text + metrics."""
    model = cfg.get("model", cli.DEFAULT_MODEL)
    tracker = cli.UsageTracker(model)
    rate_hits = 0
    parts: list[str] = []
    t0 = time.monotonic()
    session = cli.create_session(client, cfg, with_memory=False, with_repo=False, title=f"orch:{label}")
    stream = client.beta.sessions.events.stream(session.id)
    try:
        client.beta.sessions.events.send(
            session.id,
            events=[{"type": "user.message", "content": [{"type": "text", "text": prompt}]}],
        )
        for event in stream:
            if cli._is_rate_limit_error(event):
                rate_hits += 1
                continue
            if event.type == "agent.message":
                for block in event.content:
                    if block.type == "text":
                        parts.append(block.text)
            elif event.type == "span.model_request_end":
                u = getattr(event, "model_usage", None)
                if u:
                    tracker.add(u)
            if event.type == "agent.custom_tool_use":
                cli.handle_custom_tool(client, session.id, event, cfg=cfg, verbose=False)
            if event.type == "session.status_terminated":
                break
            if cli.is_terminal_idle(event):
                break
    finally:
        try:
            stream.close()
        except Exception:
            pass
        try:
            client.beta.sessions.archive(session.id)
        except Exception:
            pass
    return {
        "label": label, "text": "".join(parts).strip(), "wall": time.monotonic() - t0,
        "in": tracker.input, "out": tracker.output, "usd": tracker.cost_usd(), "rate_hits": rate_hits,
    }


def extract_json(text: str) -> dict | None:
    """Pull the first {...} object out of a possibly-fenced/prosey response."""
    s = text.strip()
    if s.startswith("```"):
        s = s.split("```", 2)[1] if s.count("```") >= 2 else s
        s = s[len("json"):] if s.lstrip().startswith("json") else s
    i, j = s.find("{"), s.rfind("}")
    if i == -1 or j == -1 or j < i:
        return None
    try:
        return json.loads(s[i:j + 1])
    except json.JSONDecodeError:
        return None


def topo_layers(subtasks: list[dict]) -> tuple[list[list[str]], dict[str, dict]]:
    by_id = {s["id"]: s for s in subtasks}
    done: set[str] = set()
    layers: list[list[str]] = []
    remaining = list(by_id)
    while remaining:
        layer = [sid for sid in remaining if all(d in done for d in by_id[sid].get("deps", []))]
        if not layer:  # cycle / dangling dep → flush the rest as one layer (graceful)
            layer = list(remaining)
        layers.append(layer)
        done.update(layer)
        remaining = [sid for sid in remaining if sid not in done]
    return layers, by_id


def main() -> None:
    ap = argparse.ArgumentParser(description="Claude-Code-style fan-out orchestrator.")
    ap.add_argument("task", nargs="+", help="the task to decompose and run")
    ap.add_argument("--config", help="path to a config json (agent_id/environment_id/model)")
    ap.add_argument("--workers", type=int, default=3, help="parallel width within a layer (default 3)")
    args = ap.parse_args()
    task = " ".join(args.task)

    cfg = load_cfg(args.config)
    try:
        client = cli.make_client()  # honours AGENT_CLI_MOCK for offline trials
    except Exception as e:
        sys.exit(f"client init failed: {e}\nDid you source ANTHROPIC_API_KEY? (or set AGENT_CLI_MOCK=1)")

    print(f"agent: {cfg.get('name', cfg['agent_id'])}  width: {args.workers}")
    print(f"task:  {task}\n")
    all_metrics: list[dict] = []

    # --- 1. plan ---  ← OBSERVE: a single planning call produces the DAG
    print("[plan] decomposing into a DAG ...", flush=True)
    plan = run_turn(client, cfg, PLAN_INSTRUCTIONS + task, "plan")
    all_metrics.append(plan)
    parsed = extract_json(plan["text"])
    subtasks = (parsed or {}).get("subtasks") if parsed else None
    if not subtasks:
        print("       ⚠️  could not parse a DAG — falling back to a single task (no fan-out).")
        subtasks = [{"id": "t1", "prompt": task, "deps": []}]
    layers, by_id = topo_layers(subtasks)
    print(f"       DAG: {len(subtasks)} subtasks, {len(layers)} layer(s)")
    for li, layer in enumerate(layers):
        labels = ", ".join(f"{sid}(deps:{by_id[sid].get('deps') or '∅'})" for sid in layer)
        print(f"       layer {li} — parallel x{min(args.workers, len(layer))}: {labels}")
    print()

    # --- 2. exec ---  ← OBSERVE: independent subtasks run concurrently, per layer
    results: dict[str, str] = {}
    layer_walls: list[float] = []
    subtask_wall_sum = 0.0
    for li, layer in enumerate(layers):
        print(f"[exec] layer {li} ({len(layer)} subtask(s), parallel) ...", flush=True)

        def make_prompt(sid: str) -> str:
            s = by_id[sid]
            ctx = "".join(f"\n[Result of {d}]:\n{results.get(d, '')}\n" for d in s.get("deps", []))
            return (ctx + "\n" + s["prompt"]) if ctx else s["prompt"]

        t0 = time.monotonic()
        with ThreadPoolExecutor(max_workers=args.workers) as ex:
            out = list(ex.map(lambda sid: (sid, run_turn(client, cfg, make_prompt(sid), sid)), layer))
        layer_walls.append(time.monotonic() - t0)
        for sid, r in out:
            results[sid] = r["text"]
            all_metrics.append(r)
            subtask_wall_sum += r["wall"]
            flag = f"  ⚠️429x{r['rate_hits']}" if r["rate_hits"] else ""
            print(f"       {sid}: {r['wall']:.1f}s, {r['out']} out tok{flag}")
    print()

    # --- 3. merge ---  ← OBSERVE: one synthesis call integrates sub-results
    print("[merge] synthesizing final answer ...", flush=True)
    synth_prompt = (
        f"Original task:\n{task}\n\nSub-results:\n"
        + "\n\n".join(f"[{sid}]\n{results.get(sid, '')}" for sid in by_id)
        + "\n\nIntegrate these into one coherent final answer for the original task."
    )
    synth = run_turn(client, cfg, synth_prompt, "synth")
    all_metrics.append(synth)
    print()
    print("=" * 72)
    print("FINAL ANSWER")
    print("=" * 72)
    print(synth["text"])

    # --- metrics + observed parallel benefit ---
    end_to_end = sum(m["wall"] for m in all_metrics if m["label"] in ("plan", "synth")) \
        + sum(layer_walls)
    parallel_exec = sum(layer_walls)
    rate_total = sum(m["rate_hits"] for m in all_metrics)
    usd_total = sum(m["usd"] for m in all_metrics)
    speedup = (subtask_wall_sum / parallel_exec) if parallel_exec > 0 else float("nan")

    print("\n" + "=" * 72)
    print("ORCHESTRATION METRICS")
    print("-" * 72)
    print(f"  subtasks / layers        {len(subtasks)} / {len(layers)}")
    print(f"  exec time (parallel)     {parallel_exec:.1f}s")
    print(f"  exec time if sequential  {subtask_wall_sum:.1f}s  (sum of subtask walls)")
    print(f"  → in-layer speedup       {speedup:.2f}x")
    print(f"  end-to-end (plan+exec+merge)  {end_to_end:.1f}s")
    print(f"  rate-limit (429) hits    {rate_total}")
    print(f"  token cost (tokens only) ${usd_total:.4f}")
    if rate_total >= max(2, len(subtasks) // 2):
        print("  ⚠️  heavy throttling → Tier is the ceiling; lower --workers or raise tier.")
    print("=" * 72)


if __name__ == "__main__":
    main()
