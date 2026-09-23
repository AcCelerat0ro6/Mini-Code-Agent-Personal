def displayfix_tool():
    try:
        import readline

        # 禁用特殊 TTY 字符绑定，防止输入控制字符异常
        readline.parse_and_bind('set bind-tty-special-chars off')
        # 允许输入并正常显示 8-bit 高位字符（如中文等多字节字符）
        readline.parse_and_bind('set input-meta on')
        readline.parse_and_bind('set output-meta on')
        # 避免终端自动将高位字节截断或转义
        readline.parse_and_bind('set convert-meta off')
    except ImportError:
        pass
