from workspace_filter import iter_workspace_files


def test_workspace_filter_keeps_source_and_prunes_environment(tmp_path):
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "main.py").write_text("print('ok')", encoding="utf-8")
    environment = tmp_path / "anything-at-all"
    environment.mkdir()
    (environment / "pyvenv.cfg").write_text("home = python", encoding="utf-8")
    (environment / "Scripts").mkdir()
    (environment / "Scripts" / "activate.py").write_text("ignored = True", encoding="utf-8")

    files = [path.relative_to(tmp_path).as_posix() for path in iter_workspace_files(tmp_path)]

    assert files == ["src/main.py"]


def test_workspace_filter_prunes_conda_and_dependency_directories(tmp_path):
    (tmp_path / "package.json").write_text("{}", encoding="utf-8")
    conda = tmp_path / "local-runtime"
    (conda / "conda-meta").mkdir(parents=True)
    (conda / "module.py").write_text("ignored = True", encoding="utf-8")
    packages = tmp_path / "vendor" / "site-packages"
    packages.mkdir(parents=True)
    (packages / "module.py").write_text("ignored = True", encoding="utf-8")

    files = [path.relative_to(tmp_path).as_posix() for path in iter_workspace_files(tmp_path)]

    assert files == ["package.json"]
