from pathlib import Path

from codebase_index import CodeChunker, CodebaseIndex, IncrementalIndexer


def _offline_index(tmp_path, monkeypatch):
    index = CodebaseIndex(tmp_path)

    def embed(values):
        texts = [values] if isinstance(values, str) else values
        return [[float("needle" in text.lower()), float(len(text) % 17), 1.0] for text in texts]

    monkeypatch.setattr(index.embedding_provider, "embed", embed)
    index.vector_store.available = False
    return index


def test_discovery_respects_defaults_and_gitignore(tmp_path):
    (tmp_path / ".gitignore").write_text("ignored.py\nprivate/\n", encoding="utf-8")
    (tmp_path / "main.py").write_text("print('ok')", encoding="utf-8")
    (tmp_path / "ignored.py").write_text("ignored = True", encoding="utf-8")
    (tmp_path / "private").mkdir()
    (tmp_path / "private" / "secret.py").write_text("secret = True", encoding="utf-8")
    (tmp_path / "node_modules").mkdir()
    (tmp_path / "node_modules" / "package.js").write_text("export {}", encoding="utf-8")

    files = [path.relative_to(tmp_path).as_posix() for path in CodebaseIndex(tmp_path).discover_files()]

    assert files == ["main.py"]


def test_discovery_prunes_custom_virtualenv_and_site_packages(tmp_path):
    (tmp_path / "app.py").write_text("print('project')", encoding="utf-8")
    custom_env = tmp_path / "runtime-python"
    custom_env.mkdir()
    (custom_env / "pyvenv.cfg").write_text("home = C:/Python", encoding="utf-8")
    (custom_env / "Lib" / "site-packages").mkdir(parents=True)
    (custom_env / "Lib" / "site-packages" / "dependency.py").write_text("large = True", encoding="utf-8")
    loose_packages = tmp_path / "vendor" / "site-packages"
    loose_packages.mkdir(parents=True)
    (loose_packages / "other.py").write_text("other = True", encoding="utf-8")

    index = CodebaseIndex(tmp_path)
    files = [path.relative_to(tmp_path).as_posix() for path in index.discover_files()]

    assert files == ["app.py"]
    assert index.index_file(custom_env / "Lib" / "site-packages" / "dependency.py") == {"indexed": False, "chunks": 0}


def test_python_chunking_preserves_symbols_and_lines():
    source = '''"""Module docs."""
import os

class Worker:
    """Runs jobs."""
    def run(self):
        return os.getcwd()

def helper(value):
    return value + 1
'''
    chunks = CodeChunker().chunk_python_file("worker.py", source, 1.0)
    symbols = {(chunk.symbol_name, chunk.symbol_type): chunk for chunk in chunks}

    assert ("Worker", "class") in symbols
    assert ("run", "function") in symbols
    assert symbols[("helper", "function")].start_line == 9
    assert symbols[("Worker", "class")].docstring == "Runs jobs."


def test_incremental_index_and_hybrid_retrieval(tmp_path, monkeypatch):
    source = tmp_path / "service.py"
    source.write_text("def find_needle():\n    return 'needle'\n", encoding="utf-8")
    index = _offline_index(tmp_path, monkeypatch)

    rebuilt = index.rebuild()
    hits = index.retrieve_relevant_code("find_needle", top_k=3)

    assert rebuilt["files"] == 1
    assert rebuilt["symbols"] >= 1
    assert hits[0]["file_path"] == "service.py"
    assert hits[0]["symbol_name"] == "find_needle"

    source.write_text("def renamed_needle():\n    return 'needle changed'\n", encoding="utf-8")
    changed = IncrementalIndexer(index).on_file_changed(source)
    assert changed["indexed"] is True
    assert index.retrieve_relevant_code("renamed_needle", top_k=1)[0]["symbol_name"] == "renamed_needle"

    source.unlink()
    removed = IncrementalIndexer(index).on_file_changed(source)
    assert removed["removed"] is True
    assert index.status()["files"] == 0


def test_unchanged_file_is_not_reindexed(tmp_path, monkeypatch):
    source = tmp_path / "app.ts"
    source.write_text("export const value = 1;", encoding="utf-8")
    index = _offline_index(tmp_path, monkeypatch)

    assert index.index_file(source)["indexed"] is True
    assert index.index_file(source) == {"indexed": False, "chunks": 0}


def test_project_graph_overview_and_query_routing(tmp_path, monkeypatch):
    (tmp_path / "README.md").write_text("# Demo\n\nA small task processing service.", encoding="utf-8")
    (tmp_path / "app.py").write_text(
        "from service import run_task\n\ndef main():\n    return run_task()\n\nif __name__ == '__main__':\n    main()\n",
        encoding="utf-8",
    )
    (tmp_path / "service.py").write_text(
        "from repository import load_tasks\n\ndef run_task():\n    return load_tasks()\n",
        encoding="utf-8",
    )
    (tmp_path / "repository.py").write_text("def load_tasks():\n    return []\n", encoding="utf-8")
    index = _offline_index(tmp_path, monkeypatch)

    rebuilt = index.rebuild()
    graph = index.get_graph()
    overview = index.get_project_overview()
    project_context = index.retrieve_context("این پروژه چیکار میکنه؟")
    code_context = index.retrieve_context("تابع run_task چگونه کار می‌کند؟", top_k=1)

    assert rebuilt["graph_nodes"] == 4
    assert any(edge["from_file"] == "app.py" and edge["to_file"] == "service.py" for edge in graph.edges)
    assert any(edge["from_file"] == "service.py" and edge["to_file"] == "repository.py" for edge in graph.edges)
    assert overview["entry_points"][0] == "app.py"
    assert "task processing service" in overview["summary"]
    assert project_context["query_type"] == "project_level"
    assert project_context["overview"]["nodes"] == 4
    assert code_context["query_type"] == "code_level"
    assert any(item["file_path"] in {"app.py", "repository.py"} for item in code_context["related_files"])


def test_incremental_change_refreshes_graph_and_summary(tmp_path, monkeypatch):
    caller = tmp_path / "caller.py"
    caller.write_text("from first import value\n", encoding="utf-8")
    (tmp_path / "first.py").write_text("value = 1\n", encoding="utf-8")
    (tmp_path / "second.py").write_text("value_two = 2\n", encoding="utf-8")
    index = _offline_index(tmp_path, monkeypatch)
    index.rebuild()

    caller.write_text("from second import value_two\n", encoding="utf-8")
    IncrementalIndexer(index).on_file_changed(caller)

    edges = index.get_graph().edges
    assert any(edge["from_file"] == "caller.py" and edge["to_file"] == "second.py" for edge in edges)
    assert not any(edge["from_file"] == "caller.py" and edge["to_file"] == "first.py" for edge in edges)
