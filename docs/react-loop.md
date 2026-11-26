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

## 13. 设计问答:Agent 形态与分层

> 每节统一写:**结论 → 为什么 → 优劣势 → 现状(已实现/`TODO`)**。现状以代码为准,不臆造。

### 为什么用 Agent 而非固定 Workflow

**结论**:本项目的流程**既不是纯 Agent 也不是纯 Workflow**,而是"代码控骨架 + 模型控选择"的混合体。

| 判断依据 | 选 Agent | 选固定 Workflow |
| --- | --- | --- |
| 步骤数是否事先确定 | 不确定(用户可能只问一句,也可能要"列凭证→逐张验证") | 确定(如"开户:4 步") |
| 分支是否取决于中间结果 | 是(有没有凭证、是否过期决定下一步) | 否 |
| 失败能否预编排 | 难(要按错误内容决定换参数还是换工具) | 能 |
| 成本敏感度 | 高延迟/高 Token | 低 |

本项目属于"步骤不确定、分支依赖中间结果",所以用 ReAct 循环(见本文第 2 节)。

**哪些交给模型,哪些由代码控制**(这是混合体的关键):

| 决策 | 谁决定 | 理由 |
| --- | --- | --- |
| 选哪个工具 | **模型** | 只有模型读得懂自然语言意图 |
| 工具参数 | **模型提议,代码校验** | `validate_call` 卡白名单/必填/类型/多余字段 |
| 执行顺序与是否继续 | **模型** | 依赖上一步观测 |
| 能否执行写操作 | **代码 + 用户** | `kind="confirm"` 强制人工确认,模型只能提案 |
| 循环上限 | **代码** | `max_turns=6` 熔断,不能交给模型自觉 |
| 返回什么字段 | **代码** | 钱包执行器只回摘要,模型无权索取原文 |
| 措辞与结构化表达 | **模型** | 这是它最擅长的部分 |

**优劣**:混合体兼顾灵活与可控,代价是"两套真源"(提示词与代码规则)需要同步维护;
纯 Workflow 更可预测但无法处理长尾,纯 Agent 更灵活但不可控、成本高。

**现状**:混合体已实现(白名单/校验/熔断/确认在代码,选择与措辞在模型)。

### Planner、Executor、Critic 如何分工

**结论**:本项目只有 **Planner + Executor**,**没有独立 Critic**;校验职责分散在 `validate_call`(参数)、
钱包执行器(数据形状)、用户确认(写操作)三处。

| 角色 | 职责 | 本项目实现 |
| --- | --- | --- |
| Planner | 决定下一步做什么 | 模型 + `plan_node` |
| Executor | 真正执行 | 本地工具(服务端)/ 钱包(设备端) |
| Critic | 判断结果是否达标、是否要重规划 | **无独立组件**;靠错误回传让 Planner 自纠 + 用户拒绝 |

**为什么拆 Planner/Executor**:执行涉及私钥与链上写操作,必须在用户设备上;规划需要大模型。
物理隔离(服务端规划 / 设备执行)比逻辑拆分更强,也顺带解决了信任边界。

**为什么不拆 Critic**:

| 方案 | 优点 | 缺点 |
| --- | --- | --- |
| 无 Critic(现状) | 少一次调用、延迟低;错误靠 `{"error":…}` 回传自纠 | 复杂任务缺少"整体验收" |
| LLM Critic | 能发现"答非所问""漏步骤" | 多一次调用;Critic 也会错;需要仲裁规则 |
| 规则 Critic | 便宜、确定(如"必须含来源") | 只能检查形式 |

**建议**:先做**规则 Critic**(检查"是否引用了工具证据""是否声称已执行写操作""是否泄露敏感字段"),
成本几乎为零;等出现"规则抓不到的质量问题"再考虑 LLM Critic,并让 Critic **只建议不执行**。

**谁汇总结果、处理冲突**:

| 事项 | 现在 | 建议 |
| --- | --- | --- |
| 结果汇总 | `plan_node` 基于 `history` 汇总(模型写自然语言) | 保持:汇总必须有唯一出口 |
| 工具间冲突(两次读到不同数据) | 无显式处理,后写覆盖前写 | `TODO`:同 key 冲突时以"最新 + 来源"标注,并在回答里说明 |
| 与用户指令冲突 | 无显式处理 | 见 `docs/memory.md` 第 9 节裁决顺序 |

### 路由、管理、执行三层怎么设计

**现状映射**(其实已经是三层,只是没这样命名):

| 层 | 职责 | 本项目 |
| --- | --- | --- |
| 路由层 | 会话识别、限流、鉴权、选模型 | `app.py`(`session_id`、`RateLimiter`)、`config.py`(单一模型) |
| 管理层 | 规划、状态、循环、终止 | `graph.py`(`plan/request_tool/finish`、`MemorySaver`) |
| 执行层 | 真正干活、返回观测 | 本地工具(知识库)+ 钱包(设备) |

**瓶颈在哪**(按影响排序):

| 瓶颈 | 表现 | 缓解 |
| --- | --- | --- |
| 模型往返次数 | 每次工具调用 = 一次完整生成 | 合并工具、限制 `max_turns`、常见问题直接答 |
| 模型首字延迟 | 用户等待感强 | 流式输出(已做)、缩短系统提示词 |
| 钱包桥调用 | `resolveDid`/`readCredentials` 可能慢 | 本地优先/网络兜底(已做)、缓存 |
| 上下文体积 | 长历史导致每次都贵 | 窗口 + 摘要(`docs/memory.md`、`docs/summarization.md`) |

**降本手段**:减少往返(工具聚合)> 缩小上下文(窗口/摘要)> 换小模型(路由)> 缓存(前缀/KV)。
顺序很重要:**先改流程再换模型**,否则只是把成本转移到"重试次数"。

### 多 Agent 如何管理 State 与 Checkpoint

**现状**:单 Agent,`thread_id = session_id`,`MemorySaver` 存整个 `AgentState`。

多 Agent 时的三种主流做法:

| 方案 | 状态归属 | 优点 | 缺点 |
| --- | --- | --- | --- |
| 共享一个 thread + 子图 | 父图持有,子图只读写自己的字段 | 简单、断点续跑一致 | 子图并发写要加锁 |
| 每个 Agent 独立 thread | 各自 checkpoint,父图只存"任务表" | 隔离好、可并行 | 跨 Agent 一致性与调试难 |
| 外部状态库 | 状态在 DB,图无状态 | 可水平扩展 | 需要自实现恢复语义 |

**避免竞争**:

| 手段 | 说明 |
| --- | --- |
| 单写者 | 同一资源只允许一个 Agent 写,其余只读 |
| 乐观锁 | 带 `version`,写回时版本不符则拒绝并重读 |
| 任务租约 | 领取任务带 `leaseUntil`,过期可被接管 |
| 副作用去重 | 以 `request_id`/`tool_call_id` 做幂等键 |

**恢复后保证幂等**:核心是"副作用前先落幂等键"——执行前写 `in_progress(idempotency_key)`,
执行后写 `done(key, result)`;恢复时先查 key,已完成直接返回结果,未完成则重试。

**现状**:单 Agent 天然无竞争;幂等键 `request_id` 已有(**仅用于日志与排查,未做去重**),
所以"重复 resume 会重复执行"是已知缺口(见 `docs/loop-guards.md` G1)。

### 单 Agent 与多 Agent 如何选型

| 判断依据 | 单 Agent | 多 Agent |
| --- | --- | --- |
| 工具数量 | <20 | 很多且领域差异大 |
| 任务可并行 | 否 | 是 |
| 上下文是否会互相污染 | 少量 | 明显(不同领域的资料互相干扰) |
| 团队能否维护 | 单人可维护 | 需要边界与协议 |

**中心式 vs 分布式**:

| 形态 | 优点 | 缺点 | 适用 |
| --- | --- | --- | --- |
| 中心式(主管编排) | 状态集中、易调试、易审计 | 主管是瓶颈与单点 | 本项目这种"一个用户一个会话" |
| 分布式(消息/黑板) | 可扩展、可并行 | 一致性、追踪、调试成本高 | 大规模后台任务 |

**通信结果怎么校验**:三条同时做 —— 结构校验(schema)、语义校验(必须带证据/引用)、
业务校验(数字与来源对得上,例如"7 张"要能列出 7 个 id)。

**现状**:单 Agent 足够;`TODO`(若引入多 Agent):先做"任务表 + 证据引用",不要先做自治。

### 多个 Agent 修改同一资源如何避免冲突

| 场景 | 风险 | 处理 |
| --- | --- | --- |
| 同时改同一份 DID 文档 | 后写覆盖前写 | 乐观锁(版本号)+ 冲突时重新读取再合并 |
| 同时签发凭证 | 重复签发 | 幂等键(主体 + 类型 + 时间窗) |
| 同时发起写提案 | 用户被多次打扰 | 提案去重 + 串行审批队列 |

**任务/进度/失败同步**:最小可用方案是"任务表"(id、owner、state、leaseUntil、attempt、lastError):
状态机 `pending → running → done/failed`,失败带原因与可重试标记;**进度靠 lease 续约**,失联可被接管。

**现状**:`TODO`(当前只有单 Agent 与人工确认,无需并发控制)。

### LangGraph 用了哪些组件,状态流转与 Checkpoint 如何实现

| 组件 | 用途 | 本项目 |
| --- | --- | --- |
| `StateGraph` | 声明节点与边 | ✅ |
| `TypedDict` state | 状态形状(`AgentState`) | ✅ |
| 条件边 | `route()` 决定去 `request_tool` 还是 `finish` | ✅ |
| `interrupt()` / `Command(resume=…)` | 挂起等外部执行(钱包) | ✅ 核心机制 |
| checkpointer(`MemorySaver`) | 保存状态,支持续跑 | ✅ 内存版 |
| 流式(`stream_mode=["updates","custom"]`) | 事件与逐 token | ✅ |
| 子图 / 并行分支 | 多 Agent 与并行 | ❌ 未用 |
| 持久化 checkpointer | 重启不丢、多实例 | ❌ `TODO`(见 `docs/memory.md` M7) |

**为什么选 LangGraph 而不是其它**:

| 方案 | 优点 | 为什么不选/何时选 |
| --- | --- | --- |
| LangGraph(现状) | 显式图、原生 `interrupt`、checkpoint、流式 | 学习曲线略陡;生态绑定 LangChain |
| 手写状态机 | 完全可控、依赖少 | 挂起/恢复、流式要自己写,容易出错 |
| AutoGen / CrewAI | 多 Agent 开箱可用 | 本项目单 Agent,引入是过度设计 |
| 纯 prompt 链 | 最简单 | 无法表达"挂起等设备执行" |

**关键结论**:选它的**唯一硬理由**是 `interrupt()` + checkpointer —— "服务端规划、设备执行、
断线可续"这个形态用其它方案要自己实现一遍。
