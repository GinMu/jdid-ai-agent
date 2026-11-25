"""工具注册表:白名单枚举。

两类:
- read    : 只读,钱包可以自动执行(用户只需一次性同意"允许 AI 读取本地摘要")
- write   : 会签名/写链/发布,必须以 kind="confirm" 交给用户确认后才允许执行

服务端**不实现**钱包工具的执行 —— 那永远发生在钱包里。
唯一的例外是 `local=True` 的**知识库工具**:语料就在服务端(knowledge/),
执行不涉及任何用户数据,因此由服务端自己跑,不需要钱包参与。
"""

from dataclasses import dataclass
from typing import Any, Literal

ToolKind = Literal["read", "write"]


@dataclass(frozen=True)
class Tool:
    name: str
    kind: ToolKind
    description: str
    params: dict[str, Any]
    # True = 服务端自己执行(knowledge 检索等);False = 交给钱包执行
    local: bool = False


def _obj(props: dict[str, Any], *, required: list[str] | None = None) -> dict[str, Any]:
    schema: dict[str, Any] = {"type": "object", "properties": props, "additionalProperties": False}
    if required:
        schema["required"] = required
    return schema


READ_TOOLS: tuple[Tool, ...] = (
    Tool(
        name="get_identity",
        kind="read",
        description="读取当前身份:DID、地址、链类型。不含私钥。",
        params=_obj({}),
    ),
    Tool(
        name="list_credentials",
        kind="read",
        description=(
            "列出凭证摘要(类型、发行方、是否过期),不含凭证原文与签名。"
            "默认读当前身份的 DID 文档;用户给了某个 did 时把它作为 did 传入,"
            "就会读那个 DID 的文档(公开数据)。"
        ),
        params=_obj(
            {
                "did": {"type": "string", "description": "可选:要查的 DID,例如 did:swtc:j…"},
                "limit": {"type": "integer", "minimum": 1, "maximum": 50},
            }
        ),
    ),
    Tool(
        name="get_credential",
        kind="read",
        description=(
            "按 id 取一张凭证的**摘要**(不返回原文)。"
            "默认在当前身份里找;给了 did 就在那个 DID 的文档里找。"
        ),
        params=_obj(
            {
                "credential_id": {"type": "string"},
                "did": {"type": "string", "description": "可选:在哪个 DID 的文档里找"},
            },
            required=["credential_id"],
        ),
    ),
    Tool(
        name="resolve_did",
        kind="read",
        description="解析 DID 文档(链上/网络只读)。",
        params=_obj({"did": {"type": "string"}}, required=["did"]),
    ),
    Tool(
        name="verify_credential",
        kind="read",
        description=(
            "验证一张凭证是否有效(只读)。默认在当前身份里找;给了 did 就在那个 DID 的文档里找。"
        ),
        params=_obj(
            {
                "credential_id": {"type": "string"},
                "did": {"type": "string", "description": "可选:在哪个 DID 的文档里找"},
            },
            required=["credential_id"],
        ),
    ),
    Tool(
        name="list_nfts",
        kind="read",
        description="列出 NFT 资产摘要(名称、合约、tokenId)。",
        params=_obj({"limit": {"type": "integer", "minimum": 1, "maximum": 50}}),
    ),
    Tool(
        name="get_dapp_grants",
        kind="read",
        description="列出已授权的 dApp origin(只读)。",
        params=_obj({}),
    ),
    Tool(
        name="query_knowledge",
        kind="read",
        description=(
            "检索本地收录的 DID 方法资料(did:swtc / did:ethr 的 W3C DID Extensions 注册表条目、"
            "方法规范与官网摘要)。问到某个方法是什么/怎么创建/文档结构/是否注册时用它;"
            "检索结果只是给你自己看的素材,回答要用自己的话总结,**不要罗列片段、编号或分数线**。"
        ),
        params=_obj(
            {
                "query": {"type": "string", "description": "检索词,例如 did:swtc 方法 注册"},
                "top_k": {"type": "integer", "minimum": 1, "maximum": 8},
            },
            required=["query"],
        ),
        local=True,
    ),
)

WRITE_TOOLS: tuple[Tool, ...] = (
    Tool(
        name="propose_sign_message",
        kind="write",
        description="**提案**:请求钱包对一条消息签名。必须由用户确认后才执行。",
        params=_obj(
            {"message": {"type": "string"}, "purpose": {"type": "string"}},
            required=["message", "purpose"],
        ),
    ),
    Tool(
        name="propose_issue_credential",
        kind="write",
        description="**提案**:请求钱包签发一张凭证。必须由用户确认后才执行。",
        params=_obj(
            {"subject_did": {"type": "string"}, "credential_type": {"type": "string"}},
            required=["subject_did", "credential_type"],
        ),
    ),
    Tool(
        name="propose_publish_did",
        kind="write",
        description="**提案**:请求把 DID 文档发布到链上(写链)。必须由用户确认后才执行。",
        params=_obj({"did": {"type": "string"}, "reason": {"type": "string"}}, required=["did"]),
    ),
)

TOOLS: dict[str, Tool] = {tool.name: tool for tool in (*READ_TOOLS, *WRITE_TOOLS)}
