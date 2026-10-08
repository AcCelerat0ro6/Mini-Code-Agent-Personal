"""
tools/mcpmanager.py - MCP 动态工具池模块 (Model Context Protocol)

MCP 建模为「晚绑定工具」：先用 connect_mcp(name) 连接 server 并发现其工具，
随后每轮 assemble_tool_pool 时以 mcp__<server>__<tool> 前缀合并进统一工具池，
下一轮模型调用立即可用；授权策略来自 host 配置（MCP_HOST_POLICY）而非 server
的自我描述，未显式放行的工具每次调用都要经过 permission_hook 确认。

    connect_mcp("docs") ── 发现 ──> docs.search / docs.get_version
                                          │ 名字归一化 + 撞名检测 + 64 字符上限
    静态工具池 (BASE_TOOL_POOL) ─────────> │
                                          v
                              统一工具池 (assemble_tool_pool)
                                          │
                                          v
                          permission_hook 按 mcp_tool_policies 审批后执行

注：MCPClient 是进程内替身（模拟 MCP tools/list 与 tools/call 的教学实现，
    配套 docs / deploy 两个 mock server）；将来接入真实 MCP 传输
    （stdio/HTTP 子进程）时只需替换 MCPClient 内部实现，
    命名归一化、冲突检测与授权策略层原样复用。

关键入口 (Key entry points):

    run_connect_mcp       connect_mcp 工具回调：连接 server 并发现工具
    assemble_tool_pool    把静态工具池与已连接 server 的动态工具合并为统一工具池
    connected_servers     返回已连接 server 名单（供系统提示词动态区块使用）
    mcp_tool_policies     工具名 -> allow | confirm 的授权策略表（permission_hook 消费）
"""

import re

# 已连接的 MCP server 注册表：server 名 -> MCPClient 实例
mcp_clients: dict = {}

# server/工具名中模型工具名字母表之外的字符统一替换为下划线
_DISALLOWED_CHARS = re.compile(r"[^a-zA-Z0-9_-]")

# 授权策略来自 host 配置，绝不采信 server 的自我描述；
# 未登记的工具默认 confirm（每次调用都询问用户）
MCP_HOST_POLICY = {
    ("docs", "search"): "allow",
    ("docs", "get_version"): "allow",
    ("deploy", "status"): "allow",
    ("deploy", "trigger"): "confirm",
}

# 工具名 -> allow | confirm（由 assemble_tool_pool 每轮原地刷新，
# hooks/pretoolusehook 持有本表的引用，因此只能就地修改不能整体换绑）
mcp_tool_policies: dict = {}


def normalize_mcp_name(name: str) -> str:
    """把 server/工具名归一化到模型工具名字母表（非法字符替换为下划线）"""
    normalized = _DISALLOWED_CHARS.sub("_", name)
    if not normalized:
        raise ValueError("MCP names cannot normalize to an empty string")
    return normalized


# ==============================================================================
# MCP 客户端替身 (MCPClient)
# ==============================================================================

class MCPClient:
    """
    单个 MCP server 的进程内替身：
    register 对应 MCP 的 tools/list（登记工具定义与执行体），
    call_tool 对应 tools/call。真实传输接入时替换本类内部即可。
    """

    def __init__(self, name: str):
        self.name = name
        self.tools: list[dict] = []
        self._handlers: dict = {}

    def register(self, tool_defs: list[dict], handlers: dict) -> None:
        """登记 server 的工具清单与执行体（名字非空、不重复、handler 齐全）"""
        names = [tool.get("name") for tool in tool_defs]
        if any(not isinstance(name, str) or not name for name in names):
            raise ValueError("Every MCP tool needs a non-empty name")
        if len(set(names)) != len(names):
            raise ValueError(f"Duplicate MCP tool name on server {self.name!r}")
        missing = [name for name in names if name not in handlers]
        if missing:
            raise ValueError(f"Missing MCP handlers: {', '.join(missing)}")
        self.tools = list(tool_defs)
        self._handlers = dict(handlers)

    def call_tool(self, tool_name: str, args: dict) -> str:
        """执行 server 上的工具并统一包装错误（对应 MCP tools/call）"""
        handler = self._handlers.get(tool_name)
        if not handler:
            return f"MCP error: unknown tool '{tool_name}'"
        try:
            return str(handler(**args))
        except Exception as exc:
            return f"MCP error: {type(exc).__name__}: {exc}"


# ==============================================================================
# 教学 mock server：进程内注册的示例工具（docs 只读检索 / deploy 触发部署）
# ==============================================================================

def _mock_server_docs() -> MCPClient:
    server = MCPClient("docs")
    server.register(
        tool_defs=[
            {"name": "search", "description": "Search the documentation.",
             "inputSchema": {"type": "object",
                             "properties": {"query": {"type": "string"}},
                             "required": ["query"]},
             "annotations": {"readOnlyHint": True}},
            {"name": "get_version",
             "description": "Get the documentation API version.",
             "inputSchema": {"type": "object", "properties": {},
                             "required": []},
             "annotations": {"readOnlyHint": True}},
        ],
        handlers={
            "search": lambda query: f"[docs] Found 3 results for '{query}'",
            "get_version": lambda: "[docs] API v2.1.0",
        })
    return server


def _mock_server_deploy() -> MCPClient:
    server = MCPClient("deploy")
    server.register(
        tool_defs=[
            {"name": "trigger", "description": "Trigger a deployment.",
             "inputSchema": {"type": "object",
                             "properties": {"service": {"type": "string"}},
                             "required": ["service"]},
             "annotations": {"destructiveHint": True}},
            {"name": "status", "description": "Check deployment status.",
             "inputSchema": {"type": "object",
                             "properties": {"service": {"type": "string"}},
                             "required": ["service"]},
             "annotations": {"readOnlyHint": True}},
        ],
        handlers={
            "trigger": lambda service: f"[deploy] Triggered: {service}",
            "status": lambda service: f"[deploy] {service}: running (v1.4.2)",
        })
    return server


MOCK_SERVERS = {
    "docs": _mock_server_docs,
    "deploy": _mock_server_deploy,
}


def connect_mcp(name: str) -> str:
    """连接 MCP server 并发现工具（幂等；未知 server 报错并列出可选项）"""
    if name in mcp_clients:
        return f"MCP server '{name}' already connected"
    factory = MOCK_SERVERS.get(name)
    if not factory:
        available = ", ".join(MOCK_SERVERS)
        return f"Unknown server '{name}'. Available: {available}"
    mcp_clients[name] = factory()
    tool_names = [tool["name"] for tool in mcp_clients[name].tools]
    print(f"\033[36m[MCP:connect] {name} -> {', '.join(tool_names)}\033[0m")
    return (f"Connected to MCP server '{name}'. "
            f"Discovered {len(tool_names)} tools: {', '.join(tool_names)}")


# ==============================================================================
# 统一工具池合并（agent_loop 每轮调用）
# ==============================================================================

def assemble_tool_pool(base_tools: list[dict],
                       base_handlers: dict) -> tuple[list[dict], dict]:
    """
    合并静态工具池与全部已连接 MCP server 的动态工具：
    - 工具名归一化为 mcp__<server>__<tool>，最长 64 字符；
    - 归一化后撞名立即报错（宁可让回合失败也不静默覆盖既有工具）；
    - inputSchema 必须是 object 型；
    - 每轮刷新授权策略表 mcp_tool_policies（host 配置优先，默认 confirm）。
    """
    tools = list(base_tools)
    handlers = dict(base_handlers)
    policies: dict = {}
    origins = {tool["name"]: f"built-in tool {tool['name']!r}"
               for tool in tools}
    for server_name, mcp_client in mcp_clients.items():
        safe_server = normalize_mcp_name(server_name)
        for tool_def in mcp_client.tools:
            raw_name = tool_def["name"]
            safe_tool = normalize_mcp_name(raw_name)
            prefixed = f"mcp__{safe_server}__{safe_tool}"
            if len(prefixed) > 64:
                raise ValueError(
                    f"MCP tool name is longer than 64 characters: {prefixed}")
            origin = f"MCP tool {server_name!r}/{raw_name!r}"
            if prefixed in origins:
                raise ValueError(
                    "MCP tool name collision after normalization: "
                    f"{prefixed!r} maps both {origins[prefixed]} and {origin}"
                )
            schema = tool_def.get("inputSchema", {})
            if (not isinstance(schema, dict)
                    or schema.get("type", "object") != "object"):
                raise ValueError(f"Invalid input schema for {origin}")
            origins[prefixed] = origin
            tools.append({
                "name": prefixed,
                "description": tool_def.get("description", ""),
                "input_schema": schema,
            })
            # 闭包绑定当前 server 与原始工具名，调用时转回 server 的 call_tool
            handlers[prefixed] = (
                lambda *, client=mcp_client, tool=raw_name, **kwargs:
                client.call_tool(tool, kwargs)
            )
            policies[prefixed] = MCP_HOST_POLICY.get(
                (server_name, raw_name), "confirm")
    # 原地刷新：pretoolusehook 持有本表引用，换绑会使其读到过期策略
    mcp_tool_policies.clear()
    mcp_tool_policies.update(policies)
    return tools, handlers


def connected_servers() -> list[str]:
    """返回已连接的 MCP server 名单（按连接顺序）"""
    return list(mcp_clients)


# ==============================================================================
# 工具列表（供 Anthropic Tool Use 注册）
# ==============================================================================

def run_connect_mcp(name: str) -> str:
    """connect_mcp 工具回调：连接 server 并发现工具"""
    return connect_mcp(name)


MCP_TOOLS = [
    {
        "name": "connect_mcp",
        "description": ("Connect to an MCP server and merge its discovered "
                        "tools into the tool pool as mcp__<server>__<tool>."),
        "input_schema": {
            "type": "object",
            "properties": {
                "name": {
                    "type": "string",
                    "description": "The MCP server name, e.g. 'docs' or 'deploy'."
                }
            },
            "required": ["name"],
            "additionalProperties": False
        }
    },
]

# ==============================================================================
# 工具映射表
# ==============================================================================

MCP_HANDLERS = {
    "connect_mcp": run_connect_mcp,
}
