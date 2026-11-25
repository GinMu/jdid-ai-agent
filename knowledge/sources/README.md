# knowledge/sources

RAG 语料的原始来源(抓取时间:2026-11-25)。

| 文件 | 来源 |
| --- | --- |
| `swtc-did-spec-en.md` | https://github.com/JCCDex/SWTC-DID/blob/main/docs/did-method-spec-en.md |
| `w3c-did-extensions-swtc.json` | https://github.com/w3c/did-extensions/blob/main/methods/swtc.json |
| `ethr-did-method-spec.md` | https://github.com/decentralized-identity/ethr-did-resolver/blob/master/doc/did-method-spec.md |
| `w3c-did-extensions-ethr.json` | https://github.com/w3c/did-extensions/blob/main/methods/ethr.json |
| `about-jdid.md` | 手写摘要,引用 https://jdid.cn 与上面各来源 |

改了原始文件之后重新生成检索块:

```sh
uv run python scripts/build_knowledge.py   # → knowledge/did-methods.jsonl
```
