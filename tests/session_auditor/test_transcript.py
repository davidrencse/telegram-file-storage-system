from __future__ import annotations

import json

from session_auditor.transcript import AssistantText, Prompt, ToolCall, parse_transcript, render_digest


def test_parses_prompts_texts_and_tool_results(tmp_path, tb):
    tb.user("add join_path").say("On it.").bash("uv run pytest -q", "3 passed").edit("/repo/src/tgfs/index.py")
    rec = parse_transcript(tb.write(tmp_path / "t.jsonl"))
    assert rec.session_id == tb.session_id
    assert rec.cwd == "/repo"
    kinds = [type(e) for e in rec.events]
    assert kinds == [Prompt, AssistantText, ToolCall, ToolCall]
    bash = rec.bash_calls[0]
    assert bash.command == "uv run pytest -q" and bash.result == "3 passed" and not bash.is_error
    assert rec.edited_files == ["/repo/src/tgfs/index.py"]
    assert [e.seq for e in rec.events] == [1, 2, 3, 4]


def test_meta_and_reminders_and_harness_noise_are_not_prompts(tmp_path, tb):
    tb.user("skill body", meta=True)
    tb.user("real ask <system-reminder>injected</system-reminder>")
    tb.user("<local-command-caveat>Caveat</local-command-caveat>")
    tb.user("<command-message>x</command-message><command-name>/review</command-name><command-args>42</command-args>")
    rec = parse_transcript(tb.write(tmp_path / "t.jsonl"))
    assert [p.text for p in rec.prompts] == ["real ask", "/review 42"]


def test_tool_error_flag_and_list_content(tmp_path, tb):
    tb.tool("Bash", {"command": "false"}, "exit 1", is_error=True)
    tb.lines[-1]["message"]["content"][0]["content"] = [{"type": "text", "text": "line1"}, {"type": "image"}]
    rec = parse_transcript(tb.write(tmp_path / "t.jsonl"))
    assert rec.bash_calls[0].is_error
    assert rec.bash_calls[0].result == "line1\n[image]"


def test_sidechain_and_malformed_lines_skipped(tmp_path, tb):
    tb.user("main")
    side = dict(tb.lines[-1], isSidechain=True, message={"role": "user", "content": "subagent"})
    path = tb.write(tmp_path / "t.jsonl")
    with open(path, "a") as fh:
        fh.write(json.dumps(side) + "\n{not json\n[1,2]\n")
    rec = parse_transcript(path)
    assert [p.text for p in rec.prompts] == ["main"]
    assert rec.malformed_lines == 1


def test_orphan_tool_result_ignored(tmp_path, tb):
    tb.user("x")
    tb.lines.append(
        {"type": "user", "message": {"content": [{"type": "tool_result", "tool_use_id": "nope", "content": "r"}]}}
    )
    rec = parse_transcript(tb.write(tmp_path / "t.jsonl"))
    assert rec.tool_calls == []


def test_skills_and_empty(tmp_path, tb):
    assert parse_transcript(tb.write(tmp_path / "e.jsonl")).is_empty
    tb.tool("Skill", {"skill": "vercel:deploy"})
    assert parse_transcript(tb.write(tmp_path / "s.jsonl")).skills_used == ["vercel:deploy"]


def test_digest_clips_and_orders(tmp_path, tb):
    tb.user("do it").bash("cat big", "Z" * 10_000).tool("Write", {"file_path": "/a.py", "content": "Q" * 5000})
    digest = render_digest(parse_transcript(tb.write(tmp_path / "t.jsonl")), max_chars=100_000)
    assert digest.index("USER") < digest.index("$ cat big") < digest.index("Write /a.py")
    assert "chars elided" in digest
    assert len(digest) < 5000
    small = render_digest(parse_transcript(tmp_path / "t.jsonl"), max_chars=500)
    assert len(small) < 600
