import json
from anthropic import Anthropic
class TodoManager:
    """管理 Agent 的待办任务清单，负责输入校验与文本可视化渲染"""

    def __init__(self):
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


# 单例全局任务管理器
TODO = TodoManager()


def run_todo_write(todos: list | str) -> str:
    """todo_write 工具的回调实现，更新状态并在终端以黄色高亮打印当前任务状态"""
    try:
        output = TODO.update(todos)
    except ValueError as e:
        return f"Error: {e}"
    print(f"\n\033[33m## Current Tasks\033[0m\n{output}")
    return output