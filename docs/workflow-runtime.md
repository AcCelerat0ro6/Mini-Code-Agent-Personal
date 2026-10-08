# 工作流运行时 (Workflow Runtime) 设计文档

> 记录 2026-10-08 从课程参考实现 s16（`s16_workflow_runtime/code.py`，约 930 行 POSIX 单文件）
> 并入本项目的机制：`tools/workflowmanager.py` 工作流运行时。
> 本文记录**设计思路、工作流程、原理、Windows 适配、示例工作流与异同对照**。
> 阅读「参考实现」类代码时优先对照 [s15-integrated-harness.md](s15-integrated-harness.md)
> 与 [s15-integration.md](s15-integration.md)。

---

## 1. 设计思路

### 1.1 为什么需要工作流运行时

主智能体的 `agent_loop` 是「模型驱动」的：每一步做什么由 LLM 即兴决定，多步协作全靠
自然语言 prompt 串联。这种模式灵活但**脆弱**——同一份 prompt 两次跑可能走出完全不同的
路径，中间步骤失败也无法精确复现。

工作流运行时把「多步 LLM 协作」从脆弱的自然语言 prompt 升级为**确定性 Python 代码**：
编排逻辑（先审计哪些维度、每个维度派几个子推理、如何对抗验证、如何排序汇总）写成纯
Python 脚本，LLM 只负责脚本里 `ctx.agent()` 派生的**单步聚焦推理**。这样：

- **可复现**：脚本逻辑固定，journal 缓存让 resume 精确回放已完成步骤；
- **可并发**：`parallel` / `pipeline` 原语把彼此独立的子推理并发起来，受信号量限额；
- **可结构化**：`agent({schema})` 强制子推理返回符合 JSON Schema 的结构，失败自动重试；
- **可预算**：`Budget` 追踪 token 消耗，超限即断，防止编排脚本无限烧钱。

### 1.2 与 task / team / cron / background 的边界

本项目已有四套「让智能体做更多事」的机制，workflow 是第五套，定位互不重叠：

| 机制 | 模块 | 定位 | 触发方 | 持久化 |
|------|------|------|--------|--------|
| **workflow** | `tools/workflowmanager.py` | 单次工具调用内的**确定性 Python 编排** | 模型调 `workflow` 工具 | `.workflows/`（快照/journal/产出） |
| task | `tools/subagent.py` | 子代理**自由探索**（全工具循环，返回凝练文本） | 模型调 `task` 工具 | 无（一次性） |
| team | `tools/teammanager.py` | 持久成员**协作**（独立线程 + 信箱 + 任务看板） | 模型调 `spawn_teammate` | `.mailboxes/` `.tasks/` `.worktrees/` |
| cron | `tools/cronmanager.py` | **定时触发**一轮智能体回合 | 时间轴轮询线程 | `.scheduled_tasks.json` |
| background | `tools/backgroundmanager.py` | **异步 bash**（不阻塞主循环，结果后续回收） | 模型调 `bash(run_in_background=True)` | 内存态就绪队列 |

一句话区分：**workflow 是「代码写死的编排」，task 是「模型自由发挥的子代理」**。
workflow 内部的 `ctx.agent()` 派生的是「聚焦单步推理」（不带工具、schema 约束），
与 `run_subagent` 的「全工具循环子代理」是两种不同粒度的东西。

### 1.3 与 s15 / s16 参考实现的关系

- s15（[s15-integrated-harness.md](s15-integrated-harness.md)）是整合 harness，提供
  错误恢复层、MCP 动态工具池、后台完成唤醒等基础设施，已于 2026-10-03 并入
  （见 [s15-integration.md](s15-integration.md)）。
- s16 在 s15 之上增加**工作流运行时**：通过 `install_workflow_tool(host)` 把 `Workflow`
  工具动态注入 s15 宿主的工具池。本项目不做 host monkey-patch，改用静态工具注册
  （`WORKFLOW_TOOLS` / `WORKFLOW_HANDLERS` 在 main.py 合并），见 §6 异同对照。

---

## 2. 工作流程

### 2.1 调用链

```
agent_loop (main.py)
  └─ execute_tool(block, tool_handlers)          # 同步分发
       └─ run_workflow_sync(**block.input)        # workflow 工具的同步回调
            └─ asyncio.run(run_workflow(...))     # 桥接到异步事件循环
                 └─ WorkflowTool().call(meta, script_fn, args, resume_from_run_id)
                      ├─ validate_meta / check_permission
                      ├─ reserve_run_id（O_CREAT|O_EXCL 原子占用）
                      ├─ workflow_run_lock(run_id)（线程级互斥）
                      └─ _call_locked
                           ├─ WorkflowJournal(run_id, resume)   # 截断重写 or 加载缓存
                           ├─ LocalWorkflowTask                  # 状态/用量/进度
                           ├─ _write_json 初次快照
                           ├─ ExecutionState(ctx) + script_fn(ctx, args)   # 跑编排脚本
                           │    └─ ctx.agent / ctx.parallel / ctx.pipeline / ctx.workflow
                           ├─ _write_json 产出 + 最终快照
                           └─ _save_last_run
```

`execute_tool` 以 `handler(**block.input)` 形式同步调用工具回调，因此 `run_workflow_sync`
内部用 `asyncio.run` 起一个全新事件循环跑异步编排——每次工具调用一个独立 loop，
Windows 安全（不依赖 Unix 信号/子进程语义）。

### 2.2 生命周期事件

编排脚本运行期间，`LocalWorkflowTask` 打印两类事件（统一 `[WORKFLOW:*]` 绿色前缀）：

```
[WORKFLOW:start]    review-changes runId=wf_review-changes_a1b2... resume=False
[WORKFLOW:event]    async_launched    runId=... taskId=local_workflow_...
[WORKFLOW:event]    task_started      workflow=review-changes phases=Review,Verify resume=False
[WORKFLOW:progress] workflow_phase    title=Review
[WORKFLOW:progress] workflow_agent    label=audit:correctness phase=Review status=done
[WORKFLOW:progress] workflow_agent    label=verify:correctness:... phase=Verify status=done
[WORKFLOW:progress] workflow_log      message=已确认 3 个有效问题
[WORKFLOW:done]     review-changes runId=... agents=12 tokens=4567
[WORKFLOW:event]    task_notification status=completed agents=12 tokens=4567 outputFile=.workflows/....output.json
```

失败时 `[WORKFLOW:done]` 换成 `[WORKFLOW:fail] ... error=...`，脚本异常被捕获后
任务标记 `failed`，产出文件写入 `{"error": "..."}`，工具回调仍返回正常 JSON
（模型据此判断失败原因，而非拿到一个裸异常）。

### 2.3 持久化文件布局

运行时状态统一落在工作区根目录 `.workflows/`（与 `.tasks/` `.mailboxes/` `.worktrees/`
同待遇，已加进 `.zcodeignore`）：

```
.workflows/
  wf_<name>_<16hex>.json           任务快照（runId/workflowName/args/task 状态）
  wf_<name>_<16hex>.journal.jsonl  执行日志（每行 {"key":..., "value":...}，断点续传的语义缓存）
  wf_<name>_<16hex>.output.json    最终产出（脚本返回值 or {"error":...}）
  wf_<name>_<16hex>.lock           （s16 用于 fcntl 跨进程锁；本项目仅线程锁，此文件不再产生）
  last_run.txt                     最近一次 runId（供编程式 resume）
```

快照与产出都用 `_write_json` 原子替换（先写 `.tmp` 再 `os.replace`），防止进程中途
被终止导致 JSON 损坏。

---

## 3. 原理

### 3.1 Journal 断点续传

journal 是 workflow 的核心容错机制。每个 `ctx.agent()` 调用生成一个**确定性语义 key**：

```python
basis = f"{kind}|{label}|{prompt}|{json.dumps(schema, sort_keys=True)}"
key = f"{kind}-{_stable_hash(basis) % 10**10:010d}"
```

- `_stable_hash` 用 sha256（**不是** Python 内建 `hash()`，后者每进程随机加盐，跨进程/resume 不一致）；
- key 只取决于「调用语义」（kind/label/prompt/schema），**与并发执行顺序无关**——
  即使 `parallel` 里多个 agent 的完成顺序每次不同，resume 时仍能精确匹配；
- 完成的结果 `journal.record(key, value)` 追加写入 `.journal.jsonl` 并 flush。

resume 时（`resume_from_run_id`）：
1. `WorkflowJournal(run_id, resume=True)` 加载历史 journal 进 `self.cache`；
2. 每个 `ctx.agent()` 先查 `journal.cached(key)`，命中（非 `MISS` 哨兵）则**直接返回缓存值，跳过 LLM**；
3. 命中缓存若有 schema，仍做一次 `SimpleJsonSchema.validate`（防止缓存被污染）；
4. 未命中的步骤照常调用 LLM，结果追加进同一份 journal。

效果：编排脚本跑到第 8 步崩了，修好脚本 bug 后 `workflow(name=..., resume_from_run_id=...)`
重跑，前 7 步秒回（journal 命中），只重跑第 8 步及之后——**省 token、省时间、可精确复现**。

### 3.2 Schema 结构化输出

`ctx.agent(prompt, schema=...)` 强制子推理返回符合 JSON Schema 的结构：

1. prompt 末尾追加 `Return only one JSON object matching this schema: <schema>`；
2. `AnthropicAgentRunner.run` 拿到文本后 `_parse_runner_json` 解析（容错 Markdown 代码块包裹、文本混杂 JSON）；
3. `SimpleJsonSchema(schema).validate(result)` 校验（支持 object/array/string/boolean/number/integer + required + enum）；
4. 校验失败**自动重试 1 次**（prompt 追加 `Return valid JSON.`）；仍失败抛 `WorkflowInputError`。

`SimpleJsonSchema` 是轻量级验证器（不依赖 jsonschema 库），覆盖编排脚本常用的结构约束。
`_fill_schema` 是 Mock 测试专用的确定性数据生成器（依 schema 自动填假数据）。

### 3.3 并发原语

`ExecutionState`（注入脚本的 `ctx`）暴露三个编排原语：

| 原语 | 语义 | 实现 |
|------|------|------|
| `ctx.parallel(thunks)` | **屏障并发**：并发跑所有异步函数，任一失败则整体失败 | `asyncio.gather(*[t() for t in thunks])` |
| `ctx.pipeline(items, *stages)` | **流水线并发**：每个 item 独立流转多个 stage，stage 间无全局屏障（A 在跑 stage3 时 B 可仍在 stage1） | 每个 item 一个 `run_item` 协程，内部顺序 `await stage(...)`，外层 `gather` |
| `ctx.workflow(name, args)` | **内联嵌套**：跑另一个预注册工作流，共享 journal/预算/并发计数，**最多嵌套 1 层** | 新建 `ExecutionState(depth+1, limits=同一份)` |

并发受 `ExecutionLimits.semaphore = asyncio.Semaphore(CONCURRENCY=8)` 限额；
`ctx.agent()` 把同步 LLM 调用用 `asyncio.to_thread` 丢到线程池，让多个 agent 真正并发。

### 3.4 预算与限额

- `Budget(total)`：追踪 token 消耗，`add(n)` 时若 `_spent + n > total` 抛异常；
  `total=None` 表示不限额（`remaining()` 返回 `inf`）。脚本通过 `args.get("budget")` 传入。
- `ExecutionLimits.agents`：`claim_agent()` 每次自增，超过 `AGENT_CAP=1000` 抛异常，
  防止编排脚本死循环无限派生子推理。
- `ExecutionLimits.semaphore`：`CONCURRENCY=8` 并发上限。

### 3.5 互斥锁

`workflow_run_lock(run_id)` 保证同一 `run_id` 同时只有一个实例在跑：

- `_run_locks: dict[str, threading.Lock]` 进程内登记表，`_run_locks_guard` 保护登记表本身；
- `acquire(blocking=False)` 非阻塞抢锁，抢不到直接抛 `WorkflowInputError`（而非死等）；
- 释放后若无人持有则从登记表清理，避免 `_run_locks` 随 runId 无限增长。

**Windows 适配**：s16 另用 `fcntl.flock` 做跨进程文件锁，但 `fcntl` 在 Windows 不可用
（项目约定），且本项目为单进程运行场景，参照 [s15-integration.md](s15-integration.md) §6
「单进程运行场景下线程锁已够」先例，此处**仅保留线程锁**。

---

## 4. Windows 适配说明

| s16 (POSIX) | 本项目 (Windows) | 理由 |
|-------------|------------------|------|
| `fcntl.flock` 跨进程文件锁 | 仅 `threading.Lock` 线程锁 | Windows 无 fcntl；单进程场景线程锁已够（s15-integration.md §6 先例） |
| `os.open(O_CREAT\|O_EXCL\|O_WRONLY, 0o600)` | 原样保留 | flags 跨平台语义一致；Windows 下 mode 参数被忽略但无害 |
| `os.replace(tmp, path)` 原子替换 | 原样保留 | 跨平台可用（Windows 上 replace 语义为覆盖） |
| `importlib.util` 动态加载 s15 host | **删除** | 项目用 main.py 静态合并工具池，不做 host monkey-patch |
| `select(stdin)` / readline CLI | **删除** | CLI 由 main.py 拥有；workflow 只是工具池里的一员 |
| 子进程相关 | 不涉及 | workflow 不派生子进程，只调 LLM API |

另：`AnthropicAgentRunner` 的模型调用接入 `recoverymanager.with_retry`（项目约定：
所有驱动大模型的调用点都要对 429/529 退避重试、连续过载切换备用模型），
s16 源码是裸 `client.messages.create`。

---

## 5. 示例工作流：review-changes

s16 自带一个 `review-changes` 示例（多维度审计 + 对抗验证流水线）。本项目**不内置注册**
（`WORKFLOWS` 初始为空），示例代码保留在此供复制使用。

### 5.1 完整代码

把下面代码存成一个模块（例如 `tools/workflows/review_changes.py`），在 main.py 里
`import` 该模块即可自动注册：

```python
"""示例工作流：多维度审查代码变更，并通过对抗验证确保问题属实。"""
import json
from tools.workflowmanager import register_workflow, WorkflowInputError

# 阶段 1 (Audit) 的结构化输出定义
FINDINGS_SCHEMA = {
    "type": "object", "required": ["findings"],
    "properties": {"findings": {"type": "array", "items": {
        "type": "object", "required": ["title", "severity"],
        "properties": {
            "title": {"type": "string"},
            "severity": {"type": "string", "enum": ["high", "medium", "low"]},
        }}}},
}
# 阶段 2 (Verify) 的裁决输出定义
VERDICT_SCHEMA = {
    "type": "object", "required": ["isReal", "reason"],
    "properties": {"isReal": {"type": "boolean"}, "reason": {"type": "string"}},
}

SAMPLE_META = {
    "name": "review-changes",
    "description": "多维度审查代码变更，并通过对抗验证确保问题属实",
    "phases": ["Review", "Verify"],
}

DIMENSIONS = ["correctness", "security", "performance", "style"]


async def sample_workflow(ctx, args):
    """按维度流水线流转 (audit -> verify-each)，利用对抗式 subagent 排除误报，
    最终按严重程度排序输出确认的问题。编排逻辑是纯 Python 代码，而非脆弱的自然语言 Prompt。
    """
    ctx.phase("Review")
    changes = args.get("changes", "")
    if not isinstance(changes, str):
        raise WorkflowInputError("args.changes 必须是字符串")
    review_input = changes.strip() or "No change context was supplied."

    # Stage 1: 审计审查
    async def audit(_value, dimension, _idx):
        out = await ctx.agent(
            f"Review this change context for {dimension} issues. "
            "Report only issues supported by the supplied text.\n\n"
            f"{review_input}",
            schema=FINDINGS_SCHEMA, label=f"audit:{dimension}", phase="Review")
        return {"dimension": dimension, "findings": out["findings"]}

    # Stage 2: 对抗验证 (每一个 Finding 都并发启动一个独立的子 Agent 进行质疑)
    async def verify(audited, dimension, _idx):
        ctx.phase("Verify")
        verdicts = await ctx.parallel([
            (lambda f=f: ctx.agent(
                f"Adversarially verify this {dimension} finding against the "
                "supplied change context.\n\n"
                f"Change context:\n{review_input}\n\n"
                f"Finding:\n{json.dumps(f, ensure_ascii=True)}",
                schema=VERDICT_SCHEMA, label=f"verify:{dimension}:{f['title']}",
                phase="Verify"))
            for f in audited["findings"]])
        # 仅保留被确认为真实有效的问题
        confirmed = [f for f, v in zip(audited["findings"], verdicts)
                     if v and v.get("isReal")]
        return {"dimension": dimension, "confirmed": confirmed}

    # 执行流水线编排
    results = await ctx.pipeline(DIMENSIONS, audit, verify)
    confirmed = [{"dimension": r["dimension"], **f}
                 for r in results if r for f in r["confirmed"]]
    # 按危害级别排序: high -> medium -> low
    confirmed.sort(key=lambda f: {"high": 0, "medium": 1, "low": 2}.get(f["severity"], 3))
    ctx.log(f"已确认 {len(confirmed)} 个有效问题")
    return {"confirmed": confirmed}


# 模块 import 时注册到全局注册表
register_workflow(SAMPLE_META, sample_workflow)
```

### 5.2 注册与调用

```python
# main.py 里 import 即自动注册（与 SKILL_LOADER 等单例同模式）
import tools.workflows.review_changes  # noqa: F401  触发 register_workflow
```

模型随后即可调用：

```json
{"name": "workflow", "input": {"name": "review-changes", "args": {"changes": "def load_user(...)..."}}}
```

断点续传（上次跑到一半崩了）：

```json
{"name": "workflow", "input": {"name": "review-changes", "resume_from_run_id": "wf_review-changes_a1b2c3d4e5f6a7b8"}}
```

### 5.3 无网络冒烟测试

`MockAgentRunner` 对 `review-changes` 的 `findings` / `isReal` 结构做了语义 Mock，
可在不调真实 LLM 的情况下跑通完整 lifecycle：

```python
import tools.workflowmanager as wm
wm.RUNNER_FACTORY = wm.MockAgentRunner   # 猴补丁
print(wm.run_workflow_sync(name="review-changes", args={"changes": "x = 1"}))
```

---

## 6. 与 s16 参考实现的异同对照

| 项 | s16 源码 | 本项目适配 | 理由 |
|---|---|---|---|
| 跨进程文件锁 | `fcntl.flock` | 仅线程锁 `_run_locks` dict | Windows 无 fcntl；s15-integration.md §6 先例 |
| 工具名 | `Workflow`（大写） | `workflow`（小写） | 项目工具名全小写：task/bash/read/connect_mcp/schedule_cron |
| STORE 路径 | `Path(__file__).parent / ".runtime"` | `WORKDIR / ".workflows"` | 运行时状态统一放工作区根目录点前缀 |
| LLM 调用 | 裸 `client.messages.create` | `with_retry(lambda: ..., RecoveryState())` | 项目约定：所有模型调用接入 recoverymanager 容错层 |
| 终端日志 | 无色 `print(f"  event ...")` | `[WORKFLOW:*]` 绿色 `\033[32m` | 项目 `[前缀:*]` 品牌前缀 + ANSI 颜色约定；前缀只用 ASCII |
| 工具注册 | `install_workflow_tool(host)` monkey-patch | `WORKFLOW_TOOLS` list + `WORKFLOW_HANDLERS` dict，main.py 静态合并 | 项目工具注册约定 |
| 全局单例 | `WORKFLOWS` dict | 保持 `WORKFLOWS`，新增 `register_workflow()` API | 项目全局单例大写命名（TASKS/CRON/BACKGROUND/BUS） |
| 示例工作流 | 内置注册 `review-changes` | 不注册，代码与注册方式写进本文档 | 只保留注册表，示例移到 docs |
| 权限检查 | `check_permission(meta, settings)` 读 settings.deny | 模块级 `WORKFLOW_DENY: set[str]` | 简化；工具级权限走 hooks/pretoolusehook.permission_hook |
| Runner 默认 | `MockAgentRunner`（demo 用） | `lambda: AnthropicAgentRunner(client, MODEL)` | workflow 工具由真实 agent 调用；Mock 保留供测试猴补丁 |
| 子代理可见性 | `INHERITS_TOOLS_FROM = "s15"` | 不加进 `SUB_TOOLS` | 与 `TASK_TOOL` parent-only 同语义，防止嵌套递归 |
| CLI | `run_demo` / `run_cli` / readline prompt | **删除** | CLI 由 main.py 拥有 |
| host 动态加载 | `load_integrated_host` importlib | **删除** | 静态工具注册，无需动态加载 host |
| 模块头部 | 简单 docstring | ASCII 架构图 + 关键入口清单 | 项目模块约定 |
| 分节 | `# -- xxx --` 短横线 | `# ====...====` 78 字符横幅 | 项目分节约定 |

**刻意不并入的部分及理由**（参照 s15-integration.md §6 体例）：

| s16 机制 | 不并入理由 |
|----------|-----------|
| `install_workflow_tool` / `load_integrated_host` | 项目用 main.py 静态合并 `BASE_TOOL_POOL`，不做 host monkey-patch |
| `run_demo` / `run_cli` / `PROMPT` / `READLINE_PROMPT` | CLI 由 main.py 拥有；workflow 只是工具池一员 |
| `SAMPLE_META` / `sample_workflow` 内置注册 | 示例移到本文档，`WORKFLOWS` 初始为空，由用户按需 `register_workflow` |
| `.runtime/` 目录名 | 改用 `.workflows/`，与 `.tasks/` `.mailboxes/` 命名风格一致 |

---

## 7. 遗留事项 / 未来扩展点

- **跨进程锁**：若未来需要多进程并发跑同一 workflow（例如 cron 触发的进程与主进程撞车），
  可用 `msvcrt.locking`（Windows）/ `fcntl.flock`（POSIX）做平台分支补回跨进程文件锁；
  当前单进程场景线程锁已够。
- **workflow 任务进 task_system 看板**：当前 `LocalWorkflowTask` 是 workflow 私有的轻量任务，
  不写 `.tasks/`。若希望 workflow 运行也能在任务看板里被 `list_tasks` 查到、被团队成员认领，
  可在 `_call_locked` 里调 `task_system.create_task` 登记一个 `local_workflow` 类型节点。
- **真实 MCP 传输接入后 agent() 的演化**：当前 `ctx.agent()` 是「聚焦单步推理」（不带工具）。
  若未来希望 workflow 子推理也能调 MCP 工具，可让 `AnthropicAgentRunner` 接受一个工具池参数，
  或改为复用 `run_subagent`（但会失去 schema 结构化输出与 token 精确计量）。
- **自动发现机制**：当前 `register_workflow` 需用户手动在 main.py import 触发。
  若工作流数量增多，可加一个 `tools/workflows/` 包扫描（importlib 遍历该包下所有模块
  并触发其 `register_workflow` 调用），与 `SKILL_LOADER.catalog()` 同模式。
- **WORKFLOW_DENY 接入 host 配置**：当前是模块级空集合，后续可从 `.env` 或配置文件读取黑名单。
