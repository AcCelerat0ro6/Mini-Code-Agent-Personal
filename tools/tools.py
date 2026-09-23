# 安全沙箱
from pathlib import Path
import os
import subprocess
from tools.todomanager import run_todo_write
from hooks.hook import trigger_hooks
WORKDIR = os.getenv("WORK_DIR", Path.cwd())


# ==============================================================================
# 安全沙箱与四个文件操作工具
# ==============================================================================
def safe_path(p: str) -> Path:
    """
    路径安全校验函数：
    解析输入路径并将其限制在工作空间 (WORKDIR) 内部，防御 '../' 等越权逃逸攻击。
    """
    path = (WORKDIR / p).resolve()
    # 检查目标真实路径是否以当前工作区目录为根
    if not path.is_relative_to(WORKDIR):
        raise ValueError(f"Path escapes workspace: {p}")
    return path

def run_bash(command: str) -> str:
    """在工作区目录下执行 Bash 命令，带有简易危险命令黑名单过滤。"""
    # 基础安全防护：拦截常见的高危破坏性指令
    dangerous = ["rm -rf /", "sudo", "shutdown", "reboot", "> /dev/"]
    if any(d in command for d in dangerous):
        return "Error: Dangerous command blocked"
    try:
        # 执行命令，统一重定向错误流与输出流，设置 120 秒超时上限
        r = subprocess.run(command, shell=True, cwd=WORKDIR,
                           capture_output=True, text=True, errors="replace",
                           timeout=120)
        out = (r.stdout + r.stderr).strip()
        # 控制返回内容长度，防止过大输出耗尽 LLM 上下文（Context Window）
        return out[:50000] if out else "(no output)"
    except subprocess.TimeoutExpired:
        return "Error: Timeout (120s)"
    except (FileNotFoundError, OSError) as e:
        return f"Error: {e}"

def run_read(path: str, limit: int | None = None) -> str:
    """读取指定文件内容，支持限制返回行数以节省 token。"""
    try:
        lines = safe_path(path).read_text(encoding="utf-8").splitlines()
        # 如果指定了行数限制且超限，截取前 limit 行并追加提示
        if limit and limit < len(lines):
            lines = lines[:limit] + [f"... ({len(lines) - limit} more lines)"]
        return "\n".join(lines)
    except Exception as e:
        return f"Error: {e}"

def run_write(path: str, content: str) -> str:
    """创建或全量覆盖写入文件内容（若父级目录不存在会自动递归创建）。"""
    try:
        file_path = safe_path(path)
        file_path.parent.mkdir(parents=True, exist_ok=True)
        file_path.write_text(content, encoding="utf-8")
        return f"Wrote {len(content)} bytes to {path}"
    except Exception as e:
        return f"Error: {e}"

def run_edit(path: str, old_text: str, new_text: str) -> str:
    """
    精确单次替换文件中的指定字符串。
    相比全量重写文件，这种方式能显著降低大文件的 token 消耗与覆盖出错率。
    """
    try:
        file_path = safe_path(path)
        text = file_path.read_text(encoding="utf-8")
        if old_text not in text:
            return f"Error: text not found in {path}"
        # 仅替换首次出现的匹配文本（count=1）
        file_path.write_text(text.replace(old_text, new_text, 1), encoding="utf-8")
        return f"Edited {path}"
    except Exception as e:
        return f"Error: {e}"

def run_glob(pattern: str) -> str:
    """在工作区内按 glob 通配符规则检索文件（支持 ** 跨目录递归搜索）。"""
    import glob as g
    try:
        # 检索文件，并确保每一个匹配到的路径都合法存在于 WORKDIR 内
        matches = sorted({
            match for match in g.glob(
                pattern, root_dir=WORKDIR, recursive=True)
            if (WORKDIR / match).resolve().is_relative_to(WORKDIR)
        })
        # 限制单次最大返回量，避免文件过多导致上下文溢出
        shown = matches[:200]
        if len(matches) > 200:
            shown.append("... (more matches omitted; narrow the pattern)")
        return "\n".join(shown) if shown else "(no matches)"
    except Exception as e:
        return f"Error: {e}"

# ==============================================================================
# 工具列表
# ==============================================================================

BASE_TOOLS = [
{
    "name": "bash",
    "description": "Run a shell command.",
    "input_schema": {
        "type": "object",
        "properties": {
            "command": {
                "type": "string",
                "description": "The bash command to execute."
            }
        },
        "required": ["command"]
    }
},
{
    "name": "read_file",
    "description": "Read file contents.",
    "input_schema": {
        "type": "object",
        "properties": {
            "path": {
                "type": "string",
                "description": "The relative or absolute path to the file."
            },
            "limit": {
                "type": "integer",
                "description": "Maximum number of lines to read from the beginning of the file."
            }
        },
        "required": ["path"]
    }
},
{
    "name": "write_file",
    "description": "Write content to a file.",
    "input_schema": {
        "type": "object",
        "properties": {
            "path": {
                "type": "string",
                "description": "The destination path of the file to create or overwrite."
            },
            "content": {
                "type": "string",
                "description": "The text content to write into the file."
            }
        },
        "required": ["path", "content"]
    }
},
{
    "name": "edit_file",
    "description": "Replace exact text in a file once.",
    "input_schema": {
        "type": "object",
        "properties": {
            "path": {
                "type": "string",
                "description": "The path to the file to edit."
            },
            "old_text": {
                "type": "string",
                "description": "The exact existing text block to be replaced."
            },
            "new_text": {
                "type": "string",
                "description": "The replacement text to insert."
            }
        },
        "required": ["path", "old_text", "new_text"]
    }
},
{
    "name": "todo_write",
    "description": "Create and manage a task list for your current coding session.",
    "input_schema": {
        "type": "object",
        "properties": {
            "todos": {
                "type": "array",
                "description": "The complete list of tasks to update and track.",
                "maxItems": 20,
                "items": {
                    "type": "object",
                    "properties": {
                        "content": {
                            "type": "string",
                            "minLength": 1,
                            "description": "The description of the task or step to be done."
                        },
                        "status": {
                            "type": "string",
                            "enum": ["pending", "in_progress", "completed"],
                            "description": "The current state of the task: 'pending' (not started), 'in_progress' (currently working on), or 'completed' (finished)."
                        }
                    },
                    "required": ["content", "status"]
                }
            }
        },
        "required": ["todos"]
    }
},
{
  "name": "load_skill",
  "description": "Load the full SKILL.md content by skill name.",
  "input_schema": {
    "type": "object",
    "properties": {
      "name": {
        "type": "string",
        "description": "The unique identifier or name of the skill to load (e.g., 'data-analysis')."
      }
    },
    "required": [
      "name"
    ]
  }
}
]

# ==============================================================================
# 工具映射表
# ==============================================================================

BASE_HANDLERS = {
    "bash": run_bash,
    "read_file": run_read,
    "write_file": run_write,
    "edit_file": run_edit,
    "glob": run_glob,
    "todo_write": run_todo_write,
}

def execute_tool(block, handlers: dict) -> str:
    """
    统一的工具执行包装器：
    依次调度 PreToolUse 钩子 -> 执行实际函数 -> 调度 PostToolUse 钩子。
    若被前置钩子拦截，则直接短路返回拒绝信息。
    """
    blocked = trigger_hooks("PreToolUse", block)
    if blocked:
        return str(blocked)

    handler = handlers.get(block.name)
    try:
        output = handler(**block.input) if handler else f"Unknown: {block.name}"
    except Exception as e:
        output = f"Error: {e}"

    trigger_hooks("PostToolUse", block, output)
    return str(output)
