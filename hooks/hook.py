# 钩子事件注册表：存储各个生命周期的回调函数列表
HOOKS = {"UserPromptSubmit": [], "PreToolUse": [], "PostToolUse": [], "Stop": []}

def register_hook(event: str, callback):
    """将回调函数注册到指定的事件上"""
    HOOKS[event].append(callback)

def trigger_hooks(event: str, *args):
    """
    触发某个事件的所有钩子函数。
    如果任意一个钩子返回了非 None 的值，则中断后续执行，并将该值作为结果返回（主要用于拦截操作）。
    """
    for callback in HOOKS[event]:
        result = callback(*args)
        if result is not None:  # 钩子的返回值可以阻止工具调用
            return result
    return None



