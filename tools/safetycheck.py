import re
from pathlib import Path
import os
import subprocess

WORKDIR = os.getenv("WORK_DIR", Path.cwd())

#拒绝命令列表
DENY_LIST = ["rm -rf /", "sudo", "shutdown", "reboot", "mkfs", "dd if=", "> /dev/sda"]

def check_deny_list(command: str) -> str | None:
    """检查命令是否含有绝不允许执行的最高危指令"""
    for pattern in DENY_LIST:
        if pattern in command:
            return f"Blocked: '{pattern}' is on the deny list"
    return None

# 正则匹配形如 "rm" 或 "del" 作为独立命令出现的位置（包含行首、分号、管道符之后）
DESTRUCTIVE_COMMAND_WORD = re.compile(
    r"(?i)(?:^|[;&|()\n])\s*(?:rm|del)(?=\s|$|[;&|()])"
)

def contains_destructive_command(command: str) -> bool:
    """检测命令中是否含有独立存在的删除指令词"""
    return bool(DESTRUCTIVE_COMMAND_WORD.search(command))

PERMISSION_RULES = [
    # 规则 1：检查读写操作是否试图逃逸出当前 WORKDIR 工作区
    {
        "tools": ["read_file", "write_file", "edit_file"],
        "check": lambda args: not (WORKDIR / args.get("path", "")).resolve().is_relative_to(WORKDIR),
        "message": "Writing outside workspace"
    },
    # 规则 2：检查 Bash 是否包含危险删除命令或敏感路径覆盖/权限提升
    {
        "tools": ["bash"],
        "check": lambda args: contains_destructive_command(args.get("command", "")) or
        any(kw in args.get("command", "") for kw in ["rm ", "> /etc/", "chmod 777"]),
        "message": "Potentially destructive command"
    },
]

def check_rules(tool_name: str, args: dict) -> str | None:
    """遍历规则列表，若触发任一安全规则则返回对应的告警原因"""
    for rule in PERMISSION_RULES:
        if tool_name in rule["tools"] and rule["check"](args):
            return rule["message"]
    return None

def ask_user(tool_name: str, args: dict, reason: str) -> str:
    """人工审核: 输出黄色警告提示风险，由用户输入 y/yes 确认是否继续放行"""
    print(f"\n\033[33m[permission] {reason}\033[0m")
    print(f"   Tool: {tool_name}({args})")
    choice = input("   Allow? [y/N] ").strip().lower()
    return "allow" if choice in ("y", "yes") else "deny"

def check_permission(block) -> bool:
    """
    权限检查总入口，串联三道关卡：
    1. 若为 Bash 工具，先跑 Gate 1 严格黑名单；命中则直接阻断并标红返回。
    2. 跑 Gate 2 规则检查；命中规则时触发 Gate 3 询问用户。
    3. 用户选择拒绝则中止执行，反之放行。
    """
    if block.name == "bash":
        reason = check_deny_list(block.input.get("command", ""))
        if reason:
            print(f"\n\033[31m[blocked] {reason}\033[0m")
            return False

    reason = check_rules(block.name, block.input)
    if reason:
        decision = ask_user(block.name, block.input, reason)
        if decision == "deny":
            return False

    return True