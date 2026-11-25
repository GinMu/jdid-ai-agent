"""本地 RAG:把 DID 方法的公开资料(did:swtc / did:ethr)切块后,按关键词相关性检索。

为什么用 BM25 而不是向量检索:
- 语料只有几十块、问题集中在"did:swtc 是什么/怎么注册/文档结构"这类术语明确的问法,
  词法检索(BM25)已经够准,还完全可离线、可解释、可单测;
- 向量检索需要 embedding 服务或本地模型,会把一个纯 Python 服务变成"要下载模型"的东西。
换向量检索只需要替换 [KnowledgeBase.search] 的实现,接口不变。

语料由 `scripts/build_knowledge.py` 生成到 `knowledge/did-methods.jsonl`,
来源见 `knowledge/sources/README.md`(W3C registry + 方法规范 + 官网摘要)。
"""

from __future__ import annotations

import json
import math
import re
from collections import Counter
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any

DEFAULT_TOP_K = 4
MAX_SNIPPET = 700

_CJK = re.compile(r"[\u4e00-\u9fff]")
_WORD = re.compile(r"[a-z0-9_]+")
_STOPWORDS = {"the", "a", "an", "of", "and", "to", "is", "in", "for", "with", "on", "by"}


@dataclass(frozen=True)
class Chunk:
    id: str
    kind: str
    section: str
    source: str
    text: str


def _tokenize(text: str) -> list[str]:
    """英文按词、中文按二元组切成检索单元(不引入分词器,保持零依赖)。"""
    lowered = text.lower()
    tokens = [word for word in _WORD.findall(lowered) if word not in _STOPWORDS]
    cjk_chars = "".join(_CJK.findall(lowered))
    tokens.extend(cjk_chars[index : index + 2] for index in range(len(cjk_chars) - 1))
    return tokens


class KnowledgeBase:
    """语料的只读检索器(BM25,参数取常用默认值)。"""

    def __init__(self, chunks: list[Chunk]) -> None:
        self._chunks = chunks
        self._tokens = [_tokenize(f"{chunk.section} {chunk.text}") for chunk in chunks]
        self._lengths = [len(tokens) for tokens in self._tokens]
        self._avg_length = (sum(self._lengths) / len(self._lengths)) if self._lengths else 0.0
        self._doc_freq: Counter[str] = Counter()
        for tokens in self._tokens:
            self._doc_freq.update(set(tokens))

    def __len__(self) -> int:
        return len(self._chunks)

    def search(self, query: str, top_k: int = DEFAULT_TOP_K) -> list[tuple[Chunk, float]]:
        if not self._chunks or not query.strip():
            return []
        query_tokens = [token for token in _tokenize(query) if token in self._doc_freq]
        if not query_tokens:
            return []
        scored: list[tuple[Chunk, float]] = []
        total = len(self._chunks)
        for index, chunk in enumerate(self._chunks):
            score = self._score(query_tokens, index, total)
            if score > 0:
                scored.append((chunk, score))
        scored.sort(key=lambda item: item[1], reverse=True)
        return scored[: max(1, top_k)]

    def _score(self, query_tokens: list[str], index: int, total: int) -> float:
        counts = Counter(self._tokens[index])
        length = self._lengths[index] or 1
        score = 0.0
        for token in query_tokens:
            frequency = counts.get(token, 0)
            if not frequency:
                continue
            idf = math.log(
                1 + (total - self._doc_freq[token] + 0.5) / (self._doc_freq[token] + 0.5)
            )
            length_ratio = 0.25 + 0.75 * length / (self._avg_length or 1)
            score += idf * (frequency * 2.5) / (frequency + 1.5 * length_ratio)
        return score


def _chunk_path() -> Path:
    return Path(__file__).resolve().parent.parent.parent / "knowledge" / "did-methods.jsonl"


@lru_cache(maxsize=1)
def default_base() -> KnowledgeBase:
    path = _chunk_path()
    if not path.exists():
        return KnowledgeBase([])
    chunks: list[Chunk] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        row = json.loads(line)
        chunks.append(
            Chunk(
                id=row["id"],
                kind=row.get("kind", "misc"),
                section=row.get("section", ""),
                source=row.get("source", ""),
                text=row.get("text", ""),
            )
        )
    return KnowledgeBase(chunks)


def search_knowledge(query: str, top_k: int = DEFAULT_TOP_K) -> dict[str, Any]:
    """工具入口:返回 `{total, hits:[{id, section, source, score, text}]}`。"""
    hits = default_base().search(query, top_k)
    return {
        "query": query,
        "total": len(hits),
        "hits": [
            {
                "id": chunk.id,
                "section": chunk.section,
                "source": chunk.source,
                "kind": chunk.kind,
                "score": round(score, 3),
                "text": chunk.text[:MAX_SNIPPET],
            }
            for chunk, score in hits
        ],
    }
