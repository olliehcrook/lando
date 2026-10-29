import subprocess

import pytest

from lando.main.scm import PatchConflict
from lando.main.scm.exceptions import SCMException
from lando.main.scm.git import GitSCM


def git(repo, *args):
    return subprocess.check_output(["git", *args], cwd=repo, text=True).strip()


def commit_file(repo, content, message="Bug 123 - Break startup"):
    (repo / "backout-test").write_text(content)
    git(repo, "add", "backout-test")
    git(repo, "commit", "-m", message)
    return git(repo, "rev-parse", "HEAD")


def test_git_revert(git_repo):
    scm = GitSCM(str(git_repo))
    base_tree = git(git_repo, "rev-parse", "HEAD^{tree}")
    target = commit_file(git_repo, "broken")
    with scm.for_push("hackbot@mozilla.tld"):
        result = scm.revert_commit(target.upper(), "  Startup failure  ")
    assert result == scm.head_ref()
    assert git(git_repo, "rev-parse", "HEAD^{tree}") == base_tree
    assert git(git_repo, "show", "-s", "--format=%B") == (
        f'Revert "Bug 123 - Break startup"\n\n'
        f"This reverts commit {target}.\n\nReason: Startup failure"
    )
    assert git(git_repo, "show", "-s", "--format=%ae") == "pytest@lando.example.net"
    assert git(git_repo, "show", "-s", "--format=%ce") == "hackbot@mozilla.tld"
    assert scm.describe_commit().reverted_commit_hashes() == [target]
    assert not git(git_repo, "status", "--porcelain")


@pytest.mark.parametrize(
    "kind",
    ["missing", "blob", "tag", "branch", "root", "merge", "empty", "already-reverted"],
)
def test_git_revert_rejects_invalid_targets(git_repo, kind):
    scm = GitSCM(str(git_repo))
    target = commit_file(git_repo, "broken")
    if kind == "missing":
        target = "0" * 40
    elif kind == "blob":
        target = git(git_repo, "rev-parse", "HEAD:backout-test")
    elif kind == "tag":
        git(git_repo, "tag", "-a", "annotated", "-m", "Annotated tag")
        target = git(git_repo, "rev-parse", "annotated")
    elif kind == "branch":
        git(git_repo, "checkout", "-b", "unlanded")
        target = commit_file(git_repo, "unlanded")
        git(git_repo, "checkout", "main")
    elif kind == "root":
        target = git(git_repo, "rev-list", "--max-parents=0", "HEAD")
    elif kind == "merge":
        git(git_repo, "merge", "--no-ff", "dev", "-m", "Merge dev")
        target = scm.head_ref()
    elif kind == "empty":
        git(git_repo, "commit", "--allow-empty", "-m", "Empty")
        target = scm.head_ref()
    elif kind == "already-reverted":
        scm.revert_commit(target, "First attempt")
    original = scm.head_ref()
    with pytest.raises((SCMException, ValueError)):
        scm.revert_commit(target, "Bad target")
    assert scm.head_ref() == original
    assert not git(git_repo, "status", "--porcelain")
    # A failure must not leave state that breaks the next job.
    next_target = commit_file(git_repo, "next")
    scm.revert_commit(next_target, "Next job")


def test_git_revert_conflict_cleanup(git_repo):
    scm = GitSCM(str(git_repo))
    commit_file(git_repo, "base\n")
    target = commit_file(git_repo, "broken\n")
    original = commit_file(git_repo, "subsequent change\n")
    with pytest.raises(PatchConflict) as raised:
        scm.revert_commit(target, "Startup failure")
    assert "backout-test" in raised.value.conflicts
    assert scm.head_ref() == original
    assert not git(git_repo, "status", "--porcelain")
    assert (git_repo / "backout-test").read_text() == "subsequent change\n"
    scm.revert_commit(original, "Next job")


def test_git_revert_unexpected_failure_cleanup(git_repo, monkeypatch):
    scm = GitSCM(str(git_repo))
    target = commit_file(git_repo, "broken")
    run = scm._git_run

    def fail_commit(*args, **kwargs):
        if args[0] == "commit":
            raise OSError("Could not execute commit command")
        return run(*args, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(scm, "_git_run", fail_commit)
        with pytest.raises(OSError):
            scm.revert_commit(target, "Startup failure")
    assert scm.head_ref() == target
    assert not git(git_repo, "status", "--porcelain")
    scm.revert_commit(target, "Next job")


@pytest.mark.parametrize("target", ["HEAD", "--help", "a" * 40 + "\n"])
def test_git_revert_rejects_refs(git_repo, target):
    with pytest.raises(ValueError):
        GitSCM(str(git_repo)).revert_commit(target, "Reason")
