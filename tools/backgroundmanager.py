"""
tools/backgroundmanager.py - 后台任务管理模块 (Background Tasks)

可选异步调用：bash 工具带 run_in_background=True 时命令不再阻塞智能体主循环，
而是丢给后台守护线程执行；主循环继续推理，待后续轮次再把结果回收注入上下文。

    主线程 (Agent Loop)                         后台线程 (Background Thread)
    +-------------------------------+         +--------------------------+
    | bash(run_in_background=True)  | ------> | 执行命令并格式化结果        |
    | 立即返回 task_id (bg_0001)     |         | 结果登记进就绪队列 _ready   |
    | 继续 Agent 循环（不阻塞）       | <------+--------------------------+
    | 下一轮 inject_background_results         |
    | 把 <task_notification> 注入 messages      |
    +-------------------------------+

关键入口 (Key entry points):

    should_run_background     判断本次工具调用是否走可选异步调用
    start_background_task     在后台线程启动命令，立即返回任务 ID
    collect_background_results 收割已完成任务，生成 <task_notification> 通知
    inject_background_results  把通知并入 messages 上下文，供下一轮模型推理消费
"""

import atexit
import os
import signal
import subprocess
import threading
import time
from pathlib import Path

# 工作区路径（可通过环境变量 WORK_DIR 覆盖）
WORKDIR = os.getenv("WORK_DIR", Path.cwd())


# ==============================================================================
# 底层命令执行与进程清理
# ==============================================================================

# 全局 shell 进程登记表：记录尚未收尸的命令进程，程序退出时统一清理
_shell_processes: set[subprocess.Popen] = set()
_shell_process_lock = threading.RLock()


def _stop_process_group(process: subprocess.Popen):
    """终止命令所在进程组/进程树中残留的进程（跨平台实现）。"""
    if os.name == "nt":
        # Windows：进程已自然退出就不再收尸，避免 PID 被复用后误杀无关进程
        if process.poll() is not None:
            return
        # 用 taskkill /T 整棵终止进程树，等价于 POSIX 的整组 killpg
        subprocess.run(["taskkill", "/PID", str(process.pid), "/T", "/F"],
                       capture_output=True)
        return
    # POSIX：先发 SIGTERM 让其优雅退出，短暂停顿后仍存活再补 SIGKILL
    for sig in (signal.SIGTERM, signal.SIGKILL):
        try:
            os.killpg(process.pid, sig)
        except (ProcessLookupError, OSError):
            return
        time.sleep(0.05)


def _stop_all_shell_processes():
    """遍历进程登记表，停止所有残留的命令进程。"""
    with _shell_process_lock:
        processes = list(_shell_processes)
    for process in processes:
        _stop_process_group(process)


def _handle_termination_signal(signum, _frame):
    """进程收到终止信号时：先清理残留子进程，再以 128+signum 退出。"""
    _stop_all_shell_processes()
    raise SystemExit(128 + signum)


# 程序退出或被终止信号击中时，确保后台命令进程不被遗留在系统里
atexit.register(_stop_all_shell_processes)
try:
    signal.signal(signal.SIGTERM, _handle_termination_signal)
except ValueError:
    # 信号处理器只能在主线程注册，非主线程导入时忽略即可（仍有 atexit 兜底）
    pass


def _run_bash_process(command: str) -> tuple[str, int | None]:
    """
    在独立进程组中执行 shell 命令（同步等待，最多 120 秒）。
    返回 (输出文本, 退出码)；退出码为 None 表示超时或启动失败。
    """
    process = None
    try:
        # Windows 用 CREATE_NEW_PROCESS_GROUP、POSIX 用 start_new_session，
        # 让命令脱离当前进程组，退出时才能整组清理其残留子进程
        popen_kwargs = (
            {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP}
            if os.name == "nt" else {"start_new_session": True}
        )
        process = subprocess.Popen(
            command,
            shell=True,
            cwd=WORKDIR,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True, errors="replace",
            **popen_kwargs,
        )
        # 登记到全局进程表，供退出钩子统一收尸
        with _shell_process_lock:
            _shell_processes.add(process)
        stdout, stderr = process.communicate(timeout=120)
        output = (stdout + stderr).strip()
        # 控制返回内容长度，防止过大输出耗尽 LLM 上下文（Context Window）
        return (output[:50000] if output else "(no output)"), process.returncode
    except subprocess.TimeoutExpired:
        return "Error: Timeout (120s)", None
    except OSError as e:
        return f"Error: {type(e).__name__}: {e}", None
    finally:
        if process is not None:
            _stop_process_group(process)
            try:
                process.wait(timeout=0.2)
            except subprocess.TimeoutExpired:
                pass
            with _shell_process_lock:
                _shell_processes.discard(process)


def _format_bash_result(output: str, exit_code: int | None) -> str:
    """把命令输出与退出码格式化为统一的工具结果文本。"""
    if exit_code in (0, None):
        return output
    return f"Error: command exited with status {exit_code}\n{output}"


# ==============================================================================
# 后台任务管理器 (BackgroundManager)
# ==============================================================================

class BackgroundManager:
    """
    后台任务管理器：
    登记后台 bash 任务，在守护线程中执行命令；任务完成后进入就绪队列，
    由 collect/inject_background_results 在后续轮次收割并注入上下文。
    """

    def __init__(self):
        self.tasks: dict[str, dict] = {}    # task_id -> {tool_use_id, command, status}
        self.results: dict[str, str] = {}   # task_id -> 格式化后的执行结果
        self._ready: list[str] = []         # 已完成、等待收割的任务 ID 队列
        self._counter = 0                   # 任务 ID 自增计数器
        self._lock = threading.Lock()

    def start(self, block) -> str:
        """
        启动一个后台任务（仅支持 bash 工具），立即返回形如 bg_0001 的任务 ID。
        实际命令在守护线程中执行，不阻塞当前智能体循环。
        """
        if block.name != "bash":
            raise ValueError("Only Bash commands can run in the background")
        command = block.input.get("command")
        if not isinstance(command, str) or not command.strip():
            raise ValueError("Bash command cannot be empty")
        # 与 tools.run_bash 的危险命令黑名单保持一致：后台执行同样拒绝这些命令
        dangerous = ["rm -rf /", "sudo", "shutdown", "reboot", "> /dev/"]
        if any(d in command for d in dangerous):
            raise ValueError("Dangerous command blocked")

        # 先登记任务再起线程，保证任务 ID 与执行顺序严格对应
        with self._lock:
            self._counter += 1
            task_id = f"bg_{self._counter:04d}"
            self.tasks[task_id] = {
                "tool_use_id": block.id,
                "command": command,
                "status": "running",
            }

        # 守护线程：主进程退出时随之消亡，残留子进程由 atexit 钩子收尸
        thread = threading.Thread(
            target=self._run,
            args=(task_id, command),
            daemon=True,
        )
        try:
            thread.start()
        except Exception:
            # 线程启动失败时回滚任务登记，避免留下永远 running 的僵尸任务
            with self._lock:
                self.tasks.pop(task_id, None)
            raise
        print(f"\033[90m[background] started {task_id}: {command[:60]}\033[0m")
        return task_id

    def _run(self, task_id: str, command: str):
        """后台线程体：执行命令并把结果登记进就绪队列。"""
        try:
            output, exit_code = _run_bash_process(command)
            result = _format_bash_result(output, exit_code)
            status = "completed" if exit_code == 0 else "failed"
        except Exception as e:
            result = f"Error: {type(e).__name__}: {e}"
            status = "failed"

        with self._lock:
            # 加锁操作 确保任务并发安全完成
            task = self.tasks.get(task_id)
            if task is None:
                return
            task["status"] = status
            self.results[task_id] = result
            self._ready.append(task_id)

    def collect(self) -> list[str]:
        """收割所有已完成任务，生成 <task_notification> 通知并清空登记。"""
        with self._lock:
            ready = []
            for task_id in self._ready:
                task = self.tasks.pop(task_id, None)
                result = self.results.pop(task_id, "")
                if task is not None:
                    ready.append((task_id, task, result))
            self._ready.clear()

        notifications = []
        for task_id, task, result in ready:
            # 通知用 XML 包裹并截断摘要，供模型在后续轮次消费后台执行结果
            notifications.append(
                f"<task_notification>\n"
                f"  <task_id>{task_id}</task_id>\n"
                f"  <status>{task['status']}</status>\n"
                f"  <command>{task['command']}</command>\n"
                f"  <summary>{result[:500]}</summary>\n"
                f"</task_notification>"
            )
            print(f"\033[90m[background] collected {task_id}: {task['status']}\033[0m")
        return notifications

    def running(self) -> list[str]:
        """返回仍在后台运行的任务 ID 列表，供收工前提醒模型别遗忘任务。"""
        with self._lock:
            return [task_id for task_id, task in self.tasks.items()
                    if task["status"] == "running"]


# 全局单例：主代理与子代理共享同一份后台任务登记表
BACKGROUND = BackgroundManager()


# ==============================================================================
# 可选异步调用的调度与上下文注入入口
# ==============================================================================

def should_run_background(tool_name: str, tool_input: dict) -> bool:
    """判断本次工具调用是否为「bash + run_in_background=True」的可选异步调用。"""
    return (
        tool_name == "bash"
        and tool_input.get("run_in_background") is True
    )


def start_background_task(block) -> str:
    """启动后台任务的模块级快捷入口，返回任务 ID。"""
    return BACKGROUND.start(block)


def collect_background_results() -> list[str]:
    """收割已完成后台任务，返回 <task_notification> 通知列表。"""
    return BACKGROUND.collect()


def inject_background_results(messages: list) -> int:
    """
    把已完成后台任务的通知并入对话历史（上下文注入）：
    若上一条已是 user 消息则原地追加文本块，否则新建一条 user 消息；
    返回本次注入的通知数量（0 表示暂无后台结果就绪）。
    """
    notifications = collect_background_results()
    if not notifications:
        return 0

    blocks = [{"type": "text", "text": item} for item in notifications]
    if messages and messages[-1].get("role") == "user":
        content = messages[-1].get("content", "")
        if isinstance(content, list):
            # 尾条是工具结果列表：把通知文本块直接并排追加到同一回合
            content.extend(blocks)
        else:
            # 尾条是纯文本输入：升级为块列表后追加通知
            messages[-1]["content"] = [
                {"type": "text", "text": str(content)},
                *blocks,
            ]
    else:
        # 尾条是 assistant 消息：通知必须以 user 角色出现，另起一条消息
        messages.append({"role": "user", "content": blocks})
    return len(notifications)
