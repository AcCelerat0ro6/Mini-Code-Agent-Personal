"""
微型 GalGame CLI 游戏引擎
========================
提供对话、选择、旁白、场景描述、打字机效果、存档/读档等基础功能。
依赖: colorama (pip install colorama)
"""

import os
import sys
import json
import time
import colorama

# ── 初始化 colorama，支持 Windows 终端彩色输出 ──────────────────────
colorama.init()

# ── 颜色常量（缩写，便于引擎内部使用） ─────────────────────────────
RST = colorama.Fore.RESET          # 重置
BRIGHT = colorama.Style.BRIGHT     # 高亮
DIM = colorama.Style.DIM           # 暗淡
ITALIC = "\033[3m"                 # 斜体（部分终端支持）
RESET_ITALIC = "\033[23m"          # 关闭斜体

# ── 预定义说话人颜色 ────────────────────────────────────────────────
SPEAKER_COLORS = [
    colorama.Fore.CYAN,             # 说话人 1 — 青色
    colorama.Fore.YELLOW,           # 说话人 2 — 黄色
    colorama.Fore.MAGENTA,          # 说话人 3 — 品红
    colorama.Fore.GREEN,            # 说话人 4 — 绿色
    colorama.Fore.RED,              # 说话人 5 — 红色
    colorama.Fore.BLUE,             # 说话人 6 — 蓝色
    colorama.Fore.WHITE,            # 说话人 7 — 白色
]
_dialogue_color_map: dict[str, str] = {}  # 已分配过的说话人→颜色映射
_color_index = 0                          # 轮转索引


class GameEngine:
    """微型 GalGame CLI 引擎 —— 一行代码开启你的文字冒险。"""

    # ── 工具方法 ──────────────────────────────────────────────────────

    def clear_screen(self) -> None:
        """清屏（跨平台）。"""
        os.system("cls" if os.name == "nt" else "clear")

    def slow_print(self, text: str, delay: float = 0.03) -> None:
        """
        打字机效果：逐字输出。
        * 延迟单位为秒（默认 0.03s/字）。
        * 按 Ctrl+C 可中断输出，后续内容一次性打印。
        """
        try:
            for ch in text:
                sys.stdout.write(ch)
                sys.stdout.flush()
                time.sleep(delay)
            sys.stdout.write("\n")
            sys.stdout.flush()
        except KeyboardInterrupt:
            # Ctrl+C 中断时，直接补完剩余文本
            sys.stdout.write("\n")
            sys.stdout.flush()

    # ── 显示方法 ──────────────────────────────────────────────────────

    def show_dialogue(self, speaker: str, text: str) -> None:
        """
        显示对话。
        * speaker: 说话人名字（自动分配颜色，同名同色）。
        * text:    对话内容。
        * 格式: 「说话人名字」: 对话内容
        """
        global _color_index
        # 如果该说话人尚未分配颜色，则轮转分配一个
        if speaker not in _dialogue_color_map:
            _dialogue_color_map[speaker] = SPEAKER_COLORS[
                _color_index % len(SPEAKER_COLORS)
            ]
            _color_index += 1
        color = _dialogue_color_map[speaker]
        # 输出: 彩色加粗名字 + 白色对话文本
        print(
            f"{BRIGHT}{color}{speaker}{RST}"
            f"{colorama.Fore.WHITE}：{text}{RST}"
        )

    def show_choices(self, choices: list[str]) -> int:
        """
        显示选择项并等待玩家输入，返回选择的索引（从 0 开始）。
        * choices: 选项文本列表。
        * 输入非法时会要求重新输入。
        """
        print()  # 空行分隔
        for i, choice in enumerate(choices, start=1):
            print(
                f"  {colorama.Fore.YELLOW}{BRIGHT}[{i}]{RST}"
                f" {colorama.Fore.WHITE}{choice}{RST}"
            )
        print()

        while True:
            try:
                raw = input(
                    f"  {colorama.Fore.CYAN}请输入选项编号 > {RST}"
                ).strip()
                idx = int(raw) - 1
                if 0 <= idx < len(choices):
                    return idx
                print(f"  {colorama.Fore.RED}编号超出范围，请重新输入。{RST}")
            except ValueError:
                print(f"  {colorama.Fore.RED}请输入数字！{RST}")

    def show_narration(self, text: str) -> None:
        """
        显示旁白（斜体 + 装饰字符包裹）。
        * 使用 ANSI 斜体码和 ~~~ 包裹。
        * 部分 Windows 终端可能不支持斜体，此时仅靠装饰字符区分。
        """
        wrapped = f"{ITALIC}~~~ {text} ~~~{RESET_ITALIC}"
        print(f"{DIM}{wrapped}{RST}")

    def show_scene(self, text: str) -> None:
        """
        显示场景描述（居中风格 + 分隔线）。
        """
        width = max(len(text) + 4, 40)
        border = "─" * width
        print(f"\n{DIM}┌{border}┐{RST}")
        print(f"{DIM}│{RST} {BRIGHT}{text}{RST} {DIM}│{RST}")
        print(f"{DIM}└{border}┘{RST}\n")

    def wait_for_input(self, prompt: str = "按 Enter 继续...") -> None:
        """等待玩家按 Enter 继续。"""
        input(f"{DIM}{prompt}{RST}")

    # ── 存档 / 读档（JSON 文件） ─────────────────────────────────────

    def save_state(self, state: dict, path: str = "save.json") -> None:
        """
        将游戏状态（dict）保存为 JSON 文件。
        * state: 可序列化的字典。
        * path:  存档路径，默认当前目录 save.json。
        """
        with open(path, "w", encoding="utf-8") as f:
            json.dump(state, f, ensure_ascii=False, indent=2)
        print(f"{colorama.Fore.GREEN}✓ 已保存到 {path}{RST}")

    def load_state(self, path: str = "save.json") -> dict | None:
        """
        从 JSON 文件加载游戏状态。
        * 文件不存在时返回 None 并提示。
        """
        if not os.path.exists(path):
            print(f"{colorama.Fore.RED}✗ 存档文件 {path} 不存在{RST}")
            return None
        with open(path, "r", encoding="utf-8") as f:
            state = json.load(f)
        print(f"{colorama.Fore.GREEN}✓ 已从 {path} 读取存档{RST}")
        return state


# ══════════════════════════════════════════════════════════════════════
# 快速自测 / 演示
# ══════════════════════════════════════════════════════════════════════
if __name__ == "__main__":
    engine = GameEngine()

    engine.clear_screen()

    # 场景描述
    engine.show_scene("黄昏的校门口")

    # 旁白
    engine.show_narration("夕阳将天空染成了橘红色，一阵风吹过，樱花瓣纷纷飘落。")

    # 对话
    engine.show_dialogue("小雪", "你怎么还在这里？今天不是说好一起去图书馆的吗？")
    engine.show_dialogue("主角", "啊……我忘了，抱歉。")
    engine.show_dialogue("小雪", "哼，每次都是这样。走啦，要关门了！")

    # 选择
    choice = engine.show_choices([
        "追上去道歉",
        "假装没听到，转身离开",
        "大声喊：小雪，等等我！",
    ])
    actions = [
        "你快步追上小雪，轻声说了句「对不起」。",
        "你默默转身，消失在夕阳的余晖中。",
        "你的声音回荡在空旷的街道上，小雪停下了脚步……",
    ]
    engine.show_narration(actions[choice])

    engine.wait_for_input()

    # 存档 / 读档演示
    engine.save_state({"scene": "校门口", "choice": choice, "time": "黄昏"})
    loaded = engine.load_state()
    print(f"读档结果: {loaded}")
