# Agent Team（智能体团队协作模块）设计文档

> 对应实现：`tools/teammanager.py`（核心）+ `tools/task_system.py` / `tools/tools.py` /
> `hooks/pretoolusehook.py` / `main.py` 的配套增强。
>
> 本文阐述该模块的设计思路、总体架构、数据结构与各个工作流程的完成原理。

---

## 目录

1. [概述](#1-概述)
2. [总体架构](#2-总体架构)
3. [设计思路](#3-设计思路)
4. [数据结构](#4-数据结构)
5. [工作流程详解](#5-工作流程详解)
6. [并发模型与线程安全](#6-并发模型与线程安全)
7. [安全模型](#7-安全模型)
8. [工具清单](#8-工具清单)
9. [与参考实现的差异](#9-与参考实现的差异)
10. [端到端示例](#10-端到端示例)
11. [边界与注意事项](#11-边界与注意事项)

---

## 1. 概述

Agent Team 让主智能体（下称 **Lead**）能够把一个大型任务拆解后，交给多个**常驻的
teammate 线程**并行完成。每个成员：

- 拥有**独立的消息历史**与自己的小型 Agent 循环（模型推理 + 工具执行）；
- 通过**共享任务看板**（`.tasks/`，任务系统模块的既有设施）认领和完成任务；
- 通过**文件信箱**（`.mailboxes/`）与 Lead 及其他成员双向通信；
- 可选绑定一个**任务专属的 Git 工作树**（`.worktrees/`），让文件操作落进隔离目录。

与既有 `task` 工具（`tools/subagent.py`，一次性子代理）的分工：

| 维度 | task 子代理 | Agent Team 成员 |
|------|------------|-----------------|
| 生命周期 | 单次派发，返回即销毁 | 常驻线程，跨多轮复用 |
| 通信方式 | 只返回最终文本 | 信箱双向消息 + 事件唤醒 |
| 任务来源 | 调用方在 prompt 里写死 | 自主认领任务板上就绪的任务 |
| 并行度 | 主循环内同步阻塞 | 多线程真并行 |
| 适用场景 | 聚焦探索、自包含子任务 | 多角色长任务分工协作 |

---

## 2. 总体架构

### 2.1 架构图

```
    +------+  spawn_teammate  +-----------+  result/idle   +------+
    | Lead | ---------------> |   WORK    | -------------> | IDLE |
    +--+---+                  +-----+-----+                +--+---+
       ^                            |                         |
       | 团队事件（唤醒投递）          | 工具                    | 等待新任务
       |                            v                         v
    +--+-------------+        +-----------+           +----------+
    | MessageBus     |        | 任务看板   | <------- | Mailbox  |
    | .mailboxes/    |        | .tasks/   |  claim    +----------+
    +----------------+        +-----------+
```

成员的生命周期是一个 **WORK / IDLE 双阶段状态机**：

```
                 spawn_teammate
                       |
                       v
                  +--------+   无工具调用且计划门不阻塞   +--------+
     +----------> |  WORK  | --------------------------> |  IDLE  |
     |            +--------+                             +--------+
     |                |  shutdown_request（有效）            |  信箱消息 / 自动认领到任务
     |                v                                    |
     |            +--------+                               |
     +------------| (循环) | <-----------------------------+
     |            +--------+
     |                |
     +----------------+   （收到有效关闭请求或异常时退出线程）
```

### 2.2 模块关系

```
main.py                   Lead 的 Agent 循环；注册 TEAM_TOOLS/TEAM_HANDLERS；
  |                        注入 run_team_wake_turn 回调与 agent_lock
  +-- tools/teammanager.py  本模块全部核心逻辑
        +-- tools/task_system.py   任务看板（TaskStore 原子认领/完成 + worktree 字段）
        +-- tools/tools.py         基础工具（cwd 参数化后供成员绑定工作目录）
        +-- hooks/hook.py          PreToolUse/PostToolUse 钩子照常生效
        +-- client/client.py       Anthropic 客户端（成员线程共用）
```

本模块**不重复实现**任务系统、基础工具、钩子与权限：参考实现（s13 Agent Teams）
里这些部分在项目中已有同源实现，整合时全部改为复用，仅在必要处做了小幅增强
（详见 [第 9 节](#9-与参考实现的差异)）。

### 2.3 磁盘布局

```
<WORKDIR>/
  .tasks/                    任务看板（任务系统模块所有，本模块共用）
    task_a1b2c3d4.json       {status, owner, blockedBy, worktree, ...}
  .mailboxes/                信箱目录（本模块所有）
    lead.jsonl               Lead 的收件箱：成员的 result/idle/error/协议响应
    <name>.jsonl             每个成员一个收件箱，读取即清空
  .worktrees/                任务绑定工作树（本模块所有，需 Git 仓库）
    <name>/                  每个工作树挂在独立分支 wt/<name> 上
```

三个目录都以「文件即状态」的方式存在：任务、消息、工作树全部落盘，
中途崩溃后重启进程，任务看板与工作树注册表依然有效（信箱是易逝的会话态）。

---

## 3. 设计思路

### 3.1 为什么是「常驻线程」而不是「一次性子代理」

一次性子代理（`task` 工具）的问题在于：每次派发都要重新建立上下文、无法中途
沟通、无法并行。常驻成员把「派发」从一次函数调用变成一段持续关系：

- **并行**：多个成员线程同时跑各自的 Agent 循环，互不阻塞；
- **反馈**：成员的结果、报错、空闲状态都以事件形式回流给 Lead；
- **复用**：成员干完一件事后转 IDLE，可以接新消息或自动认领下一个任务，
  省去反复孵化/销毁的开销；
- **可控**：Lead 能随时下发指令（send_message）、要求先出计划（request_plan）、
  或优雅叫停（request_shutdown）。

### 3.2 为什么用「文件信箱」而不是内存队列

- **与项目约定一致**：任务系统、cron 持久化、记忆目录全部是「工作区内的文件」，
  信箱延续这一风格，用户可以直接用编辑器查看 `.mailboxes/*.jsonl` 调试协作过程；
- **跨线程解耦**：写方 `send` 是追加一行 JSON，读方 `read_inbox` 一次性取走并
  删除文件（**破坏性读**），两侧不需要同时在场；
- **天然持久**：进程崩溃时未读消息仍在磁盘上（代价是重启后这些消息不会被
  重新投递——见[边界与注意事项](#11-边界与注意事项)）；
- **破坏性读的取舍**：如果用「读后保留」语义，Lead 与成员必须各自维护游标去重；
  破坏性读把去重问题消灭在存储层，代价是「读完即焚」，这与「事件」的语义匹配
  （事件错过了就错过了，任务状态永远以看板为准）。

### 3.3 为什么是「事件驱动唤醒」而不是「轮询」

参考实现里 Lead 在 CLI 层用 `select.select` 轮询 stdin + 信箱；这在 Windows 上
不可用，且与本项目「cron 空闲投递」的既有模式重复。整合后改为：

- 一个 `team-wake` 守护线程每 0.2s 检查 Lead 信箱（与 cron 队列处理器同节奏）；
- 信箱非空且 `agent_lock` 可获取（智能体空闲）时，调用 main 注入的
  `run_team_wake_turn` 唤起一轮 Agent 循环；
- 智能体正忙时事件留在信箱里排队，**绝不打断进行中的回合**。

于是 Lead 的行为准则变成系统提示词里的一句话：「孵化成员后结束当前回合，
运行时会把团队事件投递回来唤醒你」。这同时杜绝了模型空转轮询 `list_teammates`
的 token 浪费。

### 3.4 单任务租约与工作目录绑定

`teammate_assignments: dict[name, {"task_id", "cwd"}]` 是成员的**租约登记表**：

- **单任务原则**：成员认领新任务前，先查租约表与看板上的 `in_progress` 任务，
  手上有活就拒绝再认领。这防止成员同时改两处文件造成逻辑混乱；
- **cwd 绑定**：认领成功时把任务的工具默认目录（`WORKDIR` 或工作树路径）写进
  租约；成员的全部文件类工具（bash/read/write/edit/glob）在执行前都要经过
  `assignment_cwd()` 解析目录。这样「任务在哪个目录做」由看板数据唯一决定，
  而不是靠模型在命令行里手动 `cd`；
- **租约失效保护**：`assignment_cwd()` 每次都重新校验任务的归属与状态，
  租约登记与看板不一致（如任务被外部改动）时直接报错拒绝执行——宁可让成员
  停手，也不让它在错误的目录里写文件。

### 3.5 计划门（Plan Gate）：写操作的前置审批

成员对工作区的**变更类**操作（bash / write_file / edit_file）受计划门约束：

```
not_required ──request_plan──> required ──submit_plan──> pending
     ^                                    │                  │
     │                          review_plan(approve)         │ review_plan(reject)
     │                                     │                  │
     └──── release（回合结束/指派变更）<── approved         rejected ───┐
                                           │                          │
                                           └──── submit_plan（改后重提）<┘
```

- 门状态不是 `approved` 时，`_run_teammate_tool` 直接拦截变更类工具并返回提示，
  只读工具（read_file / glob / list_tasks）不受限——成员始终可以先调研再提交计划；
- **请求配对**：每条计划请求携带 `request_id`，审批响应必须 ID、双方、
  `plan_request_ids` 登记完全一致才生效；
- **版本失效**：`assignment_versions` 记录每个成员的工作版本号，任务指派一旦
  变更就递增并作废旧请求。成员拿着「上一个任务」的旧审批来放行「新任务」的
  写操作是不可能的——旧响应在 `apply_plan_response` 的多重校验中被丢弃；
- 无人值守场景下成员的危险 bash 会被权限钩子拒绝（见[第 7 节](#7-安全模型)），
  计划门则把「结构性风险」（改什么、怎么改）交给 Lead 把关，两层防线互补。

### 3.6 工作树隔离：并发改文件的保险方案

多个成员同时改同一份代码必然冲突。Agent Team 不做合并算法，而是把冲突
**在目录层面消灭**：`create_worktree` 用 `git worktree add -b wt/<name>` 给任务
开一个独立检出 + 独立分支，成员的文件工具默认 cwd 落在那里，互不可见。

三个刻意的取舍：

- **不是沙箱**：工作树只是「工具默认目录」，路径校验仍以工作区为根，成员
  理论上仍能写到别处——系统提示词会明确告知模型这一点；
- **分支永不自动删除**：`remove_worktree` 只移除检出目录，`wt/<name>` 分支
  一律保留，成果合并（merge）由 Lead 或用户在主工作区手动完成；
- **部分失败透明化**：`git worktree add` 中途失败时逐项排查遗留的检出目录、
  Git 注册表条目、分支，如实回报「哪些产物留下了、如何手动清理」，
  绝不静默删除任何 Git 数据。

### 3.7 复用既有设施，而不是另起炉灶

参考实现自带一套任务系统、基础工具、钩子和 CLI 主循环。整合时逐一对照
项目现状：任务系统（`tools/task_system.py`）功能等价且更完善（依赖环检测、
渲染折叠），直接复用，只补了两处缺口——**并发锁**（成员线程要并发认领）与
**worktree 字段**；基础工具、钩子、`execute_tool` 全部原样复用，成员的协议
动作（submit_plan 等）不注册进 Lead 工具集，物理隔开两套角色。新增代码只保留
真正的增量：信箱、成员运行时、协议、工作树、唤醒投递。

---

## 4. 数据结构

### 4.1 任务节点（`tools/task_system.py.Task`，本模块扩展）

```python
@dataclass
class Task:
    id: str                  # task_ + 8 位十六进制随机串
    subject: str             # 标题
    description: str         # 详情
    status: str              # pending | in_progress | completed
    owner: str | None        # 认领者：Lead/子代理是 "agent"，成员用各自名字
    blockedBy: list[str]     # 前置依赖（DAG）
    worktree: str | None     # [新增] 绑定的 Git 工作树名，None 表示在工作区根作业
```

`worktree` 字段带默认值 `None`，旧的 `.tasks/*.json` 无需迁移即可被读取。
绑定工作树的任务**禁止 Lead（owner="agent"）认领**——Lead 固定在工作区根
作业，落不到工作树目录里，这类任务应派给成员（见 5.7）。

### 4.2 信箱消息格式（`.mailboxes/<name>.jsonl` 每行一条）

```json
{
  "from": "alice",            // 发送方
  "to": "lead",               // 接收方
  "content": "...",           // 正文
  "type": "message",          // message | result | idle_notification | error
                              // | plan_request | plan_approval_request
                              // | plan_approval_response | shutdown_request
                              // | shutdown_response
  "ts": 1759380000.0,         // Unix 时间戳
  "metadata": {"request_id": "req_000123", "approve": true}
}
```

### 4.3 协议请求（`ProtocolState`）

```python
@dataclass
class ProtocolState:
    request_id: str            # req_ + 6 位数字
    type: str                  # shutdown | plan_approval
    sender: str                # 发起方（计划是成员，关闭是 Lead）
    target: str                # 接收方
    status: str                # pending | approved | rejected
    payload: str               # 计划正文（关闭请求为空串）
    work_version: int | None   # 提交时的工作版本号（指派变更后作废依据）
    task_id: str | None        # 提交时绑定的任务 ID
    created_at: float
```

### 4.4 团队状态登记表（全部由 `team_lock` 保护）

| 登记表 | 键 -> 值 | 用途 |
|--------|----------|------|
| `active_teammates` | name -> `working / waiting_approval / idle / stopping` | 成员运行状态 |
| `plan_gates` | name -> `not_required / required / pending / approved / rejected` | 计划门状态机 |
| `plan_request_ids` | name -> request_id | 当前待审批的计划请求 |
| `pending_requests` | request_id -> ProtocolState | 协议请求跟踪 |
| `teammate_assignments` | name -> `{"task_id", "cwd"}` | 单任务租约 + cwd 绑定 |
| `assignment_versions` | name -> int | 工作版本号（审批失效依据） |
| `teammate_threads` | name -> Thread | 线程登记（调试用） |

---

## 5. 工作流程详解

### 5.1 成员孵化流程（`spawn_teammate_thread`）

```
Lead 调用 spawn_teammate(name, role, prompt, task_id?, require_plan?)
   |
   v
[1] 名字校验：格式 1-64 位 [A-Za-z0-9_-]，不得占用保留名 lead/agent，
    与活跃成员 casefold 去重比较
   |
   v
[2] 登记预写：active_teammates[name]="working"、
    plan_gates[name]="required"/"not_required"、assignment_versions[name]=0
   |
   v
[3] （可选）认领初始任务 task_id：
    task_system.claim_task 原子认领（校验 pending + 依赖就绪）
    -> task_worktree_cwd 解析工作目录
    -> 写入租约 teammate_assignments[name] = {"task_id", "cwd"}
    任何一步失败：回滚 [2] 的登记（认领成功但绑定失败的还要把任务放回看板）
   |
   v
[4] 构造 TeammateRuntime（注入角色、初始指令、[Assigned task]/[Plan required] 标注）
    -> 启动 daemon 线程 teammate-<name>
   |
   v
返回给 Lead："End this turn; the runtime will deliver its events."
```

原理要点：

- **先登记后起线程**：登记表预写保证线程启动瞬间状态就已一致；后续任一步
  失败都逐项回滚，不会留下「登记在案但线程不存在」的幽灵成员；
- **初始任务在孵化线程里认领而非成员自己认领**：避免「线程已启动但任务被
  别人抢走」的窗口，成员醒来时任务已绑定在租约上；
- **daemon 线程**：主进程退出时成员随之消亡，未完成任务由下一次启动的
  空闲成员自动认领补做（任务在看板上仍是 pending）。

### 5.2 成员 WORK 阶段（`TeammateRuntime.work()`）

```
work():
  [1] handle_inbox(BUS.read_inbox(name))      # 收信；有效关闭请求 -> return "stop"
  [2] active_teammates[name] = "working"
  [3] client.messages.create(system=成员专属提示词, tools=TEAMMATE_TOOLS, ...)
      | 异常 -> BUS.send(name -> lead, 错误, "error") -> return "stop"
  [4] 追加 assistant 消息；提取 tool_use 块
      | 有工具调用:
  [5]     逐个 _run_teammate_tool(name, block, handlers):
            a. 计划门检查：门不在 {not_required, approved} 时
               拦截 bash/write_file/edit_file
            b. 剥掉 run_in_background（成员线程无法收割后台结果，强制同步）
            c. execute_tool(block, handlers)：PreToolUse 钩子 -> handler -> PostToolUse 钩子
          把 tool_result 打包为 user 消息 -> return "continue"（下一轮 work() 消化）
      | 无工具调用（本回合收尾）:
  [6]     summary = 最后一段文本；gate = plan_gates[name]
          gate != "pending" 且 summary 非空 -> BUS.send(name -> lead, summary, "result")
          gate == "pending"  -> active_teammates[name]="waiting_approval"
                                （挂起等审批，不发 result/idle，不放租约）
          否则               -> release_completed_assignment(name)（释放 cwd 租约）
                                active_teammates[name]="idle"
                                BUS.send(name -> lead, "Waiting for more work.",
                                         "idle_notification")
          -> return "idle"
```

原理要点：

- **成员的文件工具全部绑定租约 cwd**：`handlers` 里 bash/read/write/edit/glob
  都先经 `current_cwd()` 解析目录再调用 `tools.py` 的同名工具（本模块整合时给
  这些工具加了可选 `cwd` 参数）；没有租约时一律拒绝并提示先认领任务；
- **收尾判定即「无工具调用」**：与主循环相同的终止约定；成员的系统提示词要求
  它完成任务后调 `complete_task` 再给出简短总结，所以 `result` 事件到达 Lead 时
  看板状态已经同步更新；
- **waiting_approval 挂起**：计划待审时成员既不发 result 也不发 idle 事件，
  Lead 不会为一个「等审批」的成员做无谓的唤醒回合。

### 5.3 成员 IDLE 阶段与空闲任务认领（`wait_for_work()` / `claim_next_task()`）

```
wait_for_work():
  loop:
    inbox = BUS.wait_for_messages(name, IDLE_SCAN_INTERVAL=2.0)   # 阻塞等待
      | 有消息: handle_inbox()
      |         有效关闭请求 -> return False（线程退出）
      |         注入了工作消息 -> return True（回到 WORK）
    无消息（2s 空窗）:
      task = claim_next_task(name):
        [a] 租约表或看板上已有任务 -> None（绝不二次认领）
        [b] scan_unclaimed_tasks(): 遍历看板，筛出
            pending + 无主 + 依赖就绪(can_start) + 工作目录可用 的任务
        [c] 按序逐个尝试 claim_task_as_teammate（原子），抢到即返回
      认领成功 -> 注入 "[Auto-claimed task ...]" 用户消息 -> return True
      无任务可领 -> 继续循环等待
```

原理要点：

- **2 秒双重职责**：`IDLE_SCAN_INTERVAL` 既是信箱阻塞等待的超时（Condition
  挂起，不耗 CPU），也是任务看板的扫描节拍——空窗期每 2 秒看一眼有没有
  新活，有消息则立即被 Condition 唤醒；
- **认领是「尝试」不是「预订」**：多个成员同时空转时，同一任务只有一次
  `claim_task` 能成功（TASK_LOCK 内的读-校验-写序列），其余成员自动尝试
  看板上的下一个任务，不需要任何分布式协调；
- **工作目录不可用的任务对成员隐形**：`scan_unclaimed_tasks` 会用
  `task_worktree_cwd` 预检，绑定损坏（如工作树被手动删除）的任务不会被认领，
  也不会因认领失败在日志里刷错误。

### 5.4 计划审批协议

```
Lead                          运行时                          成员
 |                               |                             |
 | request_plan(teammate, task)  |  置 plan_gates[t]="required" |
 |------------------------------>|  send(lead->t, task,        |
 |                               |       "plan_request")       |
 |                               |---------------------------->|
 |                               |            handle_inbox 注入 "[Plan required]"
 |                               |            （WORK 循环里模型读到此标注后调 submit_plan）
 |                               |                             |
 |                               |      <--------------------__| submit_plan(plan)
 |                               |  new_request_id() 登记 ProtocolState
 |                               |  plan_gates[t]="pending"
 |                               |  send(t->lead, plan, "plan_approval_request",
 |                               |       {request_id})
 |    [Team events] 唤醒投递      |                            |
 |<------------------------------|                            |
 | Lead 调用 review_plan(request_id, approve, feedback)         |
 |------------------------------>|  校验：请求仍 pending、      |
 |                               |  work_version/task_id 未变、 |
 |                               |  是当前计划                  |
 |                               |  state.status = approved/rejected
 |                               |  send(lead->t, 反馈, "plan_approval_response",
 |                               |       {request_id, approve})
 |                               |---------------------------->|
 |                               |            apply_plan_response 多重校验：
 |                               |            from=lead, to=自己, request_id=当前待审 ID,
 |                               |            work_version/task_id 匹配，approve 与状态一致
 |                               |            通过 -> plan_gates[t]=approved/rejected
 |                               |                    写工具放行，成员继续 WORK
```

原理要点：

- **双向校验**：Lead 侧 `run_review_plan` 防止「对过期计划放行」（请求已被
  新指派作废、或不是当前待审计划时拒绝执行）；成员侧 `apply_plan_response`
  防止「错配响应误放行」（伪造的 request_id、旧任务的审批、approve 位与
  登记状态不符一律丢弃并注入 `[Ignored plan response]` 提示）；
- **work_version 失效机制**：`advance_assignment_version` 在每次租约变更时
  递增版本号——即使协议消息被复制、延迟、重放，携带旧版本号的响应也无法
  通过成员侧校验，审批永远只对「当前任务的当前计划」有效；
- **驳回即循环**：rejected 状态下写工具仍然被拦，成员必须修订计划重新
  `submit_plan`，形成「提交-审批-修订」的受控迭代。

### 5.5 关闭协议

```
Lead                              成员
 |  request_shutdown(teammate)      |
 |--------------------------------->|  shutdown_request {request_id}
 |                                  |  apply_shutdown_request 校验：
 |                                  |    from=lead、仍 pending、自己未在 stopping
 |                                  |  -> active_teammates[t]="stopping"
 |                                  |  -> BUS.send(t->lead, "Shutdown acknowledged.",
 |                                  |               "shutdown_response", {request_id})
 |                                  |  -> work() 返回 "stop"
 |  [Team events] 收到 shutdown_response（match_response 把
 |   pending_requests[request_id] 置为 approved）
 |                                  |  run() 的 finally：
 |                                  |    release_teammate_assignment（未完成任务放回看板）
 |                                  |    摘除全部登记表条目
 |                                  |    print "[TEAM:finish] <name> finished"
```

原理要点：

- **优雅停机**：请求文本明确「finish the current step」，成员在一个工作单元
  结束的边界检查信箱（WORK 每轮开头 / IDLE 唤醒时），不会在工具执行中途被打断；
- **任务不丢**：线程退出时若任务仍是 `in_progress`，`release_teammate_assignment`
  把它重置为 pending、无主，回到看板等别的成员（或下次启动的成员）认领；
- **响应即回执**：Lead 收到 `shutdown_response` 后，`match_response` 把对应
  请求置为 approved，`pending_requests` 里不残留悬挂状态。

### 5.6 团队事件与 Lead 唤醒投递

```
成员线程                            信箱                team-wake 线程            Lead 回合
   |  BUS.send(name -> lead, ...)     |                      |                      |
   |--------------------------------->|  追加 JSONL 行         |                      |
   |                                  |  Condition.notify_all |                     |
   |                                  |--------------------->| 每 0.2s: peek("lead")|
   |                                  |                      |  无信 -> 继续          |
   |                                  |                      |  有信 -> agent_lock    |
   |                                  |                      |  忙  -> 排队等下轮      |
   |                                  |                      |  闲  -> run_team_wake_turn()
   |                                  |                      |    [1] collect_team_events():
   |                                  |                      |        consume_lead_inbox()
   |                                  |                      |        （协议响应先经
   |                                  |                      |          match_response 校验）
   |                                  |                      |    [2] 事件文本追加进 history
   |                                  |                      |    [3] agent_loop(history, events)
   |                                  |                      |        （Lead 消化事件/审批/派活）
   |                                  |                      |    [4] 打印 Lead 回复
```

原理要点：

- **与 cron 空闲投递同一套约定**：`agent_lock` 互斥保证同一时刻只有一个回合
  在驱动模型；投递线程只负责「时机」，不碰对话历史——历史操作全部发生在
  持锁的投递回合内；
- **先落历史再进循环**：事件文本先 `history.append` 再调 `agent_loop`，即使
  模型调用失败，事件也已留在历史里不会被静默丢弃（下一轮输入时模型自然看到）；
- **事件文本兼作压缩主线**：`agent_loop(history, events)` 把事件文本传为
  `active_request`，上下文压缩时会优先保留这轮的任务主线；
- **协议状态先行**：`consume_lead_inbox` 在格式化前先跑 `match_response`，
  所以 Lead 的模型看到事件时，`pending_requests` 里的状态已经更新，
  `review_plan` 读到的是最新状态。

### 5.7 工作树生命周期

```
[创建] create_worktree(name, task_id)
   1. 名字校验（1-64 位，禁 ".."，需字母/数字开头）
   2. 任务校验：必须 pending 且无主、未绑定过其他工作树、工作树名未被别的任务占用
   3. 环境校验：WORKDIR 必须是 Git 仓库根；分支 wt/<name> 合法且不存在；
      目标路径 .worktrees/<name> 不存在且未在 Git 注册表中
   4. git worktree add -b wt/<name> .worktrees/<name> HEAD
      失败 -> 逐项排查遗留产物（检出目录/注册表条目/分支），如实回报部分失败
   5. task.worktree = name 写回看板；绑定失败则保留 Git 产物回报部分成功
   [此后：绑定工作树的任务对 Lead 认领关闭，仅成员可认领；成员的文件工具 cwd 落进工作树]

[移除] remove_worktree(name, discard_changes=False)      （未暴露为工具，属宿主/用户操作）
   前置：工作树已在 Git 注册且挂在预期分支；绑定的任务全部 completed；
         没有成员的 cwd 租约还指向它；无未提交变更（除非显式 discard）
   git worktree remove [--force]；解除任务绑定（worktree=None）
   分支 wt/<name> 一律保留 —— 成果的合并由 Lead/用户在主工作区手动完成
```

### 5.8 成员退出与现场回收（`TeammateRuntime.run()` 的 finally）

无论正常退出、模型调用失败还是未捕获异常：

1. `release_teammate_assignment(name)`：`in_progress` 的任务重置为
   pending + 无主（回看板），摘除租约、递增版本号、计划门复位；
2. 登记表清理：`active_teammates` / `plan_gates` / `plan_request_ids` /
   `teammate_threads` 逐项摘除；
3. 打印 `[TEAM:finish] <name> finished`。

这样「成员死亡」永远是一个干净的中间状态：看板回到任务可被重新认领的样子，
不会留下孤儿租约或永远 waiting 的计划请求。

---

## 6. 并发模型与线程安全

### 6.1 线程清单

| 线程 | 职责 | 来源 |
|------|------|------|
| 主线程 | CLI 交互 + Lead 的 Agent 循环 | main.py |
| teammate-\<name\>（daemon） | 每个成员一个，WORK/IDLE 循环 | teammanager |
| team-wake（daemon） | Lead 信箱监视 + 空闲唤醒投递 | teammanager |
| cron-scheduler / cron-queue-processor | 定时任务调度（既有） | cronmanager |

### 6.2 两把锁与加锁顺序

- **`team_lock`**（RLock，teammanager）：保护全部团队登记表
  （4.4 节的七张表）；
- **`TASK_LOCK`**（RLock，task_system）：保护 `.tasks/` 任务文件的
  「读取-校验-写回」序列（create/save/load/update/list/claim/complete 全部内含）。

全模块只允许一个加锁方向：**先 `team_lock` 后 `TASK_LOCK`**。所有「查租约 ->
认领 -> 绑定」的复合操作都在外层持 `team_lock`，内层调用 task_system 的原子
原语；task_system 从不反向依赖 teammanager，因此不存在死锁环。
两把锁都用 RLock 是因为复合操作内部会嵌套调用同锁函数（如 claim 包装器内
再调 `load_task`）。

### 6.3 原子性关键点

- **任务双认领防护**：`claim_task` 的「读状态 -> 校验依赖 -> 写 owner/status」
  整体在 `TASK_LOCK` 内，多个成员同时认领同一任务只有一人成功；
- **唤醒投递不重不漏**：`team-wake` 线程先 `peek`（不消费）再拿 `agent_lock`，
  拿锁后二次 `peek` 确认——事件消费只发生在持锁的投递回合里，
  与用户回合、cron 回合天然互斥；
- **成员收尾互斥**：成员线程的登记表清理在 `team_lock` 内完成，
  与 Lead 侧的 `list_teammates` / `spawn_teammate` 等查询互斥。

---

## 7. 安全模型

1. **权限钩子全量生效**：成员的工具执行走 `execute_tool`，PreToolUse 钩子
   （危险命令黑名单 + 工作区路径校验）照常触发。成员跑在非主线程，
   `request_permission` 的无人值守规则会**直接拒绝**需要交互审批的危险命令
   （`pretoolusehook.py` 的提示语已改为覆盖「定时任务回合与团队成员线程」），
   成员只能通过 `send_message` 请 Lead 代为执行；
2. **计划门禁写**：门未放行时成员无法改动工作区（见 3.5），这是 Lead 对成员
   行为的结构性控制点——尤其配合 `require_plan=True` 孵化时，成员从出生起
   就处于 `required` 状态；
3. **路径双重约束**：成员的相对路径以租约 cwd 为基准解析（`tools.safe_path`
   的新 `base` 参数），最终仍被限制在工作区内；权限钩子另外按工作区根校验
   一遍绝对路径；
4. **协议防伪造/防重放**：request_id 配对 + 双方地址校验 + work_version 失效
   （见 3.5 / 5.4），跨成员的消息即使被读到也无法套用到另一个成员身上；
5. **名字与路径卫生**：成员名/工作树名的白名单正则同时用作信箱与工作树的
   文件名，杜绝路径穿越；保留名 `lead`/`agent` 防止成员冒充协调者或主代理。

---

## 8. 工具清单

### 8.1 Lead 团队工具（注册进 main 的 TOOLS / TEAM_HANDLERS）

| 工具 | 作用 |
|------|------|
| `spawn_teammate(name, role, prompt, task_id?, require_plan?)` | 孵化常驻成员；`task_id` 指定初始任务，`require_plan` 开启计划门 |
| `list_teammates()` | 列出活跃成员及 `working/waiting_approval/idle/stopping` 状态 |
| `send_message(to, content)` | 向成员下发指令或补充上下文 |
| `request_shutdown(teammate)` | 发起优雅关闭（带 request_id 的协议请求） |
| `request_plan(teammate, task)` | 把成员计划门置为 required 并下发计划要求 |
| `review_plan(request_id, approve, feedback?)` | 审批/驳回成员提交的计划 |
| `create_worktree(name, task_id)` | 为任务创建绑定 Git 工作树 |

### 8.2 成员工具（TEAMMATE_TOOLS，不注册给 Lead）

基础工具的 bash（**裁掉 `run_in_background`**——成员线程无法收割后台结果）、
read_file、write_file、edit_file、glob；协作工具 `send_message`、`submit_plan`；
任务看板的 `list_tasks`、`claim_task`、`complete_task`（后两者走团队约束包装，
叠加单任务租约与计划门校验）。

成员**没有** todo_write / load_skill / 后台任务 / cron / compact——它的计划
就是看板上的任务，它的收工就是 `complete_task` + result 事件，保持成员上下文
小而聚焦。

---

## 9. 与参考实现的差异

参考代码（s13 Agent Teams 示例）为 POSIX 单文件实现，整合到本项目时做了
如下调整：

| 差异点 | 参考实现 | 本项目 |
|--------|----------|--------|
| 任务系统 | 内嵌复制品（fcntl 文件锁 + 独立实现） | 复用 `tools/task_system.py`，补 `TASK_LOCK`（线程锁，单进程语义）与 `worktree` 字段 |
| 基础工具 | 内嵌复制品（固定 WORKDIR） | 复用 `tools/tools.py`，文件工具加可选 `cwd` 参数 |
| 钩子/权限 | 内嵌 `check_permission` + `skip_permission` 参数 | 复用钩子系统；成员的非主线程身份由 `request_permission` 的无人值守规则天然覆盖 |
| Lead 唤醒 | CLI 里 `select.select` 轮询 stdin（POSIX 专用） | `team-wake` 守护线程 + `agent_lock` 空闲投递，与 cron 同模式（Windows 兼容） |
| Lead 的 cwd | Lead 认领任务后其文件工具也绑定租约 cwd | Lead 固定在工作区根作业；工作树任务直接禁止 Lead 认领，职责边界更清晰 |
| 计划门实现 | `plan_gates` 散落 globals + `task_lock` 混护 | 登记表集中在 teammanager，统一 `team_lock`，与 `TASK_LOCK` 固定加锁顺序 |
| 模块结构 | 单文件 ~1900 行 | 按项目约定拆为 `tools/teammanager.py` + 既有模块小幅增强，`关键入口` 风格文档头 |
| 终端日志 | 裸 ANSI + 旧式前缀 | `[TEAM:*]` 品牌前缀 + 亮青色，与 `[TASK:*]`/`[cron]`/`[background]` 风格统一 |

---

## 10. 端到端示例

```
用户: 帮我把 utils 模块重构和文档补全并行推进

Lead: 并行作业可行，建议两人小队：
      - alice（重构工程师）：拆分 utils/mod_a.py
      - bob（技术作者）：补全 docstrings
      是否确认？
用户: 确认

Lead: create_task("重构 mod_a") / create_task("补全 docstrings")
      spawn_teammate(alice, ..., task_id=task_x1)
      spawn_teammate(bob, ..., task_id=task_y1, require_plan=True)
      [回合结束，等待事件]

      ... .tasks/ 出现两个 in_progress；alice 直接开工；
      bob 提交计划 -> [TEAM:bus] plan_approval_request -> lead 信箱 ...

[team-wake 投递] [Team events] [plan_approval_request request_id=req_000042] bob: ...
Lead: review_plan(req_000042, approve=True)
      [回合结束]

      ... 成员陆续完成：result 事件唤醒 Lead ...

[team-wake 投递] [result] alice: 重构完成，tests 全绿 ... / [idle_notification] ...
Lead: request_shutdown(alice) / request_shutdown(bob)
      [Team events] shutdown_response x2
Lead: 两项工作均已完成并合入，团队已解散。（回合结束）
```

---

## 11. 边界与注意事项

1. **工作树不是沙箱**：成员的路径校验以工作区为根，能读到其他目录；
   隔离的只是「默认作业目录」与 Git 分支；
2. **单进程语义**：`TASK_LOCK` 与登记表都是进程内的，不支持两个 CLI 进程
   同时操作同一工作区的团队状态；
3. **信箱是会话态**：进程崩溃时未投递的信箱消息不会在重启后重新消费
   （任务看板不受影响，未完成任务会被重新认领）；
4. **事件可能合并投递**：Lead 忙碌期间多个成员的 result 会攒在信箱里，
   下一个空闲回合一次性作为 `[Team events]` 投递；
5. **计划审批是阻塞协作点**：成员处于 waiting_approval 时不会自动超时，
   Lead 长期不审批则成员长期挂起（可用 request_shutdown 兜底回收）；
6. **GBK 终端**：所有 `[TEAM:*]` 日志前缀只用 ASCII，避免 Windows 中文终端
   编码报错；成员名/工作树名白名单同样保证文件名安全；
7. **分支合并归用户**：工作树分支 `wt/<name>` 删除前永远保留，合并/清理
   是 Lead 或用户的显式动作，运行时不代替你做任何 Git 破坏性操作。
