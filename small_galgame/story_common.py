"""
story_common.py —— 共通剧情
====================================
* 序幕（星降学园的传说）
* 岔路（选择攻略对象）
* 星降传说许愿（影响 True 结局：flags["wished"]）
"""

from core import (
    scene, narrate, dialogue, choose, wait,
    yukiko, ami, amamiya, flags,
)


def scene_intro():
    """开场：星降学园的传说"""
    scene("序幕 · 星降学园的传说")
    narrate(
        "十月的风掠过教学楼顶，把金色的银杏叶吹得漫天飞舞。\n"
        "星降学园一年一度的「星降祭」即将在学园祭前夕举办，\n"
        "全校都在忙碌地布置会场、排演节目。\n\n"
        "传说：在星降祭当晚，只要对着天空许下真诚的愿望，\n"
        "流星就会回应那个人的心意——至少，大家都这么相信着。"
    )
    dialogue("你", "（又一年的星降祭……总觉得今年会有什么不一样。）")
    narrate("你走在放学后的走廊上，手里攥着一张星降祭的节目单。")
    wait()
    return "star_legend"


def scene_star_legend():
    """共通：星降传说 —— 许愿与否将影响所有路线的结局"""
    scene("星降传说")
    narrate(
        "教室的公告栏前围了几个女生，正在传阅一张手写的海报：\n"
        "「星降祭之夜，许下心愿的人，将得到流星的回应。」\n"
        "海报的角落画着一颗小小的星星，笔触温柔。"
    )
    narrate(
        "你知道那只是传说罢了。\n"
        "可不知道为什么，胸口有一点点发烫。"
    )
    idx = choose([
        "（在海报角落写下一个心愿）",
        "（移开视线，假装没看见）",
    ])
    if idx == 0:
        flags["wished"] = True
        narrate(
            "你找来一支笔，在海报最不起眼的角落，\n"
            "认真地写下了一个小小的愿望。\n\n"
            "笔尖落下的瞬间，好像有什么东西被轻轻点亮了。"
        )
        dialogue("你", "（流星啊……如果你真的存在的话，请回应我吧。）")
    else:
        flags["wished"] = False
        narrate("你别过头，把海报上的字当作小孩子的浪漫，一笑置之。")
    wait()
    return "choose_path"


def scene_choose_path():
    """选择攻略对象"""
    scene("岔路 · 三条路线")
    narrate("放学后，天台的风把云吹得很远。\n你的心意，将决定这个秋天的故事走向——")
    idx = choose([
        f"{yukiko.name} —— {yukiko.desc}",
        f"{ami.name} —— {ami.desc}",
        f"{amamiya.name} —— {amamiya.desc}",
    ])
    if idx == 0:
        return "yukiko_1"
    if idx == 1:
        return "ami_1"
    return "amamiya_1"
