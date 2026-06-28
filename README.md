# Managed Agent CLI

A small, **config-driven** command-line companion for an [Anthropic Managed Agent](https://docs.anthropic.com).
Each user provisions their *own* private agent — persistent sessions,
cross-session memory, local tools (bash / file write / GitHub issues), token &
cost tracking — with no identity baked into the code.

This is a generalized version of a personal tool. All per-user values live in one
config file, so anyone can clone, run `setup`, and get their own agent.

> **Trying it for feedback?** Two paths:
> - **Have an Anthropic API key + Managed Agents access** → do the full setup below.
> - **Don't / not sure** → run **offline mock mode**, no key or setup needed:
>   ```bash
>   pip install -r requirements.txt
>   AGENT_CLI_MOCK=1 python cli.py "hello"          # canned reply, zero cost
>   AGENT_CLI_MOCK=1 python orchestrate.py "plan a trip to Kyoto"   # see the fan-out trace
>   ```
>   Then please file feedback via the repo's **Issues → Feedback** template.

---

## What you get

| Feature | Detail |
|---|---|
| Persistent sessions | Resumes a session for 1h (TTL) so back-to-back turns skip cold start |
| Cross-session memory | Optional cloud `memory_store` for decisions/preferences/task snapshots |
| Local tools | `local_bash`, `local_write`, `gh_issue` run on *your* machine |
| Cost visibility | Per-turn token + USD, `--usage` for the running total |
| Resilience | Retries with backoff; auto-resends on per-minute rate limits |
| Diagnostics | `--doctor` checks API key, gh auth, config, session, API reachability |
| Optional passphrase | A launch guard you set at install time (empty = none) |

---

## Requirements

- Python 3.10+
- An Anthropic API key with Managed Agents access (`console.anthropic.com`)
- `gh` CLI — optional, only for the `gh_issue` tool and `--with-repo`

---

## Install

```bash
cd managed-agent-cli
pip install -r requirements.txt        # or into a venv / conda env

# Generate a launcher on your PATH. All vars are optional.
CMD_NAME=mybuddy ./install.sh
```

`install.sh` knobs (env vars):

| Var | Default | Meaning |
|---|---|---|
| `CMD_NAME` | `agent` | the command you'll type |
| `BIN_DIR` | `~/.local/bin` | where the launcher goes |
| `AGENT_CLI_HOME` | `~/.config/managed-agent-cli` | config + logs + usage |
| `ENV_FILE` | `$AGENT_CLI_HOME/anthropic.env` | file exporting `ANTHROPIC_API_KEY` |
| `PYTHON_RUNNER` | `python3` | e.g. `micromamba run -n buddy python` |
| `PASSPHRASE` | empty | optional launch guard |

Then:

```bash
# 1. paste your key
$EDITOR ~/.config/managed-agent-cli/anthropic.env   # export ANTHROPIC_API_KEY="sk-ant-..."

# 2. create YOUR agent + environment (name/model configurable)
AGENT_NAME="My Buddy" AGENT_MODEL="claude-sonnet-4-6" python setup.py

# 3. (optional) enable cross-session memory
python setup_memory.py

# 4. verify, then chat
mybuddy --doctor
mybuddy "summarize what changed in this repo"
```

---

## Usage

```text
mybuddy "question"        one turn (resume active session, else new)
mybuddy --new "question"  always a fresh session
mybuddy -i                interactive REPL
mybuddy --no-memory       do not attach the memory_store this run
mybuddy --with-repo "q"   attach a github_repository (slower; needs gh auth)
mybuddy --status          show config
mybuddy --doctor          environment diagnostics
mybuddy --usage           cumulative token / USD cost
mybuddy -v ...            verbose (show tool I/O, thinking, per-request usage)
```

---

## Configuration

`setup.py` writes `$AGENT_CLI_HOME/config.json`. See `config.example.json`.
Fields you may edit by hand:

| Field | Set by | Purpose |
|---|---|---|
| `name`, `model` | setup.py | display name + model id |
| `agent_id`, `agent_version`, `environment_id` | setup.py | created agent/env |
| `memory_store_id` | setup_memory.py | cross-session memory |
| `github_repo_url` | you | repo attached with `--with-repo` |
| `gh_repo` | you | default `owner/name` for the `gh_issue` tool |
| `memory_instructions` | you | what the agent should/shouldn't remember |
| `custom_skills` | upload_skill.py | uploaded Skill ids |
| `self_repair_verify` | you | test command run after `--self-repair` (e.g. `python -m pytest -q`); non-zero → rollback |

The system prompt lives at `$AGENT_CLI_HOME/system_prompt.md` (seeded from
`system_prompt.example.md` on first setup). Edit it to set persona, language,
and specialty. Re-running `setup.py` re-creates the agent with the current prompt.

---

## Custom skills

A Skill is a folder with a `SKILL.md` (+ supporting files) teaching a workflow:

```bash
python upload_skill.py ./skills/my-skill            # upload → records skill_id
python upload_skill.py ./skills/my-skill --update   # add a new version
```

The `skill_id` is saved under `config.custom_skills`. Attach it to your agent
definition (via the API) so the agent can use it.

---

## Local tools (run on your machine)

When the agent calls these custom tools, the CLI executes them locally and
streams results back:

| Tool | Action | Guardrails |
|---|---|---|
| `local_bash` | run a shell command | 30s default timeout, output truncated |
| `local_write` | write a file | absolute path required; won't overwrite unless `overwrite=true` |
| `gh_issue` | list/view/create/comment GitHub issues | needs `gh` auth + a repo |

> These give the agent real access to your machine. Review what you ask it to do,
> and keep the optional passphrase on if others share the host.

---

## Self-repair

The agent can patch its **own source** — safely:

```bash
agent --self-repair "fix the spinner flicker on resize"   # apply
agent --self-repair "..." --dry-run                        # preview only
agent --self-repair "..." --no-verify                      # skip the behaviour check
agent --rollback                                           # restore the last backup
```

How it stays safe — the agent never writes your disk directly. It returns a
search/replace plan; the **client** applies it under a guarded pipeline:

1. **scope** — edits are confined to the package dir (code only; no secrets there).
2. **backup** — every touched file is snapshotted first.
3. **compile** — changed `.py` must pass `py_compile` (syntax).
4. **verify** — changed modules are import-checked, then your `self_repair_verify`
   command runs (e.g. `python -m pytest -q`). This catches behaviour breakage, not
   just syntax.
5. **rollback** — any failure auto-restores the backup. `--rollback` reverts the
   last successful apply too.

Edit matching is exact first, then whitespace-tolerant on unique line-blocks.

## Files

```
managed-agent-cli/
├── cli.py                    # the CLI (config-driven, no hardcoded identity)
├── setup.py                  # create environment + agent, save IDs
├── setup_memory.py           # create a cross-session memory_store
├── upload_skill.py           # upload a custom Skill directory
├── install.sh                # generate + install the launcher
├── bin/agent.template        # launcher template (placeholders filled by install.sh)
├── config.example.json       # config reference
├── system_prompt.example.md  # default system prompt
└── requirements.txt
```

## Cost note

USD figures count **tokens only** at standard API pricing. Managed Agents also
bill sandbox runtime, which is **not** included in `--usage`.
