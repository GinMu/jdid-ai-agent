# 结构化输出:契约设计与错误恢复

这份文档回答三个问题,并把**当前 did-ai-agent 的真实做法**写成可复盘的对照表:

1. 模型需要输出结构化数据时,数据结构怎么设计?
2. 格式异常、字段缺失怎么发现(而不是让错误悄悄传下去)?
3. 发现之后怎么恢复?

> 结论先行:**模型不直接产出"业务 JSON"**。它只产出**工具调用**(`tool` + `args`),
> 结构化约束落在参数 schema 上;真正的业务数据由钱包回传,服务端只做**摘要**。
> 这样"模型编造字段"这个最危险的问题被从根上绕开了。

## 1. 三层边界,三套契约

| 层 | 数据 | 谁生产 | 谁消费 | 约束在哪 |
| --- | --- | --- | --- | --- |
| A. 模型 → 服务端 | 工具调用 `Plan(tool, args)` | 模型(function calling) | `policy.validate_call` | 参数 JSON Schema + 工具白名单 |
| B. 钱包 → 模型 | 工具结果(摘要 JSON) | 钱包执行器 | 模型 + 历史回放 | 结果 schema(字段稳定、只回摘要)+ 执行器宽容解析 |
| C. 服务端 → 客户端 | SSE 事件 `{type, …}` | FastAPI | App | `text / tool_request / error / done` 四种 |

把"结构化"拆成三层的好处:每层都能单独降级 —— 模型乱说话不影响钱包,B 层数据缺字段不影响 A 层继续规划。

### A 层:工具调用信封

```text
Plan(tool: str | None, args: dict, text: str | None, tool_call_id: str | None)
```

- `tool=None` → 这轮是纯文本回答,直接结束(不进入工具流程);
- `args` 必须是 **JSON 对象**,不是数组、不是字符串;
- `tool_call_id` 用于 OpenAI 兼容端点的配对;DeepSeek thinking 模式还有 `reasoning_content`
  的额外要求,所以历史回放最终改成了"工具结果当用户消息"(见 `docs/` 之外的提交 `ca9884d`)。

参数 schema 在 `src/did_ai_agent/tools/registry.py`,形状就是 OpenAI function schema:

```json
{
  "type": "object",
  "properties": {
    "did": { "type": "string", "description": "可选:要查的 DID" },
    "limit": { "type": "integer", "minimum": 1, "maximum": 50 }
  },
  "additionalProperties": false
}
```

三条硬约定:

| 约定 | 为什么 |
| --- | --- |
| 所有对象默认 `additionalProperties: false` | 模型很爱"顺手补字段";放开等于让它写脏数据 |
| 只把必需字段标 `required`,其余可选并给默认语义 | 缺字段是常态,可选字段缺失不应该变成错误 |
| 每个参数都写 `description` | 模型的参数质量几乎完全取决于这句描述;`did` 写清"可选,例如 did:swtc:j…" |

### B 层:结果摘要 schema

结果**不是**"把文档原样传过去",而是固定字段的摘要,例如 `list_credentials`:

```json
{
  "did": "did:swtc:jwWmui…",
  "total": 7,
  "items": [
    { "id": "…", "type": "FileAccessAuthorization", "issuer": "did:swtc:…", "expirationDate": "2027-07-03" }
  ]
}
```

设计要点:

1. **只回摘要**:`credentialSubject`、原文、签名一律不出现(隐私 + 上下文预算);
2. **字段只增不改**:客户端与模型都按"忽略未知字段"解析,新增字段不会破坏旧逻辑;
3. **数值有上界**:`limit` 在钱包侧被夹到 1–50,避免一次把几十 KB 塞进上下文;
4. **来源可解释**:解析 DID 文档时返回 `source: chain / local / none`,让"数据从哪来"可复盘。

### C 层:事件信封

```text
{ "type": "text",         "text": "…" }
{ "type": "tool_request", "tool": "…", "kind": "read|confirm", "args": {…}, "request_id": "…" }
{ "type": "error",        "error": "…" }
{ "type": "done" }
```

客户端只认 `type`,未知类型**忽略**(前向兼容):以后加 `tool_local` 这类事件不会让旧 App 崩。

## 2. 异常分类:在哪一层发现,就在哪一层收口

| 编号 | 异常 | 检测点 | 当前处理 | 用户看到 |
| --- | --- | --- | --- | --- |
| E1 | 模型没调工具也不给文本 | `plan is None` | 记 `error` | 「规划失败:模型没有返回结果」 |
| E2 | 工具名不在白名单 | `policy.validate_call` | 抛 `PolicyError`,**不执行** | 「未知工具:x」 |
| E3 | 必填参数缺失 | `validate_call`(遍历 `required`) | `PolicyError` | 「list_credentials 缺少必填参数:did」 |
| E4 | 参数类型不对 | `validate_call`(`_is_type`) | `PolicyError` | 「x.limit 类型应为 integer」 |
| E5 | 多余参数 | `validate_call`(`additionalProperties`) | `PolicyError` | 「x 不接受参数:['foo']」 |
| E6 | 参数值越界 | 钱包执行器(消费端) | **夹取**到 1–50,不报错 | 正常结果 |
| E7 | 语义不存在(如凭证 id 查不到) | 钱包执行器 | 返回 `{"error": "未找到凭证:…"}` | 模型自行解释并改口 |
| E8 | 桥/网络抛异常 | 执行器 `runCatching` | 返回 `{"error": "读取失败:…"}`,**不中断对话** | 模型说"暂时读不到" |
| E9 | 凭证/文档形状异常 | 执行器 `credentialItemsOf` | 接受 `[]` 或 `{"credentials":[…]}`;都失败 → 空数组 | 「共 0 张」+ 可继续提问 |
| E10 | 模型 4xx(模型名/鉴权) | `_explain()` | 把 provider 原文翻译成"该改哪个配置" | 「…(提示:检查 DEEPSEEK_API_KEY)」 |
| E11 | 模型反复调同一个工具 | prompt 规则 6 + `max_turns=6` | 规则劝止;熔断兜底 | 「这轮已经查得够多了…」 |
| E12 | 工具调用日志重复 | 节点重放(`interrupt` 回放) | 请求日志移到 app 层,只在发事件时打一次 | — |

两条重要的**恢复原则**:

- **错误结构化后再回给模型**(E7/E8/E9):`{"error": …}` 是普通工具结果,
  模型下一轮能看到它并自我纠正(换参数、换工具、改口)。这比"服务端重试"更省、也更准。
- **能夹取就不要报错**(E6):`limit` 是"多要一点也无所谓"的参数,夹取比报错体验好。
  判断标准:参数越界是否会让结果**语义失真**;会 → 报错,不会 → 夹取。

## 3. 当前实现的诚实缺口

| 缺口 | 现状 | 影响 | 建议 |
| --- | --- | --- | --- |
| 没有 JSON mode / `response_format` | 靠 function calling 的表达力 | 模型不能直接产出"业务 JSON"(比如结构化报告) | 需要时再加 `response_format={"type":"json_object"}` + 业务 schema,独立成一层 |
| 没有 JSON 修复(半截/围栏) | 解析失败即 `plan is None` → E1 | 网络截断时白跑一轮 | 加三级恢复:严格解析 → 去掉 json 围栏 → 截取首个平衡 `{}` |
| 没有 schema 版本号 | 靠"只增不改 + 忽略未知字段" | 字段语义变更时旧客户端会静默**错读** | 结果里加 `schemaVersion`,客户端按版本分支 |
| 工具结果没有强校验 | 钱包侧宽容解析(pydantic 未用) | 形状异常时退化成"0 条",不是报错 | 加 pydantic 校验 + `warnings` 字段,把"降级"显式化 |
| resume 与 pending 不匹配 | LangGraph 直接抛错 → 500 | 客户端会话状态错乱时体验差 | App 侧丢弃会话、服务端返回 409 + 可重试提示 |
| 没有重试预算 | 只有 `max_turns` | 同一轮里没有"再试一次"的显式策略 | 引入 per-tool retry 计数,失败两次即回传"请换个问法" |

## 4. 如果要做"模型输出业务 JSON",推荐设计

这次没有做,但设计已经想清楚,记在这里免得下次从零开始。

### 信封

```json
{
  "schemaVersion": 1,
  "kind": "credential_review",
  "data": { },
  "warnings": []
}
```

- `schemaVersion`:解析器按版本选择校验器;未知版本**拒绝**而不是猜测;
- `kind`:判别式(discriminated union),一个端点支持多种业务对象;
- `data`:真正的业务字段,由 pydantic 模型定义,缺字段用**显式默认值**;
- `warnings`:降级信息(例如"某字段缺失,已按空处理"),让 UI 能提示而不是静默。

### 解析与恢复三级

```text
1. 严格:json.loads(text)
2. 容错:去围栏、去前后说明文字,再 loads
3. 截取:找第一个 '{' 到与之平衡的 '}',只解析这一段
4. 全败:返回 ParseFailure(raw, reason),调用方决定"重问一次"还是"降级展示"
```

### 校验与回传

```text
pydantic 校验
  ├─ 通过 → 直接消费
  ├─ 缺字段/类型错 → 收集 issues,作为"工具结果"回给模型自纠一次
  └─ 仍失败 → 降级:用默认值 + warnings 展示,并把原始文本留在日志里
```

### 硬约束

| 约束 | 值 |
| --- | --- |
| 单次结构化输出上限 | 32 KB(超出直接拒绝,避免把上下文吃光) |
| 重试次数 | 1 次(第二次仍失败即降级,不无限重问) |
| 日志 | 原始文本(截断 8 KB)+ 校验 issues,便于复盘 |

## 5. 复盘清单(改动这类逻辑时逐条对)

- [ ] 新增工具时:参数有 `description`、可选字段标清楚、`additionalProperties: false`?
- [ ] 新增参数时:钱包侧有没有对应夹取/默认值?越界会语义失真吗?
- [ ] 新增结果字段时:是**只增不改**吗?客户端会不会因未知字段崩?
- [ ] 新增异常路径时:是"结构化回给模型"(可自纠)还是"抛给用户"(需翻译)?
- [ ] 新增日志时:有没有打私钥/凭证原文?(参数走 `redact()`,结果只回摘要)
- [ ] 新增测试时:坏数据(缺字段/类型错/半截 JSON)有没有覆盖?

## 6. 代码位置

| 主题 | 文件 |
| --- | --- |
| 工具白名单与参数 schema | `src/did_ai_agent/tools/registry.py` |
| 转成模型可用的 function schema | `src/did_ai_agent/tools/schema.py` |
| 参数校验、脱敏、限流 | `src/did_ai_agent/policy.py` |
| 计划信封 `Plan` / 流式产出 | `src/did_ai_agent/planner.py` |
| 图节点、熔断、事件映射 | `src/did_ai_agent/graph.py` |
| SSE 事件与错误翻译 | `src/did_ai_agent/app.py` |
| 审计记录 | `src/did_ai_agent/audit.py` |
| 钱包侧执行器(夹取/宽容解析/`{"error":…}`) | `../jdid-multi-platform/composeApp/src/commonMain/kotlin/com/android/jdid/ai/AiToolExecutor.kt` |
