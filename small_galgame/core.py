"""
core.py —— 共享数据与辅助函数
====================================
* 角色类 Character（好感度 / 结局判定）
* 全局剧情标记 flags
* 三位女主角实例
* 游戏引擎实例 E 与常用辅助函数

所有剧本模块（story_common / route_*）均从这里导入，
保证并行创作时 API 一致。
"""

import os
from engine import GameEngine


# ══════════════════════════════════════════════════════════════════════
#  角色数据
# ══════════════════════════════════════════════════════════════════════

class Character:
    """角色：名字、好感度、一句话描述"""
    def __init__(self, name: str, desc: str):
        self.name = name
        self.desc = desc
        self.affection = 50  # 初始好感 50 / 满值 100

    def change(self, delta: int):
        self.affection = max(0, min(100, self.affection + delta))

    def ending(self) -> str:
        """true: 好感 ≥ 85 且参与了星降传说的许愿；good: ≥ 70；否则 normal"""
        if self.affection >= 85 and flags.get("wished"):
            return "true"
        if self.affection >= 70:
            return "good"
        return "normal"


# 全局剧情标记（如是否参与许愿）
flags: dict[str, bool] = {}

# 自动存档路径（与 game.py 同目录）
SAVE_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "save.json")


# 三位女主角
yukiko = Character("小雪 (Yukiko)", "图书馆委员，内向文学少女")
ami = Character("亚美 (Ami)", "学生会会长，活泼开朗的行动派")
amamiya = Character("雨宫 (Amamiya)", "转学生，银发沉默却温柔")


# ══════════════════════════════════════════════════════════════════════
#  游戏引擎实例
# ══════════════════════════════════════════════════════════════════════
E = GameEngine()


# ══════════════════════════════════════════════════════════════════════
#  辅助函数
# ══════════════════════════════════════════════════════════════════════

def dialogue(speaker: str, text: str):
    E.show_dialogue(speaker, text)

def narrate(text: str):
    E.show_narration(text)

def scene(title: str):
    E.clear_screen()
    E.show_scene(title)

def choose(options: list[str]) -> int:
    return E.show_choices(options)

def wait(msg: str = "按 Enter 继续…"):
    E.wait_for_input(msg)

def affection_bar(char: Character):
    """在终端画一条简易好感条"""
    filled = char.affection // 5
    bar = "█" * filled + "░" * (20 - filled)
    print(f"  {char.name}: [{bar}] {char.affection}/100")
