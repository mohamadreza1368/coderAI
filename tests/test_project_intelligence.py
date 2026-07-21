from project_intelligence import DependencyGraphBuilder, QueryRouter, QueryType


def test_dependency_builder_extracts_python_structure():
    node = DependencyGraphBuilder().extract(
        "worker.py",
        "import os\nfrom .helpers import execute\n\nclass Worker: pass\n\ndef run():\n    return execute()\n",
    )

    assert node.imports == ["os", ".helpers"]
    assert node.exported_symbols == ["Worker", "run"]
    assert "execute" in node.internal_calls


def test_query_router_separates_project_and_code_questions():
    router = QueryRouter()

    assert router.classify("معماری این پروژه را توضیح بده") == QueryType.PROJECT_LEVEL
    assert router.classify("What does this project do?") == QueryType.PROJECT_LEVEL
    assert router.classify("Where is parse_config called?") == QueryType.CODE_LEVEL
