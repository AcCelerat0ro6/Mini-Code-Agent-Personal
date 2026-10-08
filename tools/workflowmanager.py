"""
tools/workflowmanager.py - 工作流运行时模块 (Workflow Runtime)

通过单次工具调用运行预注册的 Python 编排脚本，把「多步 LLM 协作」从脆弱的
自然语言 prompt 升级为确定性代码：脚本里用 ctx.agent() 派生聚焦子推理、
ctx.parallel() 做屏障并发、ctx.pipeline() 做流水线并发；每步结果写入 journal，
resume 时命中缓存直接回放，跳过实际模型调用，实现断点续传与 Token 节省。

    +-------------+       +--------------------------------+
    | Agent loop  | ----> | workflow(name, args, run_id)   |
    +-------------+       +---------------+----------------+
                                          |
                           +--------------+--------------+
                           | agent | parallel | pipeline  |
                           +--------------+--------------+
                                          |
                                   journal + result

    .workflows/   运行时持久化目录（工作区根目录下，与 .tasks/ .mailboxes/ 同待遇）：
                  <runId>.json           任务快照（状态/用量/进度事件）
                  <runId>.journal.jsonl  执行日志（断点续传的语义缓存）
                  <runId>.output.json    最终产出
                  last_run.txt           最近一次 runId（供编程式 resume）

关键入口 (Key entry points):

    WORKFLOWS               工作流注册表：name -> (meta, script_fn)
    register_workflow       注册一个工作流（供外部模块在 import 时调用）
    run_workflow_sync       workflow 工具回调：桥接同步分发到 asyncio.run
    WorkflowInputError      输入非法/元数据不匹配/Schema 校验失败异常
    WORKFLOW_DENY           黑名单集合：命中的工作流名拒绝运行
"""

import asyncio
import hashlib
import json
import os
import re
import secrets
import threading
from collections.abc import Callable
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

from client.client import client
from tools.recoverymanager import RecoveryState, with_retry

# 工作区路径（与 tools.py / teammanager.py 保持一致：优先读 WORK_DIR 环境变量）
WORKDIR = Path(os.getenv("WORK_DIR", Path.cwd()))
MODEL = os.environ["MODEL_ID"]

# 工作流协作日志统一使用绿色 + [WORKFLOW:*] 前缀，与 [TASK:*] 品红、[cron] 青色、
# [background]/[HOOK] 灰色、[TEAM:*] 亮青、[RETRY:*] 黄色的终端输出区分开
# （前缀只用 ASCII，避免 GBK 终端编码报错）
WORKFLOW_COLOR = "\033[32m"
RESET = "\033[0m"


# ==============================================================================
# 运行时防护配置 (Runtime Guards)
# ==============================================================================

AGENT_CAP = 1000                       # 单次运行中 subagent 调用的最大硬限制，防止死循环无限派生
CONCURRENCY = 8                        # 最大并发限制（通过信号量控制）
STORE = WORKDIR / ".workflows"         # 运行时持久化目录：存放快照（snapshots）与执行日志（journals）
MISS = object()                        # Journal 缓存未命中时的哨兵对象 (Sentinel)
WORKFLOW_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")  # 工作流名称格式校验正则
RUN_ID_RE = re.compile(r"^wf_[A-Za-z0-9][A-Za-z0-9._-]{0,63}_[0-9a-f]{16}$")  # 运行 ID (runId) 校验正则


# ==============================================================================
# 工具函数 (Helpers)
# ==============================================================================

def _stable_hash(s: str) -> int:
    """计算跨进程一致的哈希值。
    注意：Python 自带的 hash() 在每个进程启动时有随机加盐（salt），
    因此跨进程或执行 resume 时必须使用 sha256 保证 key 的一致性。
    """
    return int(hashlib.sha256(s.encode()).hexdigest(), 16)


def create_run_id(meta) -> str:
    """根据工作流元数据名称生成一个唯一的运行 ID (runId)。"""
    return f"wf_{meta['name']}_{secrets.token_hex(8)}"


def reserve_run_id(meta) -> str:
    """原子性预分配并占用一个新的 runId，防止日志被意外截断或重名覆盖。"""
    STORE.mkdir(parents=True, exist_ok=True)
    for _ in range(32):
        run_id = validate_run_id(create_run_id(meta))
        snapshot_path = STORE / f"{run_id}.json"
        try:
            # 使用 O_CREAT | O_EXCL 排他创建文件，保证原子性占用（跨平台可用，
            # Windows 下 mode 参数被忽略但 flags 语义一致）
            fd = os.open(snapshot_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        except FileExistsError:
            continue
        os.close(fd)
        return run_id
    raise WorkflowInputError("无法分配唯一的工作流 runId")


def create_task_id(run_id) -> str:
    """根据 run_id 创建对应的任务标识 taskId。"""
    return f"local_workflow_{run_id}"


def validate_run_id(run_id):
    """校验 run_id 格式是否合法。"""
    if not isinstance(run_id, str) or not RUN_ID_RE.fullmatch(run_id):
        raise WorkflowInputError("无效的工作流 runId")
    return run_id


# ==============================================================================
# 错误定义 (Errors)
# ==============================================================================

class WorkflowInputError(Exception):
    """输入数据非法、元数据不匹配或 Schema 校验失败异常。"""


# ==============================================================================
# 运行互斥锁 (Run Lock)
# ==============================================================================

_run_locks_guard = threading.Lock()
_run_locks: dict[str, threading.Lock] = {}


@contextmanager
def workflow_run_lock(run_id: str):
    """工作流执行互斥锁（线程级）。
    保证同一时刻本进程内只有一个线程在运行/恢复特定的 run_id。

    注：s16 参考实现另用 fcntl.flock 做跨进程文件锁，但 fcntl 在 Windows 不可用
    （项目约定），且本项目为单进程运行场景，参照 docs/s15-integration.md §6
    「单进程运行场景下线程锁已够」先例，此处仅保留线程锁。
    """
    with _run_locks_guard:
        local_lock = _run_locks.setdefault(run_id, threading.Lock())
    # 非阻塞抢锁：抢不到说明同一 run_id 已有实例在跑，直接报错而非死等
    if not local_lock.acquire(blocking=False):
        raise WorkflowInputError(f"工作流运行实例 {run_id} 已处于活跃状态")
    try:
        yield
    finally:
        local_lock.release()
        # 释放后若无人持有则从登记表清理，避免 _run_locks 随 runId 无限增长
        with _run_locks_guard:
            if not local_lock.locked() and _run_locks.get(run_id) is local_lock:
                _run_locks.pop(run_id, None)


# ==============================================================================
# 元数据验证 (Metadata Validation)
# ==============================================================================

# 工作流黑名单：命中的名字拒绝运行（初始为空，后续按需填充；
# 工具级权限另由 hooks/pretoolusehook.permission_hook 把关，二者互补）
WORKFLOW_DENY: set[str] = set()


def validate_meta(meta):
    """启动前校验工作流的 name、description 以及可选的 phases 阶段列表。"""
    if not isinstance(meta, dict):
        raise WorkflowInputError("meta 必须为字典对象")
    if not meta.get("name") or not meta.get("description"):
        raise WorkflowInputError("meta 必须包含 `name` 和 `description` 字段")
    if not isinstance(meta["name"], str) or not WORKFLOW_NAME_RE.fullmatch(meta["name"]):
        raise WorkflowInputError(
            "meta.name 必须是 1-64 字符的标识符，仅允许字母、数字、'.'、'_' 或 '-'"
        )
    if not isinstance(meta["description"], str):
        raise WorkflowInputError("meta.description 必须是字符串")
    if "phases" in meta:
        if not isinstance(meta["phases"], list) or not all(
            isinstance(phase, str) and phase for phase in meta["phases"]
        ):
            raise WorkflowInputError("meta.phases 必须是非空字符串组成的列表")
    return meta


def check_permission(meta):
    """执行安全准入鉴权：黑名单校验。"""
    if meta["name"] in WORKFLOW_DENY:
        raise WorkflowInputError(f"工作流 '{meta['name']}' 被系统策略拒绝运行")
    return "allow"


# ==============================================================================
# 轻量级 JSON Schema 验证器 (Simple JSON Schema)
# ==============================================================================

class SimpleJsonSchema:
    """轻量级验证器，用于验证 agent({schema}) 的结构化输出。
    支持 object/array/string/boolean/number/integer 以及 required/enum 字段。
    """

    def __init__(self, schema):
        self.schema = schema

    def validate(self, value, schema=None):
        schema = self.schema if schema is None else schema
        if "enum" in schema and value not in schema["enum"]:
            return False, f"期望值为 {schema['enum']} 之一"
        t = schema.get("type")
        if t == "object":
            if not isinstance(value, dict):
                return False, "期望类型为 object"
            for key in schema.get("required", []):
                if key not in value:
                    return False, f"缺少必填字段 '{key}'"
            for key, sub in schema.get("properties", {}).items():
                if key in value:
                    ok, err = self.validate(value[key], sub)
                    if not ok:
                        return False, f"{key}: {err}"
            return True, None
        if t == "array":
            if not isinstance(value, list):
                return False, "期望类型为 array"
            items = schema.get("items")
            if items:
                for i, el in enumerate(value):
                    ok, err = self.validate(el, items)
                    if not ok:
                        return False, f"[{i}]: {err}"
            return True, None
        if t == "string":
            return (isinstance(value, str), None if isinstance(value, str) else "期望类型为 string")
        if t == "boolean":
            return (isinstance(value, bool), None if isinstance(value, bool) else "期望类型为 boolean")
        if t in ("number", "integer"):
            ok = isinstance(value, (int, float)) and not isinstance(value, bool)
            return (ok, None if ok else "期望类型为 number")
        return True, None


def _fill_schema(schema, seed):
    """Mock 测试专用的确定性数据生成器，依据 Schema 自动填充假数据。"""
    t = schema.get("type")
    if t == "object":
        keys = schema.get("required") or list(schema.get("properties", {}))
        return {k: _fill_schema(schema["properties"][k], f"{seed}/{k}") for k in keys}
    if t == "array":
        return [_fill_schema(schema["items"], f"{seed}/0")]
    if t == "boolean":
        return _stable_hash(seed) % 4 != 0
    if t in ("number", "integer"):
        return _stable_hash(seed) % 5
    return seed.rsplit("/", 1)[-1]


# ==============================================================================
# Agent 执行器 (Runners)
# ==============================================================================

@dataclass(frozen=True)
class RunnerOutput:
    """Agent 执行输出结果，包含返回值和消耗的 Token 数。"""
    value: object
    tokens: int


class MockAgentRunner:
    """确定性 Mock 执行器，主要用于单元测试与无网络冒烟，无需真实调用 LLM。
    测试时把模块级 RUNNER_FACTORY 猴补丁成本类即可跑通完整 lifecycle。
    """

    def run(self, prompt, schema=None, label=None):
        if schema is None:
            value = f"[mock] {(label or prompt)[:60]}"
            return RunnerOutput(value, self._tokens(prompt, value))
        props = schema.get("properties", {})
        # 针对演示工作流 (review-changes) 的预设结构做语义 Mock
        if "findings" in props:
            n = 1 + (_stable_hash(prompt) % 2)
            sev = ["high", "medium", "low"]
            value = {"findings": [
                {"title": f"{label or 'audit'} #{i + 1}",
                 "severity": sev[_stable_hash(prompt + str(i)) % 3]}
                for i in range(n)
            ]}
        elif "isReal" in props:
            real = _stable_hash(prompt) % 4 != 0
            value = {"isReal": real,
                     "reason": "reproduced" if real else "could not reproduce"}
        else:
            value = _fill_schema(schema, prompt)
        return RunnerOutput(value, self._tokens(prompt, value))

    @staticmethod
    def _tokens(prompt, result):
        # 简单估算 Token 消耗量
        return len(prompt) // 4 + len(json.dumps(result, default=str)) // 4


def _response_text(response) -> str:
    """从 Anthropic API 响应中提取文本内容。"""
    return "\n".join(
        str(getattr(block, "text", ""))
        for block in getattr(response, "content", [])
        if getattr(block, "type", None) == "text"
    ).strip()


def _parse_runner_json(text: str) -> object:
    """解析 Agent 返回的内容，容错 Markdown 代码块包裹的 JSON 或文本混杂的 JSON。"""
    stripped = text.strip()
    if stripped.startswith("```"):
        lines = stripped.splitlines()
        lines = lines[1:] if lines else lines
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        stripped = "\n".join(lines).strip()
    try:
        return json.loads(stripped)
    except json.JSONDecodeError:
        # 尝试从混杂文本中寻找第一个 '{' 开始解析
        decoder = json.JSONDecoder()
        for position, character in enumerate(stripped):
            if character != "{":
                continue
            try:
                value, _ = decoder.raw_decode(stripped[position:])
            except json.JSONDecodeError:
                continue
            return value
        raise WorkflowInputError("工作流 subagent 返回的不是合法 JSON")


class AnthropicAgentRunner:
    """基于真实 Anthropic API 的 Agent 执行器。

    与 tools/subagent.run_subagent 的全工具循环不同，这里的子推理是「聚焦单步」：
    不带工具、system prompt 约束只完成给定步骤，配合 schema 强制结构化输出。
    模型调用接入 recoverymanager.with_retry 容错层（项目约定：所有驱动大模型的
    调用点都要对 429/529 退避重试、连续过载切换备用模型）。
    """

    def __init__(self, client, model):
        self.client = client
        self.model = model

    def run(self, prompt, schema=None, label=None):
        request = prompt
        if schema is not None:
            request += (
                "\n\nReturn only one JSON object matching this schema:\n"
                + json.dumps(schema, ensure_ascii=True, sort_keys=True)
            )
        # 每次调用独立持有一份恢复状态：workflow 子推理之间互不影响退避计数；
        # 以构造时传入的模型为起点，连续过载后由 with_retry 切到 FALLBACK_MODEL
        recovery = RecoveryState()
        recovery.current_model = self.model
        response = with_retry(
            lambda: self.client.messages.create(
                model=recovery.current_model,
                system=(
                    "You are a focused workflow agent. Complete only the supplied "
                    "step. Do not claim access to files or results not included in "
                    "the prompt."
                ),
                messages=[{"role": "user", "content": request}],
                max_tokens=2000,
            ),
            recovery,
        )
        text = _response_text(response)
        if schema is None:
            value = text
        else:
            try:
                value = _parse_runner_json(text)
            except WorkflowInputError:
                # 解析失败先保留原始内容，交由 ExecutionState 进行重试
                value = text
        usage = getattr(response, "usage", None)
        tokens = int(getattr(usage, "input_tokens", 0) or 0) + int(
            getattr(usage, "output_tokens", 0) or 0
        )
        return RunnerOutput(value, tokens)


# 默认 Runner 工厂：项目里 workflow 工具由真实 agent 调用，故默认走 Anthropic；
# 单元测试可猴补丁成 MockAgentRunner（workflowmanager.RUNNER_FACTORY = MockAgentRunner）
RUNNER_FACTORY = lambda: AnthropicAgentRunner(client, MODEL)


# ==============================================================================
# 执行日志系统 (Journal)
# ==============================================================================

class WorkflowJournal:
    """追加写入日志文件: <runId>.journal.jsonl。
    在断点恢复 (resume) 时：已经存在语义 key 的 agent() 结果将直接从日志中回放，
    跳过实际模型调用，实现断点续传和节省 Token 开销。
    """

    def __init__(self, run_id, resume, store=None):
        store = STORE if store is None else store
        store.mkdir(parents=True, exist_ok=True)
        self.path = store / f"{run_id}.journal.jsonl"
        self.resume = resume
        self.cache = {}
        if resume:
            if not self.path.exists():
                raise WorkflowInputError(f"未找到用于恢复运行的 journal 日志: {run_id}")
            # 读取历史日志，加载缓存
            for line_number, line in enumerate(self.path.read_text(encoding="utf-8").splitlines(), start=1):
                try:
                    rec = json.loads(line)
                    if (
                        not isinstance(rec, dict)
                        or not isinstance(rec.get("key"), str)
                        or "value" not in rec
                    ):
                        raise ValueError("期望每行为包含 key/value 的记录")
                except (json.JSONDecodeError, ValueError) as exc:
                    raise WorkflowInputError(
                        f"恢复日志在第 {line_number} 行损坏"
                    ) from exc
                self.cache[rec["key"]] = rec["value"]
            self._f = self.path.open("a", encoding="utf-8")  # 追加模式
        else:
            self._f = self.path.open("w", encoding="utf-8")  # 全新运行则截断重写

    def key(self, kind, label, prompt, schema):
        """生成确定性的语义 Key。
        与并发调用的顺序无关，确保即使并行执行顺序变化，断点恢复时也能准确匹配。
        """
        basis = f"{kind}|{label}|{prompt}|{json.dumps(schema, sort_keys=True)}"
        return f"{kind}-{_stable_hash(basis) % 10**10:010d}"

    def cached(self, key):
        """查询缓存；未命中返回 MISS 哨兵对象。"""
        return self.cache.get(key, MISS)

    def record(self, key, value):
        """追加记录一条完成的步骤及其结果。"""
        self._f.write(json.dumps({"key": key, "value": value}) + "\n")
        self._f.flush()
        self.cache[key] = value

    def close(self):
        self._f.close()


# ==============================================================================
# Token 预算控制 (Token Budget)
# ==============================================================================

class Budget:
    """预算追踪器: budget.total / spent() / remaining()。
    一旦累计消耗达到上限，后续 agent() 调用会立刻抛出异常，防止 Token 滥用超支。
    """

    def __init__(self, total=None):
        self.total = total
        self._spent = 0

    def add(self, n):
        if self.total is not None and self._spent + n > self.total:
            raise WorkflowInputError(
                f"超出 Token 预算上限 ({self._spent + n} > {self.total})"
            )
        self._spent += n

    def spent(self):
        return self._spent

    def remaining(self):
        return float("inf") if self.total is None else max(0, self.total - self._spent)


# ==============================================================================
# 工作流任务生命周期 (Workflow Task Lifecycle)
# ==============================================================================

class LocalWorkflowTask:
    """维护工作流任务的状态、资源消耗（agents/tokens）及进度事件。
    生命周期事件统一以 [WORKFLOW:*] 绿色前缀打印，与项目其它模块的终端日志区分。
    """

    def __init__(self, task_id, run_id, meta):
        self.task_id = task_id
        self.run_id = run_id
        self.meta = meta
        self.status = "running"
        self.usage = {"agents": 0, "tokens": 0}
        self.progress = []

    def event(self, name, **data):
        """输出任务生命周期事件日志（[WORKFLOW:event] 绿色前缀）。"""
        line = " ".join(f"{k}={v}" for k, v in data.items())
        print(f"{WORKFLOW_COLOR}[WORKFLOW:event] {name:<18} {line}{RESET}")

    def progress_event(self, ptype, **data):
        """记录并打印细粒度执行进度事件（[WORKFLOW:progress] 绿色前缀）。"""
        self.progress.append({"type": ptype, **data})
        line = " ".join(f"{k}={v}" for k, v in data.items())
        print(f"{WORKFLOW_COLOR}[WORKFLOW:progress] {ptype:<16} {line}{RESET}")


# ==============================================================================
# 工作流执行原语 (Workflow Primitives)
# ==============================================================================

class ExecutionLimits:
    """全局共享限额（包括嵌套的工作流层级）。"""

    def __init__(self):
        self.agents = 0
        self.semaphore = asyncio.Semaphore(CONCURRENCY)

    def claim_agent(self):
        self.agents += 1
        if self.agents > AGENT_CAP:
            raise WorkflowInputError(f"触发 agent() 调用次数硬上限 ({AGENT_CAP})")


class ExecutionState:
    """注入到工作流脚本中的上下文对象 (ctx)，暴露核心编排原语。"""

    def __init__(self, task, journal, runner, budget, args, depth=0, limits=None):
        self.task = task
        self.journal = journal
        self.runner = runner
        self.budget = budget
        self.args = args
        self._depth = depth
        self._phase = None
        self._phases_seen = set()
        self._limits = limits or ExecutionLimits()

    def phase(self, title):
        """切换阶段名称。之后的 agent() 会归属到该阶段。
        支持幂等（Upsert）：流水线各分支重复调用相同阶段名不会重复触发通知。
        """
        self._phase = title
        if title not in self._phases_seen:
            self._phases_seen.add(title)
            self.task.progress_event("workflow_phase", title=title)

    def log(self, message):
        """输出一条工作流业务日志事件。"""
        self.task.progress_event("workflow_log", message=message)

    async def agent(self, prompt, schema=None, label=None, phase=None):
        """派生一个 subagent 执行单步推理。
        - 若传入 schema，则强制要求结构化输出并在失败时自动重试 1 次。
        - 在 resume 模式下，若 journal 命中缓存则直接返回结果，跳过 LLM。
        """
        label = label or (prompt[:24] + "...")
        self._limits.claim_agent()
        if self.budget.remaining() <= 0:
            raise WorkflowInputError("超出 Token 预算")

        # 检查是否命中 Journal 历史缓存（断点续传）
        key = self.journal.key("agent", label, prompt, schema)
        cached = self.journal.cached(key)
        if cached is not MISS:
            if schema is not None:
                ok, err = SimpleJsonSchema(schema).validate(cached)
                if not ok:
                    raise WorkflowInputError(
                        f"缓存中的 agent 输出未能通过 schema 验证: {err}"
                    )
            self.task.progress_event("workflow_agent", label=label,
                                     phase=phase or self._phase, status="cached")
            return cached

        # 并发信号量限制下执行实际调用（asyncio.to_thread 把同步 LLM 调用丢到线程池，
        # 让 parallel/pipeline 里的多个 agent() 真正并发起来）
        async with self._limits.semaphore:
            run = await asyncio.to_thread(
                self.runner.run, prompt, schema, label
            )
            result = run.value
            tokens = run.tokens

        # 结构化输出校验与一次重试机制
        if schema is not None:
            ok, err = SimpleJsonSchema(schema).validate(result)
            if not ok:
                retry = await asyncio.to_thread(
                    self.runner.run,
                    prompt + "\n\nReturn valid JSON.",
                    schema,
                    label,
                )
                result = retry.value
                tokens += retry.tokens
                ok, err = SimpleJsonSchema(schema).validate(result)
                if not ok:
                    raise WorkflowInputError(f"agent({{schema}}) 返回无效输出: {err}")

        # 结算 Token 预算、更新状态并持久化记录到 Journal
        self.budget.add(tokens)
        self.task.usage["agents"] += 1
        self.task.usage["tokens"] += tokens
        self.journal.record(key, result)
        self.task.progress_event("workflow_agent", label=label,
                                 phase=phase or self._phase, status="done")
        return result

    async def parallel(self, thunks):
        """屏障并发 (BARRIER)：并发执行所有异步函数，若任一失败则整体失败。"""
        return await asyncio.gather(*[thunk() for thunk in thunks])

    async def pipeline(self, items, *stages):
        """流水线并发 (PIPELINE)：针对列表逐个流转多个阶段，各阶段之间无全局同步屏障。
        （即：数据项 A 已经在跑 stage 3 时，数据项 B 可以仍在跑 stage 1）。
        每个阶段函数入参为 (prev_result, original_item, index)。
        """
        async def run_item(item, idx):
            value = item
            for stage in stages:
                value = await stage(value, item, idx)
            return value
        return await asyncio.gather(*[run_item(it, i) for i, it in enumerate(items)])

    async def workflow(self, name, args=None):
        """内联嵌套运行另一个预存工作流（限制最大嵌套深度为 1 层），
        共享当前运行的 journal 日志、预算与并发计数器。
        """
        if self._depth >= 1:
            raise WorkflowInputError("workflow() 最多只允许嵌套 1 层")
        if name not in WORKFLOWS:
            raise WorkflowInputError(f"未知工作流 '{name}'")
        meta, fn = WORKFLOWS[name]
        child = ExecutionState(self.task, self.journal, self.runner, self.budget,
                               args or {}, depth=self._depth + 1,
                               limits=self._limits)
        return await fn(child, args or {})


# ==============================================================================
# 工作流工具实现 (Workflow Tool)
# ==============================================================================

class WorkflowTool:
    """工作流工具本体。
    .call() 负责校验元数据、检查权限、分配 runId/taskId、注册 Task 并调度脚本。
    """

    async def call(self, meta, script_fn, args=None, resume_from_run_id=None):
        validate_meta(meta)
        check_permission(meta)
        resuming = resume_from_run_id is not None
        if resuming:
            run_id = validate_run_id(resume_from_run_id)
        else:
            run_id = reserve_run_id(meta)
        # 加锁以确保单个 run_id 的互斥运行
        with workflow_run_lock(run_id):
            return await self._call_locked(
                meta, script_fn, args, run_id, resuming
            )

    async def _call_locked(self, meta, script_fn, args, run_id, resuming):
        if resuming:
            snapshot = _read_snapshot(run_id)
            if snapshot.get("workflowName") != meta["name"]:
                raise WorkflowInputError("恢复的 runId 与当前工作流 meta 不匹配")
            saved_args = snapshot.get("args", {})
            if args is None:
                args = saved_args
            elif args != saved_args:
                raise WorkflowInputError("恢复时提供的 args 与原始运行记录不一致")
            journal = WorkflowJournal(run_id, resume=True)
        else:
            args = args or {}
            journal = WorkflowJournal(run_id, resume=False)
        task_id = create_task_id(run_id)

        task = LocalWorkflowTask(task_id, run_id, meta)
        print(f"{WORKFLOW_COLOR}[WORKFLOW:start] {meta['name']} "
              f"runId={run_id} resume={resuming}{RESET}")
        # 在脚本执行前记录启动信封
        launched = {"status": "async_launched", "taskId": task_id,
                    "taskType": "local_workflow", "runId": run_id,
                    "workflowName": meta["name"]}
        task.event("async_launched", runId=run_id, taskId=task_id)
        task.event("task_started", workflow=meta["name"],
                   phases=",".join(meta.get("phases", [])) or "-",
                   resume=resuming)
        # 保存初次快照
        _write_json(STORE / f"{run_id}.json", {
            "runId": run_id,
            "workflowName": meta["name"],
            "args": args,
            "task": serialize_task(task),
        })

        try:
            ctx = ExecutionState(
                task, journal, RUNNER_FACTORY(), Budget(args.get("budget")), args
            )
            result = await script_fn(ctx, args)
            task.status = "completed"
        except Exception as e:                          # 捕获异常标记为失败
            task.status = "failed"
            result = {"error": str(e)}
        finally:
            journal.close()

        # 写入最终产出和状态快照
        _write_json(STORE / f"{run_id}.output.json", result)
        _write_json(STORE / f"{run_id}.json", {
            "runId": run_id,
            "workflowName": meta["name"],
            "args": args,
            "task": serialize_task(task),
        })
        _save_last_run(run_id)
        if task.status == "completed":
            print(f"{WORKFLOW_COLOR}[WORKFLOW:done] {meta['name']} runId={run_id} "
                  f"agents={task.usage['agents']} tokens={task.usage['tokens']}{RESET}")
        else:
            print(f"{WORKFLOW_COLOR}[WORKFLOW:fail] {meta['name']} runId={run_id} "
                  f"error={result.get('error')}{RESET}")
        task.event("task_notification", status=task.status,
                   agents=task.usage["agents"], tokens=task.usage["tokens"],
                   outputFile=f".workflows/{run_id}.output.json")
        return {"launched": launched, "result": result, "task": task}


def _write_json(path, value):
    """通过临时文件原子替换，防止进程中途被终止导致 JSON 损坏（os.replace 跨平台可用）。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, indent=2, default=str), encoding="utf-8")
    os.replace(temporary, path)


def _read_snapshot(run_id):
    """读取已存储的任务快照。"""
    path = STORE / f"{run_id}.json"
    if not path.exists():
        raise WorkflowInputError(f"未找到恢复运行所需的快照文件: {run_id}")
    try:
        snapshot = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise WorkflowInputError(f"快照文件损坏: {run_id}") from exc
    if not isinstance(snapshot, dict):
        raise WorkflowInputError(f"无效的快照格式: {run_id}")
    return snapshot


def _save_last_run(run_id):
    """记录最近一次运行的 runId，方便编程式 resume。"""
    (STORE / "last_run.txt").write_text(run_id, encoding="utf-8")


def _read_last_run():
    """读取最近一次运行的 runId（无记录返回 None）。"""
    p = STORE / "last_run.txt"
    return p.read_text(encoding="utf-8").strip() if p.exists() else None


def serialize_task(task):
    """序列化任务状态字典。"""
    return {
        "taskId": task.task_id,
        "taskType": "local_workflow",
        "runId": task.run_id,
        "workflowName": task.meta["name"],
        "status": task.status,
        "usage": dict(task.usage),
        "progress": list(task.progress),
    }


# ==============================================================================
# 注册表与工具声明 (Registry & Tool Declaration)
# ==============================================================================

# 工作流注册表：name -> (meta, script_fn)。初始为空，由外部模块在 import 时
# 调用 register_workflow 填充（示例见 docs/workflow-runtime.md 的 review-changes）
WORKFLOWS: dict[str, tuple[dict, Callable]] = {}


def register_workflow(meta: dict, script_fn: Callable) -> None:
    """注册一个工作流到全局注册表 WORKFLOWS（供外部模块在 import 时调用）。
    meta 须通过 validate_meta 校验；重名注册视为配置错误直接抛出。
    """
    validate_meta(meta)
    if meta["name"] in WORKFLOWS:
        raise WorkflowInputError(f"工作流 '{meta['name']}' 已注册")
    WORKFLOWS[meta["name"]] = (meta, script_fn)


async def run_workflow(name, args=None, resume_from_run_id=None):
    """面向模型的异步适配器：在注册表中查找受信任的 Python 脚本并执行。"""
    if not isinstance(name, str):
        raise WorkflowInputError("workflow name 必须是字符串")
    if name not in WORKFLOWS:
        raise WorkflowInputError(f"未找到工作流 '{name}'")
    if args is not None and not isinstance(args, dict):
        raise WorkflowInputError("workflow args 必须是一个字典对象")
    meta, script_fn = WORKFLOWS[name]
    out = await WorkflowTool().call(
        meta,
        script_fn,
        args=args,
        resume_from_run_id=resume_from_run_id,
    )
    return {
        "launched": out["launched"],
        "result": out["result"],
        "task": serialize_task(out["task"]),
    }


def run_workflow_sync(**tool_input):
    """workflow 工具的同步回调：桥接上层 execute_tool 的同步分发到异步事件循环。
    execute_tool 以 handler(**block.input) 形式调用，因此入参为工具 schema 的
    name / args / resume_from_run_id 关键字参数。
    """
    try:
        return json.dumps(asyncio.run(run_workflow(**tool_input)), default=str)
    except WorkflowInputError as exc:
        return f"Error: {exc}"


# 暴露给 Agent 系统的工具声明 Schema（工具名小写，与项目 task/bash/connect_mcp 等一致）
WORKFLOW_TOOLS = [
    {
        "name": "workflow",
        "description": (
            "Run a pre-registered Python orchestration script by name in a single "
            "tool call. Workflows are deterministic code (agent/parallel/pipeline "
            "primitives), not free-form prompts. Pass resume_from_run_id to replay "
            "a previous run from its journal cache."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "name": {
                    "type": "string",
                    "description": "Registered workflow name.",
                },
                "args": {
                    "type": "object",
                    "description": (
                        "Arguments dict passed to the workflow script. "
                        "May include 'budget' (integer token cap)."
                    ),
                },
                "resume_from_run_id": {
                    "type": "string",
                    "description": "Optional runId to resume from journal cache.",
                },
            },
            "required": ["name"],
            "additionalProperties": False,
        },
    },
]

# 工具映射表（main.py 合并进 BASE_HANDLER_POOL）
WORKFLOW_HANDLERS = {"workflow": run_workflow_sync}
