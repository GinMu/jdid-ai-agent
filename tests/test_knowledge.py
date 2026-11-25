"""本地 RAG:语料要能加载、检索要能命中、服务端要能自己执行这个工具。"""

from did_ai_agent.knowledge import default_base, search_knowledge
from did_ai_agent.tools.registry import TOOLS


def test_corpus_is_present():
    # 语料随仓提交(knowledge/did-swtc.jsonl),所以块数必须够检索用
    assert len(default_base()) >= 10


def test_did_swtc_question_hits_the_spec_or_registry():
    result = search_knowledge("did:swtc 是什么方法?在 W3C 注册了吗", top_k=3)
    assert result["total"] >= 1
    kinds = {hit["kind"] for hit in result["hits"]}
    assert kinds & {"spec", "registry", "about"}, result["hits"]


def test_every_hit_carries_a_source_for_citation():
    result = search_knowledge("did:swtc 创建 文档", top_k=3)
    for hit in result["hits"]:
        assert hit["source"].startswith("http"), hit
        assert hit["text"]


def test_ethr_question_hits_ethr_sources():
    """did:ethr 的注册表条目与方法规范都要能被检索到。"""
    hits = search_knowledge("did:ethr 是什么方法 ERC1056 委托", top_k=5)["hits"]
    assert hits
    assert any("ethr" in hit["source"] for hit in hits), hits


def test_both_methods_are_indexed():
    swtc = {hit["id"] for hit in search_knowledge("did:swtc 注册表", top_k=5)["hits"]}
    ethr = {hit["id"] for hit in search_knowledge("did:ethr 注册表", top_k=5)["hits"]}
    assert swtc and ethr
    assert swtc != ethr


def test_empty_query_returns_nothing():
    assert search_knowledge("", top_k=3)["total"] == 0


def test_knowledge_tool_is_server_side():
    # 这个工具由服务端执行(local=True),不经过钱包
    assert TOOLS["query_knowledge"].local is True
    assert TOOLS["list_credentials"].local is False
