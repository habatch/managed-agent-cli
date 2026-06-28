#!/usr/bin/env python3
"""Fan-out benchmark — does parallel decomposition actually finish faster *for you*?

Runs the SAME set of independent subtasks two ways against your existing Managed
Agent, and reports the difference:

  1. sequential  — one subtask after another (the conventional single-thread shape)
  2. parallel    — N subtasks at once, each in its own narrow-context session
                   (the "short context, fan out" shape)

It measures wall-clock, token cost, and rate-limit (429) hits for each mode, so
you can decide — with real numbers, before building the full layer — whether
parallelism pays off on *your* API tier.

Honest framing baked in:
  - Both modes do identical work → token cost is ~equal. Only WALL-CLOCK differs.
  - Parallelism is capped by your per-minute token limit, not by this code.
    If the parallel run racks up rate-limit hits, your tier is the bottleneck.

Usage:
    # source your API key first
    set -a && source ~/.config/managed-agent-cli/anthropic.env && set +a

    # point at any config json with agent_id / environment_id / model
    python benchmark_fanout.py --config ~/.config/managed-agent-cli/config.json --workers 3

Cost: ~8 short session-turns total (4 subtasks x 2 modes). Roughly $0.1-0.3 in
tokens at Sonnet pricing, plus Managed-Agent sandbox runtime. It spends real money
on your account — that is the point of an actual measurement.
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

# Reuse the CLI's session/usage/rate-limit primitives (same directory).
sys.path.insert(0, str(Path(__file__).resolve().parent))
import cli  # noqa: E402

from anthropic import Anthropic, APIError  # noqa: E402

# Small, independent, terse-answer subtasks. Each is its own sub-agent's whole job
# — exactly the "narrow context" shape. Cheap on purpose for a first measurement.
DEFAULT_SUBTASKS = [
    "Name the rule of this sequence in one short phrase: 2, 6, 12, 20, 30. Answer only.",
    "Give the standard English term for 計算材料科学 in one phrase. Answer only.",
    "How many digits does 20! (factorial) have? Digits only.",
    "Give the HEX complement of RGB(255,0,0). HEX only.",
]


def load_cfg(path: str | None) -> dict:
    """Load a config json that has at least agent_id / environment_id."""
    if cli.mock_enabled() and not path:
        return {"name": "MockAgent", "model": cli.DEFAULT_MODEL, "agent_id": "mock", "environment_id": "mock"}
    if path:
        p = Path(path).expanduser()
        cfg = json.loads(p.read_text())
    else:
        cfg = cli.load_config()
    for key in ("agent_id", "environment_id"):
        if not cfg.get(key):
            sys.exit(f"config missing '{key}' — pass --config to a provisioned agent's json")
    return cfg


def run_one(client: Anthropic, cfg: dict, prompt: str, label: str) -> dict:
    """One subtask in its own fresh, narrow-context session. Returns metrics."""
    model = cfg.get("model", cli.DEFAULT_MODEL)
    tracker = cli.UsageTracker(model)
    rate_hits = 0
    t0 = time.monotonic()
    try:
        session = cli.create_session(client, cfg, with_memory=False, with_repo=False,
                                     title=f"bench:{label}")
    except APIError as e:
        return {"label": label, "wall": time.monotonic() - t0, "error": f"create:{getattr(e,'status',e)}",
                "in": 0, "out": 0, "usd": 0.0, "rate_hits": 1}

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
            if event.type == "span.model_request_end":
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
        "label": label, "wall": time.monotonic() - t0,
        "in": tracker.input, "out": tracker.output,
        "cache_r": tracker.cache_read, "cache_w": tracker.cache_write,
        "usd": tracker.cost_usd(), "rate_hits": rate_hits,
    }


def agg(results: list[dict]) -> dict:
    return {
        "in": sum(r["in"] for r in results),
        "out": sum(r["out"] for r in results),
        "usd": sum(r["usd"] for r in results),
        "rate_hits": sum(r["rate_hits"] for r in results),
        "errors": sum(1 for r in results if r.get("error")),
    }


def main() -> None:
    ap = argparse.ArgumentParser(description="Fan-out (parallel) vs sequential benchmark.")
    ap.add_argument("--config", help="path to a config json with agent_id/environment_id/model")
    ap.add_argument("--workers", type=int, default=3, help="parallel width (default 3; Tier 1 ~ 2-3)")
    ap.add_argument("--tasks-file", help="optional file, one subtask prompt per line")
    args = ap.parse_args()

    cfg = load_cfg(args.config)
    if args.tasks_file:
        subtasks = [l.strip() for l in Path(args.tasks_file).read_text().splitlines() if l.strip()]
    else:
        subtasks = DEFAULT_SUBTASKS
    n = len(subtasks)

    try:
        client = cli.make_client()  # honours AGENT_CLI_MOCK for offline trials
    except Exception as e:
        sys.exit(f"client init failed: {e}\nDid you source ANTHROPIC_API_KEY? (or set AGENT_CLI_MOCK=1)")

    print(f"agent: {cfg.get('name', cfg['agent_id'])}  model: {cfg.get('model', cli.DEFAULT_MODEL)}")
    print(f"subtasks: {n}   parallel width: {args.workers}\n")

    # --- sequential ---
    print("[1/2] sequential ...", flush=True)
    t0 = time.monotonic()
    seq = [run_one(client, cfg, p, f"seq{i}") for i, p in enumerate(subtasks)]
    seq_wall = time.monotonic() - t0
    print(f"      done in {seq_wall:.1f}s")

    # --- parallel ---
    print(f"[2/2] parallel (workers={args.workers}) ...", flush=True)
    t0 = time.monotonic()
    with ThreadPoolExecutor(max_workers=args.workers) as ex:
        par = list(ex.map(lambda ip: run_one(client, cfg, ip[1], f"par{ip[0]}"), enumerate(subtasks)))
    par_wall = time.monotonic() - t0
    print(f"      done in {par_wall:.1f}s\n")

    sa, pa = agg(seq), agg(par)
    speedup = (seq_wall / par_wall) if par_wall > 0 else float("nan")

    print("=" * 72)
    print(f"{'mode':18s} {'wall_s':>8s} {'tok_in':>8s} {'tok_out':>8s} {'USD':>8s} {'429':>5s} {'err':>4s}")
    print("-" * 72)
    print(f"{'sequential':18s} {seq_wall:>8.1f} {sa['in']:>8d} {sa['out']:>8d} {sa['usd']:>8.4f} {sa['rate_hits']:>5d} {sa['errors']:>4d}")
    print(f"{f'parallel(x{args.workers})':18s} {par_wall:>8.1f} {pa['in']:>8d} {pa['out']:>8d} {pa['usd']:>8.4f} {pa['rate_hits']:>5d} {pa['errors']:>4d}")
    print("=" * 72)
    print(f"wall-clock speedup: {speedup:.2f}x   (sequential / parallel)")
    print(f"token cost delta:   ${pa['usd'] - sa['usd']:+.4f}  (should be ~0 — same work)")

    # --- honest verdict ---
    print("\nverdict:")
    heavy_throttle = pa["rate_hits"] >= max(2, (n + 1) // 2)
    if pa["errors"] or heavy_throttle:
        print("  ⚠️  parallel run hit rate limits / errors → YOUR TIER is the bottleneck, not the algorithm.")
        print("      Raise tier at console.anthropic.com before building a wide fan-out layer.")
    elif speedup >= 1.5:
        print(f"  ✅  parallelism pays off ({speedup:.2f}x faster, low throttling). Building the fan-out layer is worth it.")
    elif speedup >= 1.15:
        print(f"  ◐   modest gain ({speedup:.2f}x). Worth it for big decomposable tasks; marginal for small ones.")
    else:
        print(f"  ✗   little gain ({speedup:.2f}x). Cold-start/overhead dominates at this size; fan-out won't help much here.")
    print("\nnote: token cost is tokens only; Managed-Agent sandbox runtime is billed separately.")


if __name__ == "__main__":
    main()
