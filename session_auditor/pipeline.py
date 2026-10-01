"""End-to-end audit of one session: record → checks → validate → agent critique + redo → compare → report."""

from __future__ import annotations

import fcntl
import json
import shutil
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path

from . import gitops
from .agent import AgentResult, run_agent
from .checks import Finding, run_process_checks, run_tree_checks, severity_score
from .config import AuditorConfig, audits_dir, worktrees_dir
from .prompt import AUDIT_DIR, build_prompt
from .transcript import SessionRecord, parse_transcript, render_digest
from .validate import Validation, validate_tree

AUDIT_OUTPUTS = ("critique.md", "critique.json", "redo_notes.md", "redo_answer.md")


class AuditBusy(RuntimeError):
    pass


@dataclass
class SessionState:
    """Persisted as `.claude/audits/<sid>/session.json` by the hooks."""

    session_id: str
    transcript_path: str = ""
    baseline_tree: str = ""
    baseline_head: str | None = None
    final_tree: str = ""
    started_at: float = 0.0
    ended_at: float = 0.0
    end_reason: str = ""

    @classmethod
    def load(cls, path: Path) -> SessionState:
        return cls(**json.loads(path.read_text()))

    def save(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.__dict__, indent=2))
        tmp.replace(path)


@dataclass
class TreeScore:
    label: str
    tree: str
    findings: list[Finding] = field(default_factory=list)
    validation: Validation | None = None

    @property
    def score(self) -> int:
        """Lower is better. Failed gates dominate; tree-level rule violations add weight."""
        gate_failures = self.validation.failures if self.validation else 0
        return 100 * gate_failures + severity_score(self.findings)

    def to_dict(self) -> dict:
        return {
            "label": self.label,
            "tree": self.tree,
            "score": self.score,
            "findings": [f.to_dict() for f in self.findings],
            "validation": self.validation.to_dict() if self.validation else None,
        }


@dataclass
class AuditResult:
    session_id: str
    out_dir: Path
    verdict: str = "pending"
    process_findings: list[Finding] = field(default_factory=list)
    original: TreeScore | None = None
    redo: TreeScore | None = None
    agent: AgentResult | None = None
    critique: dict | None = None
    changed_files: int = 0
    redo_changed_files: int = 0
    notes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "session_id": self.session_id,
            "verdict": self.verdict,
            "changed_files": self.changed_files,
            "redo_changed_files": self.redo_changed_files,
            "process_findings": [f.to_dict() for f in self.process_findings],
            "original": self.original.to_dict() if self.original else None,
            "redo": self.redo.to_dict() if self.redo else None,
            "agent": self.agent.to_dict() if self.agent else None,
            "critique": self.critique,
            "notes": self.notes,
            "refs": {k: f"refs/audits/{self.session_id}/{k}" for k in ("baseline", "original", "redo")},
        }


@contextmanager
def session_lock(out_dir: Path) -> Iterator[None]:
    out_dir.mkdir(parents=True, exist_ok=True)
    fh = open(out_dir / ".lock", "w")  # noqa: SIM115 — must stay open while the lock is held
    try:
        try:
            fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise AuditBusy(f"audit already running for {out_dir.name}") from None
        yield
    finally:
        fh.close()


def _reader(repo: Path, tree: str):
    return lambda path: gitops.read_file(repo, tree, path)


def score_tree(repo: Path, tree: str, label: str, cfg: AuditorConfig, run_gates: bool = True) -> TreeScore:
    paths = gitops.list_files(repo, tree) if tree != gitops.EMPTY_TREE else []
    ts = TreeScore(label, tree, run_tree_checks(_reader(repo, tree), paths))
    if run_gates and cfg.validate_commands:
        ts.validation = validate_tree(repo, tree, cfg.validate_commands, label, cfg.validate_timeout_s)
    return ts


def decide_verdict(res: AuditResult) -> str:
    if res.agent is None:
        return "static-only"
    if not res.agent.ok:
        return "agent-failed"
    if res.agent.permission_denials and not (res.out_dir / "critique.md").exists():
        return "agent-blocked"
    if res.redo is None or res.original is None:
        return "incomplete"
    if res.changed_files and not res.redo_changed_files and not (res.out_dir / "redo_answer.md").exists():
        return "redo-empty"
    if res.redo.score < res.original.score:
        return "redo-better"
    if res.redo.score > res.original.score:
        return "original-better"
    ro = res.original.validation.tests_passed if res.original.validation else 0
    rr = res.redo.validation.tests_passed if res.redo.validation else 0
    return "redo-better" if rr > ro else "equivalent"


def _write_json(path: Path, data) -> None:
    path.write_text(json.dumps(data, indent=2, default=str))


def run_audit(
    repo: Path,
    state: SessionState,
    cfg: AuditorConfig,
    *,
    use_agent: bool = True,
    run_gates: bool = True,
) -> AuditResult:
    repo = gitops.repo_root(repo)
    out = audits_dir(repo) / state.session_id
    with session_lock(out):
        return _run_locked(repo, out, state, cfg, use_agent=use_agent, run_gates=run_gates)


def _run_locked(
    repo: Path, out: Path, state: SessionState, cfg: AuditorConfig, *, use_agent: bool, run_gates: bool
) -> AuditResult:
    sid = state.session_id
    res = AuditResult(sid, out)
    for stale in AUDIT_OUTPUTS + ("report.md", "report.json"):
        (out / stale).unlink(missing_ok=True)

    rec = parse_transcript(state.transcript_path) if state.transcript_path else SessionRecord(session_id=sid)
    if rec.malformed_lines:
        res.notes.append(f"{rec.malformed_lines} malformed transcript lines skipped")
    digest = render_digest(rec, cfg.max_digest_chars)
    (out / "transcript.md").write_text(digest)

    baseline, final = state.baseline_tree or gitops.EMPTY_TREE, state.final_tree
    if not final:
        final = gitops.snapshot_tree(repo)
        res.notes.append("final tree missing; snapshotted working tree at audit time")
    changes = gitops.changed_files(repo, baseline, final)
    res.changed_files = len(changes)
    original_diff = gitops.diff(repo, baseline, final)
    original_stat = gitops.diff(repo, baseline, final, stat=True)
    (out / "original.diff").write_text(original_diff)

    base_commit = gitops.commit_tree(repo, baseline, f"audit {sid}: baseline", state.baseline_head)
    orig_commit = gitops.commit_tree(repo, final, f"audit {sid}: original session result", base_commit)
    gitops.update_ref(repo, f"refs/audits/{sid}/baseline", base_commit)
    gitops.update_ref(repo, f"refs/audits/{sid}/original", orig_commit)

    res.process_findings = run_process_checks(rec, changes)
    res.original = score_tree(repo, final, "original", cfg, run_gates)
    _write_json(
        out / "findings.json",
        {"process": [f.to_dict() for f in res.process_findings], "tree": [f.to_dict() for f in res.original.findings]},
    )

    wants_agent = use_agent and not rec.is_empty and (bool(changes) or cfg.audit_readonly_sessions)
    if not wants_agent:
        if use_agent:
            res.notes.append("agent skipped: session made no changes (set audit_readonly_sessions to audit anyway)")
        return _finish(res)

    wt = worktrees_dir(repo) / sid
    wt.parent.mkdir(parents=True, exist_ok=True)
    if wt.exists():
        gitops.remove_worktree(repo, wt)
        shutil.rmtree(wt, ignore_errors=True)
    gitops.add_worktree(repo, wt, base_commit)
    try:
        audit_in = wt / AUDIT_DIR
        audit_in.mkdir(exist_ok=True)
        (audit_in / "transcript.md").write_text(digest)
        (audit_in / "original.diff").write_text(original_diff)
        (audit_in / "original.stat").write_text(original_stat)
        shutil.copy(out / "findings.json", audit_in / "findings.json")
        _write_json(audit_in / "validation.json", res.original.validation.to_dict() if res.original.validation else {})
        agents_md = gitops.read_file(repo, final, "AGENTS.md") or gitops.read_file(repo, baseline, "AGENTS.md") or ""
        (audit_in / "AGENTS.final.md").write_text(agents_md)

        res.agent = run_agent(cfg, build_prompt(sid), wt, out / "agent.log")

        for name in AUDIT_OUTPUTS:
            if (audit_in / name).exists():
                shutil.copy(audit_in / name, out / name)
        shutil.rmtree(audit_in, ignore_errors=True)
        if (out / "critique.json").exists():
            try:
                res.critique = json.loads((out / "critique.json").read_text())
            except json.JSONDecodeError as exc:
                res.notes.append(f"critique.json invalid: {exc}")

        redo_commit = gitops.commit_all(wt, f"audit {sid}: redo")
        gitops.update_ref(repo, f"refs/audits/{sid}/redo", redo_commit)
        redo_tree = gitops.resolve_tree(repo, redo_commit)
        res.redo_changed_files = len(gitops.changed_files(repo, baseline, redo_tree))
        (out / "redo.diff").write_text(gitops.diff(repo, baseline, redo_tree))
        (out / "original_vs_redo.diff").write_text(gitops.diff(repo, final, redo_tree))
        res.redo = score_tree(repo, redo_tree, "redo", cfg, run_gates)
    finally:
        if not cfg.keep_worktree:
            gitops.remove_worktree(repo, wt)
            shutil.rmtree(wt, ignore_errors=True)
    return _finish(res)


def _finish(res: AuditResult) -> AuditResult:
    res.verdict = decide_verdict(res)
    _write_json(res.out_dir / "report.json", res.to_dict())
    (res.out_dir / "report.md").write_text(render_report(res))
    return res


def _fmt_validation(v: Validation | None) -> str:
    if v is None:
        return "not run"
    parts = [f"{'✅' if c.ok else '❌'} `{' '.join(c.argv[-3:])}`" for c in v.commands]
    return f"{v.tests_passed} passed / {v.tests_failed} failed — " + ", ".join(parts)


def _fmt_findings(findings: list[Finding]) -> list[str]:
    if not findings:
        return ["_none_"]
    return [
        f"- **{f.severity}** `{f.rule}` — {f.message}" + (f" (`{f.evidence[:120]}`)" if f.evidence else "")
        for f in findings
    ]


def render_report(res: AuditResult) -> str:
    sid = res.session_id
    lines = [
        f"# Audit — session `{sid}`",
        "",
        f"**Verdict:** `{res.verdict}`  ·  generated {time.strftime('%Y-%m-%d %H:%M:%S')}",
        f"Files changed by session: {res.changed_files}  ·  by redo: {res.redo_changed_files}",
        "",
    ]
    if res.critique:
        lines += [f"**Auditor score:** {res.critique.get('score', '?')}/10 — {res.critique.get('summary', '')}", ""]
    lines += ["## Scoreboard (lower is better)", "", "| | score | gates | tree findings |", "|---|---|---|---|"]
    for ts in (res.original, res.redo):
        if ts:
            lines.append(f"| {ts.label} | {ts.score} | {_fmt_validation(ts.validation)} | {len(ts.findings)} |")
    lines += ["", "## Process findings (session behaviour)", *_fmt_findings(res.process_findings), ""]
    if res.original:
        lines += ["## Code findings — original", *_fmt_findings(res.original.findings), ""]
    if res.redo:
        lines += ["## Code findings — redo", *_fmt_findings(res.redo.findings), ""]
    if res.critique and res.critique.get("issues"):
        lines += ["## Auditor issues", ""]
        for i in res.critique["issues"]:
            lines.append(f"- **{i.get('severity', '?')}** {i.get('title', '')} — {i.get('location', '')}")
        lines.append("")
    if res.agent:
        cost = f"${res.agent.cost_usd:.2f}" if res.agent.cost_usd is not None else "n/a"
        lines += ["## Agent", f"ok={res.agent.ok} · {res.agent.seconds}s · cost {cost}"]
        if res.agent.error:
            lines.append(f"error: `{res.agent.error[:300]}`")
        if res.agent.permission_denials:
            lines.append(f"permission denials ({len(res.agent.permission_denials)}) — widen `allowed_tools` if needed:")
            lines += [f"- `{d}`" for d in res.agent.permission_denials[:10]]
        lines.append("")
    if res.notes:
        lines += ["## Notes", *[f"- {n}" for n in res.notes], ""]
    lines += [
        "## Files",
        "`critique.md` · `redo_notes.md` · `original.diff` · `redo.diff` · `original_vs_redo.diff` · `transcript.md`",
        "",
        "## Adopt the redo",
        "```bash",
        f"git diff refs/audits/{sid}/original refs/audits/{sid}/redo   # compare",
        f"git restore --source=refs/audits/{sid}/redo --worktree -- .  # take it (overwrites working tree!)",
        "```",
    ]
    return "\n".join(lines) + "\n"
