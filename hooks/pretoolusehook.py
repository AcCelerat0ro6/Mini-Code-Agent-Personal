from pathlib import Path
import os
import subprocess
import re

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
            print(f"\n\033[33m[permission] Potentially destructive command\033[0m")
            print(f"   Tool: {block.name}({block.input})")
            choice = input("   Allow? [y/N] ").strip().lower()
            if choice not in ("y", "yes"):
                return "Permission denied by user"

    # 限制文件操作只能在 WORKDIR 及其子目录下进行
    if block.name in ("read_file", "write_file", "edit_file"):
        path = block.input.get("path", "")
        if not (WORKDIR / path).resolve().is_relative_to(WORKDIR):
            print(f"\n\033[33m[permission] Access outside workspace\033[0m")
            print(f"   Tool: {block.name}({block.input})")
            choice = input("   Allow? [y/N] ").strip().lower()
            if choice not in ("y", "yes"):
                return "Permission denied by user"
    return None

def log_hook(block):
    """PreToolUse 钩子：打印即将调用的工具和参数概览作为日志。"""
    args_preview = str(list(block.input.values())[:2])[:60]
    print(f"\033[90m[HOOK] {block.name}({args_preview})\033[0m")
    return None


