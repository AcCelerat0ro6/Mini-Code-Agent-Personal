def large_output_hook(block, output):
    """PostToolUse 钩子：当工具返回的结果极其庞大时抛出警告日志。"""
    if len(str(output)) > 100000:
        print(f"\033[33m[HOOK] Large output from {block.name}: {len(str(output))} chars\033[0m")
    return None