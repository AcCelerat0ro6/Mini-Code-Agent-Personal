import ast
import json
from anthropic import Anthropic
class TodoManager:
    """管理 Agent 的待办任务清单，负责输入校验与文本可视化渲染"""

    def __init__(self, label: str = "main"):
        # 所属 Agent 标识（main/sub），终端输出时用于区分父子代理的清单
        self.label = label
        self.items: list[dict] = []

    def update(self, todos: list | str) -> str:
        """接收并校验新的 todos 列表，更新Manager持有的ToDolist的状态并返回格式化后的文本"""
        # 兼容处理：支持传入 JSON 字符串或 Python 字典字面量字符串
        if isinstance(todos, str):
            try:
                todos = json.loads(todos)
            except json.JSONDecodeError:
                try:
                    todos = ast.literal_eval(todos)
                except (SyntaxError, ValueError) as e:
                    raise ValueError("todos must be a list or JSON array string") from e

        if not isinstance(todos, list):
            raise ValueError("todos must be a list")
        if len(todos) > 20:
            raise ValueError("Max 20 todos allowed")

        validated = []
        in_progress_count = 0
        for index, todo in enumerate(todos):
            if not isinstance(todo, dict):
                raise ValueError(f"todos[{index}] must be an object")

            content = str(todo.get("content", "")).strip()
            status = str(todo.get("status", "pending")).lower()
            if not content:
                raise ValueError(f"todos[{index}] requires content")
            # 状态枚举：待办、进行中、已完成
            if status not in ("pending", "in_progress", "completed"):
                raise ValueError(f"todos[{index}] has invalid status '{status}'")
            if status == "in_progress":
                in_progress_count += 1
            validated.append({"content": content, "status": status})

        # 强约束规则：任意时刻只能有 1 个任务处于 in_progress 状态，避免模型注意力发散
        if in_progress_count > 1:
            raise ValueError("Only one todo can be in_progress at a time")

        self.items = validated
        return self.render()

    def render(self) -> str:
        """将当前的 todo 列表渲染为可读文本，用于展示和工具输出"""
        if not self.items:
            return "No todos."

        lines = []
        for todo in self.items:
            marker = {
                "pending": "[ ]",
                "in_progress": "[>]",
                "completed": "[x]",
            }[todo["status"]]
            lines.append(f"{marker} {todo['content']}")

        done = sum(todo["status"] == "completed" for todo in self.items)
        lines.append(f"\n({done}/{len(self.items)} completed)")
        return "\n".join(lines)

    def has_unfinished(self) -> bool:
        """判断清单中是否还有未完成的条目（子代理收工前自检用）"""
        return any(todo["status"] != "completed" for todo in self.items)

    def summary(self) -> str:
        """生成单行紧凑摘要供终端展示（完整清单仍作为工具结果返回给模型）"""
        if not self.items:
            return "empty"
        done = sum(todo["status"] == "completed" for todo in self.items)
        text = f"{done}/{len(self.items)} completed"
        current = next(
            (todo["content"] for todo in self.items
             if todo["status"] == "in_progress"),
            None,
        )
        if current:
            # 进行中的条目过长时截断，保持摘要单行可读
            if len(current) > 30:
                current = current[:30] + "..."
            text += f" | doing: {current}"
        return text


# 单例全局任务管理器（主代理使用）
TODO = TodoManager()

# 当前生效的任务管理器：子代理运行期间切换为它的私有清单，避免父子清单互相覆盖
ACTIVE_TODO = TODO


def run_todo_write(todos: list | str) -> str:
    """todo_write 工具的回调实现：更新状态并在终端打印单行摘要，完整清单作为工具结果返回"""
    try:
        output = ACTIVE_TODO.update(todos)
    except ValueError as e:
        return f"Error: {e}"
    # 终端编码多为 GBK，日志只用 ASCII 前缀 + 颜色，避免 emoji 编码报错
    print(f"\033[33m[TODO:{ACTIVE_TODO.label}] {ACTIVE_TODO.summary()}\033[0m")
    return output
