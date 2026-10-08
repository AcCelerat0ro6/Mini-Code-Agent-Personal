# ==============================================================================
# 任务系统 (Task System)：带依赖关系的持久化任务图
# ==============================================================================
# 每个任务节点以 JSON 文件形式持久化存储在 .tasks/ 目录下，例如：
#
#     .tasks/
#       task_a1b2c3d4.json  {status: completed, blockedBy: []}
#       task_e5f6a7b8.json  {status: pending, blockedBy: [task_a1b2c3d4]}
#       task_11223344.json  {status: pending, blockedBy: [task_e5f6a7b8]}
#
# 任务之间通过 blockedBy 字段构成有向无环图（DAG）依赖关系：
#
#     +-----------+      +-----------+      +-----------+
#     | schema    | ---> | API       | ---> | tests     |
#     | completed |      | pending   |      | pending   |
#     +-----------+      +-----------+      +-----------+
#
#     can_start(API) 为 True，因为它依赖的 schema 已完成。
#
# 任务生命周期：
#
#     pending --claim_task--> in_progress --complete_task--> completed
# ==============================================================================

import json
import os
import re
import secrets
import threading
from dataclasses import asdict, dataclass
from pathlib import Path

# 工作区路径（与 tools.py 保持一致：优先读取 WORK_DIR 环境变量，否则取当前目录）
WORKDIR = Path(os.getenv("WORK_DIR", Path.cwd()))

# 任务持久化目录（位于工作区根目录下的 .tasks/）
TASKS_DIR = WORKDIR / ".tasks"
# 任务 ID 命名规则：task_ 前缀 + 8 位十六进制随机串
TASK_ID_PATTERN = re.compile(r"^task_[0-9a-f]{8}$")

# 任务文件读写锁：主线程与团队成员线程（teammanager）会并发认领/完成任务，
# 统一串行化对 .tasks/ 下任务文件的「读取-校验-写回」序列，防止同一任务被双认领
TASK_LOCK = threading.RLock()


@dataclass
class Task:
    """单个任务节点的数据结构（字段与 .tasks/task_*.json 一一对应）"""

    id: str                 # 任务唯一 ID（运行时随机生成），如 task_a1b2c3d4
    subject: str            # 任务标题：一句话概括要做的事
    description: str        # 任务详细描述（可为空串）
    status: str             # 任务状态：pending / in_progress / completed
    owner: str | None       # 认领者标识，未被认领时为 None
    blockedBy: list[str]    # 前置依赖任务的 ID 列表，全部完成前不可认领
    worktree: str | None = None  # 任务绑定的 Git 工作树名（.worktrees/ 下），为空表示直接在工作区作业


class TaskStore:
    """任务仓库：负责 .tasks/ 下任务 JSON 文件的读写、校验与依赖关系维护"""

    def __init__(self, directory: Path):
        self.directory = directory

    def _root(self, create: bool = False) -> Path:
        """返回任务仓库根目录（可选自动创建），并校验其没有逃逸出工作区"""
        if create:
            self.directory.mkdir(parents=True, exist_ok=True)
        root = self.directory.resolve()
        # 安全检查：仓库目录必须位于工作区内部，防御路径穿越攻击
        if not root.is_relative_to(WORKDIR.resolve()):
            raise ValueError("Task store escapes the workspace")
        return root

    def _path(self, task_id: str, create_root: bool = False) -> Path:
        """根据任务 ID 计算对应的 JSON 文件路径，并做 ID 合法性与越权校验"""
        # 严格校验 ID 格式，拒绝非法构造的 ID（如含路径分隔符）
        if not isinstance(task_id, str) or not TASK_ID_PATTERN.fullmatch(task_id):
            raise ValueError(f"Invalid task ID: {task_id!r}")
        root = self._root(create=create_root)
        path = (root / f"{task_id}.json").resolve()
        # 双重保险：解析后的真实路径必须仍在仓库目录内部
        if not path.is_relative_to(root):
            raise ValueError(f"Invalid task ID: {task_id!r}")
        return path

    def exists(self, task_id: str) -> bool:
        """判断任务文件是否存在"""
        return self._path(task_id).is_file()

    def create(self, subject: str, description: str = "") -> Task:
        """创建任务：随机分配唯一 ID，以独占写（'x'）方式落盘，避免并发覆盖"""
        subject = subject.strip()
        if not subject:
            raise ValueError("Task subject cannot be empty")

        self._root(create=True)
        with TASK_LOCK:
            # 随机 ID 几乎不会重复；万一撞上就重新生成，最多尝试 100 次
            for _ in range(100):
                task = Task(
                    id=f"task_{secrets.token_hex(4)}",
                    subject=subject,
                    description=description,
                    status="pending",
                    owner=None,
                    blockedBy=[],
                )
                try:
                    with self._path(task.id, create_root=True).open(
                        "x", encoding="utf-8"
                    ) as handle:
                        json.dump(asdict(task), handle, indent=2)
                    return task
                except FileExistsError:
                    continue
        raise RuntimeError("Could not allocate a unique task ID")

    def _depends_on(self, task_id: str, target_id: str) -> bool:
        """bfs遍历 判断 task_id 是否（直接或间接）依赖 target_id，用于依赖环检测"""
        pending = [task_id]
        visited = set()
        while pending:
            current = pending.pop()
            if current == target_id:
                return True
            if current in visited:
                continue
            visited.add(current)
            # 沿 blockedBy 边不断向上游追溯
            pending.extend(self.load(current).blockedBy)
        return False

    def update_dependencies(self, task_id: str,
                            add_blocked_by: list[str]) -> Task:
        """为任务追加前置依赖（仅允许 pending 且未认领时修改），并做环检测"""
        if not isinstance(add_blocked_by, list):
            raise ValueError("addBlockedBy must be a list of task IDs")

        with TASK_LOCK:
            task = self.load(task_id)
            # 依赖关系一旦开工/被认领就不允许再改，保证执行视图稳定
            if task.status != "pending" or task.owner is not None:
                raise ValueError(
                    f"Task {task_id} dependencies can only be updated while "
                    "pending and unowned"
                )

            # 去重并保持原有顺序
            dependencies = list(dict.fromkeys(add_blocked_by))
            for dependency in dependencies:
                # 不允许自连接
                if dependency == task_id:
                    raise ValueError("Task cannot depend on itself")
                if not self.exists(dependency):
                    raise ValueError(f"Dependency not found: {dependency}")
                # 若新依赖已经（间接）依赖当前任务，添加后会形成环，直接拒绝
                if dependency not in task.blockedBy and self._depends_on(
                    dependency, task_id
                ):
                    raise ValueError(
                        f"Dependency cycle detected: {task_id} -> {dependency}"
                    )

            task.blockedBy.extend(
                dependency for dependency in dependencies
                if dependency not in task.blockedBy
            )
            self.save(task)
            return task

    def save(self, task: Task) -> None:
        """把任务全量写回对应的 JSON 文件"""
        with TASK_LOCK:
            self._path(task.id, create_root=True).write_text(
                json.dumps(asdict(task), indent=2),
                encoding="utf-8",
            )

    def load(self, task_id: str) -> Task:
        """读取任务文件并做字段校验（ID 匹配、状态合法）"""
        with TASK_LOCK:
            data = json.loads(self._path(task_id).read_text(encoding="utf-8"))
            task = Task(**data)
            if task.id != task_id:
                raise ValueError(f"Task file ID does not match {task_id}")
            if task.status not in ("pending", "in_progress", "completed"):
                raise ValueError(f"Invalid task status: {task.status}")
            return task

    def list(self) -> list[Task]:
        """按文件名排序返回全部任务（仓库目录不存在时返回空列表）"""
        if not self.directory.exists():
            return []
        with TASK_LOCK:
            root = self._root()
            return [self.load(path.stem)
                    for path in sorted(root.glob("task_*.json"))]


# 全局单例任务仓库
TASKS = TaskStore(TASKS_DIR)


# ==============================================================================
# 任务操作的高层封装（供工具回调与业务逻辑使用）
# ==============================================================================

def create_task(subject: str, description: str = "") -> Task:
    """创建任务节点（ID 由运行时随机生成）"""
    return TASKS.create(subject, description)


def update_task(task_id: str, addBlockedBy: list[str]) -> Task:
    """为任务追加前置依赖任务 ID 列表"""
    return TASKS.update_dependencies(task_id, addBlockedBy)


def load_task(task_id: str) -> Task:
    """按 ID 加载单个任务"""
    return TASKS.load(task_id)


def list_tasks() -> list[Task]:
    """列出全部任务"""
    return TASKS.list()


def get_task(task_id: str) -> str:
    """返回单个任务的紧凑 JSON 字符串（供工具输出，单行展示避免结果过长）"""
    return json.dumps(asdict(load_task(task_id)), ensure_ascii=False,
                      separators=(", ", ": "))


def incomplete_dependencies(task: Task) -> list[str]:
    """收集任务所有尚未完成（或文件已丢失）的前置依赖 ID"""
    incomplete = []
    for dependency in task.blockedBy:
        try:
            if load_task(dependency).status != "completed":
                incomplete.append(dependency)
        except (FileNotFoundError, ValueError):
            # 依赖文件丢失或损坏时视为未完成，保守地阻止开工
            incomplete.append(dependency)
    return incomplete


def can_start(task_id: str) -> bool:
    """判断任务的所有前置依赖是否都已完成，即能否开工"""
    return not incomplete_dependencies(load_task(task_id))


def claim_task(task_id: str, owner: str = "agent") -> str:
    """认领任务：校验状态与依赖后置为 in_progress，并记录归属者"""
    with TASK_LOCK:
        task = load_task(task_id)
        if task.status != "pending":
            return f"Task {task_id} is {task.status}, cannot claim"
        # 绑定了工作树的任务拥有专属工作目录，主代理固定在 WORKDIR 下作业落不到该目录，
        # 此类任务只应由持有工作目录绑定的团队成员（teammanager）认领
        if task.worktree and owner == "agent":
            return (f"Task {task_id} is bound to worktree '{task.worktree}'; "
                    "delegate it to a teammate")
        # 前置依赖未全部完成时拒绝认领
        dependencies = incomplete_dependencies(task)
        if dependencies:
            return f"Blocked by: {dependencies}"
        task.owner = owner
        task.status = "in_progress"
        TASKS.save(task)
        # 终端日志统一使用 [TASK:*] 品牌前缀 + 品红色，与黄色的 [TODO:*] 清单输出区分开
        # （前缀只用 ASCII，避免 GBK 终端编码 emoji 报错）
        print(f"\033[35m[TASK:claim] {task.id}: {task.subject} "
              f"-> in_progress (owner: {owner})\033[0m")
        return f"Claimed {task.id} ({task.subject})"


def complete_task(task_id: str, owner: str = "agent") -> str:
    """完成任务：置为 completed，并提示因本次完成而新解锁的后续任务"""
    with TASK_LOCK:
        task = load_task(task_id)
        if task.status != "in_progress":
            return f"Task {task_id} is {task.status}, cannot complete"
        # 只有任务归属者本人才能完成任务
        if task.owner != owner:
            return f"Task {task_id} is owned by {task.owner}, not {owner}"
        # 先记录完成前就已解锁的待办任务，稍后做差集找出新解锁的任务
        ready_before = {
            candidate.id
            for candidate in list_tasks()
            if candidate.status == "pending"
            and candidate.blockedBy
            and can_start(candidate.id)
        }
        task.status = "completed"
        TASKS.save(task)
        unblocked = [candidate.subject for candidate in list_tasks()
                     if candidate.status == "pending"
                     and candidate.blockedBy
                     and candidate.id not in ready_before
                     and can_start(candidate.id)]
        print(f"\033[35m[TASK:done] {task.id}: {task.subject}\033[0m")
        message = f"Completed {task.id} ({task.subject})"
        if unblocked:
            message += f"\nUnblocked: {', '.join(unblocked)}"
            print(f"\033[35m[TASK:unblocked] {', '.join(unblocked)}\033[0m")
        return message


# ==============================================================================
# 任务系统工具的回调实现（Tool Handlers）
# ==============================================================================

def run_create_task(subject: str, description: str = "") -> str:
    """create_task 工具回调：创建任务节点并返回运行时生成的 ID"""
    task = create_task(subject, description)
    print(f"\033[35m[TASK:create] {task.id}: {task.subject}\033[0m")
    return f"Created {task.id}: {task.subject}"


def run_update_task(task_id: str, addBlockedBy: list[str]) -> str:
    """update_task 工具回调：用 create_task 返回的真实 ID 追加依赖关系"""
    task = update_task(task_id, addBlockedBy)
    dependencies = ", ".join(task.blockedBy) or "(none)"
    print(f"\033[35m[TASK:update] {task.id} blockedBy: {dependencies}\033[0m")
    return f"Updated {task.id} blockedBy: {dependencies}"


def run_list_tasks() -> str:
    """list_tasks 工具回调：以清单形式渲染全部任务的状态、归属与依赖"""
    tasks = list_tasks()
    if not tasks:
        return "No tasks. Use create_task to add some."
    lines = ["## Task Graph (.tasks/)"]
    done = 0
    # 结果长度控制：最多展示 30 条，超出部分折叠
    shown = tasks[:30]
    for task in shown:
        # 状态标记（○ 待办 / → 进行中 / ● 已完成），与 todo 清单的 [ ]/[>]/[x] 视觉区分
        # 标记只用 GBK 可编码符号，防止工具结果被终端日志打印时编码报错
        marker = {
            "pending": "○",
            "in_progress": "→",
            "completed": "●",
        }.get(task.status, "?")
        if task.status == "completed":
            done += 1
        # 单行长度控制：超长标题截断，依赖列表最多展示 3 个
        subject = (task.subject if len(task.subject) <= 30
                   else task.subject[:30] + "...")
        blocked = task.blockedBy[:3]
        dependencies = (
            f" (blockedBy: {', '.join(blocked)}"
            f"{', ...' if len(task.blockedBy) > 3 else ''})"
            if task.blockedBy else ""
        )
        owner = f" [{task.owner}]" if task.owner else ""
        lines.append(
            f"{marker} {task.id}: {subject} "
            f"[{task.status}]{owner}{dependencies}"
        )
    if len(tasks) > 30:
        lines.append(f"... ({len(tasks) - 30} more tasks)")
    lines.append(f"\n({done}/{len(tasks)} completed)")
    return "\n".join(lines)


def run_get_task(task_id: str) -> str:
    """get_task 工具回调：返回单个任务的 JSON 详情"""
    return get_task(task_id)


def run_claim_task(task_id: str) -> str:
    """claim_task 工具回调：以 agent 身份认领任务"""
    return claim_task(task_id, owner="agent")


def run_complete_task(task_id: str) -> str:
    """complete_task 工具回调：以 agent 身份完成自己认领的任务"""
    return complete_task(task_id, owner="agent")


# ==============================================================================
# 工具列表（供 Anthropic Tool Use 注册）
# ==============================================================================

TASK_TOOLS = [
{
    "name": "create_task",
    "description": "Create a task and return its runtime-generated ID.",
    "input_schema": {
        "type": "object",
        "properties": {
            "subject": {
                "type": "string",
                "description": "A short title summarizing the task."
            },
            "description": {
                "type": "string",
                "description": "Optional detailed description of the task."
            }
        },
        "required": ["subject"],
        "additionalProperties": False
    }
},
{
    "name": "update_task",
    "description": "Add dependencies using IDs returned by create_task.",
    "input_schema": {
        "type": "object",
        "properties": {
            "task_id": {
                "type": "string",
                "pattern": "^task_[0-9a-f]{8}$",
                "description": "The ID of the task whose dependencies are being updated."
            },
            "addBlockedBy": {
                "type": "array",
                "items": {
                    "type": "string",
                    "pattern": "^task_[0-9a-f]{8}$"
                },
                "minItems": 1,
                "description": "The IDs of tasks that must be completed before this task can start."
            }
        },
        "required": ["task_id", "addBlockedBy"],
        "additionalProperties": False
    }
},
{
    "name": "list_tasks",
    "description": "List tasks with status, owner, and dependencies.",
    "input_schema": {
        "type": "object",
        "properties": {}
    }
},
{
    "name": "get_task",
    "description": "Get a task by ID.",
    "input_schema": {
        "type": "object",
        "properties": {
            "task_id": {
                "type": "string",
                "description": "The ID of the task to fetch."
            }
        },
        "required": ["task_id"]
    }
},
{
    "name": "claim_task",
    "description": "Claim a pending task whose dependencies are complete.",
    "input_schema": {
        "type": "object",
        "properties": {
            "task_id": {
                "type": "string",
                "description": "The ID of the task to claim."
            }
        },
        "required": ["task_id"]
    }
},
{
    "name": "complete_task",
    "description": "Complete the task claimed by this agent.",
    "input_schema": {
        "type": "object",
        "properties": {
            "task_id": {
                "type": "string",
                "description": "The ID of the claimed task to complete."
            }
        },
        "required": ["task_id"]
    }
},
]

# ==============================================================================
# 工具映射表
# ==============================================================================

TASK_HANDLERS = {
    "create_task": run_create_task,
    "update_task": run_update_task,
    "list_tasks": run_list_tasks,
    "get_task": run_get_task,
    "claim_task": run_claim_task,
    "complete_task": run_complete_task,
}
