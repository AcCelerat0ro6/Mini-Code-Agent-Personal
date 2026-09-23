import os
from pathlib import Path
WORKDIR = os.getenv("WORK_DIR", Path.cwd())
def context_inject_hook(query: str):
    """UserPromptSubmit 钩子：在用户输入准备好后，打印当前工作目录作为上下文日志。"""
    print(f"\033[90m[HOOK] UserPromptSubmit: working in {WORKDIR}\033[0m")
    return None