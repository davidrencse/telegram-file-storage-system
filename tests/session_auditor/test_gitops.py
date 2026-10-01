from __future__ import annotations

from conftest import git

from session_auditor import gitops


def test_snapshot_includes_untracked_respects_ignore_and_leaves_index_alone(mini_repo):
    before_status = git(mini_repo, "status", "--porcelain")
    base = gitops.snapshot_tree(mini_repo)
    assert base == gitops.resolve_tree(mini_repo, "HEAD")

    (mini_repo / "new.py").write_text("x = 1\n")
    (mini_repo / "secret.session").write_text("TOKEN")
    (mini_repo / "src/tgfs/index.py").write_text("# changed\n")
    after = gitops.snapshot_tree(mini_repo)

    changes = {(c.status, c.path) for c in gitops.changed_files(mini_repo, base, after)}
    assert changes == {("A", "new.py"), ("M", "src/tgfs/index.py")}
    assert "secret.session" not in gitops.list_files(mini_repo, after)
    # user's real index untouched: new.py still untracked, nothing staged
    status = git(mini_repo, "status", "--porcelain")
    assert "?? new.py" in status and before_status == ""
    assert git(mini_repo, "diff", "--cached", "--name-only") == ""


def test_snapshot_on_repo_without_commits(tmp_path):
    repo = tmp_path / "r"
    repo.mkdir()
    git(repo, "init", "-q")
    assert gitops.head_commit(repo) is None
    assert gitops.snapshot_tree(repo) == gitops.EMPTY_TREE
    (repo / "a.txt").write_text("a")
    tree = gitops.snapshot_tree(repo)
    assert gitops.list_files(repo, tree) == ["a.txt"]


def test_diff_read_export_and_refs(mini_repo, tmp_path):
    base = gitops.snapshot_tree(mini_repo)
    (mini_repo / "src/tgfs/index.py").write_text("NEW\n")
    final = gitops.snapshot_tree(mini_repo)
    assert "+NEW" in gitops.diff(mini_repo, base, final)
    assert "index.py" in gitops.diff(mini_repo, base, final, stat=True)
    assert gitops.read_file(mini_repo, final, "src/tgfs/index.py") == "NEW\n"
    assert gitops.read_file(mini_repo, final, "missing.py") is None

    gitops.export_tree(mini_repo, final, tmp_path / "out")
    assert (tmp_path / "out/src/tgfs/index.py").read_text() == "NEW\n"
    assert not (tmp_path / "out/.git").exists()
    gitops.export_tree(mini_repo, gitops.EMPTY_TREE, tmp_path / "empty")
    assert list((tmp_path / "empty").iterdir()) == []

    c1 = gitops.commit_tree(mini_repo, base, "b", gitops.head_commit(mini_repo))
    c2 = gitops.commit_tree(mini_repo, final, "o", c1)
    gitops.update_ref(mini_repo, "refs/audits/s1/original", c2)
    assert gitops.resolve_tree(mini_repo, "refs/audits/s1/original") == final
    assert "audits" not in git(mini_repo, "branch", "-a")  # no branch clutter


def test_worktree_lifecycle(mini_repo, tmp_path):
    base = gitops.snapshot_tree(mini_repo)
    commit = gitops.commit_tree(mini_repo, base, "b")
    wt = tmp_path / "wt"
    gitops.add_worktree(mini_repo, wt, commit)
    (wt / "redo.py").write_text("y = 2\n")
    redo = gitops.commit_all(wt, "redo")
    assert "redo.py" in gitops.list_files(mini_repo, gitops.resolve_tree(mini_repo, redo))
    head_before = git(mini_repo, "rev-parse", "HEAD")
    gitops.remove_worktree(mini_repo, wt)
    assert not wt.exists()
    assert git(mini_repo, "rev-parse", "HEAD") == head_before
    assert not (mini_repo / "redo.py").exists()
