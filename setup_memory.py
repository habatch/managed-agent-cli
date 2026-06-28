#!/usr/bin/env python3
"""Create a memory_store for cross-session memory (optional).

A memory_store is cloud-persisted memory the agent can read/write across
sessions — decisions, preferences, in-progress task snapshots. Once created,
the CLI attaches it automatically (disable per-run with `agent --no-memory`).

Usage:
    python setup_memory.py
    python setup_memory.py --force   # recreate even if one already exists

Environment:
    ANTHROPIC_API_KEY   required.
    AGENT_CLI_HOME      optional; config directory (default ~/.config/managed-agent-cli).
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

from anthropic import Anthropic, APIError


def config_dir() -> Path:
    env = os.environ.get("AGENT_CLI_HOME")
    if env:
        return Path(env).expanduser()
    return Path.home() / ".config" / "managed-agent-cli"


CONFIG_FILE = config_dir() / "config.json"


def main() -> None:
    force = "--force" in sys.argv[1:]

    if not CONFIG_FILE.exists():
        sys.exit(f"Config not found: {CONFIG_FILE} — run setup.py first.")
    cfg = json.loads(CONFIG_FILE.read_text())

    if cfg.get("memory_store_id") and not force:
        print(f"memory_store_id already set: {cfg['memory_store_id']}")
        print("Pass --force to recreate.")
        return

    name = cfg.get("name", "agent")
    store_name = f"{name.lower().replace(' ', '-')}-memory"

    client = Anthropic()
    try:
        print(f"creating memory_store '{store_name}' ...")
        store = client.beta.memory_stores.create(
            name=store_name,
            description=f"Cross-session memory for {name}.",
        )
    except APIError as e:
        sys.exit(f"API error: status={getattr(e, 'status', '?')} message={getattr(e, 'message', e)}")

    print(f"      memory_store_id: {store.id}")

    cfg["memory_store_id"] = store.id
    CONFIG_FILE.write_text(json.dumps(cfg, indent=2, ensure_ascii=False))

    print(f"\n✅ memory_store created — saved to {CONFIG_FILE}")
    print("   the CLI will attach it automatically on the next run.")


if __name__ == "__main__":
    main()
