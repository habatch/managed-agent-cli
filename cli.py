#!/usr/bin/env python3
"""Managed Agent CLI — a personal, config-driven CLI for an Anthropic Managed Agent.

Generalized from the original single-user "habatchLM" tool so that *any* user can
provision and drive their own private agent (persistent sessions, cross-session
memory, local tools) without any hardcoded identity.

All per-user values live in a single config file (see ``config_dir()`` /
``config.example.json``). Nothing about a specific person, repo, or framework is
baked into this script.

Usage:
    agent "question"        # one turn (resume active session, else new)
    agent --new "question"  # always start a fresh session
    agent -i                # interactive REPL (fresh session)
    agent --no-memory       # do not attach the memory_store
    agent --with-repo       # attach a github_repository (slower; only when needed)
    agent --status          # show config
    agent --doctor          # environment diagnostics (API key, gh, session, ...)
    agent --usage           # cumulative token / USD cost
    agent --self-repair "fix X"   # let the agent patch its OWN source (backup + compile + verify + auto-rollback)
    agent --self-repair "fix X" --dry-run    # preview the patch without writing
    agent --self-repair "fix X" --no-verify  # skip the post-apply import/test check
    agent --rollback        # restore the most recent self-repair backup

Self-repair runs, after applying: py_compile → import-check the changed modules →
the optional config "self_repair_verify" command (e.g. "python -m pytest -q").
Any failure auto-rolls-back to the pre-edit backup.

    agent --mock "hello"    # offline trial: canned replies, no key/config/cost
                            #   (or set AGENT_CLI_MOCK=1; works for orchestrate.py too)

Environment:
    ANTHROPIC_API_KEY   required (except in mock mode).
    AGENT_CLI_HOME      optional; overrides the config directory
                        (default: ~/.config/managed-agent-cli).
    AGENT_CLI_MOCK      set to 1 for offline mock mode (no API key / no cost).
"""
from __future__ import annotations

import json
import os
import py_compile
import shutil
import subprocess
import sys
import threading
import time
import traceback
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

try:
    import readline  # noqa: F401  (nicer REPL line editing when available)
except ImportError:
    pass

import httpx
from anthropic import Anthropic, APIError

MAX_OUTPUT_CHARS = 10000
SESSION_TTL_SECONDS = 3600
RETRY_MAX_ATTEMPTS = 3
RETRY_INITIAL_DELAY = 1.0
HTTP_READ_TIMEOUT = 120.0
# Tier 1 input limit is ~30k tok/min. A turn can spend ~15k tok, so back-to-back
# turns hit 429 easily — wait out the per-minute window and auto-resend.
RATE_LIMIT_WAIT_SECONDS = 65
RATE_LIMIT_MAX_RETRIES = 2

DEFAULT_MODEL = "claude-sonnet-4-6"


def config_dir() -> Path:
    """Resolve the config directory from AGENT_CLI_HOME, else the XDG-ish default."""
    env = os.environ.get("AGENT_CLI_HOME")
    if env:
        return Path(env).expanduser()
    return Path.home() / ".config" / "managed-agent-cli"


CONFIG_DIR = config_dir()
CONFIG_FILE = CONFIG_DIR / "config.json"
ACTIVE_SESSION_FILE = CONFIG_DIR / "active_session.json"
LOG_DIR = CONFIG_DIR / "logs"
USAGE_FILE = CONFIG_DIR / "usage.jsonl"

# Self-repair: the package edits its OWN source under APP_DIR, with backup +
# compile-check + auto-rollback. APP_DIR holds code only — secrets live in
# CONFIG_DIR — so self-repair is scoped safely to this tree.
APP_DIR = Path(__file__).resolve().parent
SELF_REPAIR_BACKUP_DIR = CONFIG_DIR / "self_repair_backups"
SELF_REPAIR_EXTS = {".py", ".md", ".json", ".sh", ".template", ".txt", ".toml", ".cfg"}

# USD per 1M tokens (api.anthropic.com standard pricing, <=200K context).
# Note: Managed Agents sandbox-runtime billing is NOT included here (tokens only).
PRICING_PER_MTOK: dict[str, dict[str, float]] = {
    "claude-sonnet-4-6": {"input": 3.00, "output": 15.00, "cache_read": 0.30, "cache_write": 3.75},
    "claude-opus-4-8": {"input": 15.00, "output": 75.00, "cache_read": 1.50, "cache_write": 18.75},
    "claude-haiku-4-5": {"input": 1.00, "output": 5.00, "cache_read": 0.10, "cache_write": 1.25},
}
DEFAULT_PRICING = PRICING_PER_MTOK[DEFAULT_MODEL]

DEFAULT_MEMORY_INSTRUCTIONS = (
    "Cross-session memory for this agent. "
    "Keep: decisions, user preferences, snapshots of in-progress tasks, explicit user rules. "
    "Do not keep: ephemeral reasoning, transient computation results, secrets/API keys."
)


# === Logging ===

def log_error(msg: str, exc: BaseException | None = None) -> None:
    try:
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        LOG_DIR.chmod(0o700)
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        log_file = LOG_DIR / f"error-{today}.log"
        ts = datetime.now(timezone.utc).isoformat()
        with log_file.open("a", encoding="utf-8") as f:
            f.write(f"\n=== {ts} ===\n{msg}\n")
            if exc is not None:
                f.write(traceback.format_exc())
            f.write("\n")
    except Exception:
        pass


# === Spinner ===

class Spinner:
    """Thinking indicator: an animated glyph + elapsed seconds on one line.

    Does nothing when stdout is not a TTY (pipe / redirect).
    Always stop() before print() so lines don't interleave.
    """

    FRAMES = ["·", "✢", "✳", "✶", "✻", "✽", "✻", "✶", "✳", "✢"]
    INTERVAL = 0.12

    def __init__(self) -> None:
        self.enabled = sys.stdout.isatty()
        self._thread: threading.Thread | None = None
        self._stop_event = threading.Event()
        self._label = "thinking"
        self._started_at = 0.0

    def start(self, label: str = "thinking") -> None:
        if not self.enabled or self._thread is not None:
            return
        self._label = label
        self._started_at = time.monotonic()
        self._stop_event.clear()
        self._thread = threading.Thread(target=self._spin, daemon=True)
        self._thread.start()

    def _spin(self) -> None:
        i = 0
        while not self._stop_event.is_set():
            frame = self.FRAMES[i % len(self.FRAMES)]
            elapsed = int(time.monotonic() - self._started_at)
            sys.stdout.write(
                f"\r\033[38;5;208m{frame}\033[0m \033[2m{self._label}… ({elapsed}s)\033[0m\033[K"
            )
            sys.stdout.flush()
            i += 1
            self._stop_event.wait(self.INTERVAL)

    def stop(self) -> None:
        if not self.enabled or self._thread is None:
            return
        self._stop_event.set()
        self._thread.join()
        self._thread = None
        sys.stdout.write("\r\033[K")  # erase the spinner line
        sys.stdout.flush()


# === Usage / Cost tracking ===

class UsageTracker:
    """Aggregate model_usage from span.model_request_end per turn and price it."""

    def __init__(self, model: str) -> None:
        self.model = model
        self.pricing = PRICING_PER_MTOK.get(model, DEFAULT_PRICING)
        self.input = 0
        self.output = 0
        self.cache_read = 0
        self.cache_write = 0
        self.requests = 0

    def add(self, usage: Any) -> None:
        self.input += getattr(usage, "input_tokens", 0) or 0
        self.output += getattr(usage, "output_tokens", 0) or 0
        self.cache_read += getattr(usage, "cache_read_input_tokens", 0) or 0
        self.cache_write += getattr(usage, "cache_creation_input_tokens", 0) or 0
        self.requests += 1

    def cost_usd(self) -> float:
        p = self.pricing
        return (
            self.input * p["input"]
            + self.output * p["output"]
            + self.cache_read * p["cache_read"]
            + self.cache_write * p["cache_write"]
        ) / 1_000_000

    def record(self, session_id: str) -> None:
        """Append a per-turn record to usage.jsonl."""
        try:
            entry = {
                "ts": datetime.now(timezone.utc).isoformat(),
                "session_id": session_id,
                "model": self.model,
                "input": self.input,
                "output": self.output,
                "cache_read": self.cache_read,
                "cache_write": self.cache_write,
                "requests": self.requests,
                "usd": round(self.cost_usd(), 6),
            }
            with USAGE_FILE.open("a", encoding="utf-8") as f:
                f.write(json.dumps(entry, ensure_ascii=False) + "\n")
            USAGE_FILE.chmod(0o600)
        except OSError:
            pass

    def print_summary(self) -> None:
        if self.requests == 0:
            return
        lifetime = lifetime_usage_usd()
        print(
            f"\033[2m[usage] in={self.input} out={self.output} "
            f"cache_r={self.cache_read} cache_w={self.cache_write} "
            f"| this turn: ${self.cost_usd():.4f} | lifetime: ${lifetime:.2f} (tokens only)\033[0m",
            flush=True,
        )


def lifetime_usage_usd() -> float:
    """Sum of USD across every turn recorded in usage.jsonl."""
    total = 0.0
    if not USAGE_FILE.exists():
        return total
    try:
        for line in USAGE_FILE.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            try:
                total += float(json.loads(line).get("usd", 0))
            except (json.JSONDecodeError, TypeError, ValueError):
                continue
    except OSError:
        pass
    return total


def show_usage() -> None:
    """--usage: per-day aggregate of usage.jsonl."""
    if not USAGE_FILE.exists():
        print("No usage recorded yet.")
        return
    by_day: dict[str, dict[str, float]] = {}
    for line in USAGE_FILE.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            e = json.loads(line)
        except json.JSONDecodeError:
            continue
        day = (e.get("ts") or "")[:10] or "?"
        d = by_day.setdefault(day, {"turns": 0, "in": 0, "out": 0, "cache_r": 0, "cache_w": 0, "usd": 0.0})
        d["turns"] += 1
        d["in"] += e.get("input", 0)
        d["out"] += e.get("output", 0)
        d["cache_r"] += e.get("cache_read", 0)
        d["cache_w"] += e.get("cache_write", 0)
        d["usd"] += e.get("usd", 0)
    print("Usage (usage.jsonl aggregate, tokens only — sandbox runtime not included)")
    print("=" * 78)
    print(f"{'date':12s} {'turns':>5s} {'in':>8s} {'out':>8s} {'cache_r':>9s} {'cache_w':>9s} {'USD':>9s}")
    total = 0.0
    for day in sorted(by_day):
        d = by_day[day]
        print(f"{day:12s} {d['turns']:>5.0f} {d['in']:>8.0f} {d['out']:>8.0f} "
              f"{d['cache_r']:>9.0f} {d['cache_w']:>9.0f} {d['usd']:>9.4f}")
        total += d["usd"]
    print("-" * 78)
    print(f"{'total':12s} {'':>5s} {'':>8s} {'':>8s} {'':>9s} {'':>9s} {total:>9.4f}")


# === Config / Session State ===

def load_config() -> dict[str, Any]:
    if not CONFIG_FILE.exists():
        sys.exit(
            f"Config not found: {CONFIG_FILE}\n"
            f"Run setup first:\n"
            f"  python {CONFIG_DIR / 'setup.py'}\n"
            f"(or copy config.example.json to {CONFIG_FILE} and fill in your IDs)"
        )
    return json.loads(CONFIG_FILE.read_text())


def agent_name(cfg: dict[str, Any]) -> str:
    return cfg.get("name") or "agent"


def memory_instructions(cfg: dict[str, Any]) -> str:
    return cfg.get("memory_instructions") or DEFAULT_MEMORY_INSTRUCTIONS


def mock_enabled(flag: bool = False) -> bool:
    return flag or os.environ.get("AGENT_CLI_MOCK") == "1"


def make_client(mock: bool = False):
    """Real Anthropic client, or an offline mock (no key/network/cost) for UX trials."""
    if mock_enabled(mock):
        from mock_client import MockAnthropic
        return MockAnthropic()
    return Anthropic(timeout=httpx.Timeout(
        connect=10.0, read=HTTP_READ_TIMEOUT, write=10.0, pool=10.0))


def load_active_session() -> dict[str, Any] | None:
    if not ACTIVE_SESSION_FILE.exists():
        return None
    try:
        data = json.loads(ACTIVE_SESSION_FILE.read_text())
        last_used = data.get("last_used_at")
        if last_used:
            t = datetime.fromisoformat(last_used.replace("Z", "+00:00"))
            age = (datetime.now(timezone.utc) - t).total_seconds()
            if age > SESSION_TTL_SECONDS:
                return None
        return data
    except (json.JSONDecodeError, ValueError, KeyError):
        return None


def save_active_session(session_id: str, *, with_memory: bool, with_repo: bool) -> None:
    ACTIVE_SESSION_FILE.parent.mkdir(parents=True, exist_ok=True)
    now = datetime.now(timezone.utc).isoformat()
    ACTIVE_SESSION_FILE.write_text(json.dumps({
        "session_id": session_id,
        "created_at": now,
        "last_used_at": now,
        "with_memory": with_memory,
        "with_repo": with_repo,
    }, ensure_ascii=False, indent=2))
    ACTIVE_SESSION_FILE.chmod(0o600)


def clear_active_session() -> None:
    if ACTIVE_SESSION_FILE.exists():
        try:
            ACTIVE_SESSION_FILE.unlink()
        except OSError:
            pass


def touch_active_session() -> None:
    if not ACTIVE_SESSION_FILE.exists():
        return
    try:
        data = json.loads(ACTIVE_SESSION_FILE.read_text())
        data["last_used_at"] = datetime.now(timezone.utc).isoformat()
        ACTIVE_SESSION_FILE.write_text(json.dumps(data, ensure_ascii=False, indent=2))
    except (json.JSONDecodeError, OSError):
        pass


# === Retry ===

def with_retry(fn, *, max_attempts: int = RETRY_MAX_ATTEMPTS, label: str = "operation"):
    last_err: BaseException | None = None
    for attempt in range(max_attempts):
        try:
            return fn()
        except APIError as e:
            last_err = e
            status = getattr(e, "status_code", getattr(e, "status", 0)) or 0
            if status >= 500 or status == 429:
                if attempt < max_attempts - 1:
                    delay = RETRY_INITIAL_DELAY * (2 ** attempt)
                    print(f"\033[33m[retry]\033[0m {label}: status={status} → wait {delay:.0f}s ({attempt+1}/{max_attempts})",
                          flush=True)
                    time.sleep(delay)
                    continue
            raise
    if last_err:
        raise last_err
    raise RuntimeError(f"{label} failed after {max_attempts} attempts")


# === Custom tool handlers ===

def _truncate(s: str, limit: int = MAX_OUTPUT_CHARS) -> str:
    if len(s) <= limit:
        return s
    return s[:limit] + f"\n... [truncated, total {len(s)} chars]"


def exec_local_bash(input_data: dict[str, Any], *, cfg: dict[str, Any], verbose: bool = False) -> dict[str, Any]:
    cmd = (input_data.get("command") or "").strip()
    if not cmd:
        return {"error": "empty command"}

    cwd = input_data.get("cwd")
    try:
        timeout = float(input_data.get("timeout") or 30)
    except (TypeError, ValueError):
        timeout = 30.0

    print(f"\n\033[36m[local_bash]\033[0m {cmd}", flush=True)
    if verbose and cwd:
        print(f"\033[2m  cwd: {cwd}\033[0m", flush=True)

    try:
        result = subprocess.run(
            cmd, shell=True, executable="/bin/bash",
            cwd=cwd, timeout=timeout, capture_output=True, text=True,
        )
    except subprocess.TimeoutExpired as e:
        return {
            "error": f"timeout after {timeout}s",
            "stdout": _truncate((e.stdout or "") if isinstance(e.stdout, str) else (e.stdout.decode() if e.stdout else "")),
            "stderr": _truncate((e.stderr or "") if isinstance(e.stderr, str) else (e.stderr.decode() if e.stderr else "")),
        }
    except Exception as e:
        return {"error": f"{type(e).__name__}: {e}"}

    out: dict[str, Any] = {"return_code": result.returncode}
    if result.stdout:
        out["stdout"] = _truncate(result.stdout)
    if result.stderr:
        out["stderr"] = _truncate(result.stderr)
    if verbose and out.get("stdout"):
        snippet = out["stdout"][:300].replace("\n", " ⏎ ")
        print(f"\033[2m  → {snippet}\033[0m", flush=True)
    return out


def exec_local_write(input_data: dict[str, Any], *, cfg: dict[str, Any], verbose: bool = False) -> dict[str, Any]:
    file_path = (input_data.get("file_path") or "").strip()
    content = input_data.get("content")
    overwrite = bool(input_data.get("overwrite", False))

    if not file_path:
        return {"error": "empty file_path"}
    if not file_path.startswith("/"):
        return {"error": "file_path must be absolute"}
    if content is None:
        return {"error": "content is required"}

    path = Path(file_path)
    print(f"\n\033[36m[local_write]\033[0m {file_path} ({len(content)} chars)", flush=True)

    if not path.parent.exists():
        return {"error": f"parent directory does not exist: {path.parent}"}
    if path.exists() and not overwrite:
        return {"error": f"file exists (set overwrite=true to replace): {file_path}"}

    try:
        path.write_text(content, encoding="utf-8")
    except Exception as e:
        return {"error": f"{type(e).__name__}: {e}"}

    out = {"return_code": 0, "bytes_written": len(content.encode("utf-8"))}
    if verbose:
        print(f"\033[2m  → wrote {out['bytes_written']} bytes\033[0m", flush=True)
    return out


def exec_gh_issue(input_data: dict[str, Any], *, cfg: dict[str, Any], verbose: bool = False) -> dict[str, Any]:
    action = input_data.get("action", "list")
    if action not in {"list", "view", "create", "comment"}:
        return {"error": "action must be one of: list, view, create, comment"}

    repo = input_data.get("repo") or cfg.get("gh_repo")
    if not repo:
        return {"error": "no repo given and config has no 'gh_repo'"}

    if action == "list":
        cmd = ["gh", "issue", "list", "--repo", repo]
        f = input_data.get("filter") or {}
        state = f.get("state")
        if state:
            cmd.extend(["--state", state])
        labels = f.get("labels") or []
        if labels:
            cmd.extend(["--label", ",".join(labels)])
        cmd.extend(["--limit", str(input_data.get("limit", 30))])
        cmd.extend(["--json", "number,title,state,labels,createdAt,updatedAt"])

    elif action == "view":
        n = input_data.get("issue_number")
        if not n:
            return {"error": "issue_number required for view"}
        cmd = ["gh", "issue", "view", str(n), "--repo", repo,
               "--json", "number,title,body,state,labels,comments,createdAt"]

    elif action == "create":
        title = (input_data.get("title") or "").strip()
        body = input_data.get("body") or ""
        labels = input_data.get("labels") or []
        if not title:
            return {"error": "title required for create"}
        cmd = ["gh", "issue", "create", "--repo", repo, "--title", title, "--body", body]
        for l in labels:
            cmd.extend(["--label", l])

    else:  # comment
        n = input_data.get("issue_number")
        body = input_data.get("body") or ""
        if not n or not body:
            return {"error": "issue_number and body required for comment"}
        cmd = ["gh", "issue", "comment", str(n), "--repo", repo, "--body", body]

    print(f"\n\033[36m[gh_issue: {action}]\033[0m repo={repo}", flush=True)
    if verbose:
        print(f"\033[2m  {' '.join(cmd[:8])}{'...' if len(cmd) > 8 else ''}\033[0m", flush=True)

    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
    except subprocess.TimeoutExpired:
        return {"error": "gh command timeout (30s)"}
    except FileNotFoundError:
        return {"error": "gh CLI not installed on host"}

    out: dict[str, Any] = {"return_code": r.returncode}
    if r.stdout:
        out["stdout"] = _truncate(r.stdout)
    if r.stderr:
        out["stderr"] = _truncate(r.stderr)
    return out


# Custom tools the agent may call. local_bash/local_write are always useful;
# gh_issue is only active when the agent definition exposes it AND a repo is set.
CUSTOM_TOOL_DISPATCH = {
    "local_bash": exec_local_bash,
    "local_write": exec_local_write,
    "gh_issue": exec_gh_issue,
}


# === Stream handling ===

def handle_event(event: Any, *, verbose: bool = False, tracker: UsageTracker | None = None) -> None:
    et = event.type

    if et == "agent.message":
        for block in event.content:
            if block.type == "text":
                print(block.text, end="", flush=True)
        print()

    elif et == "agent.thinking":
        if verbose:
            print("\n\033[2m[thinking...]\033[0m", flush=True)

    elif et == "agent.tool_use":
        name = getattr(event, "tool_name", None) or getattr(event, "name", None) or "?"
        if verbose:
            inp = getattr(event, "input", {})
            print(f"\n\033[36m[tool: {name}]\033[0m {json.dumps(inp, ensure_ascii=False)[:200]}", flush=True)
        else:
            print(f"\n\033[36m[tool: {name}]\033[0m", flush=True)

    elif et == "agent.tool_result":
        if verbose:
            content = getattr(event, "content", [])
            for block in content:
                if hasattr(block, "text"):
                    print(f"\033[2m  → {block.text[:300]}\033[0m", flush=True)

    elif et == "session.error":
        err = getattr(event, "error", None)
        msg = err.message if err and hasattr(err, "message") else "unknown"
        print(f"\n\033[31m[ERROR: {msg}]\033[0m", flush=True)
        log_error(f"session.error: {msg}")

    elif et == "span.model_request_end":
        usage = getattr(event, "model_usage", None)
        if usage:
            if tracker is not None:
                tracker.add(usage)
            if verbose:
                print(
                    f"\033[2m  [usage in={usage.input_tokens} out={usage.output_tokens} "
                    f"cache_r={usage.cache_read_input_tokens} cache_w={usage.cache_creation_input_tokens}]\033[0m",
                    flush=True,
                )


def handle_custom_tool(client: Anthropic, session_id: str, event: Any, *,
                       cfg: dict[str, Any], verbose: bool = False) -> None:
    name = getattr(event, "tool_name", None) or getattr(event, "name", None)
    tool_use_id = event.id
    input_data = getattr(event, "input", {}) or {}

    handler = CUSTOM_TOOL_DISPATCH.get(name)
    if handler:
        result = handler(input_data, cfg=cfg, verbose=verbose)
    else:
        result = {"error": f"unknown custom tool: {name}"}

    is_error = "error" in result
    payload = json.dumps(result, ensure_ascii=False, separators=(",", ":"))
    client.beta.sessions.events.send(
        session_id,
        events=[{
            "type": "user.custom_tool_result",
            "custom_tool_use_id": tool_use_id,
            "content": [{"type": "text", "text": payload}],
            "is_error": is_error,
        }],
    )


# === Session building ===

def _gh_token() -> str | None:
    try:
        r = subprocess.run(["gh", "auth", "token"], capture_output=True, text=True, timeout=5)
        if r.returncode == 0:
            t = r.stdout.strip()
            return t if t else None
    except (FileNotFoundError, subprocess.TimeoutExpired):
        pass
    return None


def build_session_kwargs(cfg: dict[str, Any], *, with_memory: bool, with_repo: bool, title: str) -> dict[str, Any]:
    kwargs: dict[str, Any] = {
        "agent": cfg["agent_id"],
        "environment_id": cfg["environment_id"],
        "title": title,
    }
    resources: list[dict[str, Any]] = []
    memory_store_id = cfg.get("memory_store_id")
    if with_memory and memory_store_id:
        resources.append({
            "type": "memory_store",
            "memory_store_id": memory_store_id,
            "access": "read_write",
            "instructions": memory_instructions(cfg),
        })
    repo_url = cfg.get("github_repo_url")
    if with_repo and repo_url:
        token = _gh_token()
        if token:
            resources.append({
                "type": "github_repository",
                "url": repo_url,
                "authorization_token": token,
            })
    if resources:
        kwargs["resources"] = resources
    return kwargs


def is_terminal_idle(event: Any) -> bool:
    if event.type != "session.status_idle":
        return False
    sr = getattr(event, "stop_reason", None)
    if sr is None:
        return True
    return getattr(sr, "type", None) != "requires_action"


# === Run ===

def create_session(client: Anthropic, cfg: dict[str, Any], *, with_memory: bool, with_repo: bool, title: str):
    def _create():
        return client.beta.sessions.create(
            **build_session_kwargs(cfg, with_memory=with_memory, with_repo=with_repo, title=title)
        )
    return with_retry(_create, label="sessions.create")


def _next_spinner_label(event: Any) -> str:
    """Pick the label to show while waiting, based on the previous event."""
    et = event.type
    if et == "agent.tool_use":
        name = getattr(event, "tool_name", None) or getattr(event, "name", None) or "?"
        return f"tool: {name}"
    return "thinking"


def _is_rate_limit_error(event: Any) -> bool:
    if event.type != "session.error":
        return False
    err = getattr(event, "error", None)
    msg = (getattr(err, "message", "") or "") if err else ""
    return "rate limit" in msg.lower()


def stream_with_session(client: Anthropic, session_id: str, message: str, *,
                        cfg: dict[str, Any], verbose: bool, model: str = DEFAULT_MODEL) -> bool:
    """Send one message and stream the response. Returns True if the session is reusable.

    On a rate-limit session.error (Tier 1: input ~30k tok/min), wait out the
    per-minute window and auto-resend the same message (up to RATE_LIMIT_MAX_RETRIES).
    """
    reusable = True
    tracker = UsageTracker(model)
    spinner = Spinner()
    attempt = 0
    try:
        while True:
            rate_limited = False
            stream = client.beta.sessions.events.stream(session_id)
            try:
                client.beta.sessions.events.send(
                    session_id,
                    events=[{
                        "type": "user.message",
                        "content": [{"type": "text", "text": message}],
                    }],
                )
                spinner.start("thinking")
                for event in stream:
                    spinner.stop()  # always stop before print so output doesn't interleave
                    if _is_rate_limit_error(event):
                        rate_limited = True
                        log_error("session.error: rate limited")
                        spinner.start("rate limit — waiting")
                        continue  # will resend; hold off on the red error
                    handle_event(event, verbose=verbose, tracker=tracker)
                    if event.type == "agent.custom_tool_use":
                        handle_custom_tool(client, session_id, event, cfg=cfg, verbose=verbose)
                    if event.type == "session.status_terminated":
                        reusable = False
                        break
                    if is_terminal_idle(event):
                        break
                    spinner.start(_next_spinner_label(event))
            finally:
                spinner.stop()
                try:
                    stream.close()
                except Exception:
                    pass

            if not rate_limited or not reusable:
                break
            if attempt >= RATE_LIMIT_MAX_RETRIES:
                print(
                    "\033[31m[ERROR: rate limit not clearing]\033[0m\n"
                    "  Cause: Tier 1 per-minute token cap (input ~30k tok/min); a turn spends ~15k tok.\n"
                    "  Fix: wait 1-2 min and retry / space out consecutive turns /\n"
                    "       permanently raise your usage tier at console.anthropic.com",
                    flush=True,
                )
                break
            attempt += 1
            print(f"\033[33m[rate limit] per-minute cap → wait {RATE_LIMIT_WAIT_SECONDS}s and auto-resend "
                  f"({attempt}/{RATE_LIMIT_MAX_RETRIES})\033[0m", flush=True)
            spinner.start("rate limit wait")
            time.sleep(RATE_LIMIT_WAIT_SECONDS)
            spinner.stop()
    finally:
        if tracker.requests > 0:
            tracker.record(session_id)
            tracker.print_summary()
    return reusable


def run_once(client: Anthropic, cfg: dict[str, Any], message: str, *,
             verbose: bool, with_memory: bool, with_repo: bool, force_new: bool) -> None:
    session_id: str | None = None

    if not force_new:
        active = load_active_session()
        if active and active.get("with_memory") == with_memory and active.get("with_repo") == with_repo:
            session_id = active.get("session_id")
            if verbose and session_id:
                print(f"\033[2m[resume: {session_id}]\033[0m", flush=True)

    is_new = session_id is None
    if is_new:
        session = create_session(client, cfg,
                                 with_memory=with_memory, with_repo=with_repo,
                                 title=f"{agent_name(cfg)} CLI: {message[:60]}")
        session_id = session.id
        save_active_session(session_id, with_memory=with_memory, with_repo=with_repo)
        if verbose:
            mem = cfg.get("memory_store_id") if with_memory else None
            repo = cfg.get("github_repo_url") if with_repo else None
            suffix = ""
            if mem:
                suffix += f" memory: {mem}"
            if repo:
                suffix += f" repo: {repo}"
            print(f"\033[2m[session: {session_id}{suffix}]\033[0m", flush=True)

    try:
        reusable = stream_with_session(client, session_id, message, cfg=cfg, verbose=verbose,
                                       model=cfg.get("model", DEFAULT_MODEL))
    except APIError as e:
        status = getattr(e, "status_code", getattr(e, "status", 0)) or 0
        if status == 404 and not force_new:
            print("\033[33m[session expired or archived, creating new]\033[0m", flush=True)
            clear_active_session()
            run_once(client, cfg, message, verbose=verbose,
                     with_memory=with_memory, with_repo=with_repo, force_new=True)
            return
        log_error(f"APIError in run_once: status={status}", e)
        raise

    if reusable:
        touch_active_session()
    else:
        clear_active_session()


def run_repl(client: Anthropic, cfg: dict[str, Any], *,
             verbose: bool, with_memory: bool, with_repo: bool) -> None:
    name = agent_name(cfg)
    session = create_session(client, cfg,
                             with_memory=with_memory, with_repo=with_repo,
                             title=f"{name} interactive")
    print(f"{name} REPL (session: {session.id})")
    mem = cfg.get("memory_store_id") if with_memory else None
    repo = cfg.get("github_repo_url") if with_repo else None
    if mem:
        print(f"  memory: {mem}")
    elif cfg.get("memory_store_id"):
        print("  memory: disabled (--no-memory)")
    if repo:
        print(f"  repo:   {repo}")
    elif cfg.get("github_repo_url"):
        print("  repo:   disabled (default; use --with-repo to attach)")
    print("Ctrl+D / 'exit' to quit\n")

    try:
        while True:
            try:
                user_input = input("\033[1;33myou>\033[0m ").strip()
            except (EOFError, KeyboardInterrupt):
                print()
                break
            if not user_input:
                continue
            if user_input.lower() in ("exit", "quit", ":q"):
                break
            print(f"\033[1;32m{name}>\033[0m")
            try:
                stream_with_session(client, session.id, user_input, cfg=cfg, verbose=verbose,
                                    model=cfg.get("model", DEFAULT_MODEL))
            except APIError as e:
                status = getattr(e, "status_code", getattr(e, "status", 0)) or 0
                print(f"\033[31m[API error status={status}]\033[0m")
                log_error(f"REPL APIError status={status}", e)
            print()
    finally:
        try:
            client.beta.sessions.archive(session.id)
        except Exception:
            pass


# === --doctor ===

def run_doctor() -> None:
    print("Managed Agent CLI — Doctor")
    print("=" * 60)
    failed: list[str] = []

    def check(name: str, ok: bool, msg: str) -> None:
        mark = "✅" if ok else "❌"
        print(f"  {mark} {name:25s} {msg}")
        if not ok:
            failed.append(name)

    api_key = os.environ.get("ANTHROPIC_API_KEY", "")
    check("ANTHROPIC_API_KEY",
          bool(api_key) and api_key.startswith("sk-"),
          "set" if api_key else "missing (source your env file with the key)")

    try:
        r = subprocess.run(["gh", "auth", "status"], capture_output=True, text=True, timeout=5)
        check("gh CLI auth", r.returncode == 0,
              "OK" if r.returncode == 0 else r.stderr.strip()[:80])
    except (FileNotFoundError, subprocess.TimeoutExpired) as e:
        check("gh CLI auth", False, f"unavailable: {type(e).__name__} (optional)")

    check("config file", CONFIG_FILE.exists(),
          str(CONFIG_FILE) if CONFIG_FILE.exists() else "missing (run setup.py)")

    if CONFIG_FILE.exists():
        try:
            cfg = json.loads(CONFIG_FILE.read_text())
            for key in ("agent_id", "environment_id"):
                check(f"config.{key}", bool(cfg.get(key)),
                      cfg.get(key, "missing")[:40] if cfg.get(key) else "missing (run setup.py)")
            check("config.memory_store_id", True,
                  cfg.get("memory_store_id", "none (run setup_memory.py to enable)"))
        except json.JSONDecodeError:
            check("config parse", False, "invalid JSON")

    pf = CONFIG_DIR / "system_prompt.md"
    if pf.exists():
        size = pf.stat().st_size
        check("system_prompt", True, f"{size} bytes (~{size//4} tokens)")
    else:
        check("system_prompt", True, "none (using the agent's stored system prompt)")

    active = load_active_session()
    if active:
        sid = active.get("session_id", "?")
        check("active session", True, f"{sid[:32]}... (within TTL)")
    else:
        check("active session", True, "none (a new one is created next run)")

    if api_key:
        try:
            client = Anthropic(timeout=httpx.Timeout(10.0))
            client.models.list()
            check("Anthropic API reach", True, "OK")
        except APIError as e:
            check("Anthropic API reach", False,
                  f"status={getattr(e, 'status_code', '?')} {getattr(e, 'message', '')[:60]}")
        except Exception as e:
            check("Anthropic API reach", False, f"{type(e).__name__}: {str(e)[:60]}")
    else:
        check("Anthropic API reach", False, "skipped (no API key)")

    print()
    if failed:
        print(f"⚠️  {len(failed)} issue(s): {', '.join(failed)}")
        sys.exit(1)
    print("✅ all checks passed")


def show_status() -> None:
    cfg = load_config()
    print(f"{agent_name(cfg)} config:")
    for k, v in cfg.items():
        print(f"  {k}: {v}")
    print(f"\nconfig file: {CONFIG_FILE}")
    active = load_active_session()
    if active:
        print(f"\nactive session: {active.get('session_id')} (last_used: {active.get('last_used_at')})")
    else:
        print("\nactive session: none")


# === Self-repair ===

def _extract_json(text: str) -> dict[str, Any] | None:
    s = text.strip()
    if s.startswith("```"):
        s = s.split("```", 2)[1] if s.count("```") >= 2 else s
        s = s[4:] if s.lstrip()[:4].lower() == "json" else s
    i, j = s.find("{"), s.rfind("}")
    if i == -1 or j == -1 or j < i:
        return None
    try:
        return json.loads(s[i:j + 1])
    except json.JSONDecodeError:
        return None


def _collect_session_text(client: Anthropic, cfg: dict[str, Any], message: str) -> str:
    """One turn against a fresh, no-resource session; return the assistant text."""
    parts: list[str] = []
    session = create_session(client, cfg, with_memory=False, with_repo=False, title="self-repair")
    stream = client.beta.sessions.events.stream(session.id)
    try:
        client.beta.sessions.events.send(
            session.id,
            events=[{"type": "user.message", "content": [{"type": "text", "text": message}]}],
        )
        for event in stream:
            if event.type == "agent.message":
                for block in event.content:
                    if block.type == "text":
                        parts.append(block.text)
            if event.type == "session.status_terminated":
                break
            if is_terminal_idle(event):
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
    return "".join(parts).strip()


def collect_self_source(max_bytes: int = 200_000) -> dict[str, str]:
    """Read the package's own source files (code only, no secrets)."""
    files: dict[str, str] = {}
    total = 0
    for p in sorted(APP_DIR.rglob("*")):
        if not p.is_file() or p.suffix not in SELF_REPAIR_EXTS:
            continue
        if "__pycache__" in p.parts or SELF_REPAIR_BACKUP_DIR in p.parents:
            continue
        rel = p.relative_to(APP_DIR).as_posix()
        try:
            text = p.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        total += len(text.encode("utf-8"))
        files[rel] = text if total <= max_bytes else "<omitted: size budget exceeded>"
    return files


SELF_REPAIR_INSTRUCTIONS = (
    "You are repairing YOUR OWN source code. The full package source is shown below "
    "(paths relative to the package root). Make the MINIMAL change to achieve the GOAL.\n\n"
    "Return ONLY a JSON object — no prose, no code fences:\n"
    '{"summary":"<one line>","edits":[{"path":"<rel path>","old":"<exact snippet>",'
    '"new":"<replacement>","desc":"<why>"}]}\n'
    "Rules:\n"
    "- 'old' MUST be an exact, UNIQUE substring of the current file (copy verbatim, include enough lines).\n"
    "- Keep edits surgical; do not reformat unrelated code.\n"
    "- Only edit files shown. To create a new file, set old=\"\" and new=<full content>.\n"
    "- If the goal is unclear or unsafe, return an empty edits list and explain in summary.\n\n"
    "GOAL:\n"
)


def _backup_files(paths: list[Path]) -> Path:
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    bdir = SELF_REPAIR_BACKUP_DIR / ts
    bdir.mkdir(parents=True, exist_ok=True)
    manifest: list[str] = []
    for p in paths:
        rel = p.relative_to(APP_DIR).as_posix()
        dest = bdir / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(p, dest)
        manifest.append(rel)
    (bdir / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False))
    return bdir


def _restore_backup(bdir: Path) -> list[str]:
    try:
        manifest = json.loads((bdir / "manifest.json").read_text())
    except (OSError, json.JSONDecodeError):
        return []
    restored: list[str] = []
    for rel in manifest:
        src = bdir / rel
        if src.exists():
            shutil.copy2(src, APP_DIR / rel)
            restored.append(rel)
    return restored


def rollback_self_repair() -> None:
    """--rollback: restore the most recent self-repair backup."""
    backups = []
    if SELF_REPAIR_BACKUP_DIR.exists():
        backups = sorted(d for d in SELF_REPAIR_BACKUP_DIR.iterdir() if (d / "manifest.json").exists())
    if not backups:
        print("No self-repair backups found.")
        return
    latest = backups[-1]
    restored = _restore_backup(latest)
    print(f"Rolled back {len(restored)} file(s) from {latest.name}:")
    for r in restored:
        print(f"  - {r}")


def _locate(cur: str, old: str) -> tuple[int, int] | None:
    """Find a UNIQUE span in cur matching old. Exact first; then a whitespace-tolerant
    match on the sequence of non-blank, stripped lines. Returns (start,end) char
    offsets, or None if absent or ambiguous (caller then fails + rolls back)."""
    c = cur.count(old)
    if c == 1:
        i = cur.find(old)
        return (i, i + len(old))
    if c > 1:
        return None  # ambiguous exact match → unsafe
    old_lines = [ln.strip() for ln in old.splitlines() if ln.strip()]
    if not old_lines:
        return None
    lines = cur.splitlines(keepends=True)
    offsets, pos = [], 0
    for ln in lines:
        offsets.append(pos)
        pos += len(ln)
    stripped = [ln.strip() for ln in lines]
    matches: list[tuple[int, int]] = []
    for start in range(len(lines)):
        collected, idxs, j = [], [], start
        while j < len(lines) and len(collected) < len(old_lines):
            if stripped[j]:
                collected.append(stripped[j])
                idxs.append(j)
            j += 1
        if collected == old_lines and idxs:
            matches.append((offsets[idxs[0]], offsets[idxs[-1]] + len(lines[idxs[-1]])))
    return matches[0] if len(matches) == 1 else None


def _verify(changed: list[Path], cfg: dict[str, Any]) -> tuple[bool, str]:
    """Behaviour check beyond syntax: import the changed modules (catches import
    errors / module-level NameErrors that py_compile misses), then run the optional
    user-configured test command (config 'self_repair_verify'). Returns (ok, detail)."""
    # 1. import-check top-level package modules, in the SAME interpreter (same deps).
    for d in changed:
        if d.suffix == ".py" and d.parent == APP_DIR:
            r = subprocess.run([sys.executable, "-c", f"import {d.stem}"],
                               cwd=str(APP_DIR), capture_output=True, text=True, timeout=60)
            if r.returncode != 0:
                last = (r.stderr.strip().splitlines() or ["import failed"])[-1]
                return False, f"import {d.stem}: {last}"
    # 2. optional user test command (e.g. 'python -m pytest -q' or 'agent --doctor').
    cmd = cfg.get("self_repair_verify")
    if cmd:
        r = subprocess.run(cmd, shell=True, cwd=str(APP_DIR),
                           capture_output=True, text=True, timeout=300)
        if r.returncode != 0:
            tail = (r.stdout + r.stderr).strip()[-400:]
            return False, f"`{cmd}` rc={r.returncode}: {tail}"
    return True, "ok"


def _apply_edits(edits: list[dict], cfg: dict[str, Any], *,
                 verify: bool = True) -> tuple[Path | None, str | None, list[str]]:
    """Apply edits under APP_DIR with backup + compile-check + behaviour verify +
    auto-rollback on any failure."""
    targets: list[tuple[dict, Path, bool]] = []
    for e in edits:
        rel = (e.get("path") or "").strip()
        if not rel:
            return None, "edit with empty path", []
        dest = (APP_DIR / rel).resolve()
        if dest != APP_DIR and not str(dest).startswith(str(APP_DIR) + os.sep):
            return None, f"refused path outside package: {rel}", []
        targets.append((e, dest, dest.exists()))

    backup = _backup_files([dest for _, dest, ex in targets if ex])
    changed: list[Path] = []
    created: list[Path] = []
    try:
        for e, dest, ex in targets:
            old, new = e.get("old", ""), e.get("new", "")
            if old == "":
                if ex:
                    raise ValueError(f"create requested but file exists: {e.get('path')}")
                dest.parent.mkdir(parents=True, exist_ok=True)
                dest.write_text(new, encoding="utf-8")
                created.append(dest)
            else:
                cur = dest.read_text(encoding="utf-8")
                span = _locate(cur, old)
                if span is None:
                    raise ValueError(f"'old' not found uniquely in {e.get('path')}")
                s, en = span
                dest.write_text(cur[:s] + new + cur[en:], encoding="utf-8")
            changed.append(dest)
        bad = []
        for d in changed:
            if d.suffix == ".py":
                try:
                    py_compile.compile(str(d), doraise=True)
                except py_compile.PyCompileError as ce:
                    bad.append(f"{d.name}: {ce.msg}")
        if bad:
            raise ValueError("compile check failed → " + "; ".join(bad))
        if verify:
            ok, detail = _verify(changed, cfg)
            if not ok:
                raise ValueError(f"verification failed → {detail}")
    except Exception as ex:
        _restore_backup(backup)
        for d in created:
            try:
                d.unlink()
            except OSError:
                pass
        return None, f"{ex} (rolled back)", []
    return backup, None, [d.relative_to(APP_DIR).as_posix() for d in changed]


def run_self_repair(client: Anthropic, cfg: dict[str, Any], goal: str, *,
                    dry_run: bool, verbose: bool, verify: bool = True) -> None:
    if not goal:
        sys.exit("Error: --self-repair needs a goal, e.g. habatch --self-repair 'fix the spinner flicker'")
    src = collect_self_source()
    blob = "\n".join(f"===== {path} =====\n{text}" for path, text in src.items())
    prompt = SELF_REPAIR_INSTRUCTIONS + goal + "\n\n=== SOURCE ===\n" + blob

    print(f"\033[36m[self-repair]\033[0m goal: {goal}")
    print(f"\033[2m  package: {APP_DIR}  ({len(src)} files)\033[0m", flush=True)
    spinner = Spinner()
    spinner.start("analyzing own source")
    try:
        text = _collect_session_text(client, cfg, prompt)
    finally:
        spinner.stop()

    parsed = _extract_json(text)
    if not parsed or not isinstance(parsed.get("edits"), list):
        print("\033[31m[self-repair] could not parse an edit plan from the response.\033[0m")
        if verbose:
            print(text[:1000])
        return

    edits = parsed["edits"]
    print(f"  proposed: {parsed.get('summary', '')}")
    if not edits:
        print("  (no edits proposed)")
        return
    for e in edits:
        print(f"\n  • {e.get('path')} — {e.get('desc', '')}")
        if verbose or dry_run:
            print(f"\033[31m    - {(e.get('old') or '')[:400]!r}\033[0m")
            print(f"\033[32m    + {(e.get('new') or '')[:400]!r}\033[0m")

    if dry_run:
        print("\n\033[33m[dry-run] no files changed. Re-run without --dry-run to apply.\033[0m")
        return

    if verify:
        vc = cfg.get("self_repair_verify")
        print(f"\033[2m  verify: import-check{' + `' + vc + '`' if vc else ''}\033[0m")
    backup, err, changed = _apply_edits(edits, cfg, verify=verify)
    if err:
        print(f"\n\033[31m[self-repair FAILED] {err}\033[0m")
        return
    print(f"\n\033[32m[self-repair OK]\033[0m applied to {len(changed)} file(s): {', '.join(changed)}")
    print(f"\033[2m  backup: {backup}  (restore with: --rollback)\033[0m")


# === Args ===

def normalize_args(argv: list[str]) -> list[str]:
    out: list[str] = []
    i = 0
    while i < len(argv):
        a = argv[i]
        if a == "-" and i + 1 < len(argv) and len(argv[i + 1]) == 1:
            out.append("-" + argv[i + 1])
            i += 2
            continue
        if a == "--" and i + 1 < len(argv) and argv[i + 1].isalpha():
            out.append("--" + argv[i + 1])
            i += 2
            continue
        out.append(a)
        i += 1
    return out


def parse_args(argv: list[str]) -> dict[str, Any]:
    args = normalize_args(argv)
    parsed: dict[str, Any] = {
        "interactive": False,
        "verbose": False,
        "status": False,
        "doctor": False,
        "no_memory": False,
        "with_repo": False,
        "force_new": False,
        "usage": False,
        "self_repair": False,
        "dry_run": False,
        "rollback": False,
        "no_verify": False,
        "mock": False,
        "message": [],
    }
    known = {"-i", "--interactive", "-v", "--verbose", "--status", "--doctor",
             "--usage", "--no-memory", "--no-repo", "--with-repo", "--new",
             "--self-repair", "--dry-run", "--rollback", "--no-verify", "--mock",
             "-h", "--help"}
    for a in args:
        if a in ("-i", "--interactive"):
            parsed["interactive"] = True
        elif a in ("-v", "--verbose"):
            parsed["verbose"] = True
        elif a == "--status":
            parsed["status"] = True
        elif a == "--doctor":
            parsed["doctor"] = True
        elif a == "--usage":
            parsed["usage"] = True
        elif a == "--no-memory":
            parsed["no_memory"] = True
        elif a == "--with-repo":
            parsed["with_repo"] = True
        elif a == "--no-repo":
            pass  # default now; kept for backward compat
        elif a == "--new":
            parsed["force_new"] = True
        elif a == "--self-repair":
            parsed["self_repair"] = True
        elif a == "--dry-run":
            parsed["dry_run"] = True
        elif a == "--rollback":
            parsed["rollback"] = True
        elif a == "--no-verify":
            parsed["no_verify"] = True
        elif a == "--mock":
            parsed["mock"] = True
        elif a in ("-h", "--help"):
            print(__doc__)
            sys.exit(0)
        elif a.startswith("-"):
            sys.exit(f"Error: unknown option '{a}'\nAvailable: {', '.join(sorted(known))}")
        else:
            parsed["message"].append(a)
    return parsed


def main() -> None:
    raw_args = sys.argv[1:]
    if not raw_args:
        print(__doc__)
        sys.exit(0)

    if "--doctor" in raw_args:
        run_doctor()
        return

    p = parse_args(raw_args)

    if p["status"]:
        show_status()
        return

    if p["usage"]:
        show_usage()
        return

    if p["rollback"]:
        rollback_self_repair()
        return

    mock = mock_enabled(p["mock"])
    if mock and not CONFIG_FILE.exists():
        # let people without setup/config try the UX offline
        cfg = {"name": "MockAgent", "model": DEFAULT_MODEL, "agent_id": "mock", "environment_id": "mock"}
        print("\033[2m[mock] offline mode — no API key/config needed, responses are canned\033[0m")
    else:
        cfg = load_config()
        if mock:
            print("\033[2m[mock] offline mode — responses are canned, no API used\033[0m")

    try:
        client = make_client(mock)
    except Exception as e:
        log_error("client init failed", e)
        sys.exit(f"client init failed: {e}\nCheck ANTHROPIC_API_KEY (or try --mock).")

    with_memory = not p["no_memory"]
    with_repo = p["with_repo"]

    try:
        if p["self_repair"]:
            goal = " ".join(p["message"]).strip()
            run_self_repair(client, cfg, goal, dry_run=p["dry_run"], verbose=p["verbose"],
                            verify=not p["no_verify"])
        elif p["interactive"]:
            run_repl(client, cfg, verbose=p["verbose"],
                     with_memory=with_memory, with_repo=with_repo)
        else:
            message = " ".join(p["message"]).strip()
            if not message:
                sys.exit(
                    "Error: empty message.\n"
                    "Usage:\n"
                    "  agent 'question'         one turn (resume session)\n"
                    "  agent --new 'question'   always a fresh session\n"
                    "  agent -i                 interactive REPL\n"
                    "  agent --with-repo 'q'    attach a github_repository\n"
                    "  agent --no-memory        do not attach the memory_store\n"
                    "  agent --doctor           environment diagnostics\n"
                    "  agent --status           show config"
                )
            run_once(client, cfg, message, verbose=p["verbose"],
                     with_memory=with_memory, with_repo=with_repo,
                     force_new=p["force_new"])
    except APIError as e:
        status = getattr(e, "status_code", getattr(e, "status", "?"))
        msg = getattr(e, "message", str(e))
        log_error(f"Unhandled APIError status={status}", e)
        sys.exit(f"\nAPI error: status={status} message={msg}\nLog: {LOG_DIR}/")
    except KeyboardInterrupt:
        print("\nInterrupted.")
    except Exception as e:
        log_error("Unexpected error", e)
        sys.exit(f"Unexpected error: {e}\nLog: {LOG_DIR}/")


if __name__ == "__main__":
    main()
