#!/usr/bin/env python3
"""First-time setup — create an Anthropic environment + agent and save the IDs.

Run once per user. Re-running offers to overwrite. Agent updates (system prompt,
tools, ...) are done by editing the agent in the API / a separate update script.

Usage:
    python setup.py
    AGENT_NAME="MyBuddy" AGENT_MODEL="claude-sonnet-4-6" python setup.py

Environment:
    ANTHROPIC_API_KEY   required.
    AGENT_CLI_HOME      optional; config directory (default ~/.config/managed-agent-cli).
    AGENT_NAME          optional; display name for the agent (default "MyAgent").
    AGENT_MODEL         optional; model id (default claude-sonnet-4-6).
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


CONFIG_DIR = config_dir()
CONFIG_FILE = CONFIG_DIR / "config.json"
PROMPT_FILE = CONFIG_DIR / "system_prompt.md"

AGENT_NAME = os.environ.get("AGENT_NAME", "MyAgent")
AGENT_MODEL = os.environ.get("AGENT_MODEL", "claude-sonnet-4-6")

# Used only if no system_prompt.md exists yet. Edit the file afterwards to taste.
DEFAULT_SYSTEM_PROMPT = f"""You are {AGENT_NAME}, a personal research / coding assistant.

# Style
- Be concise, technical, and honest about uncertainty.
- Mark anything you are guessing. Cite sources when you have them.

# Behavior
- Before changing code, `read` the relevant file to understand the current state.
- Confirm with the user before large or destructive actions (deletes, git push,
  external side effects). Reading, inspecting, and running tests need no confirmation.
- Use web_search / web_fetch for post-cutoff facts or specific URLs.
- Run type-checks, linters, and tests via bash to verify your own output.
"""


def main() -> None:
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    CONFIG_DIR.chmod(0o700)

    if CONFIG_FILE.exists():
        existing = json.loads(CONFIG_FILE.read_text())
        print("Existing config found:")
        print(f"  agent_id:       {existing.get('agent_id')}")
        print(f"  agent_version:  {existing.get('agent_version')}")
        print(f"  environment_id: {existing.get('environment_id')}")
        ans = input("Create new and overwrite? [y/N]: ").strip().lower()
        if ans != "y":
            print("Aborted.")
            sys.exit(0)

    if not PROMPT_FILE.exists():
        PROMPT_FILE.write_text(DEFAULT_SYSTEM_PROMPT)
        PROMPT_FILE.chmod(0o600)
    system_prompt = PROMPT_FILE.read_text()
    print(f"system prompt: {PROMPT_FILE} ({len(system_prompt)} chars)")

    client = Anthropic()

    try:
        print("\n[1/2] creating environment ...")
        env = client.beta.environments.create(
            name=f"{AGENT_NAME.lower().replace(' ', '-')}-env",
            config={
                "type": "cloud",
                "networking": {"type": "unrestricted"},
            },
        )
        print(f"      environment_id: {env.id}")

        print("\n[2/2] creating agent ...")
        agent = client.beta.agents.create(
            name=AGENT_NAME,
            description=f"{AGENT_NAME} — personal research / coding agent",
            model=AGENT_MODEL,
            system=system_prompt,
            tools=[{"type": "agent_toolset_20260401"}],
        )
        print(f"      agent_id:      {agent.id}")
        print(f"      agent_version: {agent.version}")

    except APIError as e:
        print(f"\nAPI error: status={getattr(e, 'status', '?')} message={getattr(e, 'message', e)}")
        sys.exit(1)

    # Preserve any user-set fields (gh_repo, github_repo_url, memory_instructions).
    config = json.loads(CONFIG_FILE.read_text()) if CONFIG_FILE.exists() else {}
    config.update({
        "agent_id": agent.id,
        "agent_version": agent.version,
        "environment_id": env.id,
        "model": AGENT_MODEL,
        "name": AGENT_NAME,
    })
    CONFIG_FILE.write_text(json.dumps(config, indent=2, ensure_ascii=False))
    CONFIG_FILE.chmod(0o600)

    print("\n✅ setup complete")
    print(f"   saved: {CONFIG_FILE}")
    print("   next:  run setup_memory.py to enable cross-session memory (optional)")
    print("   start: agent 'your question'   or   agent -i")


if __name__ == "__main__":
    main()
