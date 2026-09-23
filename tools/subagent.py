import os
from pathlib import Path
from tools.tools import BASE_TOOLS, BASE_HANDLERS, execute_tool
from anthropic import Anthropic
from dotenv import load_dotenv
from client.client import client
from hooks.hook import  trigger_hooks
from context.compactor import COMPACTOR

WORKDIR = os.getenv("WORK_DIR", Path.cwd())
MODEL = os.environ["MODEL_ID"]
from tools.skills import SKILL_LOADER
# 子代理系统提示词：强调聚焦单点任务，最终只需返回凝练的答案
SUB_SYSTEM = (
    f"You are a coding agent at {WORKDIR}. Use tools to solve tasks. "
    "Act, don't explain.\n\n"
    f"Skills available:\n{SKILL_LOADER.catalog()}\n\n"
    "Use load_skill to read the full instructions when a skill applies.\n\n"
    "Complete the given task, then return a concise final answer."
)

# ==============================================================================
# -- 子代理核心实现 (Subagent Loop) --
# ==============================================================================

# 子代理仅持有基础工具集合，不注入 TASK_TOOL，物理切断嵌套调用链
SUB_TOOLS = list(BASE_TOOLS)
SUB_HANDLERS = dict(BASE_HANDLERS)


def extract_text(content) -> str:
    """从模型返回的多模态/块状 content 中提取纯文本"""
    if not isinstance(content, list):
        return str(content)
    return "\n".join(
        getattr(block, "text", "")
        for block in content
        if getattr(block, "type", None) == "text"
    )

def extract_text(content) -> str:
    """从模型返回的多模态/块状 content 中提取纯文本"""
    if not isinstance(content, list):
        return str(content)
    return "\n".join(
        getattr(block, "text", "")
        for block in content
        if getattr(block, "type", None) == "text"
    )

def run_subagent(prompt: str) -> str:
    """
    执行独立子代理任务（即 task 工具的核心 handler）：
    1. 拥有以 prompt 为初始消息的全新独立 messages 上下文；
    2. 最大循环轮数上限为 30，防止死循环；
    3. 子代理执行完毕后，仅返回最终的纯文本回答，中间工具日志被完全封包。
    """
    print("\n\033[35m[Subagent started]\033[0m")
    # 初始化子代理私有上下文
    messages = [{"role": "user", "content": prompt}]

    for _ in range(30):
        # 每次模型推理前同样执行分级渐进式上下文压缩流水线，防止子代理长任务上下文爆量
        messages[:] = COMPACTOR.prepare(messages, prompt)

        response = client.messages.create(
            model=MODEL,
            system=SUB_SYSTEM,
            messages=messages,
            tools=SUB_TOOLS,
            max_tokens=8000,
        )
        messages.append({"role": "assistant", "content": response.content})

        # 检查模型是否需要调用工具
        tool_calls = [
            block for block in response.content if block.type == "tool_use"
        ]

        # 无需调用工具说明子代理已完成任务，返回最终文本
        if not tool_calls:
            force = trigger_hooks("Stop", messages)
            if force:
                # 若 Stop 钩子返回了补充指令，则追加并发起下一轮调用
                messages.append({"role": "user", "content": force})
                continue
            print("\033[35m[Subagent done]\033[0m")
            return extract_text(response.content) or "(no summary)"

        # 顺序执行子代理派发的各项工具
        results = []
        for block in tool_calls:
            output = execute_tool(block, SUB_HANDLERS)
            print(f"  \033[90m[sub] {block.name}: {output[:100]}\033[0m")
            results.append({
                "type": "tool_result",
                "tool_use_id": block.id,
                "content": output,
            })
        messages.append({"role": "user", "content": results})

    print("\033[35m[Subagent stopped]\033[0m")
    return "Subagent stopped after 30 turns without a final answer."

# 对父代理开放的 task 工具声明
TASK_TOOL = {
    "name": "task",
    "description": "Run a subagent with fresh conversation context and return its final text.",
    "input_schema": {
        "type": "object",
        "properties": {"prompt": {"type": "string", "minLength": 1}},
        "required": ["prompt"],
    },
}