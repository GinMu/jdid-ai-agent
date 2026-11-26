# Agent 的 ReAct 循环

这份文档讲清楚:ReAct 是什么、**本项目的循环具体长什么样**、什么时候终止、
失败了怎么恢复,以及和"教科书版 ReAct"差在哪。

> 结论先行:本项目用的是**原生 function calling 版的 ReAct**——
> `Reason` = 模型决定调哪个工具(结构化 `tool_calls`),
> `Act` = 服务端执行本地工具 **或** `interrupt()` 交给钱包,
> `Observation` = 工具结果(结构化 JSON 摘要)作为一条用户消息回放。
> 循环上限 `max_turns=6`,唯一环是 `request_tool → plan`。

## 1. ReAct 是什么

ReAct = **Rea**soning + **Act**ing。核心思想是让模型"边想边做":

```text
Thought  →  我应该先看用户的凭证
Action   →  list_credentials(limit=50)
Observation → {"total": 7, "items": [...]}
Thought  →  有 7 张,可以逐张验证
Action   →  verify_credential(id=...)
Observation → {"verified": true}
Answer   →  ...
```

为什么有效:模型不需要一次猜对,可以**先取事实再下结论**。代价是要多轮往返(延迟、成本、死循环风险)。

## 2. 本项目的三拍

| ReAct 环节 | 本项目实现 | 代码 |
| --- | --- | --- |
| Reason | 模型输出 `tool_calls`(或纯文本) | `planner.LangChainPlanner.iter_plan` |
| Act(本地) | 服务端直接执行知识库检索,不打断 | `graph.request_tool_node`(`local=True`) |
| Act(钱包) | `interrupt()` 挂起,等客户端 `/resume` | 同上 + `app.resume` |
| Observation | 工具结果写入 `history`,回放成一条用户消息 | `planner._messages` |
| 再循环 | `request_tool → plan` | `graph.build_graph` |
| 终止 | 无工具 / 有错误 / 达 `max_turns` | `graph.route` + `plan_node` |

### 2.1 状态

```python
class AgentState(TypedDict, total=False):
    session_id: str          # 会话(就是 LangGraph 的 thread_id)
    message: str             # 用户这一轮说的话
    history: list[dict]      # 已执行的工具调用:{tool, args, tool_call_id, result}
    pending: dict | None     # 正要交给钱包执行的调用
    final: str | None        # 最终回答
    turns: int               # 已走过的工具往返次数(熔断用)
    error: str | None        # 失败原因(优先于回答)
```

### 2.2 图

```text
START → plan
plan  --(有 pending)--> request_tool
plan  --(无 pending / 有 error)--> finish → END
request_tool --> plan        ← 唯一的环
```

`route()` 只看两件事:`error` 优先,其次 `pending` 是否存在。

## 3. 两条例路径的时序

### 3.1 本地工具(知识库):一拍完成

```text
客户端 POST /chat
  → plan: 模型要 query_knowledge("did:swtc 方法")
  → request_tool: local=True,服务端直接检索(无 interrupt)
  → plan: 拿到 8 个片段,输出最终文本
  → finish
客户端只看到 text 事件,没有 tool_request
```

### 3.2 钱包工具:两拍,中间挂起

```text
客户端 POST /chat
  → plan: 模型要 list_credentials
  → request_tool: interrupt(pending)  ← 本轮结束
客户端收到 {"type":"tool_request", tool:"list_credentials", args:{…}}
  …客户端执行(读本地文档/链上)…
客户端 POST /resume(result)
  → request_tool: 恢复,把结果写进 history
  → plan: 基于结果回答
  → finish
```

关键点:`interrupt()` 让"人/设备执行"成为循环的一部分,而**服务端从不持有私钥**。

## 4. 与教科书 ReAct 的差异

| 维度 | 教科书写法 | 本项目 |
| --- | --- | --- |
| 协议 | 文本里写 `Thought:/Action:/Observation:` | **原生 function calling**(结构化 `tool_calls`) |
| 解析 | 正则/字符串切分,容易崩 | 由模型 API 保证结构 |
| Observation | 自然语言 | **结构化 JSON 摘要**(只回 id/类型/过期等) |
| Thought | 显式字段 | 不落库;DeepSeek thinking 模式的 `reasoning_content` 只用于本轮 |
| Act 的执行者 | 通常是 agent 自己的工具 | **钱包**(用户设备),或服务端本地工具 |
| 终止 | 模型输出 `Final Answer` | 无工具调用即终止 + `max_turns` 熔断 |
| 安全边界 | 工具即能力 | 双白名单(服务端 + 钱包),写操作必须用户确认 |

为什么不用文本版 ReAct:文本协议在中文输出、JSON 参数、括号嵌套上极易解析失败,
而 function calling 把"结构"交给 API,我们只做**白名单 + 参数校验**。

## 5. 终止条件

| 条件 | 触发点 | 结果 |
| --- | --- | --- |
| 模型不再要工具 | `plan.tool is None` | 文本即最终回答 |
| 规划失败 | `plan is None` / 抛异常 | `error` → finish |
| 策略拒绝 | `validate_call` 抛 `PolicyError` | `error` → finish |
| 熔断 | `turns >= max_turns`(默认 6) | "这轮已经查得够多了…" |
| 工具结果不可用 | 钱包回 `{"error": …}` | **不终止**,回给模型自纠(见 `docs/structured-output.md`) |

注意 `turns` 在 `/chat` 里重置为 0:**熔断是"每轮"的**,跨轮只有限流(30 次/分钟)兜底。

## 6. 流式与"半拍文本"

模型常在没有工具调用时直接边想边说,所以文本和工具调用是**同一条流**:

```text
text 事件(逐 token) → 如果最后决定了工具:补一个 "\n\n",本轮结束发 tool_request
                     → 如果没决定工具:继续流,最后 done
```

两个细节:

- 客户端在 `tool_request` 时把已收到的文本 `commitDraft()` 成一条消息(否则会被后续回答覆盖);
- 服务端在"已经吐过文字又要调工具"时补 `"\n\n"`,避免"我来查一下。"和答案粘成一行。

## 7. 失败与恢复

| 失败 | 表现 | 恢复 |
| --- | --- | --- |
| 模型 4xx(模型名/鉴权) | `error` 事件 | `_explain()` 把原文翻译成"该改哪个配置" |
| 工具名不在白名单 | `error` 事件 | 不执行,提示模型 |
| 参数不合 schema | `error` 事件 | 同上 |
| 工具执行报错 | `{"error": …}` 作为 Observation | 模型下一轮可换参数/换工具/改口 |
| 客户端断开 | 图停在 `interrupt` | 下次 `/resume` 可继续(checkpointer 保存状态) |
| resume 没有对应 pending | 当前会**开新一轮** | 已知缺口,见 `docs/loop-guards.md` G1 |

## 8. 怎么观测这个循环

一次完整往返在日志里是四行(见 `docs/loop-guards.md` 第 4 节的排查手法):

```text
chat         session=… chars=8
tool_request session=… tool=list_credentials kind=read args={"limit": 50}
resume       session=… decision={"result": {"total": 7, …}}
tool_result  session=… tool=list_credentials result={"result": {"total": 7, …}}
```

`request_id`(`session:tool:turns`)能直接看出"这是本轮第几次往返"。

## 9. 什么时候该换更强的循环形态

| 形态 | 适用 | 代价 |
| --- | --- | --- |
| 单次调用 | 问答、无副作用 | 无 |
| 固定管道 | 步骤确定的流程 | 不灵活 |
| **ReAct(本项目)** | 工具少、每步依赖上一步结果 | 往返次数、延迟 |
| Plan-and-Execute | 步骤多且可预先规划 | 需要额外的计划表示与重规划 |
| 多智能体 | 子任务能并行、角色差异大 | 协调成本、调试困难 |

升级信号:一次任务经常要 >6 次工具往返,或需要并行取多个来源 —— 那时再考虑
Plan-and-Execute(先出计划再执行)而不是继续加 `max_turns`。

## 10. TODO

| 编号 | 事项 | 说明 |
| --- | --- | --- |
| A1 | 显式记录每步 Thought | 便于复盘"模型为什么这么选";需注意隐私 |
| A2 | 并行工具调用 | 当前一次只调一个(串行),多来源场景会慢 |
| A3 | 循环检测 | 同工具同参数重复拦截,见 `docs/loop-guards.md` G2 |
| A4 | Observation 压缩 | 大结果进历史前先压,见 `docs/summarization.md` |
| A5 | 每步耗时指标 | 现在只有日志时间戳,没有分阶段统计 |

## 11. 复盘清单

- [ ] 新增节点时:是否引入了新的环?环上有熔断吗?
- [ ] 新增工具时:`kind` 是 read 还是 write?写操作是否走 confirm?
- [ ] 工具结果是否只回摘要(不含私钥/原文)?
- [ ] 新增分支是否更新了 `route()`?有没有把 `error` 放在最高优先级?
- [ ] 流式文本与工具事件是否会造成"双份回答"?

## 12. 代码位置

| 主题 | 文件 |
| --- | --- |
| 状态、节点、边、熔断 | `src/did_ai_agent/graph.py` |
| 计划与消息组装 | `src/did_ai_agent/planner.py` |
| 工具白名单与参数 schema | `src/did_ai_agent/tools/registry.py`、`policy.py` |
| `/chat`、`/resume`、SSE 翻译 | `src/did_ai_agent/app.py` |
| 死循环护栏与排查 | `docs/loop-guards.md` |
| 结构化输出与错误恢复 | `docs/structured-output.md` |
| 记忆与摘要 | `docs/memory.md`、`docs/summarization.md` |
| 客户端状态机 | `../jdid-multi-platform/.../ai/AiChatState.kt` |
