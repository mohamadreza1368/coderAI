from pathlib import Path
import subprocess

from git_manager import GitManager
from web_app import _resolve_clone_destination


def test_non_repo_is_not_initialized_automatically(tmp_path: Path):
    manager = GitManager(tmp_path)

    assert manager.is_repo() is False
    assert not (tmp_path / ".git").exists()


def test_preview_commit_log_and_revert(tmp_path: Path):
    manager = GitManager(tmp_path)
    manager.init_repo()
    target = tmp_path / "app.py"
    target.write_text("print('one')\n", encoding="utf-8")
    first = manager.stage_and_commit(["app.py"], "Add app")

    preview = manager.get_diff_preview("app.py", "print('two')\n")
    assert "-print('one')" in preview
    assert "+print('two')" in preview

    target.write_text("print('two')\n", encoding="utf-8")
    second = manager.stage_and_commit(["app.py"], "Update app")
    assert first and second and first != second
    assert manager.get_log(2)[0]["message"] == "Update app"

    revert_hash = manager.revert_to(second)
    assert revert_hash and target.read_text(encoding="utf-8") == "print('one')\n"


def test_commit_does_not_include_other_staged_files(tmp_path: Path):
    manager = GitManager(tmp_path)
    manager.init_repo()
    (tmp_path / "agent.txt").write_text("agent\n", encoding="utf-8")
    (tmp_path / "user.txt").write_text("user\n", encoding="utf-8")
    manager.stage_files(["user.txt"])

    commit_hash = manager.stage_and_commit(["agent.txt"], "Agent change")

    assert commit_hash
    status = manager.get_status()
    assert any(item["path"] == "user.txt" and item["status"][0] == "A" for item in status["files"])


def test_clone_preview_and_push_to_local_remote(tmp_path: Path):
    source = tmp_path / "source"
    source.mkdir()
    source_manager = GitManager(source)
    source_manager.init_repo()
    (source / "README.md").write_text("first\n", encoding="utf-8")
    source_manager.stage_and_commit(["README.md"], "Initial commit")

    bare = tmp_path / "remote.git"
    subprocess.run(["git", "init", "--bare", str(bare)], check=True, capture_output=True)
    source_manager._run("remote", "add", "origin", str(bare))
    source_manager.push()

    output = []
    clone = tmp_path / "clone"
    clone_manager = GitManager.clone_repository(str(bare), clone, output.append)
    assert clone_manager.is_repo()
    assert (clone / "README.md").read_text(encoding="utf-8") == "first\n"

    (clone / "README.md").write_text("second\n", encoding="utf-8")
    clone_manager.stage_and_commit(["README.md"], "Update README")
    preview = clone_manager.get_push_preview()
    assert preview["ahead"] == 1
    assert preview["commits"][0]["message"] == "Update README"

    clone_manager.push()
    assert clone_manager.get_push_preview()["ahead"] == 0


def test_token_auth_is_ephemeral_and_not_written_to_remote(tmp_path: Path):
    manager = GitManager(tmp_path)
    env = manager._credential_env("user", "secret-token")

    assert env["GIT_CONFIG_KEY_0"] == "http.extraHeader"
    assert "secret-token" not in env["GIT_CONFIG_VALUE_0"]
    assert not (tmp_path / ".gitconfig").exists()


def test_non_empty_clone_folder_gets_repository_subfolder(tmp_path: Path):
    (tmp_path / "existing.txt").write_text("keep", encoding="utf-8")

    destination = _resolve_clone_destination("https://github.com/emilkowalski/skills", str(tmp_path))

    assert destination == tmp_path.resolve() / "skills"
