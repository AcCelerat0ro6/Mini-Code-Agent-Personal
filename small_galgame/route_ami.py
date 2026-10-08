"""
route_ami.py —— 亚美路线
====================================
学生会会长，活泼开朗的行动派。
True 结局需要好感 ≥ 85 且参与了星降传说的许愿（flags["wished"]）。
"""

from core import (
    E, scene, narrate, dialogue, choose, wait,
    affection_bar, ami, flags,
)


def scene_ami_1():
    """亚美路线 第一幕：学生会的委托"""
    scene("亚美 · 学生会的委托")
    narrate(
        "午休时间，你被学生会办公室的敲门声叫了过去。\n"
        "门口站着的，是学生会会长——亚美。\n"
        "她手里拿着一叠传单，笑得像夏日阳光一样灿烂。"
    )
    dialogue("亚美", "同学！星降祭的宣传海报人手不够，帮我一张吧！")
    idx = choose([
        "（爽快地接过传单）",
        "（委婉地表示自己很忙）",
    ])
    if idx == 0:
        dialogue("亚美", "太好了！你真是我的救星！放心，之后请你喝汽水！")
        narrate("她不由分说地把传单塞进你怀里，转身又风风火火地忙去了。")
        ami.change(+10)
    else:
        dialogue("亚美", "诶——别这么说嘛，就一张！一张就好！")
        narrate("她双手合十，用闪闪发亮的眼睛盯着你。")
        narrate("……你败下阵来。")
        ami.change(+4)
    wait()
    return "ami_2"


def scene_ami_2():
    """亚美路线 第二幕：屋顶的便当"""
    scene("亚美 · 屋顶的便当")
    narrate(
        "几天后的午休，你在教学楼的天台找到了亚美。\n"
        "她正一个人坐在栏杆旁，难得安静地吃着便当。"
    )
    dialogue("亚美", "啊，是你！……坐吧，我今天多带了一份。")
    idx = choose([
        "（在她身边坐下）",
        "（说自己吃过了，只是路过）",
    ])
    if idx == 0:
        narrate("你们并肩坐在天台的阳光里，分享着简单的便当。")
        dialogue("亚美", "其实……当会长也挺累的。不过，有你在就轻松多了。")
        ami.change(+15)
    else:
        dialogue("亚美", "是吗？那……下次吧。下次一定！")
        narrate("她挥挥手，笑容依旧灿烂，却好像藏着一点失落。")
        ami.change(+5)
    wait()
    return "ami_3"


def scene_ami_3():
    """亚美路线 第三幕：星降祭当天（待扩写）"""
    scene("亚美 · 星降祭当天")
    narrate("（本幕内容将由后续扩写补全）")
    wait()
    return "ami_end"


def scene_ami_end():
    """亚美路线 结局：true / good / normal"""
    ending = ami.ending()
    scene("亚美 · 结局")
    if ending == "true":
        narrate(
            "星降祭的夜晚，舞台的灯光与星光交相辉映。\n"
            "亚美拉着你的手，挤过人群，来到操场中央。\n\n"
            "「快看！流星！」\n\n"
            "她闭上眼睛，许下心愿，然后转头对你笑。\n\n"
            "「我的愿望，已经实现一半了。」\n\n"
            "（★ True End：阳光般的心意，终于抵达。）"
        )
        ami.change(+5)
    elif ending == "good":
        narrate(
            "星降祭的夜晚，亚美在人群中找到了你。\n"
            "「谢谢你这一路的帮忙！明年……明年也要一起哦！」\n"
            "她的笑容比舞台的灯光还要耀眼。\n\n"
            "（★ Good End：一段充满阳光的约定。）"
        )
        ami.change(+3)
    else:
        narrate(
            "星降祭结束了。\n"
            "你和亚美依旧是「会长与帮手」的关系。\n"
            "也许，那句「明年也要一起」只是客套话。\n\n"
            "（○ Normal End：故事停在了最热闹的散场。）"
        )
    affection_bar(ami)
    wait("按 Enter 查看总结…")
    return "ending_summary"
