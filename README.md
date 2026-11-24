# did-ai-agent

JDID 钱包的**云端 AI agent 服务**(LangGraph):理解意图 → 选工具 → 生成**提案**。
它不持有私钥、也不执行钱包侧副作用工具。

## 边界(写死在设计里)

| 规则 | 落点 |
| --- | --- |
| 服务端永远不接触私钥/助记词 | 没有 secret 模块;`policy.SENSITIVE_KEYS` 兜底脱敏 |
| 工具调用只是"请求" | `graph.request_tool_node` 用 LangGraph `interrupt()` 把请求交回钱包 |
| 写操作必须用户确认 | 工具注册表里 `kind="write"` → 事件里 `kind="confirm"` |
| 外部文本只当数据 | system prompt 明确禁止执行数据区里的指令;工具名走白名单 |

## 快速开始

```sh
uv sync                                  # 按 .python-version 装 3.13
uv run uvicorn did_ai_agent.app:app --reload --port 8099
curl -s localhost:8099/healthz | jq
```

默认用**确定性假模型**(`DID_AI_PLANNER=fake`),离线可跑、可测试。

## 接真实模型(DeepSeek)

模型侧用 **[`langchain-deepseek`](https://pypi.org/project/langchain-deepseek/)** 的 `ChatDeepSeek`
(OpenAI 兼容端点),默认模型 id:`deepseek:deepseek-v4.1-flash`(`<provider>:<model-id>` 形式)。

```sh
# .env
DID_AI_PLANNER=langchain
DID_AI_MODEL=deepseek:deepseek-v4.1-flash
DEEPSEEK_API_KEY=sk-...            # 或 DID_AI_API_KEY
# DID_AI_BASE_URL=                 # 留空用官方端点;指向本地桩时才填
```

```sh
uv run uvicorn did_ai_agent.app:app --port 8099
curl -s localhost:8099/healthz | jq     # planner 应显示 LangChainPlanner
```

> 生产上不要把模型 key 放进 App:钱包只调**你自己的网关**,网关再转发给 DeepSeek。

**离线验证真实链路**:`tests/test_planner_langchain.py` 起一个本地 OpenAI 兼容桩,
断言 `ChatDeepSeek` 的 tool_calls 能被解析成 `Plan`、白名单外的工具名会被丢弃 ——
不需要 key、不打外网。

## HTTP 契约

### `POST /chat`

```json
{ "session_id": "u1-s1", "message": "我有哪些快过期的凭证?" }
```

返回 SSE,每行 `data: {json}`,事件只有四种:

| type | 含义 |
| --- | --- |
| `text` | 文本(可多段) |
| `tool_request` | 需要**钱包本机**执行:`{tool, kind, args, request_id}`,写操作 `kind="confirm"` |
| `error` | 可展示的失败 |
| `done` | 本轮结束 |

### `POST /resume`

```json
{ "session_id": "u1-s1", "result": [...] }        // 读工具:直接回传结果
{ "session_id": "u1-s1", "approved": true }       // 写工具:回传用户决定
```

### 其他

- `GET /healthz` → 版本、planner 类型、工具数
- `GET /audit` → 内存审计(工具名/事件/数据类别,不含原文)

## 工具(白名单,服务端不执行)

| 读 | 写(提案) |
| --- | --- |
| `get_identity` | `propose_sign_message` |
| `list_credentials` | `propose_issue_credential` |
| `get_credential` | `propose_publish_did` |
| `resolve_did` | |
| `verify_credential` | |
| `list_nfts` | |
| `get_dapp_grants` | |

## 开发

```sh
uv run pytest -q          # 单测:策略/工具/图/接口
uv run ruff check .       # 静态检查
uv run ruff format --check .
```

测试全部离线:假模型 + `TestClient`,不打真模型。

## 路线

- [x] P0:图骨架 + 白名单 + 审计 + 假模型
- [ ] P1:真实模型接入(含流式输出)
- [ ] P2:提案卡片协议与钱包侧确认 UI 对齐
- [ ] P3:dApp 页面风险摘要(页面内容按数据处理)
- [ ] P4:后台事件 → 一句话解释
