"""Parse a Claude Code session transcript (JSONL) into an ordered, typed record.

Only the main thread is parsed (`isSidechain` entries are subagent chatter and are skipped).
Unknown entry types are ignored so format additions don't break the auditor.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path

_REMINDER_RE = re.compile(r"<system-reminder>.*?</system-reminder>", re.S)
_COMMAND_RE = re.compile(r"<command-name>(.*?)</command-name>", re.S)
_COMMAND_ARGS_RE = re.compile(r"<command-args>(.*?)</command-args>", re.S)

EDIT_TOOLS = frozenset({"Edit", "Write", "MultiEdit", "NotebookEdit"})


@dataclass
class Prompt:
    seq: int
    text: str
    timestamp: str = ""


@dataclass
class AssistantText:
    seq: int
    text: str
    timestamp: str = ""


@dataclass
class ToolCall:
    seq: int
    id: str
    name: str
    input: dict
    timestamp: str = ""
    result: str | None = None
    is_error: bool = False

    @property
    def command(self) -> str:
        return str(self.input.get("command", "")) if self.name == "Bash" else ""

    @property
    def file_path(self) -> str:
        return str(self.input.get("file_path") or self.input.get("notebook_path") or "")


Event = Prompt | AssistantText | ToolCall


@dataclass
class SessionRecord:
    session_id: str = ""
    cwd: str = ""
    events: list[Event] = field(default_factory=list)
    malformed_lines: int = 0

    @property
    def prompts(self) -> list[Prompt]:
        return [e for e in self.events if isinstance(e, Prompt)]

    @property
    def texts(self) -> list[AssistantText]:
        return [e for e in self.events if isinstance(e, AssistantText)]

    @property
    def tool_calls(self) -> list[ToolCall]:
        return [e for e in self.events if isinstance(e, ToolCall)]

    @property
    def bash_calls(self) -> list[ToolCall]:
        return [c for c in self.tool_calls if c.name == "Bash"]

    @property
    def edit_calls(self) -> list[ToolCall]:
        return [c for c in self.tool_calls if c.name in EDIT_TOOLS]

    @property
    def edited_files(self) -> list[str]:
        seen: dict[str, None] = {}
        for c in self.edit_calls:
            if c.file_path:
                seen[c.file_path] = None
        return list(seen)

    @property
    def skills_used(self) -> list[str]:
        return [str(c.input.get("skill", "")) for c in self.tool_calls if c.name == "Skill"]

    @property
    def is_empty(self) -> bool:
        return not self.prompts and not self.tool_calls


def _result_text(content) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for block in content:
            if isinstance(block, dict):
                if block.get("type") == "text":
                    parts.append(str(block.get("text", "")))
                elif block.get("type") == "image":
                    parts.append("[image]")
        return "\n".join(parts)
    return "" if content is None else str(content)


def _clean_prompt(text: str) -> str | None:
    """Strip injected reminders; turn slash-command wrappers into `/cmd args`; drop harness noise."""
    if "<local-command-caveat>" in text or "<local-command-stdout>" in text:
        return None
    m = _COMMAND_RE.search(text)
    if m:
        args = _COMMAND_ARGS_RE.search(text)
        cmd = m.group(1).strip()
        return f"{cmd} {args.group(1).strip()}".strip() if args else cmd
    text = _REMINDER_RE.sub("", text).strip()
    return text or None


def parse_transcript(path: str | Path) -> SessionRecord:
    rec = SessionRecord()
    calls: dict[str, ToolCall] = {}
    seq = 0
    with open(path, encoding="utf-8", errors="replace") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                rec.malformed_lines += 1
                continue
            if not isinstance(entry, dict):
                continue
            rec.session_id = rec.session_id or str(entry.get("sessionId", ""))
            rec.cwd = rec.cwd or str(entry.get("cwd", ""))
            kind = entry.get("type")
            if kind not in ("user", "assistant") or entry.get("isSidechain"):
                continue
            msg = entry.get("message") or {}
            content = msg.get("content")
            ts = str(entry.get("timestamp", ""))

            if kind == "user":
                if isinstance(content, list) and any(
                    isinstance(b, dict) and b.get("type") == "tool_result" for b in content
                ):
                    for b in content:
                        if isinstance(b, dict) and b.get("type") == "tool_result":
                            call = calls.get(str(b.get("tool_use_id", "")))
                            if call is not None:
                                call.result = _result_text(b.get("content"))
                                call.is_error = bool(b.get("is_error"))
                    continue
                if entry.get("isMeta"):
                    continue  # skill bodies, injected context — not typed by the human
                text = content if isinstance(content, str) else _result_text(content)
                cleaned = _clean_prompt(text)
                if cleaned:
                    seq += 1
                    rec.events.append(Prompt(seq, cleaned, ts))
                continue

            for b in content if isinstance(content, list) else []:
                if not isinstance(b, dict):
                    continue
                if b.get("type") == "text" and str(b.get("text", "")).strip():
                    seq += 1
                    rec.events.append(AssistantText(seq, str(b["text"]).strip(), ts))
                elif b.get("type") == "tool_use":
                    seq += 1
                    raw_input = b.get("input")
                    call = ToolCall(
                        seq,
                        str(b.get("id", "")),
                        str(b.get("name", "")),
                        raw_input if isinstance(raw_input, dict) else {},
                        ts,
                    )
                    calls[call.id] = call
                    rec.events.append(call)
    return rec


def _clip(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    head = limit * 2 // 3
    tail = limit - head
    return f"{text[:head]}\n… [{len(text) - limit} chars elided] …\n{text[-tail:]}"


def render_digest(rec: SessionRecord, max_chars: int = 80_000) -> str:
    """Human/agent-readable replay of the session. File bodies are clipped — the diff carries the real content."""
    out: list[str] = [f"# Session {rec.session_id}", f"cwd: {rec.cwd}", ""]
    for e in rec.events:
        if isinstance(e, Prompt):
            out += [f"## [{e.seq}] USER", e.text, ""]
        elif isinstance(e, AssistantText):
            out += [f"### [{e.seq}] ASSISTANT", _clip(e.text, 4000), ""]
        else:
            if e.name == "Bash":
                shown = f"$ {e.command}"
            elif e.name in EDIT_TOOLS:
                shown = f"{e.name} {e.file_path}"
                body = e.input.get("content") or e.input.get("new_string") or ""
                if body:
                    shown += "\n" + _clip(str(body), 600)
            else:
                shown = f"{e.name} {_clip(json.dumps(e.input, ensure_ascii=False), 600)}"
            status = "ERROR" if e.is_error else "ok"
            out.append(f"### [{e.seq}] TOOL {e.name} ({status})")
            out.append(shown)
            if e.result:
                out.append("--- result ---")
                out.append(_clip(e.result, 1500))
            out.append("")
    text = "\n".join(out)
    return _clip(text, max_chars)
