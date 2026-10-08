"""
tools/teammanager.py - 智能体团队协作模块 (Agent Teams)

持久化团队成员协作：Lead（主智能体）孵化多个常驻 teammate 线程，
通过共享任务看板（.tasks/，与任务系统模块共用）和文件信箱（.mailboxes/）分工协作；
成员在自己的线程里跑独立的小型 Agent 循环，空闲时自动认领任务板上就绪的任务。

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

    .tasks/       共享任务看板：Lead 建任务并补依赖，成员认领后作业
    .mailboxes/   每个成员一个 JSONL 信箱，读取即清空（破坏性读）
    .worktrees/   可选的任务绑定 Git 工作树：成员工具的默认 cwd

关键入口 (Key entry points):

    run_spawn_teammate     spawn_teammate 工具回调：孵化一个常驻成员线程
    run_list_teammates     list_teammates 工具回调：列出活跃成员及状态
    run_send_message       send_message 工具回调：向成员发送消息
    run_request_shutdown   request_shutdown 工具回调：请求成员优雅关闭
    run_request_plan       request_plan 工具回调：要求成员先提交计划再动手
    run_review_plan        review_plan 工具回调：审批/驳回成员提交的计划
    run_create_worktree    create_worktree 工具回调：为任务创建绑定工作树
    collect_team_events    收割 Lead 信箱并格式化成团队事件文本
    start_team_runtime     启动团队事件唤醒投递守护线程
    stop_team_runtime      停止唤醒线程（程序退出前调用）
"""

import json
import os
import random
import re
import secrets
import subprocess
import threading
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path

from client.client import client
from tools import task_system
from tools.tools import BASE_TOOLS, execute_tool, run_bash, run_edit, \
    run_glob, run_read, run_write

# 工作区路径（与 tools.py 保持一致：优先读取 WORK_DIR 环境变量，否则取当前目录）
WORKDIR = Path(os.getenv("WORK_DIR", Path.cwd()))
MODEL = os.environ["MODEL_ID"]

# 协调者信箱名：Lead 的团队事件都投递到 .mailboxes/lead.jsonl
LEAD_NAME = "lead"
# 成员空闲时扫描任务看板的间隔（秒），同时也是信箱阻塞等待的超时粒度
IDLE_SCAN_INTERVAL = 2.0

# 成员协作日志统一使用亮青色 + [TEAM:*] 前缀，与 [TASK:*] 品红、[cron] 青色、
# [background] 灰色的终端输出区分开（前缀只用 ASCII，避免 GBK 终端编码报错）
TEAM_COLOR = "\033[96m"

# 信箱持久化目录（位于工作区根目录下的 .mailboxes/）
MAILBOX_DIR = WORKDIR / ".mailboxes"
MAILBOX_ROOT = MAILBOX_DIR.resolve()
# 成员名合法性：1-64 位字母/数字/下划线/中划线（同时用作信箱文件名）
VALID_AGENT_NAME = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
# 保留名：lead 是协调者信箱；agent 是主代理在任务系统中的默认归属者
RESERVED_TEAMMATE_NAMES = {"lead", "agent"}


def is_valid_agent_name(name: str) -> bool:
    """成员名合法性：1-64 位字母/数字/下划线/中划线"""
    return bool(isinstance(name, str) and VALID_AGENT_NAME.fullmatch(name))


# ==============================================================================
# 消息总线 (MessageBus)：线程安全的文件信箱，读取即清空
# ==============================================================================

class MessageBus:
    """
    文件信箱消息总线：
    每个成员对应 .mailboxes/<name>.jsonl 一个追加写文件，send 追加一行 JSON；
    read_inbox 一次性取走全部消息并删除文件（破坏性读），避免重复消费。
    Condition 变量让等待消息的线程休眠挂起，新消息到达时统一唤醒。
    """

    def __init__(self):
        self._lock = threading.RLock()
        self._changed = threading.Condition(self._lock)

    def _path(self, agent: str) -> Path:
        """计算成员信箱文件路径，并做名字合法性与路径穿越校验"""
        if not is_valid_agent_name(agent):
            raise ValueError(f"Invalid mailbox recipient: {agent!r}")
        path = (MAILBOX_DIR / f"{agent}.jsonl").resolve()
        if not path.is_relative_to(MAILBOX_ROOT):
            raise ValueError(f"Mailbox path escapes directory: {agent!r}")
        return path

    def _read_unlocked(self, agent: str) -> list[dict]:
        """取走信箱里的全部消息并删除文件（调用前须持有 self._lock）"""
        inbox = self._path(agent)
        if not inbox.exists():
            return []
        messages = [
            json.loads(line)
            for line in inbox.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
        inbox.unlink()
        return messages

    def send(self, from_agent: str, to_agent: str, content: str,
             msg_type: str = "message", metadata: dict | None = None):
        """向目标信箱追加一条消息并唤醒所有等待者"""
        message = {"from": from_agent, "to": to_agent, "content": content,
                   "type": msg_type, "ts": time.time(),
                   "metadata": metadata or {}}
        with self._changed:
            MAILBOX_DIR.mkdir(parents=True, exist_ok=True)
            with self._path(to_agent).open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(message, ensure_ascii=True) + "\n")
            self._changed.notify_all()
        print(f"{TEAM_COLOR}[TEAM:bus] {from_agent} -> {to_agent}: "
              f"({msg_type}) {content[:50]}\033[0m")

    def read_inbox(self, agent: str) -> list[dict]:
        """非阻塞地取走信箱全部消息（无消息返回空列表）"""
        with self._lock:
            return self._read_unlocked(agent)

    def peek(self, agent: str) -> bool:
        """判断信箱里是否有待读消息（不消费）"""
        with self._lock:
            inbox = self._path(agent)
            return inbox.exists() and inbox.stat().st_size > 0

    def wait_for_messages(self, agent: str,
                          timeout: float | None = None) -> list[dict]:
        """阻塞直到信箱出现消息或超时；返回取走的消息（破坏性读）"""
        deadline = None if timeout is None else time.monotonic() + timeout
        with self._changed:
            while not self.peek(agent):
                remaining = (None if deadline is None
                             else deadline - time.monotonic())
                if remaining is not None and remaining <= 0:
                    return []
                self._changed.wait(remaining)
            return self._read_unlocked(agent)


# 全局单例消息总线：Lead 与全部成员共享
BUS = MessageBus()


# ==============================================================================
# 团队状态登记表与协议状态（全部由 team_lock 保护）
# ==============================================================================

@dataclass
class ProtocolState:
    """一条协议请求（计划审批/关闭）的跟踪状态"""

    request_id: str
    type: str                        # shutdown | plan_approval
    sender: str                      # 请求发起方
    target: str                      # 请求接收方
    status: str                      # pending | approved | rejected
    payload: str                     # 计划正文（关闭请求为空串）
    work_version: int | None = None  # 提交计划时的工作版本号（指派变更后旧请求作废）
    task_id: str | None = None       # 提交计划时绑定的任务 ID
    created_at: float = field(default_factory=time.time)


# name -> working | waiting_approval | idle | stopping
active_teammates: dict[str, str] = {}
# name -> not_required | required | pending | approved | rejected（计划门状态机）
plan_gates: dict[str, str] = {}
# name -> 当前等待审批的计划请求 ID
plan_request_ids: dict[str, str] = {}
# request_id -> 协议请求状态（计划审批/关闭请求共用）
pending_requests: dict[str, ProtocolState] = {}
# name -> {"task_id": str, "cwd": Path}：成员当前任务与绑定的工作目录（单任务租约）
teammate_assignments: dict[str, dict] = {}
# name -> 工作版本号：任务指派一旦变更即递增，旧的计划审批请求随之作废
assignment_versions: dict[str, int] = {}
# name -> 成员线程（登记用途，便于调试与状态排查）
teammate_threads: dict[str, threading.Thread] = {}
# 团队登记表总锁：保护以上全部字典；
# 如需同时拿任务文件锁，必须先 team_lock 后 TASK_LOCK，全模块保持同一加锁顺序
team_lock = threading.RLock()


def new_request_id() -> str:
    """分配不冲突的协议请求 ID（req_ + 6 位数字）"""
    with team_lock:
        while True:
            request_id = f"req_{random.randint(0, 999999):06d}"
            if request_id not in pending_requests:
                return request_id


def match_response(response_type: str, request_id: str, approve: bool,
                   from_agent: str, to_agent: str) -> bool:
    """把一条协议响应匹配到对应的待决请求上（类型/双方/状态三重校验）"""
    with team_lock:
        state = pending_requests.get(request_id)
        if not state:
            print(f"{TEAM_COLOR}[TEAM:protocol] unknown request_id: {request_id}\033[0m")
            return False
        expected = {
            "shutdown": "shutdown_response",
            "plan_approval": "plan_approval_response",
        }[state.type]
        if response_type != expected:
            print(f"{TEAM_COLOR}[TEAM:protocol] expected {expected}, "
                  f"got {response_type}\033[0m")
            return False
        if from_agent != state.target or to_agent != state.sender:
            print(f"{TEAM_COLOR}[TEAM:protocol] {request_id} responder mismatch\033[0m")
            return False
        if state.status != "pending":
            print(f"{TEAM_COLOR}[TEAM:protocol] {request_id} already {state.status}\033[0m")
            return False
        state.status = "approved" if approve else "rejected"
    print(f"{TEAM_COLOR}[TEAM:protocol] {request_id} -> {state.status}\033[0m")
    return True


def consume_lead_inbox() -> list[dict]:
    """收割 Lead 信箱：先按协议校验响应并更新请求状态，再交由上层格式化投递"""
    messages = BUS.read_inbox(LEAD_NAME)
    for message in messages:
        metadata = message.get("metadata", {})
        request_id = metadata.get("request_id", "")
        if request_id and message.get("type", "").endswith("_response"):
            match_response(message["type"], request_id,
                           metadata.get("approve", False),
                           message.get("from", ""), message.get("to", ""))
    return messages


def format_team_events(messages: list[dict]) -> str:
    """把收割到的团队事件渲染成一段 [Team events] 用户消息文本"""
    lines = []
    for message in messages:
        metadata = message.get("metadata", {})
        request_id = metadata.get("request_id")
        suffix = f" request_id={request_id}" if request_id else ""
        lines.append(
            f"[{message['type']}{suffix}] {message['from']}: {message['content']}"
        )
    return "[Team events]\n" + "\n".join(lines)


def collect_team_events() -> str | None:
    """收割 Lead 信箱并格式化成事件文本；无事件返回 None（供唤醒投递回合调用）"""
    messages = consume_lead_inbox()
    if not messages:
        return None
    return format_team_events(messages)


def running_teammates() -> list[str]:
    """返回仍活跃的成员名单（供 Lead 收工前的未完事项提醒）"""
    with team_lock:
        return sorted(active_teammates)


# ==============================================================================
# 任务绑定工作树 (Worktrees)：可选的独立工作目录
# ==============================================================================

WORKTREES_DIR = WORKDIR / ".worktrees"
WORKTREES_ROOT = WORKTREES_DIR.resolve()
VALID_WORKTREE_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")


def validate_worktree_name(name: str) -> str | None:
    """校验工作树名合法性，合法返回 None，否则返回错误说明"""
    if not isinstance(name, str) or not VALID_WORKTREE_NAME.fullmatch(name):
        return ("worktree name must be 1-64 letters, digits, dots, "
                "underscores, or dashes, and start with a letter or digit")
    if name in {".", ".."} or ".." in name:
        return "worktree name cannot contain '..'"
    return None


def _worktree_path(name: str) -> Path:
    """计算工作树目录路径，并做路径穿越校验"""
    path = (WORKTREES_DIR / name).resolve()
    if (not WORKTREES_ROOT.is_relative_to(WORKDIR.resolve())
            or not path.is_relative_to(WORKTREES_ROOT)
            or path == WORKTREES_ROOT):
        raise ValueError(f"Worktree path escapes directory: {name!r}")
    return path


def _worktree_branch(name: str) -> str:
    """工作树对应的专用分支名（wt/ 前缀）"""
    return f"wt/{name}"


def _run_git(args: list[str], cwd: Path | None = None) -> tuple[bool, str]:
    """以参数列表方式执行 Git（不经 shell 展开），合并 stdout/stderr 返回"""
    try:
        result = subprocess.run(
            ["git", *args], cwd=cwd or WORKDIR,
            capture_output=True, text=True, errors="replace", timeout=30,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return False, f"{type(exc).__name__}: {exc}"
    output = (result.stdout + result.stderr).strip()
    return result.returncode == 0, output or "(no output)"


def run_git(args: list[str], cwd: Path | None = None) -> tuple[bool, str]:
    """执行 Git 并限制回传给模型的文本长度（防止异常输出刷爆上下文）"""
    ok, output = _run_git(args, cwd)
    return ok, output[:5000]


def _registered_worktrees() -> tuple[dict[Path, dict[str, str]], str | None]:
    """读取 Git 的工作树注册表（--porcelain 输出解析为 路径 -> 元数据）"""
    ok, output = _run_git(["worktree", "list", "--porcelain"])
    if not ok:
        return {}, f"cannot read Git worktree registry: {output}"
    entries: dict[Path, dict[str, str]] = {}
    current: dict[str, str] = {}
    for line in output.splitlines() + [""]:
        if not line:
            raw_path = current.get("worktree")
            if raw_path:
                entries[Path(raw_path).resolve()] = current
            current = {}
            continue
        key, _, value = line.partition(" ")
        current[key] = value
    return entries, None


def _registered_worktree(name: str) -> tuple[Path | None, str | None]:
    """校验工作树已在 Git 注册、目录存在且挂在预期分支上，返回 (路径, 错误)"""
    try:
        path = _worktree_path(name)
    except ValueError as exc:
        return None, str(exc)
    entries, error = _registered_worktrees()
    if error:
        return None, error
    if path not in entries:
        return None, f"worktree '{name}' is not registered with Git"
    if not path.is_dir():
        return None, f"worktree '{name}' is missing at {path}"
    expected_branch = f"refs/heads/{_worktree_branch(name)}"
    if entries[path].get("branch") != expected_branch:
        return None, (f"worktree '{name}' is not registered on expected "
                      f"branch '{_worktree_branch(name)}'")
    return path, None


def task_worktree_cwd(task: task_system.Task) -> tuple[Path, str | None]:
    """解析任务的工具默认 cwd；工作树绑定损坏时保守报错（fail-closed）"""
    if not task.worktree:
        return WORKDIR, None
    return _registered_worktree(task.worktree)


def create_worktree(name: str, task_id: str) -> str:
    """
    为任务创建并绑定专属工作树（全部输入校验通过后才真正落盘）：
    git worktree add 失败时逐项排查遗留产物（目录/注册表/分支），如实回报部分失败状态；
    任务绑定失败时保留 Git 产物供人工恢复，绝不静默删除任何 Git 数据。
    """
    error = validate_worktree_name(name)
    if error:
        return f"Error: {error}"
    try:
        path = _worktree_path(name)
    except ValueError as exc:
        return f"Error: {exc}"

    with team_lock:
        try:
            task = task_system.load_task(task_id)
        except (FileNotFoundError, ValueError) as exc:
            return f"Error: {exc}"
        if task.status != "pending" or task.owner is not None:
            return f"Error: Task {task_id} must be pending and unowned"
        if task.worktree:
            return f"Error: Task {task_id} already uses worktree '{task.worktree}'"
        if any(t.worktree == name for t in task_system.list_tasks()
               if t.id != task_id):
            return f"Error: Worktree '{name}' is already bound to another task"
        if path.exists():
            return f"Error: Worktree path already exists: {path}"

        ok, root = run_git(["rev-parse", "--show-toplevel"])
        if not ok or Path(root).resolve() != WORKDIR.resolve():
            return "Error: Working directory must be the root of a Git repository"
        branch = _worktree_branch(name)
        ok, branch_check = run_git(["check-ref-format", "--branch", branch])
        if not ok:
            return f"Error: Invalid worktree branch '{branch}': {branch_check}"
        exists, _ = run_git(["show-ref", "--verify", "--quiet",
                             f"refs/heads/{branch}"])
        if exists:
            return f"Error: Branch '{branch}' already exists"
        entries, registry_error = _registered_worktrees()
        if registry_error:
            return f"Error: {registry_error}"
        if path in entries:
            return f"Error: Worktree path is already registered: {path}"

        WORKTREES_DIR.mkdir(parents=True, exist_ok=True)
        ok, result = run_git(["worktree", "add", "-b", branch,
                              str(path), "HEAD"])
        if not ok:
            # 部分失败排查：git worktree add 报错但可能已留下目录/注册/分支产物
            entries, registry_error = _registered_worktrees()
            branch_exists, _ = run_git(
                ["show-ref", "--verify", "--quiet", f"refs/heads/{branch}"])
            artifacts = []
            if path.exists():
                artifacts.append(f"checkout path '{path}'")
            if registry_error is None and path in entries:
                artifacts.append("registered Git worktree")
            if branch_exists:
                artifacts.append(f"branch '{branch}'")
            if artifacts:
                return (
                    "Partial operation: git worktree add reported an error "
                    f"after leaving {', '.join(artifacts)}. Task {task_id} "
                    "remains unbound and no Git data was deleted. Run "
                    f"`git worktree list`, inspect '{path}' and '{branch}', "
                    "then keep or remove those artifacts manually after "
                    f"preserving any work. Git error: {result}"
                )
            return f"Git error: {result}"

        try:
            task.worktree = name
            task_system.TASKS.save(task)
        except Exception as exc:
            return (f"Partial success: Worktree '{name}' was created at "
                    f"{path} on branch '{branch}', but task binding failed: "
                    f"{exc}. Git data was retained for manual recovery.")

    print(f"{TEAM_COLOR}[TEAM:worktree] created: {name} at {path}\033[0m")
    return f"Worktree '{name}' created at {path} for task {task_id}"


def remove_worktree(name: str, discard_changes: bool = False) -> str:
    """
    移除已注册的工作树（仅限已完结任务；分支一律保留供事后归档）：
    有未提交变更时默认拒绝，需显式 discard_changes 丢弃；解除任务绑定失败时回报部分成功。
    """
    error = validate_worktree_name(name)
    if error:
        return f"Error: {error}"

    with team_lock:
        path, error = _registered_worktree(name)
        if error:
            return f"Error: {error}"
        bound = [task for task in task_system.list_tasks()
                 if task.worktree == name]
        if not bound:
            return f"Error: Worktree '{name}' is not bound to a task"
        active = [task for task in bound if task.status != "completed"]
        if active:
            return (f"Error: Worktree '{name}' is bound to active task "
                    f"{active[0].id}; complete it before removal")
        leased = [owner for owner, assignment in teammate_assignments.items()
                  if Path(assignment["cwd"]).resolve() == path.resolve()]
        if leased:
            return (f"Error: Worktree '{name}' is still in use by "
                    f"{', '.join(sorted(leased))}; wait for the turn to end")
        ok, status = run_git(["status", "--porcelain", "--ignored"], cwd=path)
        if not ok:
            return f"Error: Cannot verify worktree '{name}' status: {status}"
        if status != "(no output)" and not discard_changes:
            changed = len([line for line in status.splitlines() if line.strip()])
            return (f"Error: Worktree '{name}' has {changed} uncommitted "
                    "change(s); preserve or discard them manually")

        args = ["worktree", "remove"]
        if discard_changes:
            args.append("--force")
        args.append(str(path))
        ok, result = run_git(args)
        if not ok:
            return f"Git error: {result}"

        try:
            for task in bound:
                task.worktree = None
                task_system.TASKS.save(task)
        except Exception as exc:
            return (f"Partial success: Worktree '{name}' was removed and "
                    f"branch '{_worktree_branch(name)}' retained, but task "
                    f"unbinding failed: {exc}. Manual recovery is required.")

    print(f"{TEAM_COLOR}[TEAM:worktree] removed: {name}; branch retained\033[0m")
    return (f"Worktree '{name}' removed; "
            f"branch '{_worktree_branch(name)}' retained")


# ==============================================================================
# 成员任务租约与工作目录绑定
# ==============================================================================

def _owner_in_progress(owner: str) -> task_system.Task | None:
    """查找该归属者名下进行中的任务（单任务租约的权威依据）"""
    return next((task for task in task_system.list_tasks()
                 if task.status == "in_progress" and task.owner == owner), None)


def advance_assignment_version(owner: str):
    """任务指派变更时递增工作版本号，令旧的计划审批请求全部作废"""
    with team_lock:
        assignment_versions[owner] = assignment_versions.get(owner, 0) + 1
        # 计划门已处于要求/等待/驳回状态时复位为 required（保留显式的计划要求），并解绑请求 ID
        if plan_gates.get(owner, "not_required") != "not_required":
            plan_gates[owner] = "required"
        plan_request_ids.pop(owner, None)


def assignment_cwd(owner: str) -> Path:
    """
    解析成员当前的工具默认 cwd（单任务租约的工作目录绑定）：
    登记表缺失时从任务看板补建；任务不再是该成员的进行中/已完成任务时视为租约失效。
    """
    with team_lock:
        assignment = teammate_assignments.get(owner)
        task = _owner_in_progress(owner)
        if task and (not assignment or assignment.get("task_id") != task.id):
            # 登记表滞后于看板：按看板上的进行中任务补建租约
            cwd, error = task_worktree_cwd(task)
            if error:
                raise ValueError(error)
            assignment = {"task_id": task.id, "cwd": cwd}
            teammate_assignments[owner] = assignment
        elif not assignment:
            # 没有任何任务指派：退回工作区根目录
            return WORKDIR
        task = task_system.load_task(str(assignment["task_id"]))
        if task.status not in ("in_progress", "completed") or task.owner != owner:
            raise ValueError(f"Assignment for {owner} is no longer active")
        cwd, error = task_worktree_cwd(task)
        if error:
            raise ValueError(error)
        if cwd.resolve() != Path(assignment["cwd"]).resolve():
            raise ValueError(f"Assignment cwd changed for task {task.id}")
        return cwd


def claim_task_as_teammate(task_id: str, owner: str) -> str:
    """
    成员认领任务（在任务系统原子认领之上叠加团队约束）：
    1. 单任务租约：手上还有任务（登记表或看板在案）时不允许认领新的任务；
    2. 认领成功后把工作目录绑定进登记表，成员的全部文件工具随后都落在该目录。
    """
    with team_lock:
        assignment = teammate_assignments.get(owner)
        if assignment:
            return (f"Owner {owner} must finish the current work turn for "
                    f"{assignment['task_id']} before claiming another task")
        current = _owner_in_progress(owner)
        if current:
            return (f"Owner {owner} must complete {current.id} "
                    "before claiming another task")
        result = task_system.claim_task(task_id, owner)
        if not result.startswith("Claimed "):
            return result
        task = task_system.load_task(task_id)
        cwd, error = task_worktree_cwd(task)
        if error:
            # 工作目录不可用（如工作树被手动删除）：回滚认领，任务回到看板
            task.status = "pending"
            task.owner = None
            task_system.TASKS.save(task)
            return f"Cannot claim {task_id}: {error}"
        teammate_assignments[owner] = {"task_id": task.id, "cwd": cwd}
        advance_assignment_version(owner)
    return result


def complete_task_as_teammate(task_id: str, owner: str) -> str:
    """成员完成任务：计划门未放行时拒绝收工；cwd 租约保留到本回合结束再释放"""
    with team_lock:
        gate = plan_gates.get(owner, "not_required")
        if gate in ("required", "pending", "rejected"):
            return f"Task {task_id} cannot complete while plan status is {gate}"
        return task_system.complete_task(task_id, owner)


def release_completed_assignment(owner: str) -> bool:
    """回合边界上释放已完结任务的 cwd 租约（成员转 IDLE 前调用）"""
    with team_lock:
        assignment = teammate_assignments.get(owner)
        if not assignment:
            return False
        task = task_system.load_task(str(assignment["task_id"]))
        if task.status != "completed" or task.owner != owner:
            return False
        teammate_assignments.pop(owner, None)
        advance_assignment_version(owner)
        if owner in plan_gates:
            plan_gates[owner] = "not_required"
        return True


def release_teammate_assignment(owner: str):
    """成员线程退出时回收现场：未完成任务放回看板（重新变为可认领）"""
    with team_lock:
        try:
            task = _owner_in_progress(owner)
            if task:
                task.status = "pending"
                task.owner = None
                task_system.TASKS.save(task)
        finally:
            teammate_assignments.pop(owner, None)
            advance_assignment_version(owner)
            if owner in plan_gates:
                plan_gates[owner] = "not_required"


# ==============================================================================
# 成员协议动作：提交计划 / 发消息 / 响应校验
# ==============================================================================

def _teammate_submit_plan(from_name: str, plan: str) -> str:
    """成员提交工作计划：登记待决请求并置计划门为 pending，等待 Lead 审批"""
    with team_lock:
        assignment = teammate_assignments.get(from_name)
        task_id = str(assignment["task_id"]) if assignment else None
        work_version = assignment_versions.get(from_name, 0)
        if plan_gates.get(from_name) == "pending":
            return "A plan is already waiting for review."
        request_id = new_request_id()
        pending_requests[request_id] = ProtocolState(
            request_id=request_id,
            type="plan_approval",
            sender=from_name,
            target=LEAD_NAME,
            status="pending",
            payload=plan,
            work_version=work_version,
            task_id=task_id,
        )
        plan_gates[from_name] = "pending"
        plan_request_ids[from_name] = request_id
        active_teammates[from_name] = "waiting_approval"
    BUS.send(from_name, LEAD_NAME, plan, "plan_approval_request",
             {"request_id": request_id})
    return f"Plan submitted ({request_id}). Wait for Lead's decision."


def _teammate_send_message(from_name: str, to: str, content: str) -> str:
    """成员间消息：只允许发给 Lead 或当前活跃的成员"""
    with team_lock:
        if to != LEAD_NAME and to not in active_teammates:
            return f"Agent '{to}' is not active"
    BUS.send(from_name, to, content)
    return f"Sent to {to}"


def current_work_identity(owner: str) -> tuple[int, str | None]:
    """读取成员当前的工作版本号与任务 ID（用于校验审批请求是否仍然新鲜）"""
    with team_lock:
        assignment = teammate_assignments.get(owner)
        task_id = str(assignment["task_id"]) if assignment else None
        return assignment_versions.get(owner, 0), task_id


def apply_plan_response(name: str, message: dict) -> tuple[bool, str]:
    """
    成员侧应用 Lead 的计划审批响应：
    请求 ID、双方、工作版本号、任务绑定必须全部匹配当前成员的待决计划才生效，
    过期/错配的响应一律忽略（防止旧审批误放行新任务的写操作）。
    """
    metadata = message.get("metadata", {})
    request_id = metadata.get("request_id", "")
    work_version, task_id = current_work_identity(name)
    with team_lock:
        state = pending_requests.get(request_id)
        expected_id = plan_request_ids.get(name)
        valid = (
            message.get("from") == LEAD_NAME
            and message.get("to") == name
            and request_id == expected_id
            and state is not None
            and state.type == "plan_approval"
            and state.sender == name
            and state.target == LEAD_NAME
            and state.work_version == work_version
            and state.task_id == task_id
            and state.status in ("approved", "rejected")
            and metadata.get("approve", False) == (state.status == "approved")
        )
        if not valid:
            return False, "[Ignored plan response: request mismatch]"
        plan_gates[name] = state.status
        active_teammates[name] = "working"
        plan_request_ids.pop(name, None)
        outcome = state.status
    return True, f"[Plan {outcome}] {message['content']}"


def apply_shutdown_request(name: str, message: dict) -> tuple[bool, str]:
    """成员侧应用 Lead 的关闭请求：只接受 Lead 发给自己且仍待决的请求"""
    request_id = message.get("metadata", {}).get("request_id", "")
    with team_lock:
        state = pending_requests.get(request_id)
        valid = (
            message.get("from") == LEAD_NAME
            and message.get("to") == name
            and state is not None
            and state.type == "shutdown"
            and state.sender == LEAD_NAME
            and state.target == name
            and state.status == "pending"
            and active_teammates.get(name) != "stopping"
        )
        if not valid:
            return False, "[Ignored shutdown request: request mismatch]"
        active_teammates[name] = "stopping"
    return True, request_id


# ==============================================================================
# 成员工具执行入口（计划门 + 统一 execute_tool 包装）
# ==============================================================================

def _run_teammate_tool(name: str, block, handlers: dict) -> str:
    """
    成员工具执行入口：
    计划门未放行（required/pending/rejected）时禁止成员执行 bash 与写类工具，
    迫使其先走 submit_plan -> Lead review_plan 的审批流程；其余照常走
    execute_tool（PreToolUse 钩子照常生效，成员线程的危险命令会被
    无人值守规则直接拒绝，成员只能改请 Lead 代为执行）。
    """
    gate = plan_gates.get(name, "not_required")
    if (block.name in ("bash", "write_file", "edit_file")
            and gate not in ("not_required", "approved")):
        return (f"Blocked: plan status is {gate}. Submit or revise the plan "
                "and wait for approval before changing the workspace.")
    # 成员线程无法收割后台任务结果，剥掉可选异步开关强制同步执行
    block.input.pop("run_in_background", None)
    return execute_tool(block, handlers)


# ==============================================================================
# 成员运行时 (TeammateRuntime)：WORK / IDLE 两阶段循环
# ==============================================================================

def _last_assistant_text(content) -> str:
    """从模型返回的 content 块中提取第一段文本"""
    for block in content:
        if getattr(block, "type", None) == "text":
            return block.text.strip()
        if isinstance(block, dict) and block.get("type") == "text":
            return str(block.get("text", "")).strip()
    return ""


class TeammateRuntime:
    """
    单个常驻成员：
    拥有独立的消息历史与小型 Agent 循环；WORK 阶段持续推理与执行工具，
    直到没有工具调用转 IDLE；IDLE 阶段等待信箱消息或自动认领就绪任务。
    文件类工具的 cwd 一律绑定到当前任务的工作目录。
    """

    def __init__(self, name: str, role: str, prompt: str,
                 task_id: str | None, require_plan: bool):
        self.name = name
        # 成员系统提示词：聚焦分派的任务，最终文本会作为 result 事件回传 Lead
        self.system = (
            f"You are '{name}', a {role}. Use tools to complete the assigned "
            "Task, then call complete_task and report a concise result. "
            "If the first user message contains [Assigned task], that Task is "
            "already claimed; do not call claim_task for it again. "
            "When asked for a plan, call submit_plan and wait for approval "
            "before bash or file changes. File and shell tools use the Task's "
            "working directory; that directory is not a sandbox. The runtime "
            "delivers your final text to Lead. Use send_message only for "
            "intermediate coordination, and address the coordinator as 'lead'."
        )
        self.messages = [{"role": "user", "content": prompt}]
        if task_id:
            task = task_system.load_task(task_id)
            cwd = assignment_cwd(name)
            self.messages[0]["content"] += (
                f"\n\n[Assigned task {task.id}] {task.subject}\n"
                f"{task.description}\nWork directory: {cwd}"
            )
        if require_plan:
            self.messages[0]["content"] += (
                "\n\n[Plan required] Submit a plan and wait for Lead approval "
                "before changing files or using bash."
            )
        # 成员私有工具映射表：文件类工具绑定到任务工作目录，认领/完成走团队约束包装
        self.handlers = {
            "bash": self.bash,
            "read_file": self.read,
            "write_file": self.write,
            "edit_file": self.edit,
            "glob": self.glob,
            "send_message": lambda to, content: _teammate_send_message(
                name, to, content),
            "submit_plan": lambda plan: _teammate_submit_plan(name, plan),
            "list_tasks": task_system.run_list_tasks,
            "claim_task": self.claim,
            "complete_task": self.complete,
        }

    # ---- 绑定工作目录的文件类工具 ----

    def current_cwd(self) -> tuple[Path | None, str | None]:
        """取当前租约的工作目录；没有任务指派时拒绝使用工作区工具"""
        if self.name not in teammate_assignments:
            return None, "Error: Claim a Task before using workspace tools."
        try:
            return assignment_cwd(self.name), None
        except (FileNotFoundError, ValueError) as exc:
            return None, f"Error: Invalid task assignment: {exc}"

    def bash(self, command: str) -> str:
        cwd, error = self.current_cwd()
        return error or run_bash(command, cwd=cwd)

    def read(self, path: str, limit: int | None = None) -> str:
        cwd, error = self.current_cwd()
        return error or run_read(path, limit=limit, cwd=cwd)

    def write(self, path: str, content: str) -> str:
        cwd, error = self.current_cwd()
        return error or run_write(path, content, cwd=cwd)

    def edit(self, path: str, old_text: str, new_text: str) -> str:
        cwd, error = self.current_cwd()
        return error or run_edit(path, old_text, new_text, cwd=cwd)

    def glob(self, pattern: str) -> str:
        cwd, error = self.current_cwd()
        return error or run_glob(pattern, cwd=cwd)

    # ---- 任务认领 / 完成（走团队约束包装） ----

    def claim(self, task_id: str) -> str:
        try:
            return claim_task_as_teammate(task_id, owner=self.name)
        except (FileNotFoundError, ValueError) as exc:
            return f"Error: {exc}"

    def complete(self, task_id: str) -> str:
        try:
            return complete_task_as_teammate(task_id, owner=self.name)
        except (FileNotFoundError, ValueError) as exc:
            return f"Error: {exc}"

    # ---- 收信 ----

    def handle_inbox(self, inbox: list[dict]) -> bool:
        """
        把信箱消息转成成员上下文里的工作消息；返回 True 表示已接受有效关闭请求。
        协议类消息（计划审批响应/关闭请求）先经 apply_* 校验，错配的只作为提示注入。
        """
        work_messages = []
        for message in inbox:
            msg_type = message.get("type", "message")
            if msg_type == "shutdown_request":
                accepted, notice = apply_shutdown_request(self.name, message)
                if not accepted:
                    work_messages.append(notice)
                    continue
                BUS.send(self.name, LEAD_NAME, "Shutdown acknowledged.",
                         "shutdown_response",
                         {"request_id": notice, "approve": True})
                return True
            if msg_type == "plan_approval_response":
                _, notice = apply_plan_response(self.name, message)
                work_messages.append(notice)
                continue
            if msg_type == "plan_request":
                work_messages.append(f"[Plan required] {message['content']}")
                continue
            work_messages.append(
                f"[Message from {message['from']}] {message['content']}"
            )
        if work_messages:
            self.messages.append({"role": "user",
                                  "content": "\n".join(work_messages)})
        return False

    # ---- WORK / IDLE 两阶段主循环 ----

    def work(self) -> str:
        """
        WORK 阶段：跑一轮模型推理与工具执行。
        返回 continue（还有工具结果要消化）、idle（本回合收尾）或 stop（关闭/异常）。
        """
        # 收到有效关闭请求立即停机
        if self.handle_inbox(BUS.read_inbox(self.name)):
            return "stop"
        with team_lock:
            active_teammates[self.name] = "working"
        try:
            response = client.messages.create(
                model=MODEL,
                system=self.system,
                messages=self.messages,
                tools=TEAMMATE_TOOLS,
                max_tokens=8000,
            )
        except Exception as exc:
            # 模型调用失败：把错误回传 Lead 后停机（线程退出时会回收任务现场）
            BUS.send(self.name, LEAD_NAME,
                     f"{type(exc).__name__}: {exc}", "error")
            return "stop"

        self.messages.append({"role": "assistant", "content": response.content})
        tool_calls = [
            block for block in response.content if block.type == "tool_use"
        ]
        if tool_calls:
            results = []
            for block in tool_calls:
                output = _run_teammate_tool(self.name, block, self.handlers)
                results.append({"type": "tool_result",
                                "tool_use_id": block.id,
                                "content": output})
            self.messages.append({"role": "user", "content": results})
            return "continue"

        # 没有工具调用即本回合收尾：最终文本作为 result 事件回传 Lead
        summary = _last_assistant_text(response.content)
        gate = plan_gates.get(self.name, "not_required")
        if gate != "pending" and summary:
            BUS.send(self.name, LEAD_NAME, summary, "result")
        if gate == "pending":
            # 计划还在等审批：转入 waiting_approval 挂起，不发 idle 事件也不放租约
            with team_lock:
                active_teammates[self.name] = "waiting_approval"
        else:
            # 任务完结：释放 cwd 租约并转 IDLE，等待新消息或自动认领
            release_completed_assignment(self.name)
            with team_lock:
                active_teammates[self.name] = "idle"
            BUS.send(self.name, LEAD_NAME, "Waiting for more work.",
                     "idle_notification")
        return "idle"

    def wait_for_work(self) -> bool:
        """
        IDLE 阶段：等待信箱消息或自动认领就绪任务；返回 False 表示成员应退出。
        """
        while True:
            # 阻塞等待信箱消息，超时后借返回空窗期扫描一次任务看板
            inbox = BUS.wait_for_messages(self.name, IDLE_SCAN_INTERVAL)
            if inbox:
                before = len(self.messages)
                if self.handle_inbox(inbox):
                    return False
                if len(self.messages) > before:
                    return True
                continue

            # 信箱空窗期：尝试从任务看板自动认领一个就绪任务
            task = claim_next_task(self.name)
            if not task:
                continue
            cwd = assignment_cwd(self.name)
            self.messages.append({
                "role": "user",
                "content": (
                    f"[Auto-claimed task {task.id}] {task.subject}\n"
                    f"{task.description}\nWork directory: {cwd}"
                ),
            })
            print(f"{TEAM_COLOR}[TEAM:idle] {self.name} claimed "
                  f"{task.id}: {task.subject}\033[0m")
            return True

    def run(self):
        """成员线程体：WORK/IDLE 交替，直到收到关闭请求或发生异常"""
        try:
            state = "continue"
            while state != "stop":
                if state == "idle" and not self.wait_for_work():
                    break
                state = self.work()
        except Exception as exc:
            try:
                BUS.send(self.name, LEAD_NAME,
                         f"{type(exc).__name__}: {exc}", "error")
            except Exception:
                pass
        finally:
            # 线程退出回收现场：未完成任务放回看板，全部登记表条目摘除
            try:
                release_teammate_assignment(self.name)
            except Exception as exc:
                try:
                    BUS.send(self.name, LEAD_NAME,
                             f"Assignment cleanup failed: "
                             f"{type(exc).__name__}: {exc}", "error")
                except Exception:
                    pass
            with team_lock:
                active_teammates.pop(self.name, None)
                plan_gates.pop(self.name, None)
                plan_request_ids.pop(self.name, None)
                teammate_threads.pop(self.name, None)
            print(f"{TEAM_COLOR}[TEAM:finish] {self.name} finished\033[0m")


# ==============================================================================
# 空闲任务发现：成员 IDLE 时自动认领就绪任务
# ==============================================================================

def scan_unclaimed_tasks() -> list[task_system.Task]:
    """扫描任务看板：返回 pending、无人认领、依赖就绪且工作目录可用的任务"""
    ready = []
    for task in task_system.list_tasks():
        if (task.status != "pending" or task.owner is not None
                or not task_system.can_start(task.id)):
            continue
        _, error = task_worktree_cwd(task)
        if not error:
            ready.append(task)
    return ready


def claim_next_task(name: str) -> task_system.Task | None:
    """按看板顺序尝试认领第一个仍可认领的任务；已有租约时绝不二次认领"""
    with team_lock:
        if teammate_assignments.get(name) or _owner_in_progress(name):
            return None
    for task in scan_unclaimed_tasks():
        result = claim_task_as_teammate(task.id, owner=name)
        if result.startswith("Claimed "):
            return task_system.load_task(task.id)
    return None


# ==============================================================================
# 成员孵化 (spawn_teammate_thread)
# ==============================================================================

def spawn_teammate_thread(name: str, role: str, prompt: str,
                          task_id: str | None = None,
                          require_plan: bool = False) -> str:
    """登记成员状态、认领初始任务并启动一个常驻成员线程"""
    if not is_valid_agent_name(name):
        return ("Invalid teammate name: use 1-64 letters, digits, "
                "underscores, or dashes")
    if name.lower() in RESERVED_TEAMMATE_NAMES:
        return f"Invalid teammate name: '{name}' is reserved by the runtime"
    with team_lock:
        if any(existing.casefold() == name.casefold()
               for existing in active_teammates):
            return f"Teammate '{name}' already exists"
        active_teammates[name] = "working"
        plan_gates[name] = "required" if require_plan else "not_required"
        assignment_versions[name] = 0

    if task_id:
        try:
            claimed = task_system.claim_task(task_id, owner=name)
        except (FileNotFoundError, ValueError) as exc:
            claimed = f"Error: {exc}"
        if not claimed.startswith("Claimed "):
            with team_lock:
                active_teammates.pop(name, None)
                plan_gates.pop(name, None)
                assignment_versions.pop(name, None)
            return f"Cannot spawn teammate '{name}': {claimed}"
        # 认领成功后立即绑定工作目录租约；绑定失败则连任务带登记一起回滚
        try:
            task = task_system.load_task(task_id)
            cwd, error = task_worktree_cwd(task)
            if error:
                raise ValueError(error)
            teammate_assignments[name] = {"task_id": task.id, "cwd": cwd}
            advance_assignment_version(name)
        except (FileNotFoundError, ValueError) as exc:
            task = task_system.load_task(task_id)
            task.status = "pending"
            task.owner = None
            task_system.TASKS.save(task)
            with team_lock:
                active_teammates.pop(name, None)
                plan_gates.pop(name, None)
                assignment_versions.pop(name, None)
            return f"Cannot spawn teammate '{name}': {exc}"

    runtime = TeammateRuntime(name, role, prompt, task_id, require_plan)
    thread = threading.Thread(target=runtime.run, daemon=True,
                              name=f"teammate-{name}")
    with team_lock:
        teammate_threads[name] = thread
    thread.start()
    print(f"{TEAM_COLOR}[TEAM:spawn] {name} spawned as {role}\033[0m")
    assigned = f" for {task_id}" if task_id else " without an initial Task"
    return (
        f"Teammate '{name}' spawned as {role}{assigned}. "
        "End this turn; the runtime will deliver its events."
    )


# ==============================================================================
# Lead 团队工具的回调实现 (Tool Handlers)
# ==============================================================================

def run_spawn_teammate(name: str, role: str, prompt: str,
                       task_id: str | None = None,
                       require_plan: bool = False) -> str:
    """spawn_teammate 工具回调：孵化一个常驻成员线程"""
    return spawn_teammate_thread(name, role, prompt, task_id, require_plan)


def run_list_teammates() -> str:
    """list_teammates 工具回调：列出活跃成员及其运行状态"""
    with team_lock:
        if not active_teammates:
            return "No active teammates."
        return "\n".join(
            f"{name}: {status}"
            for name, status in sorted(active_teammates.items())
        )


def run_send_message(to: str, content: str) -> str:
    """send_message 工具回调：Lead 向成员发送消息"""
    with team_lock:
        if to not in active_teammates:
            return f"Teammate '{to}' is not active"
    BUS.send(LEAD_NAME, to, content)
    return f"Sent to {to}"


def run_request_shutdown(teammate: str) -> str:
    """request_shutdown 工具回调：向成员发出带请求 ID 的关闭协议请求"""
    with team_lock:
        if teammate not in active_teammates:
            return f"Teammate '{teammate}' is not active"
        request_id = new_request_id()
        pending_requests[request_id] = ProtocolState(
            request_id=request_id,
            type="shutdown",
            sender=LEAD_NAME,
            target=teammate,
            status="pending",
            payload="",
        )
    BUS.send(LEAD_NAME, teammate, "Finish the current step and shut down.",
             "shutdown_request", {"request_id": request_id})
    return f"Shutdown requested from {teammate} ({request_id})"


def run_request_plan(teammate: str, task: str) -> str:
    """request_plan 工具回调：把成员计划门置为 required 并下发计划要求"""
    with team_lock:
        if teammate not in active_teammates:
            return f"Teammate '{teammate}' is not active"
        plan_gates[teammate] = "required"
    BUS.send(LEAD_NAME, teammate, task, "plan_request")
    return f"Plan requested from {teammate}"


def run_review_plan(request_id: str, approve: bool, feedback: str = "") -> str:
    """
    review_plan 工具回调：审批/驳回成员提交的计划。
    校验请求仍待决、工作版本号与任务绑定未变更（防止对过期计划放行），
    通过后把审批响应发回成员信箱。
    """
    state = pending_requests.get(request_id)
    if not state:
        return f"Request {request_id} not found"
    work_version, task_id = current_work_identity(state.sender)
    with team_lock:
        state = pending_requests.get(request_id)
        if not state:
            return f"Request {request_id} not found"
        if state.type != "plan_approval":
            return f"Request {request_id} is not a plan"
        if state.status != "pending":
            return f"Request {request_id} already {state.status}"
        if state.work_version != work_version or state.task_id != task_id:
            return f"Request {request_id} belongs to an earlier assignment"
        if plan_request_ids.get(state.sender) != request_id:
            return f"Request {request_id} is not the current plan"
        state.status = "approved" if approve else "rejected"
    content = feedback or ("Plan approved." if approve
                           else "Revise the plan and submit it again.")
    BUS.send(LEAD_NAME, state.sender, content, "plan_approval_response",
             {"request_id": request_id, "approve": approve})
    return f"Plan {state.status} ({request_id})"


def run_create_worktree(name: str, task_id: str) -> str:
    """create_worktree 工具回调：为任务创建并绑定专属工作树"""
    return create_worktree(name, task_id)


# ==============================================================================
# 工具列表（供 Anthropic Tool Use 注册）
# ==============================================================================

def _base_tool(name: str) -> dict:
    """从基础工具列表中按名取工具定义（成员工具集复用同一份 schema）"""
    return next(tool for tool in BASE_TOOLS if tool["name"] == name)


# 成员版 bash 工具：成员线程无法收割后台任务结果，
# 去掉可选异步开关 run_in_background，强制成员的命令同步执行
TEAMMATE_BASH_TOOL = {
    "name": "bash",
    "description": _base_tool("bash")["description"],
    "input_schema": {
        "type": "object",
        "properties": {
            "command":
                _base_tool("bash")["input_schema"]["properties"]["command"]
        },
        "required": ["command"],
    },
}

# 成员工具集：基础工具（bash 裁剪版）+ 协作工具 + 任务看板的认领/完成工具
TEAMMATE_TOOLS = [
    TEAMMATE_BASH_TOOL,
    _base_tool("read_file"),
    _base_tool("write_file"),
    _base_tool("edit_file"),
    _base_tool("glob"),
    {
        "name": "send_message",
        "description": "Send an intermediate message to 'lead' or an active teammate.",
        "input_schema": {
            "type": "object",
            "properties": {
                "to": {
                    "type": "string",
                    "description": "Recipient: 'lead' or a teammate name."
                },
                "content": {
                    "type": "string",
                    "description": "The message content."
                }
            },
            "required": ["to", "content"],
            "additionalProperties": False
        }
    },
    {
        "name": "submit_plan",
        "description": "Submit a work plan for Lead approval before changing the workspace.",
        "input_schema": {
            "type": "object",
            "properties": {
                "plan": {
                    "type": "string",
                    "description": "The full plan text to review."
                }
            },
            "required": ["plan"],
            "additionalProperties": False
        }
    },
    next(tool for tool in task_system.TASK_TOOLS if tool["name"] == "list_tasks"),
    next(tool for tool in task_system.TASK_TOOLS if tool["name"] == "claim_task"),
    next(tool for tool in task_system.TASK_TOOLS if tool["name"] == "complete_task"),
]

TEAM_TOOLS = [
{
    "name": "spawn_teammate",
    "description": "Spawn a persistent teammate thread that works on tasks and reports back via team events.",
    "input_schema": {
        "type": "object",
        "properties": {
            "name": {
                "type": "string",
                "pattern": "^[A-Za-z0-9_-]{1,64}$",
                "description": "Unique teammate name (letters, digits, underscores, dashes)."
            },
            "role": {
                "type": "string",
                "description": "A short role title for the teammate, e.g. 'test engineer'."
            },
            "prompt": {
                "type": "string",
                "description": "The initial instruction describing the teammate's mission."
            },
            "task_id": {
                "type": "string",
                "pattern": "^task_[0-9a-f]{8}$",
                "description": "Optional task ID to claim and assign as the teammate's first assignment."
            },
            "require_plan": {
                "type": "boolean",
                "description": "Optional: true requires the teammate to submit a plan and wait for approval before any workspace change."
            }
        },
        "required": ["name", "role", "prompt"],
        "additionalProperties": False
    }
},
{
    "name": "list_teammates",
    "description": "List active teammates and their statuses.",
    "input_schema": {
        "type": "object",
        "properties": {}
    }
},
{
    "name": "send_message",
    "description": "Send a message to an active teammate.",
    "input_schema": {
        "type": "object",
        "properties": {
            "to": {
                "type": "string",
                "description": "The teammate name to send to."
            },
            "content": {
                "type": "string",
                "description": "The message content."
            }
        },
        "required": ["to", "content"],
        "additionalProperties": False
    }
},
{
    "name": "request_shutdown",
    "description": "Ask a teammate to finish the current step and shut down gracefully.",
    "input_schema": {
        "type": "object",
        "properties": {
            "teammate": {
                "type": "string",
                "description": "The teammate name to shut down."
            }
        },
        "required": ["teammate"],
        "additionalProperties": False
    }
},
{
    "name": "request_plan",
    "description": "Require a teammate to submit a plan for approval before changing the workspace.",
    "input_schema": {
        "type": "object",
        "properties": {
            "teammate": {
                "type": "string",
                "description": "The teammate to require a plan from."
            },
            "task": {
                "type": "string",
                "description": "What the plan should cover."
            }
        },
        "required": ["teammate", "task"],
        "additionalProperties": False
    }
},
{
    "name": "review_plan",
    "description": "Approve or reject a teammate's submitted plan by request ID.",
    "input_schema": {
        "type": "object",
        "properties": {
            "request_id": {
                "type": "string",
                "description": "The request_id from the plan_approval_request event."
            },
            "approve": {
                "type": "boolean",
                "description": "true approves the plan; false asks for revision."
            },
            "feedback": {
                "type": "string",
                "description": "Optional feedback delivered with the decision."
            }
        },
        "required": ["request_id", "approve"],
        "additionalProperties": False
    }
},
{
    "name": "create_worktree",
    "description": "Create a dedicated Git worktree and bind it to a pending task so the teammate works in an isolated directory.",
    "input_schema": {
        "type": "object",
        "properties": {
            "name": {
                "type": "string",
                "pattern": "^(?!.*\\.\\.)[A-Za-z0-9][A-Za-z0-9._-]{0,63}$",
                "maxLength": 64,
                "description": "Worktree name; also used as the wt/<name> branch name."
            },
            "task_id": {
                "type": "string",
                "description": "The pending unowned task to bind the worktree to."
            }
        },
        "required": ["name", "task_id"],
        "additionalProperties": False
    }
},
]

# ==============================================================================
# 工具映射表
# ==============================================================================

TEAM_HANDLERS = {
    "spawn_teammate": run_spawn_teammate,
    "list_teammates": run_list_teammates,
    "send_message": run_send_message,
    "request_shutdown": run_request_shutdown,
    "request_plan": run_request_plan,
    "review_plan": run_review_plan,
    "create_worktree": run_create_worktree,
}


# ==============================================================================
# 团队事件唤醒运行时（空闲投递线程）
# ==============================================================================

TEAM_STOP = threading.Event()   # 运行时停止事件：置位后唤醒线程退出
_team_wake_thread: threading.Thread | None = None
_team_runtime_started = False
_team_runtime_lock = threading.Lock()


def _team_wake_loop(stop_event: threading.Event, run_wake_turn, agent_lock):
    """
    团队事件唤醒线程：Lead 信箱有信且智能体空闲（拿到回合锁）时唤起一轮循环。
    智能体正忙时事件留在信箱里排队，等其收工后再投递，绝不打断进行中的回合
    （与定时任务的空闲投递线程同一套约定）。
    """
    while not stop_event.wait(0.2):
        if not BUS.peek(LEAD_NAME):
            continue
        if not agent_lock.acquire(blocking=False):
            continue
        try:
            # 持锁后二次确认：避免拿锁期间事件已被上一轮投递消费完而空跑
            if BUS.peek(LEAD_NAME):
                run_wake_turn()
        except Exception as exc:
            # 唤醒回合失败不让线程死掉：已消费的事件保留在历史里，下轮继续消化
            print(f"\033[33m[TEAM:wake] wake turn failed: "
                  f"{type(exc).__name__}: {exc}\033[0m")
        finally:
            agent_lock.release()


def start_team_runtime(run_wake_turn, agent_lock: threading.Lock):
    """
    启动团队事件唤醒运行时（幂等）。
    run_wake_turn 为空闲投递回调（由 main 注入），agent_lock 为智能体回合互斥锁。
    """
    global _team_wake_thread, _team_runtime_started
    with _team_runtime_lock:
        if _team_runtime_started:
            return
        TEAM_STOP.clear()
        _team_wake_thread = threading.Thread(
            target=_team_wake_loop,
            args=(TEAM_STOP, run_wake_turn, agent_lock),
            name="team-wake",
            daemon=True,
        )
        _team_wake_thread.start()
        _team_runtime_started = True


def stop_team_runtime():
    """停止团队事件唤醒线程（程序退出前调用，与 start_team_runtime 配对）"""
    global _team_runtime_started
    with _team_runtime_lock:
        if not _team_runtime_started:
            return
        TEAM_STOP.set()
        if _team_wake_thread is not None:
            _team_wake_thread.join(timeout=1)
        _team_runtime_started = False
