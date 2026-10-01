"""CLI: `python -m session_auditor {hook,run,audit,sweep,list,show}`."""

from __future__ import annotations

import argparse
import json
import sys
import uuid
from pathlib import Path

from . import gitops
from .config import AuditorConfig, audits_dir
from .pipeline import AuditBusy, SessionState, run_audit


def _repo(args) -> Path:
    return gitops.repo_root(Path(args.repo).resolve())


def cmd_hook(args) -> int:
    from .hook import main

    return main()


def cmd_run(args) -> int:
    repo = _repo(args)
    state_path = audits_dir(repo) / args.session_id / "session.json"
    if not state_path.exists():
        print(f"no session state at {state_path}", file=sys.stderr)
        return 2
    return _execute(repo, SessionState.load(state_path), args)


def cmd_audit(args) -> int:
    """Audit any transcript on demand (e.g. sessions from before the hooks were installed)."""
    repo = _repo(args)
    sid = args.session_id or (Path(args.transcript).stem if args.transcript else f"manual-{uuid.uuid4().hex[:8]}")
    final = gitops.snapshot_tree(repo) if args.final == "worktree" else gitops.resolve_tree(repo, args.final)
    head = gitops.head_commit(repo)
    state = SessionState(
        session_id=sid,
        transcript_path=args.transcript or "",
        baseline_tree=gitops.resolve_tree(repo, args.baseline),
        baseline_head=head if args.baseline not in ("empty",) else None,
        final_tree=final,
    )
    state.save(audits_dir(repo) / sid / "session.json")
    return _execute(repo, state, args)


def _execute(repo: Path, state: SessionState, args) -> int:
    cfg = AuditorConfig.load(repo)
    try:
        res = run_audit(repo, state, cfg, use_agent=not args.no_agent, run_gates=not args.no_gates)
    except AuditBusy as exc:
        print(exc, file=sys.stderr)
        return 3
    print(f"{state.session_id}: {res.verdict} → {res.out_dir / 'report.md'}")
    return 0


def cmd_sweep(args) -> int:
    """Audit sessions whose SessionEnd never fired or whose run crashed (state present, report missing)."""
    repo = _repo(args)
    root = audits_dir(repo)
    pending = [p.parent for p in root.glob("*/session.json") if not (p.parent / "report.json").exists()]
    for d in sorted(pending):
        state = SessionState.load(d / "session.json")
        if not state.final_tree:
            state.final_tree = gitops.snapshot_tree(repo)
        _execute(repo, state, args)
    if not pending:
        print("nothing pending")
    return 0


def cmd_list(args) -> int:
    root = audits_dir(_repo(args))
    rows = []
    for d in sorted(root.glob("*/"), key=lambda p: p.stat().st_mtime):
        rep = d / "report.json"
        if rep.exists():
            data = json.loads(rep.read_text())
            score = (data.get("critique") or {}).get("score", "-")
            rows.append(f"{d.name}  {data['verdict']:<16} files={data['changed_files']:<3} score={score}")
        elif (d / "session.json").exists():
            rows.append(f"{d.name}  {'running/pending':<16}")
    print("\n".join(rows) or "no audits yet")
    return 0


def cmd_show(args) -> int:
    report = audits_dir(_repo(args)) / args.session_id / "report.md"
    if not report.exists():
        print(f"no report at {report}", file=sys.stderr)
        return 2
    print(report.read_text())
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="session_auditor", description=__doc__)
    p.add_argument("--repo", default=".", help="repository path (default: cwd)")
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("hook", help="Claude Code hook entry (reads hook JSON on stdin)").set_defaults(fn=cmd_hook)

    def gate_flags(sp):
        sp.add_argument("--no-agent", action="store_true", help="static checks + validation only (no LLM, free)")
        sp.add_argument("--no-gates", action="store_true", help="skip pytest/ruff validation")

    r = sub.add_parser("run", help="audit a session recorded by the hooks")
    r.add_argument("session_id")
    gate_flags(r)
    r.set_defaults(fn=cmd_run)

    a = sub.add_parser("audit", help="audit an arbitrary transcript")
    a.add_argument("--transcript", help="path to a session .jsonl")
    a.add_argument("--baseline", default="HEAD", help="rev the session started from, or 'empty' (default HEAD)")
    a.add_argument("--final", default="worktree", help="rev of the end state, or 'worktree' (default)")
    a.add_argument("--session-id")
    gate_flags(a)
    a.set_defaults(fn=cmd_audit)

    s = sub.add_parser("sweep", help="audit sessions left without a report")
    gate_flags(s)
    s.set_defaults(fn=cmd_sweep)

    sub.add_parser("list", help="list audits").set_defaults(fn=cmd_list)
    sh = sub.add_parser("show", help="print a report")
    sh.add_argument("session_id")
    sh.set_defaults(fn=cmd_show)
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return args.fn(args)


if __name__ == "__main__":
    raise SystemExit(main())
