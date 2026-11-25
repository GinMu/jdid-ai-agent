"""把 knowledge/sources 下的原始资料切成小块,产出 knowledge/did-methods.jsonl。

为什么要切块:RAG 检索的单位是"块",整篇 7KB 的规范丢给模型既浪费上下文、也让引用定位困难。
切块规则刻意简单(按 markdown 标题 + 段落,单块不超过 MAX_CHARS 字符),
因为这里的目标是"能检索到、能引用",而不是追求语义最优分块。

用法:`uv run python scripts/build_knowledge.py`
"""

from __future__ import annotations

import json
import re
from pathlib import Path

SOURCES = Path(__file__).resolve().parent.parent / "knowledge" / "sources"
OUTPUT = SOURCES.parent / "did-methods.jsonl"
MAX_CHARS = 800

SOURCE_URLS = {
    "swtc-did-spec-en.md": (
        "https://github.com/JCCDex/SWTC-DID/blob/main/docs/did-method-spec-en.md",
        "spec",
    ),
    "w3c-did-extensions-swtc.json": (
        "https://github.com/w3c/did-extensions/blob/main/methods/swtc.json",
        "registry",
    ),
    "about-jdid.md": ("https://jdid.cn", "about"),
    "w3c-did-extensions-ethr.json": (
        "https://github.com/w3c/did-extensions/blob/main/methods/ethr.json",
        "registry",
    ),
    "ethr-did-method-spec.md": (
        "https://github.com/decentralized-identity/ethr-did-resolver/blob/master/doc/did-method-spec.md",
        "spec",
    ),
}


def chunks_for(path: Path) -> list[dict[str, str]]:
    url, kind = SOURCE_URLS.get(path.name, ("", "misc"))
    text = path.read_text(encoding="utf-8").strip()
    if path.suffix == ".json":
        return [{"source": url, "kind": kind, "section": path.stem, "text": text}]

    chunks: list[dict[str, str]] = []
    section = path.stem
    buffer: list[str] = []

    def flush() -> None:
        if not buffer:
            return
        body = "\n".join(buffer).strip()
        if body:
            chunks.append({"source": url, "kind": kind, "section": section, "text": body})
        buffer.clear()

    for line in text.splitlines():
        heading = re.match(r"^(#{1,6})\s+(.*)$", line)
        if heading:
            flush()
            section = heading.group(2).strip()
            continue
        buffer.append(line)
        if sum(len(part) for part in buffer) >= MAX_CHARS:
            flush()
    flush()
    return chunks


def main() -> None:
    rows: list[dict[str, str]] = []
    for path in sorted(SOURCES.iterdir()):
        if path.name.startswith(".") or path.name == "README.md":
            continue
        rows.extend(chunks_for(path))
    with OUTPUT.open("w", encoding="utf-8") as handle:
        for index, row in enumerate(rows):
            handle.write(json.dumps({"id": f"chunk-{index:03d}", **row}, ensure_ascii=False) + "\n")
    print(f"wrote {len(rows)} chunks to {OUTPUT}")


if __name__ == "__main__":
    main()
