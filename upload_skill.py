#!/usr/bin/env python3
"""Upload a custom Skill directory to Anthropic and record its skill_id.

A Skill is a folder containing a SKILL.md (plus any supporting files) that
teaches the agent a domain workflow. After uploading, attach the returned
skill_id to your agent definition so the agent can use it.

Usage:
    python upload_skill.py ./skills/my-skill
    python upload_skill.py ./skills/my-skill --update   # add a new version

Environment:
    ANTHROPIC_API_KEY   required.
    AGENT_CLI_HOME      optional; config directory (default ~/.config/managed-agent-cli).

The resulting skill_id is stored in config.json under "custom_skills":
    { "custom_skills": { "<skill-dir-name>": "skill_..." } }
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

from anthropic import Anthropic, APIError
from anthropic.lib import files_from_dir


def config_dir() -> Path:
    env = os.environ.get("AGENT_CLI_HOME")
    if env:
        return Path(env).expanduser()
    return Path.home() / ".config" / "managed-agent-cli"


CONFIG_FILE = config_dir() / "config.json"


def main() -> None:
    argv = [a for a in sys.argv[1:] if a not in ("-h", "--help")]
    if not argv or len(sys.argv) < 2 or sys.argv[1] in ("-h", "--help"):
        print(__doc__)
        sys.exit(0)

    skill_dir = Path(argv[0]).expanduser().resolve()
    update_mode = "--update" in argv[1:]
    key = skill_dir.name

    if not CONFIG_FILE.exists():
        sys.exit(f"Config not found: {CONFIG_FILE} — run setup.py first.")
    if not skill_dir.is_dir():
        sys.exit(f"Skill directory not found: {skill_dir}")
    if not (skill_dir / "SKILL.md").exists():
        sys.exit(f"SKILL.md missing in: {skill_dir}")

    cfg = json.loads(CONFIG_FILE.read_text())
    custom = cfg.setdefault("custom_skills", {})
    existing_id = custom.get(key)

    if existing_id and not update_mode:
        print(f"Skill '{key}' already uploaded: {existing_id}")
        print("Pass --update to add a new version.")
        return

    files = list(files_from_dir(str(skill_dir)))
    print(f"Uploading {len(files)} file(s) from {skill_dir}")

    client = Anthropic()
    try:
        if update_mode and existing_id:
            print(f"Adding a new version to {existing_id} ...")
            try:
                result = client.beta.skills.versions.create(existing_id, files=files)
                print(f"      new version: {getattr(result, 'version', '?')}")
            except (AttributeError, TypeError):
                print("      versions.create unavailable; creating a new skill ...")
                result = client.beta.skills.create(display_title=key, files=files)
                custom[key] = result.id
                print(f"      new skill_id: {result.id}")
        else:
            print(f"Creating skill '{key}' ...")
            result = client.beta.skills.create(display_title=key, files=files)
            custom[key] = result.id
            print(f"      skill_id: {result.id}")
    except APIError as e:
        status = getattr(e, "status_code", getattr(e, "status", "?"))
        sys.exit(f"\nAPI error: status={status} message={getattr(e, 'message', str(e))}")

    CONFIG_FILE.write_text(json.dumps(cfg, indent=2, ensure_ascii=False))
    print(f"\n✅ uploaded — saved to {CONFIG_FILE} (custom_skills.{key})")
    print("   attach this skill_id to your agent definition to enable it.")


if __name__ == "__main__":
    main()
