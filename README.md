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
(OpenAI 兼容端点),默认模型 id:`deepseek:deepseek-flash`(服务端另支持 `deepseek-v4-pro`)(`<provider>:<model-id>` 形式)。

```sh
# .env
DID_AI_PLANNER=langchain
DID_AI_MODEL=deepseek:deepseek-flash
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

## 代码导读(给第一次读的人)

按这个顺序读,20 分钟能看懂整条链路:

| 顺序 | 文件 | 看什么 |
| --- | --- | --- |
| 1 | `src/did_ai_agent/tools/registry.py` | 10 个工具的白名单。**服务端一个都不执行**,只描述"钱包能做什么" |
| 2 | `src/did_ai_agent/planner.py` | `Plan` / `Delta` 两个数据类和两种规划器。真实模型的 `iter_plan()` 是"流式 + 工具调用"的核心 |
| 3 | `src/did_ai_agent/graph.py` | 三个节点 `plan / request_tool / finish`。**看懂 `interrupt()` 就懂了一半** |
| 4 | `src/did_ai_agent/app.py` | SSE 翻译层:把图的 `updates` 与 `custom` 两种流翻译成 4 种事件 |
| 5 | `src/did_ai_agent/policy.py` | 白名单、参数校验、脱敏、限流 —— "AI 不能为所欲为"的落点 |

每个文件顶部都有"这个文件在做什么"的注释;关键分支(interrupt、tool_calls 回放、流式拼接)也都写了行内注释。

## 流式输出

`POST /chat` 与 `/resume` 都是 SSE,文本是**逐段**推的:

```text
data: {"type":"text","text":"你"}
data: {"type":"text","text":"目前"}
data: {"type":"text","text":"有"}
...
data: {"type":"done"}
```

实现靠 LangGraph 的**自定义流**:节点里 `get_stream_writer()({type:"text",text:...})`,
`app._run()` 同时订阅 `updates`(状态变化)与 `custom`(文本片段)。
好处是:首字延迟从"等整段生成完"降到几百毫秒,而"要不要调工具"的决策仍然非流式、简单可靠。

> 注意:`text` 事件可能很多条,客户端应做增量拼接;`done` 才是本轮结束。

## 多步规划

一句话可以连着调多个工具。图的边是 `request_tool → plan`,所以每拿到一个结果就重新判断一次:

```text
用户:帮我确认这几张凭证还有效吗
  plan → list_credentials  ──(钱包执行)──►  plan
  plan → verify_credential ──(钱包执行)──►  plan
  plan → 直接回答
```

两道保险:

| 保险 | 位置 |
| --- | --- |
| `max_turns`(默认 6) | `graph.plan_node` 开头:超过就收尾,防止模型绕圈 |
| 策略校验 | 每次工具调用都要过 `validate_call`(白名单 + 参数 schema) |

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

## 文档格式约定(避免 VSCode 告警)

VSCode 的 `DavidAnson.vscode-markdownlint` 会读仓根的 [`.markdownlint.json`](.markdownlint.json),
所以下面这几条是**硬约定**,不是个人偏好:

| 规则 | 说明 |
| --- | --- |
| 表格分隔行写 `\| --- \| --- \|` | **不要**写 `\|---\|`;两种混用会报 `MD060/table-column-style` |
| 代码块标注语言 | ` ```sh `/` ```json `/` ```text `,不要用裸 ` ``` ` 开场 |
| 行尾不留空格 | 编辑器保存时可开 `trim trailing whitespace` |
| 一个文件只有一个一级标题 | 且必须是第一行 |
| `MD013`(行长)已关闭 | 表格和 URL 天然超长,不再为它折行 |
| `MD060` 配成 `compact` | markdownlint 里 `compact` 的含义是"管道两侧要有空格"(即 `\| --- \|`);`tight` 才是无空格写法。VSCode 的 schema 只接受 `any/aligned/compact/tight`,写 `consistent` 会报"Value is not accepted" |

自查(离线、秒级,不需要 Node):

```sh
uv run pytest tests/test_markdown_style.py -q
```

装过 markdownlint 的话也可以直接跑:

```sh
npx markdownlint-cli2 "*.md" "docs/**/*.md"
```

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
