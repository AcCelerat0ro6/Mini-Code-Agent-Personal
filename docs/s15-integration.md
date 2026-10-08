# s15 优秀机制并入改动说明

> 依据 [s15-integrated-harness.md](s15-integrated-harness.md) 第 7.3 节的差距清单，
> 从课程参考实现 s15 中挑选本项目缺失且确实优秀的机制并入。
> 本文记录**并入了什么、怎么整合的、哪些刻意没并入**，以及验证结果。
> 改动日期：2026-10-03。

---

## 1. 改动清单

| 文件 | 类型 | 内容 |
|------|------|------|
| `tools/recoverymanager.py` | **新增** | 模型调用错误恢复层：429/529 指数退避重试、连续过载切换备用模型、超长报错判定、max_tokens 策略常量 |
| `tools/mcpmanager.py` | **新增** | MCP 动态工具池：`connect_mcp` 工具、工具池合并（归一化/撞名检测/授权策略）、教学 mock server |
| `tools/backgroundmanager.py` | 修改 | `BackgroundManager.has_ready()` 就绪检测 + `background-wake` 空闲唤醒守护线程（启停与 cron/团队运行时同模式） |
| `hooks/pretoolusehook.py` | 修改 | `permission_hook` 增加 MCP 工具分支：按 host 授权策略（`mcp_tool_policies`）决定放行或确认 |
| `tools/subagent.py` | 修改 | 子代理模型调用接入 `with_retry`（每个子代理独立 `RecoveryState`） |
| `main.py` | 修改 | agent_loop 接入恢复层与 max_tokens 恢复；工具池改为"静态池 + 每轮合并 MCP"；系统提示词增加动态区块（时间/MCP）；新增后台任务唤醒回合；清理死变量 `MODEL` |

---

## 2. 错误恢复层（`tools/recoverymanager.py`）

### 2.1 职责边界

s15 的恢复逻辑分两类：**可封装成纯函数的**（重试、切模型、报错判定）与**必须操作消息历史的**（max_tokens 升级重发、残段保留 + 续跑指令注入）。本项目按此切分：

- `RecoveryState` / `with_retry` / `is_prompt_too_long_error` 与策略常量收进 `recoverymanager`；
- max_tokens 恢复放在 `main.py: agent_loop` 里（它要 `continue` 循环、追加 assistant 残段与 `CONTINUATION_PROMPT`，是循环结构的一部分）。

### 2.2 `with_retry` 契约

```python
response = with_retry(
    lambda: client.messages.create(
        model=recovery.current_model, ...),   # 闭包每轮调用时读取当前模型
    recovery,
)
```

- **429（限流）**：`BASE_DELAY_MS·2^n` 指数退避 + 最多 25% 抖动，最多 `MAX_RETRIES=3` 次；
- **529（过载）**：同样退避；**连续 2 次**后把 `state.current_model` 切到 `FALLBACK_MODEL_ID`（未配置则不切换）并继续重试；调用成功即清零连续计数；
- **其它异常**：立即原样抛出——prompt_too_long 交给 `agent_loop` 已有的 reactive_compact 兜底，业务错误不该被吞；
- **配额耗尽**：抛 `RuntimeError`，由调用方走各自的失败路径（主循环回滚 cron 投递、子代理把错误作为工具结果返回）。

日志遵循项目 `[前缀:*]` 约定：`[RETRY:429]` / `[RETRY:529]`（黄），模型切换（红）；前缀只用 ASCII。

### 2.3 agent_loop 的集成与投递确认时机调整

`agent_loop` 每回合新建一个 `RecoveryState`（回合结束即废弃，下回合从主模型起步），并把两处逻辑接了进去：

1. **模型调用**从裸 `client.messages.create` 换成 `with_retry(...)`；
2. **超长判定**改用 `is_prompt_too_long_error`（合并了 s15 与本项目两套关键字：`prompt…long`、`too many tokens`、`context_length_exceeded`、`context length`、`max_context_window`），替换原先的 `("prompt_too_long", "too many tokens")` 元组匹配。

一处**顺序调整**：`delivery.acknowledge()`（cron 投递确认）从"追加 assistant 消息之后"前移到"模型成功响应之后、max_tokens 处理之前"。原因：新增的 max_tokens 升级/续跑路径会 `continue` 跳过旧的确认点，若不前移，截断恢复成功后一次性 cron 任务会永远删不掉。前移后语义与 s15 一致（模型成功响应即投递成功），也正好落在 s15 的确认位置上。

max_tokens 恢复流程（对齐 s15）：

```
stop_reason == "max_tokens"
  ├─ 未升级过 → max_tokens 升到 16000，原样重发（截断响应丢弃）      [continue]
  ├─ 已升级   → 保留残段为 assistant 消息，注入 CONTINUATION_PROMPT，
  │             最多 MAX_RECOVERY_RETRIES=2 次                       [continue]
  └─ 仍截断   → 打印放弃日志后结束回合（残段保留在历史里）
非截断响应  → max_tokens 与升级标记复位（后续截断仍可再次升级）
```

### 2.4 子代理接入；成员线程不接入

- `tools/subagent.py` 的模型调用同样包上 `with_retry`（独立 `RecoveryState`）：子代理常跑几十轮长任务，一次瞬时限流就把整个子代理打断太浪费。
- **团队成员线程刻意不接**：成员的模型调用失败会把错误作为 `error` 事件回传 Lead（现有行为），与参考实现一致；Lead 可以重新分派或重启成员，比成员自己反复重试更符合团队协议。

---

## 3. MCP 动态工具池（`tools/mcpmanager.py`）

### 3.1 定位

`MCPClient` 是进程内替身（模拟 MCP 的 `tools/list` 与 `tools/call`），配套 `docs`（只读检索）与 `deploy`（触发部署）两个 mock server——与 s15 相同的教学实现。**有价值的部分是周边的合并与治理层**，它们与传输方式无关，将来接入真实 MCP 传输（stdio/HTTP 子进程）时只需替换 `MCPClient` 内部，命名归一化、撞名检测与授权策略原样复用。

### 3.2 合并规则（`assemble_tool_pool`）

- 工具名统一为 `mcp__<server>__<tool>`；server/工具名中的非法字符归一化为 `_`；总长超 64 字符报错；
- **归一化后撞名立即抛 `ValueError`**（例如静态池里已有一个名字恰好叫 `mcp__deploy__status` 的内置工具）——宁可让回合失败也不静默覆盖既有工具；
- `inputSchema` 必须是 object 型，否则报错；
- 每轮刷新授权策略表 `mcp_tool_policies`：`MCP_HOST_POLICY` 里登记的取 host 配置（如 `docs.search → allow`），未登记的一律 `confirm`。**授权只来自 host 配置，绝不采信 server 的自我描述**。

一个实现细节：`mcp_tool_policies` 用 `clear() + update()` **原地刷新**而不是换绑新 dict——因为 `hooks/pretoolusehook.py` 通过 `from ... import` 持有这张表的引用，换绑会让钩子读到永远过期的空表（自测第 5 组专门验证了这一点）。

### 3.3 集成点

- **main.py**：原模块级 `TOOLS / TOOL_HANDLERS` 改名为 `BASE_TOOL_POOL / BASE_HANDLER_POOL`（并加入 `MCP_TOOLS / MCP_HANDLERS`）；`agent_loop` 每轮调用 `assemble_tool_pool(BASE_TOOL_POOL, BASE_HANDLER_POOL)` 得到当轮工具池——新连接的 server 下一轮立即可用，与 s15 的每轮合并一致。工具分发改用当轮的 `tool_handlers`。
- **系统提示词**：静态 `SYSTEM` 增加 MCP 使用准则；新增 `dynamic_context_section()` 在每轮拼接"当前时间 + 已连接 MCP server 名单"（s15 每轮全量重建 system prompt 的轻量化版本，只保留真正变化的部分）。
- **权限钩子**：`permission_hook` 对 `mcp__*` 工具查 `mcp_tool_policies`，非 `allow` 的走既有 `request_permission` 交互确认——无人值守回合（cron/团队/后台唤醒）里自动拒绝的语义随之天然生效。

---

## 4. 后台任务完成唤醒（`tools/backgroundmanager.py`）

原项目中后台结果只能在"碰巧有回合在跑"时被收割（循环开头注入 + 收尾兜底）；若智能体空闲时后台命令才完成，结果要等到下一次任何回合才会被拾取。本次补上 s15 的"后台完成作为唤醒源"：

- `BackgroundManager.has_ready()`：就绪队列非空即有可收割结果；
- 新增 `background-wake` 守护线程（`start/stop_background_wake_runtime`，与 cron 队列处理器、团队唤醒线程同一套模式）：每 0.2s 检查就绪队列 → 非阻塞抢 `agent_lock` → 持锁后二次确认 → 调用 main 注入的 `run_background_wake_turn`；唤醒回合失败只打日志不退出线程（结果仍在队列，下轮重投）；
- `main.py` 新增 `run_background_wake_turn`：直接唤起 `agent_loop`（`<task_notification>` 由循环开头的后台结果注入统一收割），压缩主线取 `session["last_request"]`——新增的会话状态，记录最近一次用户请求原文（与 s15 的 `active_user_request` 同语义）。

与 cron/团队唤醒互斥地共用 `agent_lock`，进程退出时与另两个运行时一起在 `finally` 中停止。

---

## 5. 其它小改动

- `main.py` 删除了不再被引用的 `MODEL = os.environ["MODEL_ID"]`（agent_loop 改用 `recovery.current_model`；MODEL_ID 的存在性校验由 `recoverymanager` 导入时完成，保留快速失败语义）。

---

## 6. 刻意不并入的部分及理由

| s15 机制 | 不并入理由 |
|----------|-----------|
| `.tasks/.lock` 跨进程 fcntl 文件锁 | `fcntl` 在 Windows 不可用（项目约定）；单进程运行场景下线程锁 `TASK_LOCK` 已够 |
| ConsoleBroker / terminal_print | 本项目无人值守回合的交互审批直接拒绝（`request_permission`），不存在多线程抢 stdin 的场景；唤醒线程打印交错问题由既有 `displaytool` 缓解 |
| system prompt 每轮全量重建 | 已用轻量替代：静态 SYSTEM + 每轮拼接记忆区块与动态区块（时间/MCP） |
| 成员线程接入重试 | 见 §2.4，错误回传 Lead 更符合团队协议 |
| compact 工具的 `focus` 参数 | 现有 compact 无参设计已够用，且压缩主线统一由 `active_request` 承担 |

---

## 7. 行为变化一览（用户视角）

1. **限流/过载不再打断回合**：429/529 自动退避重试并打印 `[RETRY:*]` 日志；连续过载自动切到 `FALLBACK_MODEL_ID`（需在 `.env` 配置，未配置则行为同前）。
2. **长回复不再戛然而止**：输出被 max_tokens 截断时自动升级上限重发，最多再注入 2 次续跑指令。
3. **模型可以挂载外部工具**：调用 `connect_mcp("docs" | "deploy")` 后，`mcp__docs__search` 等工具进入工具池；`deploy.trigger` 这类未放行工具在交互回合会请求确认，在无人值守回合会被拒绝。
4. **空闲时完成的后台命令不再滞留**：后台命令完成即唤醒一轮循环消化 `<task_notification>`。
5. cron 一次性任务在"截断恢复"路径下也能正常删除（确认时机前移的副作用修复）。

---

## 8. 验证记录

- 全部改动文件 `ast.parse` 语法通过；`import main` 冒烟通过（无网络请求、无线程启动）。
- 逻辑自测脚本（conda `LLM` 解释器，40 项断言，全部通过，测后已删除）：
  - `with_retry`：429 两次后成功 / 连续 529 切换备用模型后耗尽抛 RuntimeError / 非瞬时异常立即抛出；
  - `is_prompt_too_long_error`：7 组正反关键字判定；
  - MCP：连接、前缀合并、静态工具保留、策略表就地刷新（引用可见）、handler 绑定原始工具名、host 策略与默认 confirm、归一化撞名报错、`connected_servers` 名单；
  - 后台：`has_ready` 就绪/收割复位、通知格式、唤醒运行时启停幂等；
  - 权限：无人值守线程调用 confirm 类 MCP 工具被拒、allow 类放行；
  - 动态区块：时间与 MCP 名单；主工具池合并无重复、`compact` 无 handler（由主循环拦截）。
- 自测过程中发现并修复 1 个真实缺陷：`permission_hook` 初版只改了 import、漏加 MCP 分支（被第 8 组断言抓住）。

---

## 9. 遗留事项

- 建议在 `.env` 中配置 `FALLBACK_MODEL_ID`，否则连续过载时只退避不切换。
- 真实 MCP 传输接入点：替换 `tools/mcpmanager.py` 中 `MCPClient` 的 `register/call_tool` 内部为 stdio/HTTP 子进程实现，并更新 `MOCK_SERVERS` 为真实 server 清单；治理层不用动。
