import os
from pathlib import Path
from tools.tools import BASE_TOOLS, BASE_HANDLERS, execute_tool
from tools.task_system import TASK_TOOLS, TASK_HANDLERS
from tools import todomanager
from anthropic import Anthropic
from dotenv import load_dotenv
from client.client import client
from hooks.hook import  trigger_hooks
from context.compactor import COMPACTOR
from tools.backgroundmanager import inject_background_results
from tools.recoverymanager import RecoveryState, with_retry

WORKDIR = os.getenv("WORK_DIR", Path.cwd())
MODEL = os.environ["MODEL_ID"]
from tools.skills import SKILL_LOADER
# 子代理系统提示词：强调聚焦单点任务，最终只需返回凝练的答案
SUB_SYSTEM = (
    f"You are a coding agent at {WORKDIR}. Use tools to solve tasks. "
    "Act, don't explain.\n\n"
    f"Skills available:\n{SKILL_LOADER.catalog()}\n\n"
    "Use load_skill to read the full instructions when a skill applies.\n\n"
    # 任务系统使用准则：若派发的任务带有任务 ID，开工前先认领，完成后标记完成
    "When the prompt includes task IDs, use claim_task before working on a "
    "task and complete_task when it is done.\n\n"
    # 收工准则：先把计划执行落地并自测通过，再返回最终答案，避免列完清单就交差
    "If you plan with todos, execute the plan: do not return a final answer "
    "until every todo is completed and the work is verified. "
    "Complete the given task, then return a concise final answer.\n\n"
    # 可选异步调用准则：仅对彼此独立的 Bash 命令开启后台执行，结果稍后回收
    "Set run_in_background to true only for independent Bash commands."
)

# ==============================================================================
# -- 子代理核心实现 (Subagent Loop) --
# ==============================================================================

# 子代理持有基础工具 + 任务系统工具：任务仓库持久化在 .tasks/ 目录，
# 父子代理共享同一份任务状态，便于认领（claim_task）/完成（complete_task）协作
# 但不注入 TASK_TOOL，物理切断嵌套调用链
SUB_TOOLS = [*BASE_TOOLS, *TASK_TOOLS]
SUB_HANDLERS = {**BASE_HANDLERS, **TASK_HANDLERS}


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
    3. 自己列的 todo 计划未全部完成前不允许收工，最多强制续跑 3 轮；
    4. 子代理执行完毕后，仅返回最终的纯文本回答，中间工具日志被完全封包。
    """
    print("\n\033[35m[Subagent started]\033[0m")
    # 初始化子代理私有上下文
    messages = [{"role": "user", "content": prompt}]
    # 错误恢复状态：子代理同样对 429/529 做退避重试与备模型切换
    # （每个子代理独立计数，避免长任务被一次瞬时限流整个打断）
    recovery = RecoveryState()
    # 子代理改用私有的 todo 清单，与主代理清单相互隔离，终端输出也互不混淆
    sub_todo = todomanager.TodoManager(label="sub")
    outer_todo = todomanager.ACTIVE_TODO
    todomanager.ACTIVE_TODO = sub_todo
    # 计划未完成时强制续跑的次数上限，防止无限催促
    forced_continues = 0

    try:
        for _ in range(30):
            # 每次模型推理前同样执行分级渐进式上下文压缩流水线，防止子代理长任务上下文爆量
            messages[:] = COMPACTOR.prepare(messages, prompt)
            # 可选异步上下文调用：收割子代理自己派发的后台任务结果并注入上下文
            inject_background_results(messages)

            response = with_retry(
                lambda: client.messages.create(
                    model=recovery.current_model,
                    system=SUB_SYSTEM,
                    messages=messages,
                    tools=SUB_TOOLS,
                    max_tokens=8000,
                ),
                recovery,
            )
            messages.append({"role": "assistant", "content": response.content})

            # 检查模型是否需要调用工具
            tool_calls = [
                block for block in response.content if block.type == "tool_use"
            ]

            # 无需调用工具说明子代理想收工，返回最终文本
            if not tool_calls:
                # 收尾兜底：后台任务恰在收工前完成时先注入结果再续跑一轮消化
                if inject_background_results(messages):
                    continue
                force = trigger_hooks("Stop", messages)
                if force:
                    # 若 Stop 钩子返回了补充指令，则追加并发起下一轮调用
                    messages.append({"role": "user", "content": force})
                    continue
                # 自己列的计划还没做完就不许交差，注入提醒强制继续执行
                if sub_todo.has_unfinished() and forced_continues < 3:
                    forced_continues += 1
                    print("\033[33m[Subagent] plan unfinished, continue...\033[0m")
                    messages.append({
                        "role": "user",
                        "content": "<reminder>Your plan still has unfinished "
                                   "todos. Keep working until every todo is "
                                   "completed, then return the final answer."
                                   "</reminder>",
                    })
                    continue
                print("\033[35m[Subagent done]\033[0m")
                return extract_text(response.content) or "(no summary)"

            # 顺序执行子代理派发的各项工具
            results = []
            for block in tool_calls:
                output = execute_tool(block, SUB_HANDLERS)
                # 终端日志压成单行并截断，避免多行工具结果刷屏
                preview = " ".join(str(output).split())[:80]
                print(f"  \033[90m[sub] {block.name}: {preview}\033[0m")
                results.append({
                    "type": "tool_result",
                    "tool_use_id": block.id,
                    "content": output,
                })
            messages.append({"role": "user", "content": results})
    finally:
        # 子代理结束（含中途异常）后恢复主代理的任务清单
        todomanager.ACTIVE_TODO = outer_todo

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