"""Offline mock of the Anthropic Managed-Agents surface this CLI uses.

Lets people WITHOUT an API key / Managed-Agents access try the CLI's UX and flow —
`agent "..."`, the REPL, the self-repair plumbing, and even the orchestrator's
fan-out trace — and give feedback. No network, no key, no cost. Replies are canned
and clearly marked [mock].

Enable with: AGENT_CLI_MOCK=1   (or `agent --mock "..."`)
"""
from __future__ import annotations

import itertools
import json
from types import SimpleNamespace

_counter = itertools.count(1)


class _Block:
    def __init__(self, text: str) -> None:
        self.type = "text"
        self.text = text


def _msg(text: str):
    return SimpleNamespace(type="agent.message", content=[_Block(text)])


def _usage():
    u = SimpleNamespace(input_tokens=120, output_tokens=80,
                        cache_read_input_tokens=0, cache_creation_input_tokens=0)
    return SimpleNamespace(type="span.model_request_end", model_usage=u)


def _idle():
    return SimpleNamespace(type="session.status_idle", stop_reason=None)


def _canned_reply(pending: str) -> str:
    """Pick a canned response that keeps each flow working offline."""
    if "repairing YOUR OWN source" in pending:
        # self-repair: return a valid, empty edit plan (clean "no edits" path).
        return json.dumps({"summary": "[mock] no changes proposed (offline mode)", "edits": []})
    if "task decomposer" in pending:
        # orchestrator plan: return a valid 3-way DAG so the fan-out trace is real.
        return json.dumps({"summary": "[mock] plan", "subtasks": [
            {"id": "t1", "prompt": "[mock] subtask 1", "deps": []},
            {"id": "t2", "prompt": "[mock] subtask 2", "deps": []},
            {"id": "t3", "prompt": "[mock] subtask 3", "deps": []},
        ]})
    preview = " ".join(pending.split())[:140]
    return ("[mock] Simulated response — no API was used.\n"
            f"You said: {preview}\n"
            "(Set a real ANTHROPIC_API_KEY and drop AGENT_CLI_MOCK for real answers.)")


class _MockStream:
    def __init__(self, session: "_Session") -> None:
        self._session = session

    def __iter__(self):
        yield _msg(_canned_reply(self._session.pending or ""))
        yield _usage()
        yield _idle()

    def close(self) -> None:
        pass


class _Session:
    def __init__(self) -> None:
        self.id = f"mock_session_{next(_counter)}"
        self.pending: str | None = None


class _Events:
    def __init__(self, sessions: "_Sessions") -> None:
        self._sessions = sessions

    def stream(self, session_id: str):
        return _MockStream(self._sessions.by_id[session_id])

    def send(self, session_id: str, events=None) -> None:
        text = ""
        for e in events or []:
            for b in e.get("content", []):
                if b.get("type") == "text":
                    text += b.get("text", "")
        if text:
            self._sessions.by_id[session_id].pending = text


class _Sessions:
    def __init__(self) -> None:
        self.by_id: dict[str, _Session] = {}
        self.events = _Events(self)

    def create(self, **kwargs) -> _Session:
        s = _Session()
        self.by_id[s.id] = s
        return s

    def archive(self, session_id: str) -> None:
        self.by_id.pop(session_id, None)


class _Beta:
    def __init__(self) -> None:
        self.sessions = _Sessions()


class _Models:
    def list(self):
        return SimpleNamespace(data=[])


class MockAnthropic:
    """Drop-in stand-in for anthropic.Anthropic covering only what this CLI calls."""

    def __init__(self, *args, **kwargs) -> None:
        self.beta = _Beta()
        self.models = _Models()
