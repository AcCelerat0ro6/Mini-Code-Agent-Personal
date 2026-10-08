"""
tools/recoverymanager.py - 模型调用错误恢复模块 (Error Recovery)

给所有「驱动大模型」的调用点提供统一的容错层（移植自课程参考实现 s15）：
瞬时错误（限流 429 / 过载 529）按指数退避自动重试，连续过载时切换备用模型；
上下文超长类报错识别后交给调用方的 reactive_compact 兜底；max_tokens 截断的
升级与续跑策略由 agent_loop 消化，本模块提供判定函数与策略常量。

    model call ──429──> 指数退避重试（BASE_DELAY_MS * 2^n + 抖动，最多 MAX_RETRIES 次）
        ^
        |         连续 MAX_CONSECUTIVE_529 次 529
        529 ────────────────────────> 切换 FALLBACK_MODEL_ID 后继续重试
        |
        其它异常 ──> 原样抛出（prompt_too_long 由 agent_loop 的 reactive_compact 兜底）

关键入口 (Key entry points):

    RecoveryState             一次回合内累积的恢复状态（退避/切换/升级进度）
    with_retry(fn, state)     执行模型调用并对 429/529 做退避重试与备模型切换
    is_prompt_too_long_error  识别上下文超长类报错（交给 reactive_compact 兜底）
    DEFAULT_MAX_TOKENS        常规单轮输出上限
    ESCALATED_MAX_TOKENS      max_tokens 截断后升级到的输出上限
    MAX_RECOVERY_RETRIES      截断后注入续跑指令的最大次数
    CONTINUATION_PROMPT       截断续跑时注入的续跑指令
"""

import os
import random
import time

# 主模型与备用模型：备用模型仅在连续过载时启用，未配置表示不切换
PRIMARY_MODEL = os.environ["MODEL_ID"]
FALLBACK_MODEL = os.getenv("FALLBACK_MODEL_ID")

# ---- 恢复策略阈值 ----
DEFAULT_MAX_TOKENS = 8000        # 常规单轮输出上限
ESCALATED_MAX_TOKENS = 16000     # 被 max_tokens 截断后升级到的输出上限
MAX_RETRIES = 3                  # 429/529 的最大重试次数
MAX_CONSECUTIVE_529 = 2          # 连续过载多少次后切换备用模型
BASE_DELAY_MS = 500              # 退避基值（毫秒），按 2^n 指数增长，封顶 32 秒
MAX_RECOVERY_RETRIES = 2         # max_tokens 截断后允许的续跑指令次数

# 截断续跑指令：要求模型从上次中断处继续，不要重复已完成的工作
CONTINUATION_PROMPT = ("Continue from the previous response. "
                       "Do not repeat completed work.")


class RecoveryState:
    """一次回合内累积的错误恢复状态（agent_loop / 子代理各持一份，回合结束即废弃）"""

    def __init__(self):
        self.has_escalated = False           # 本次截断是否已升级过输出上限
        self.recovery_count = 0              # 已注入续跑指令的次数
        self.consecutive_529 = 0             # 连续过载计数（调用成功后清零）
        self.current_model = PRIMARY_MODEL   # 当前使用的模型（连续过载后切换）


def retry_delay(attempt: int) -> float:
    """指数退避 + 最多 25% 随机抖动（秒）；封顶 32 秒，避免重试间隔失控"""
    base = min(BASE_DELAY_MS * (2 ** attempt), 32000) / 1000
    return base + random.uniform(0, base * 0.25)


def _is_rate_limit_error(exc: Exception) -> bool:
    """识别限流 429：异常类名或报错文本携带 ratelimit / 429"""
    name = type(exc).__name__.lower()
    msg = str(exc).lower()
    return "ratelimit" in name or "429" in msg


def _is_overloaded_error(exc: Exception) -> bool:
    """识别过载 529：异常类名或报错文本携带 overloaded / 529"""
    name = type(exc).__name__.lower()
    msg = str(exc).lower()
    return "overloaded" in name or "529" in msg


def with_retry(fn, state: RecoveryState):
    """
    执行 fn() 并对瞬时错误做退避重试：
    - 429（限流）：指数退避后重试；
    - 529（过载）：退避重试，连续 MAX_CONSECUTIVE_529 次后把 state.current_model
      切到 FALLBACK_MODEL 再继续（fn 闭包须在每次调用时读取 current_model）；
    - 其它异常：立即原样抛出，由调用方决定兜底方式（如 reactive_compact）；
    - 重试配额耗尽：抛 RuntimeError（上限之后不再吞错，避免无限卡住回合）。
    """
    for attempt in range(MAX_RETRIES):
        try:
            result = fn()
            state.consecutive_529 = 0
            return result
        except Exception as exc:
            if _is_rate_limit_error(exc):
                delay = retry_delay(attempt)
                print(f"\033[33m[RETRY:429] {attempt + 1}/{MAX_RETRIES} "
                      f"after {delay:.1f}s\033[0m")
                time.sleep(delay)
                continue
            if _is_overloaded_error(exc):
                state.consecutive_529 += 1
                if (state.consecutive_529 >= MAX_CONSECUTIVE_529
                        and FALLBACK_MODEL
                        and state.current_model != FALLBACK_MODEL):
                    state.current_model = FALLBACK_MODEL
                    state.consecutive_529 = 0
                    print(f"\033[31m[RETRY:529] switching to fallback model "
                          f"{FALLBACK_MODEL}\033[0m")
                delay = retry_delay(attempt)
                print(f"\033[33m[RETRY:529] {attempt + 1}/{MAX_RETRIES} "
                      f"after {delay:.1f}s\033[0m")
                time.sleep(delay)
                continue
            raise
    raise RuntimeError(f"Max retries ({MAX_RETRIES}) exceeded")


def is_prompt_too_long_error(exc: Exception) -> bool:
    """
    识别上下文超长类报错（合并 s15 与本项目两套判定关键字）：
    命中后由调用方触发 reactive_compact 压缩历史再重试。
    """
    msg = str(exc).lower()
    return (("prompt" in msg and "long" in msg)
            or "too many tokens" in msg
            or "context_length_exceeded" in msg
            or "context length" in msg
            or "max_context_window" in msg
            or "max context window" in msg)
