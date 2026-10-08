from pathlib import Path
import os
import subprocess
import re
import threading

from tools.mcpmanager import mcp_tool_policies

WORKDIR = os.getenv("WORK_DIR", Path.cwd())

# 危险命令黑名单，用于权限安全检查
DENY_LIST = ["rm -rf /", "sudo", "shutdown", "reboot", "mkfs", "dd if="]
# 用于匹配潜在危险删除命令的正则表达式
DESTRUCTIVE_COMMAND_WORD = re.compile(
    r"(?i)(?:^|[;&|()\n])\s*(?:rm|del)(?=\s|$|[;&|()])"
)
DESTRUCTIVE = ["rm ", "> /etc/", "chmod 777"]


def contains_destructive_command(command: str) -> bool:
    """检查命令中是否包含破坏性操作关键字"""
    return bool(DESTRUCTIVE_COMMAND_WORD.search(command))


# ---- 具体钩子回调函数实现 ----

def request_permission(block, reason: str) -> str | None:
    """
    交互式权限确认：打印拦截原因并询问用户是否放行，放行返回 None、拒绝返回拒绝信息。
    定时任务回合与团队成员线程都运行在后台，不能与主线程抢终端输入做交互审批，
    此时直接拒绝，让智能体改用无需提权的做法（符合无人值守回合的语义）。
    """
    if threading.current_thread() is not threading.main_thread():
        return "Permission denied: unattended turns cannot request interactive approval"

    print(f"\n\033[33m[permission] {reason}\033[0m")
    print(f"   Tool: {block.name}({block.input})")
    choice = input("   Allow? [y/N] ").strip().lower()
    if choice not in ("y", "yes"):
        return "Permission denied by user"
    return None


def permission_hook(block):
    """PreToolUse 钩子：权限检查。拦截危险操作或越权文件访问。
       返回值：若返回值非None, 则权限检查不通过
    """
    if block.name == "bash":
        command = block.input.get("command", "")
        # 绝对拦截黑名单命令
        for pattern in DENY_LIST:
            if pattern in command:
                print(f"\n\033[31m[blocked] '{pattern}'\033[0m")
                return "Permission denied by deny list"
        # 询问是否放行存在潜在风险的命令
        if contains_destructive_command(command) or any(
                kw in command for kw in DESTRUCTIVE
        ):
            return request_permission(block, "Potentially destructive command")

    # 限制文件操作只能在 WORKDIR 及其子目录下进行
    if block.name in ("read_file", "write_file", "edit_file"):
        path = block.input.get("path", "")
        if not (WORKDIR / path).resolve().is_relative_to(WORKDIR):
            return request_permission(block, "Access outside workspace")

    # MCP 动态工具：授权策略来自 host 配置（tools/mcpmanager），
    # 未显式放行（allow）的工具每次调用都要确认；无人值守回合里
    # request_permission 会直接拒绝，让模型改用无需审批的替代方案
    if block.name.startswith("mcp__") and mcp_tool_policies.get(
            block.name, "confirm") != "allow":
        return request_permission(block, f"MCP tool {block.name}")
    return None

def log_hook(block):
    """PreToolUse 钩子：打印即将调用的工具和参数概览作为日志。"""
    args_preview = str(list(block.input.values())[:2])[:60]
    print(f"\033[90m[HOOK] {block.name}({args_preview})\033[0m")
    return None


