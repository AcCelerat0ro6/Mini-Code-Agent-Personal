"""
tools/cronmanager.py - 定时任务调度模块 (Cron Scheduler)

定时周期任务调度：用 5 字段 cron 表达式（分 时 日 月 周）把提示词排期到未来的本地时间点，
到期后包装成 [Scheduled] 用户消息投递给智能体，趁空闲自动跑一轮 Agent 循环消化。

    +--------------------------+   09:00   +-----------------------+
    | 0 9 * * *               | --------> | [Scheduled] run tests |
    | prompt: "run tests"      |           +-----------+-----------+
    +--------------------------+                       |
          scheduled_jobs                    cron_queue | agent idle
                                                        v
                                                +-------------+
                                                | Agent Loop  |
                                                +-------------+

关键入口 (Key entry points):

    run_schedule_cron     schedule_cron 工具回调：登记一条 cron 定时任务
    run_list_crons        list_crons 工具回调：列出全部定时任务
    run_cancel_cron       cancel_cron 工具回调：按 ID 取消定时任务
    inject_scheduled_jobs 把到期任务注入上下文，返回投递事务句柄（支持确认/回滚）
    start_cron_runtime    启动时间轴轮询与空闲投递两个守护线程
    stop_cron_runtime     停止调度线程（程序退出前调用）
"""

import json
import os
import secrets
import threading
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path

# 工作区路径（与 tools.py 保持一致：优先读取 WORK_DIR 环境变量，否则取当前目录）
WORKDIR = Path(os.getenv("WORK_DIR", Path.cwd()))

# 定时任务持久化文件（durable=True 的任务跨重启保存在工作区根目录）
DURABLE_PATH = WORKDIR / ".scheduled_tasks.json"


# ==============================================================================
# cron 表达式解析、校验与时间匹配
# ==============================================================================

def _cron_field_matches(field: str, value: int) -> bool:
    """判断单个 cron 字段（* / */n / a,b / a-b / 常量）是否命中当前数值"""
    if field == "*":
        return True
    # 步长匹配：*/n 表示每 n 个单位触发一次
    if field.startswith("*/"):
        return value % int(field[2:]) == 0
    # 逗号枚举：任意一个子字段命中即算命中
    if "," in field:
        return any(_cron_field_matches(part.strip(), value)
                   for part in field.split(","))
    # 闭区间匹配：a-b 表示 [a, b] 范围内的每个取值
    if "-" in field:
        start, end = field.split("-", 1)
        return int(start) <= value <= int(end)
    return value == int(field)


def cron_matches(cron_expr: str, moment: datetime) -> bool:
    """
    判断 5 字段 cron 表达式在指定时刻是否命中。
    日（day-of-month）与周（day-of-week）遵循标准 cron 语义：
    两者都限定时取「或」，只限定其中一个时取该字段的匹配结果。
    """
    fields = cron_expr.strip().split()
    if len(fields) != 5:
        return False

    minute, hour, day, month, weekday = fields
    # 标准 cron 周日为 0、周一为 1；datetime.weekday() 周一为 0，做一次换算
    cron_weekday = (moment.weekday() + 1) % 7
    if not (
        _cron_field_matches(minute, moment.minute)
        and _cron_field_matches(hour, moment.hour)
        and _cron_field_matches(month, moment.month)
    ):
        return False

    day_matches = _cron_field_matches(day, moment.day)
    weekday_matches = _cron_field_matches(weekday, cron_weekday)
    if day == "*" and weekday == "*":
        return True
    if day == "*":
        return weekday_matches
    if weekday == "*":
        return day_matches
    return day_matches or weekday_matches


def _validate_cron_field(field: str, minimum: int, maximum: int) -> str | None:
    """校验单个 cron 字段的语法与取值范围，合法返回 None，否则返回错误说明"""
    if field == "*":
        return None
    if field.startswith("*/"):
        step = field[2:]
        if not step.isdigit() or int(step) <= 0:
            return f"Invalid step: {field}"
        return None
    if "," in field:
        for part in field.split(","):
            error = _validate_cron_field(part.strip(), minimum, maximum)
            if error:
                return error
        return None
    if "-" in field:
        start, end = field.split("-", 1)
        if not start.isdigit() or not end.isdigit():
            return f"Invalid range: {field}"
        start_value, end_value = int(start), int(end)
        if start_value > end_value:
            return f"Range start is greater than end: {field}"
        if start_value < minimum or end_value > maximum:
            return f"Range {field} is outside [{minimum}-{maximum}]"
        return None
    if not field.isdigit():
        return f"Invalid field: {field}"
    value = int(field)
    if value < minimum or value > maximum:
        return f"Value {value} is outside [{minimum}-{maximum}]"
    return None


def validate_cron(cron_expr: str) -> str | None:
    """校验 5 字段 cron 表达式整体合法性，合法返回 None，否则返回错误说明"""
    fields = cron_expr.strip().split()
    if len(fields) != 5:
        return f"Expected 5 fields, got {len(fields)}"

    # 五个字段各自的合法取值范围（分 时 日 月 周）
    field_rules = [
        ("minute", 0, 59),
        ("hour", 0, 23),
        ("day-of-month", 1, 31),
        ("month", 1, 12),
        ("day-of-week", 0, 6),
    ]
    for field, (name, minimum, maximum) in zip(fields, field_rules):
        error = _validate_cron_field(field, minimum, maximum)
        if error:
            return f"{name}: {error}"
    return None


# ==============================================================================
# 定时任务数据结构与调度器 (CronScheduler)
# ==============================================================================

@dataclass
class CronJob:
    """单个定时任务的数据结构（字段与 .scheduled_tasks.json 中的条目一一对应）"""

    id: str                         # 任务唯一 ID（cron_ 前缀 + 8 位十六进制随机串）
    cron: str                       # 5 字段 cron 表达式（分 时 日 月 周）
    prompt: str                     # 到期后投递给智能体的提示词
    recurring: bool                 # True=周期重复触发；False=一次性任务，投递后自动删除
    durable: bool                   # True=落盘持久化跨重启存活；False=仅本会话内存态
    pending_delivery: bool = False  # 已触发但尚未被智能体回合确认（崩溃重启后补投）
    last_fired: str | None = None   # 上次触发的分钟标记，防止同一分钟内重复触发


class CronScheduler:
    """
    定时任务调度器：
    登记 cron 定时任务，由守护线程每秒轮询一次时间轴；
    到期任务进入待投递队列，趁智能体空闲时投递成 [Scheduled] 用户消息。
    """

    def __init__(self):
        self.jobs: dict[str, CronJob] = {}   # job_id -> 定时任务登记表
        self.queue: list[CronJob] = []       # 已触发、等待投递的任务队列
        self._lock = threading.RLock()       # 保护登记表与队列的并发访问

    # ---- 持久化 ----

    def _save(self):
        """把 durable 任务原子化落盘：先写临时文件再替换，防止写坏旧文件"""
        with self._lock:
            payload = [asdict(job) for job in self.jobs.values() if job.durable]
        # 临时文件名带上 pid+线程 id，避免多线程同时落盘互相覆盖
        temporary = DURABLE_PATH.with_name(
            f"{DURABLE_PATH.name}.{os.getpid()}.{threading.get_ident()}.tmp"
        )
        try:
            temporary.write_text(json.dumps(payload, indent=2), encoding="utf-8")
            os.replace(temporary, DURABLE_PATH)
        finally:
            temporary.unlink(missing_ok=True)

    def load(self):
        """启动时加载持久化的 durable 任务；待投递的任务补进队列防止漏投"""
        if not DURABLE_PATH.exists():
            return
        try:
            payload = json.loads(DURABLE_PATH.read_text(encoding="utf-8"))
            if not isinstance(payload, list):
                raise ValueError("expected a JSON list")
        except (OSError, json.JSONDecodeError, ValueError) as e:
            print(f"\033[33m[cron] could not load {DURABLE_PATH.name}: {e}\033[0m")
            return

        loaded = 0
        with self._lock:
            for item in payload:
                try:
                    job = CronJob(**item)
                    error = validate_cron(job.cron)
                    if error:
                        raise ValueError(error)
                    if not job.id.startswith("cron_"):
                        raise ValueError("invalid job ID")
                    if not job.prompt.strip():
                        raise ValueError("prompt cannot be empty")
                except (TypeError, ValueError) as e:
                    # 逐条校验：损坏的条目只跳过，不让整个调度器起不来
                    print(f"\033[33m[cron] skipped invalid saved job: {e}\033[0m")
                    continue
                self.jobs[job.id] = job
                if job.pending_delivery:
                    self.queue.append(job)
                loaded += 1
        if loaded:
            print(f"\033[36m[cron] loaded {loaded} durable job(s)\033[0m")

    # ---- 任务增删查 ----

    def _new_id(self) -> str:
        """分配唯一的任务 ID；随机串万一撞上就重新生成，最多尝试 100 次"""
        for _ in range(100):
            job_id = f"cron_{secrets.token_hex(4)}"
            if job_id not in self.jobs:
                return job_id
        raise RuntimeError("Could not allocate a cron job ID")

    def schedule(self, cron: str, prompt: str, recurring: bool = True,
                 durable: bool = True) -> CronJob | str:
        """登记定时任务；成功返回 CronJob，参数非法返回错误说明字符串"""
        error = validate_cron(cron)
        if error:
            return error
        if not prompt.strip():
            return "Prompt cannot be empty"

        with self._lock:
            job = CronJob(
                id=self._new_id(),
                cron=cron,
                prompt=prompt,
                recurring=recurring,
                durable=durable,
            )
            self.jobs[job.id] = job
            try:
                if durable:
                    self._save()
            except Exception:
                # 落盘失败立即回滚登记，避免内存里留下重启后会丢失的任务
                self.jobs.pop(job.id, None)
                raise
        print(f"\033[36m[cron] scheduled {job.id}: {cron} -> {prompt[:60]}\033[0m")
        return job

    def cancel(self, job_id: str) -> str:
        """按 ID 取消定时任务（同时从待投递队列摘除）"""
        with self._lock:
            job = self.jobs.get(job_id)
            if job is None:
                return f"Job {job_id} not found"

            # 先留好现场快照，落盘失败时原样恢复
            previous_queue = list(self.queue)
            self.jobs.pop(job_id)
            self.queue[:] = [queued for queued in self.queue if queued.id != job_id]
            try:
                if job.durable:
                    self._save()
            except Exception:
                self.jobs[job_id] = job
                self.queue[:] = previous_queue
                raise
        print(f"\033[36m[cron] cancelled {job_id}\033[0m")
        return f"Cancelled {job_id}"

    def list_jobs(self) -> list[CronJob]:
        """返回全部定时任务的快照列表"""
        with self._lock:
            return list(self.jobs.values())

    # ---- 触发与投递 ----

    def _enqueue_due(self, job: CronJob, minute_marker: str | None = None):
        """
        把到期任务排进待投递队列：先落盘「已触发未投递」状态再入队，
        保证程序中途崩溃后重启也能补投（宁可重投不可漏投）。
        """
        old_pending = job.pending_delivery
        old_last_fired = job.last_fired
        job.pending_delivery = True
        if minute_marker is not None:
            job.last_fired = minute_marker
        try:
            if job.durable:
                self._save()
        except Exception:
            # 落盘失败还原字段并抛出，任务留在队列外等待下一轮扫描
            job.pending_delivery = old_pending
            job.last_fired = old_last_fired
            raise
        self.queue.append(job)

    def poll_due(self, moment: datetime):
        """按当前时刻扫描登记表，把命中 cron 表达式的任务排进待投递队列"""
        minute_marker = moment.strftime("%Y-%m-%d %H:%M")
        with self._lock:
            for job in list(self.jobs.values()):
                try:
                    # 尚未投递确认、或本分钟已触发过的任务跳过，防止重复投递
                    if job.pending_delivery or job.last_fired == minute_marker:
                        continue
                    if cron_matches(job.cron, moment):
                        self._enqueue_due(job, minute_marker)
                        print(f"\033[36m[cron] due {job.id}: {job.prompt[:60]}\033[0m")
                except Exception as e:
                    # 单个任务出错不影响其余任务的扫描
                    print(f"\033[33m[cron] could not enqueue {job.id}: {e}\033[0m")

    def consume(self) -> list[CronJob]:
        """一次性取走待投递队列中的全部任务（清空队列）"""
        with self._lock:
            jobs = list(self.queue)
            self.queue.clear()
        return jobs

    def acknowledge(self, jobs: list[CronJob]):
        """
        确认投递成功：周期任务复位 pending_delivery 等待下次触发，
        一次性任务直接从登记表删除；落盘失败时整体回滚并重新排队。
        """
        changed: list[tuple[CronJob, bool]] = []
        removed: list[CronJob] = []
        with self._lock:
            for delivered in jobs:
                current = self.jobs.get(delivered.id)
                if current is None:
                    continue
                changed.append((current, current.pending_delivery))
                if current.recurring:
                    current.pending_delivery = False
                else:
                    removed.append(current)
                    self.jobs.pop(current.id)

            try:
                if any(job.durable for job, _ in changed):
                    self._save()
            except Exception:
                # 回滚：一次性任务放回登记表，所有任务还原 pending 状态并重新排队
                for job in removed:
                    self.jobs[job.id] = job
                for job, pending in changed:
                    job.pending_delivery = pending
                queued_ids = {job.id for job in self.queue}
                for job, _ in changed:
                    if job.id not in queued_ids:
                        self.queue.append(job)
                raise

    def restore(self, jobs: list[CronJob]):
        """投递失败时把任务放回待投递队列，等待智能体空闲时重试"""
        with self._lock:
            queued_ids = {job.id for job in self.queue}
            for delivered in jobs:
                current = self.jobs.get(delivered.id)
                if current is None:
                    continue
                current.pending_delivery = True
                if current.id not in queued_ids:
                    self.queue.append(current)
                    queued_ids.add(current.id)

    def has_pending(self) -> bool:
        """判断待投递队列是否非空（空闲投递线程据此决定是否唤起 Agent 循环）"""
        with self._lock:
            return bool(self.queue)


# 全局单例调度器：主代理与工具回调共享同一份定时任务登记表
CRON = CronScheduler()


# ==============================================================================
# 定时任务的上下文注入（Agent 循环投递入口）
# ==============================================================================

class ScheduledDelivery:
    """
    一次定时任务投递的事务句柄：
    模型成功响应后调用 acknowledge 确认投递；调用失败时调用 rollback 还原上下文并重新排队，
    保证「要么投递被消化、要么原样退回队列」，不会出现漏投或半投状态。
    """

    def __init__(self, jobs: list[CronJob], start: int,
                 target: dict | None = None, origin=None):
        self.jobs = jobs                # 本次投递的任务列表（确认/回滚后清空）
        self.start = start              # 新建 user 消息时的插入下标（回滚截断点）
        self.target = target            # 被原地追加的目标消息（None 表示新建了消息）
        self.origin = origin            # 目标消息追加前的原始 content（回滚时还原）

    @property
    def request_text(self) -> str:
        """本次投递的提示词全文，可作为压缩要保留的任务主线"""
        return "\n".join(f"[Scheduled] {job.prompt}" for job in self.jobs)

    def acknowledge(self):
        """模型成功响应后确认投递：周期任务复位、一次性任务删除"""
        jobs, self.jobs = self.jobs, []
        try:
            CRON.acknowledge(jobs)
        except Exception as e:
            # 确认失败时 acknowledge 内部已把任务重新排队，宁可重投也不漏投
            print(f"\033[33m[cron] acknowledgement failed: {e}\033[0m")

    def rollback(self, messages: list):
        """投递未被确认即失败：还原注入的上下文并把任务放回队列，等待空闲重试"""
        if not self.jobs:
            return
        if self.target is not None:
            # 原地追加的消息：还原成注入前的 content 快照
            self.target["content"] = self.origin
        elif len(messages) > self.start:
            # 新建的消息：从插入点整体截断
            del messages[self.start:]
        CRON.restore(self.jobs)
        self.jobs = []


def inject_scheduled_jobs(messages: list) -> ScheduledDelivery | None:
    """
    把待投递的到期任务包装成 [Scheduled] 文本块并入对话历史（上下文注入）：
    若上一条已是 user 消息则原地追加文本块，否则新建一条 user 消息
    （与后台任务通知 inject_background_results 保持同一套角色规则）；
    队列为空时返回 None。
    """
    jobs = CRON.consume()
    if not jobs:
        return None

    for job in jobs:
        print(f"\033[36m[cron] delivered {job.id}: {job.prompt[:60]}\033[0m")
    blocks = [{"type": "text", "text": f"[Scheduled] {job.prompt}"} for job in jobs]

    # 尾条是 user 消息（用户输入或工具结果）：留快照后原地追加，回滚时还原
    if messages and messages[-1].get("role") == "user":
        target = messages[-1]
        content = target.get("content", "")
        if isinstance(content, list):
            origin = list(content)
            content.extend(blocks)
        else:
            # 尾条是纯文本输入：升级为块列表后追加定时提示词
            origin = content
            target["content"] = [{"type": "text", "text": str(content)}, *blocks]
        return ScheduledDelivery(jobs, len(messages), target, origin)

    # 尾条是 assistant 消息：定时提示词必须以 user 角色出现，另起一条消息
    start = len(messages)
    messages.append({"role": "user", "content": blocks})
    return ScheduledDelivery(jobs, start)


# ==============================================================================
# 定时任务工具的回调实现（Tool Handlers）
# ==============================================================================

def run_schedule_cron(cron: str, prompt: str, recurring: bool = True,
                      durable: bool = True) -> str:
    """schedule_cron 工具回调：登记定时任务并回执任务 ID 与排期信息"""
    result = CRON.schedule(cron, prompt, recurring, durable)
    if isinstance(result, str):
        return f"Error: {result}"
    return f"Scheduled {result.id}: {cron} -> {prompt}"


def run_list_crons() -> str:
    """list_crons 工具回调：以清单形式渲染全部定时任务的排期与属性"""
    jobs = CRON.list_jobs()
    if not jobs:
        return "No cron jobs."

    lines = []
    for job in jobs:
        # 展示任务的重复模式与存储模式，便于模型判断是否需要调整排期
        frequency = "recurring" if job.recurring else "one-shot"
        storage = "durable" if job.durable else "session"
        lines.append(
            f"{job.id}: {job.cron} -> {job.prompt[:60]} "
            f"[{frequency}, {storage}]"
        )
    return "\n".join(lines)


def run_cancel_cron(job_id: str) -> str:
    """cancel_cron 工具回调：按 ID 取消定时任务"""
    return CRON.cancel(job_id)


# ==============================================================================
# 工具列表（供 Anthropic Tool Use 注册）
# ==============================================================================

CRON_TOOLS = [
{
    "name": "schedule_cron",
    "description": "Schedule a prompt with a 5-field cron expression (minute hour day-of-month month day-of-week) in local time.",
    "input_schema": {
        "type": "object",
        "properties": {
            "cron": {
                "type": "string",
                "description": "The 5-field cron expression, e.g. '0 9 * * *' for 09:00 every day."
            },
            "prompt": {
                "type": "string",
                "description": "The prompt to run when the schedule fires."
            },
            "recurring": {
                "type": "boolean",
                "description": "Optional: true (default) keeps the job firing repeatedly; false makes it one-shot and deletes it after the first delivery."
            },
            "durable": {
                "type": "boolean",
                "description": "Optional: true (default) persists the job across restarts in .scheduled_tasks.json; false keeps it in this session only."
            }
        },
        "required": ["cron", "prompt"],
        "additionalProperties": False
    }
},
{
    "name": "list_crons",
    "description": "List scheduled cron jobs.",
    "input_schema": {
        "type": "object",
        "properties": {}
    }
},
{
    "name": "cancel_cron",
    "description": "Cancel a cron job by ID.",
    "input_schema": {
        "type": "object",
        "properties": {
            "job_id": {
                "type": "string",
                "description": "The ID of the cron job to cancel, e.g. cron_a1b2c3d4."
            }
        },
        "required": ["job_id"],
        "additionalProperties": False
    }
},
]

# ==============================================================================
# 工具映射表
# ==============================================================================

CRON_HANDLERS = {
    "schedule_cron": run_schedule_cron,
    "list_crons": run_list_crons,
    "cancel_cron": run_cancel_cron,
}


# ==============================================================================
# 调度运行时线程（时间轴轮询 + 空闲投递）
# ==============================================================================

RUNTIME_STOP = threading.Event()   # 运行时停止事件：置位后所有调度线程退出
_runtime_threads: list[threading.Thread] = []
_runtime_started = False
_runtime_lock = threading.Lock()


def _scheduler_loop(stop_event: threading.Event):
    """时间轴轮询线程：每秒扫描一次登记表，把命中 cron 表达式的任务排进待投递队列"""
    while not stop_event.wait(1.0):
        CRON.poll_due(datetime.now())


def _queue_processor_loop(stop_event: threading.Event, run_turn, agent_lock):
    """
    空闲投递线程：待投递队列非空且智能体空闲（拿到回合锁）时唤起一轮 Agent 循环；
    智能体正忙时任务留在队列里排队，等其收工后再投递，绝不打断进行中的回合。
    """
    while not stop_event.wait(0.2):
        if not CRON.has_pending() or not agent_lock.acquire(blocking=False):
            continue
        try:
            # 持锁后二次确认：避免拿锁期间队列已被别的回合消费完而空跑
            if CRON.has_pending():
                run_turn()
        finally:
            agent_lock.release()


def start_cron_runtime(run_turn, agent_lock: threading.Lock):
    """
    启动定时任务运行时（幂等）：先加载持久化任务，再拉起两个守护线程。
    run_turn 为空闲投递回调（由 main 注入），agent_lock 为智能体回合互斥锁。
    """
    global _runtime_started
    with _runtime_lock:
        if _runtime_started:
            return
        CRON.load()
        RUNTIME_STOP.clear()
        _runtime_threads.extend([
            # 这个线程时刻扫描所有job，判断是否到期
            threading.Thread(
                target=_scheduler_loop,
                args=(RUNTIME_STOP,),
                name="cron-scheduler",
                daemon=True,
            ),
            # 这个线程判断是否有到期任务，如果有，尝试获取锁执行定时任务
            threading.Thread(
                target=_queue_processor_loop,
                args=(RUNTIME_STOP, run_turn, agent_lock),
                name="cron-queue-processor",
                daemon=True,
            ),
        ])
        for thread in _runtime_threads:
            thread.start()
        _runtime_started = True


def stop_cron_runtime():
    """停止定时任务运行时线程（程序退出前调用，与 start_cron_runtime 配对）"""
    global _runtime_started
    with _runtime_lock:
        if not _runtime_started:
            return
        RUNTIME_STOP.set()
        for thread in _runtime_threads:
            thread.join(timeout=1)
        _runtime_threads.clear()
        _runtime_started = False
