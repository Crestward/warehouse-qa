from warehouse_graph.llm import FakeProvider, LLMRequest

import warehouse_qa


def test_package_imports() -> None:
    assert warehouse_qa.__version__ == "0.1.0"


def test_shared_llm_layer_is_available() -> None:
    request = LLMRequest(
        model="claude-sonnet-5-5",
        max_tokens=8,
        call_site="warehouse_qa.smoke",
        messages=[{"role": "user", "content": "hi"}],
    )
    assert FakeProvider().complete(request).text == "fake:warehouse_qa.smoke"
