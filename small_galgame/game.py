"""
学园祭前夜 ~Starlight Festival Eve~
====================================
基于 engine.py 的 CLI 微型 GalGame 演示
运行方式: python game.py
"""

import sys
import os
import random

# 确保能导入同目录的 engine
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
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
        if self.affection >= 70:
            return "good"
        return "normal"


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


# ══════════════════════════════════════════════════════════════════════
#  场景处理器
# ══════════════════════════════════════════════════════════════════════

def scene_intro():
    """开场：学园祭前夜，夕阳西下"""
    scene("学 园 祭 前 夜")
    narrate("九月的最后一个傍晚，校园被夕阳染成温暖的橘色。")
    narrate("明天就是一年一度的学园祭了，各社团都在做最后的准备。")
    narrate("你——一个普通的二年级学生——因为帮忙搬道具，被留在了空荡荡的教学楼里。")
    print()
    dialogue("你", "……终于搬完了。大家都回去了吧。")
    narrate("你擦了擦额头的汗，望向窗外的天空。")
    narrate("夕阳的余晖洒在走廊上，远处传来零星的笑声。")
    print()
    dialogue("你", "差不多该回去了——")
    narrate("突然，走廊尽头传来一阵脚步声。")
    narrate("一个身影出现在楼梯口，逆光看不清面容。")
    print()
    dialogue("？", "……还没走吗？")
    narrate("声音很轻，像风吹过书页。")
    narrate("你还没来得及回答，那个身影就转身消失在了走廊另一端。")
    print()
    dialogue("你", "那是谁……？")
    narrate("你心中涌起一股莫名的在意。追上去看看？还是先去别的地方？")
    wait()
    return "choose_path"


def scene_choose_path():
    """选择路线"""
    scene("岔 路")
    narrate("你站在走廊的分岔口，面前有三条路。")
    narrate("左边通往图书馆，右边通往学生会办公室，前方是操场方向。")
    print()

    choice = choose([
        "去图书馆——也许能在那里找到答案",
        "去学生会办公室——亚美应该还在忙",
        "追去操场方向——那个身影往那边去了",
    ])

    if choice == 0:
        return "yukiko_1"
    elif choice == 1:
        return "ami_1"
    else:
        return "amamiya_1"


# ─── 小雪路线 ──────────────────────────────────────────────────────

def scene_yukiko_1():
    """小雪路线第 1 场"""
    scene("图书馆的邂逅")
    narrate("你推开图书馆厚重的木门，熟悉的墨香扑面而来。")
    narrate("夕阳透过落地窗，在书架间投下长长的影子。")
    narrate("图书馆里安静极了，只有翻书的声音。")
    print()
    dialogue("？", "啊——！")
    narrate("角落里传来一声小小的惊呼。你循声望去，看到一个女孩正手忙脚乱地捡拾散落的书本。")
    narrate("那是小雪——图书馆委员，总是安静地待在角落里的文学少女。")
    print()
    dialogue("小雪", "你、你怎么在这里……大家不是都走了吗？")
    dialogue("你", "我帮忙搬东西来着。你呢，怎么还没回去？")
    dialogue("小雪", "我……我在整理明天学园祭要用的书签。可是不小心把书碰倒了……")
    narrate("她低着头，长长的黑发垂下来，遮住了泛红的脸颊。")
    print()

    choice = choose([
        "我帮你一起整理吧",
        "你一个人整理到这么晚，真辛苦啊",
    ])

    if choice == 0:
        dialogue("你", "我来帮你吧，两个人快一点。")
        dialogue("小雪", "诶……真的可以吗？谢谢你。")
        yukiko.change(10)
        narrate("你蹲下来，和小雪一起把散落的书一本一本捡起来。")
        narrate("她的手指偶尔碰到你的手背，每次都会触电般缩回去。")
    else:
        dialogue("你", "你一个人整理到这么晚，真的很辛苦呢。")
        dialogue("小雪", "没、没什么……我习惯了。")
        narrate("虽然嘴上这么说，但她的表情柔和了一些。")
        yukiko.change(5)

    print()
    dialogue("小雪", "那个……明天学园祭，你会来看我们的书签展吗？")
    dialogue("你", "当然。")
    dialogue("小雪", "……嗯。")
    narrate("她微微笑了，那笑容像是冬日里的第一缕阳光。")
    wait()
    return "yukiko_2"


def scene_yukiko_2():
    """小雪路线第 2 场：黄昏散步"""
    scene("黄昏的走廊")
    narrate("整理完书签，小雪说要去还钥匙，你陪她一起走出图书馆。")
    narrate("走廊里空荡荡的，只有你们两个人的脚步声。")
    print()
    dialogue("小雪", "其实……我明天有一点紧张。")
    dialogue("你", "紧张什么？")
    dialogue("小雪", "书签展的介绍词是我写的……要在很多人面前读出来，我做不到的。")
    narrate("她停下脚步，低头看着自己的鞋尖。")
    print()

    choice = choose([
        "那我明天去给你加油，你就不会紧张了",
        "不用怕，你的文字那么好，大家一定会喜欢的",
    ])

    if choice == 0:
        dialogue("你", "明天我会去的，你看到我就不会紧张了。")
        dialogue("小雪", "你……会来吗？真的？")
        dialogue("你", "说到做到。")
        yukiko.change(15)
        narrate("她抬起头，眼睛里好像有星光在闪烁。")
        dialogue("小雪", "谢谢你……我、我会努力的。")
    else:
        dialogue("你", "你的文字一直都很温柔，大家一定会被感动的。")
        dialogue("小雪", "才没有……你太夸张了。")
        yukiko.change(8)
        narrate("她虽然在否定，但嘴角微微上扬。")

    print()
    narrate("夕阳渐渐沉入地平线，走廊被染成了深橘色。")
    dialogue("小雪", "天快黑了……我该回去了。")
    dialogue("你", "嗯，明天见。")
    dialogue("小雪", "……明天见。")
    narrate("她转过身，走了几步又回过头。")
    dialogue("小雪", "那个——谢谢你今天帮我。")
    narrate("说完，她快步跑走了，马尾在风中轻轻摇晃。")
    wait()
    return "yukiko_end"


def scene_yukiko_end():
    """小雪结局"""
    scene("小雪 · 结局")
    affection_bar(yukiko)

    if yukiko.ending() == "good":
        print()
        narrate("～～～ Good Ending ～～～")
        print()
        narrate("第二天，你如约来到书签展。")
        narrate("小雪站在展台前，手里拿着稿纸，声音微微颤抖地读着介绍词。")
        narrate("当你在人群中向她挥手时，她的眼神亮了起来。")
        narrate("她的声音变得坚定，温柔的文字一个一个从她口中流出。")
        narrate("结束后，她跑到你面前，红着脸递给你一枚手写的书签。")
        print()
        dialogue("小雪", "这、这是我亲手做的……上面写了你喜欢的诗句。")
        dialogue("你", "……谢谢你，小雪。")
        dialogue("小雪", "应该是我说谢谢才对。")
        narrate("她低下头，声音轻得几乎听不见。")
        dialogue("小雪", "……因为你来了，我才能鼓起勇气。")
        print()
        narrate("夕阳下的书签，承载着少女的心意。")
        narrate("这大概是学园祭里，最温暖的一个故事。")
    else:
        print()
        narrate("～～～ Normal Ending ～～～")
        print()
        narrate("第二天，你远远地看到了小雪的书签展。")
        narrate("她的介绍词虽然声音很小，但每一个字都很认真。")
        narrate("你没有走近，只是在人群后面默默地点了点头。")
        narrate("也许，有些温柔的距离，也是一种美好的结局。")

    print()
    narrate("~ 学园祭前夜 ~ 小雪路线 · 完 ~")
    wait("按 Enter 查看总结…")
    return "ending_summary"


# ─── 亚美路线 ──────────────────────────────────────────────────────

def scene_ami_1():
    """亚美路线第 1 场"""
    scene("学生会办公室")
    narrate("你敲了敲学生会办公室的门，里面传来响亮的声音。")
    print()
    dialogue("亚美", "请进——！")
    narrate("推开门，满地都是海报、彩带和还没拆封的装饰品。")
    narrate("亚美——学生会会长，短发飞扬的活力少女——正站在椅子上挂横幅。")
    print()
    dialogue("亚美", "哦！是你啊，来得正好！快帮我扶一下椅子，要倒了要倒了——！")
    narrate("你赶忙冲过去扶住椅子，亚美在上面摇摇晃晃地把最后一个角固定好。")
    print()
    dialogue("亚美", "搞定！大功告成！")
    narrate("她从椅子上跳下来，拍了拍手上的灰，对你竖起大拇指。")
    dialogue("亚美", "谢啦！你果然是可靠的人。")
    dialogue("你", "怎么就你一个人？其他学生会成员呢？")
    dialogue("亚美", "我让他们先回去了嘛。毕竟我是会长，最后走是应该的。")
    narrate("她说得轻描淡写，但你注意到她眼下的淡淡黑眼圈。")
    print()

    choice = choose([
        "你看起来很累了，让我帮忙收尾吧",
        "会长大人还真是辛苦呢",
    ])

    if choice == 0:
        dialogue("你", "你看起来很累了，剩下的我帮你一起弄。")
        dialogue("亚美", "诶？不用啦，我一个人——")
        dialogue("你", "别逞强了。两个人十分钟就能搞定。")
        ami.change(15)
        narrate("她愣了一下，然后笑了。")
        dialogue("亚美", "……那我就不客气了。谢谢！")
    else:
        dialogue("你", "会长大人还真是辛苦呢。")
        dialogue("亚美", "嘿嘿，这是我的职责嘛。")
        ami.change(5)
        narrate("她笑着摆了摆手，但你看到她悄悄甩了甩酸痛的手腕。")

    print()
    dialogue("亚美", "对了，明天学园祭，你有什么安排吗？")
    dialogue("你", "还没想好。")
    dialogue("亚美", "那要不要来帮忙？开幕式的主持，我一个人有点紧张……")
    narrate("她的眼神里有一瞬间的不安，很快就恢复了平常的笑容。")
    wait()
    return "ami_2"


def scene_ami_2():
    """亚美路线第 2 场：夜晚的操场"""
    scene("夜晚的操场")
    narrate("帮亚美收拾完办公室，你们一起走在回家的路上。")
    narrate("经过操场时，亚美突然停下了脚步。")
    print()
    dialogue("亚美", "等一下……你看。")
    narrate("操场中央的舞台上，装饰灯已经亮了。")
    narrate("虽然明天才正式开始，但提前亮起的灯光在夜色中格外梦幻。")
    print()
    dialogue("亚美", "好看吗？这是我设计的灯光方案。")
    dialogue("你", "很漂亮。")
    dialogue("亚美", "嘿嘿，谢谢。其实……我花了一个月才说服学校批准这个方案。")
    narrate("她的声音难得地安静了下来。")
    dialogue("亚美", "所有人都觉得学生会的工作很风光，但其实……很多时候也会想放弃。")
    print()

    choice = choose([
        "但你还是坚持下来了，这就是你厉害的地方",
        "如果累了，偶尔也可以依靠别人",
    ])

    if choice == 0:
        dialogue("你", "但你还是坚持下来了，这就是你最厉害的地方。")
        ami.change(10)
        narrate("她回过头，灯光映在她的瞳孔里。")
        dialogue("亚美", "……你这么一说，好像确实挺厉害的？")
        narrate("她又恢复了平常的笑容，但这次看起来更加真实。")
    else:
        dialogue("你", "如果累了，偶尔也可以依靠别人的。")
        ami.change(15)
        narrate("她沉默了一会儿。")
        dialogue("亚美", "……你知道吗，你是第一个对我说这种话的人。")
        narrate("她望着舞台上的灯光，声音很轻。")
        dialogue("亚美", "大家都觉得亚美什么都能做到，不需要帮助。")
        dialogue("你", "但你也是普通人。")
        dialogue("亚美", "……嗯。谢谢你。")

    print()
    narrate("晚风吹过操场，舞台上的灯光闪烁了一下。")
    dialogue("亚美", "明天……你真的会来吗？开幕式九点开始，别迟到哦。")
    dialogue("你", "一定到。")
    dialogue("亚美", "那就说定了！拉钩？")
    narrate("她伸出小拇指，笑着看着你。")
    wait()
    return "ami_end"


def scene_ami_end():
    """亚美结局"""
    scene("亚美 · 结局")
    affection_bar(ami)

    if ami.ending() == "good":
        print()
        narrate("～～～ Good Ending ～～～")
        print()
        narrate("第二天的开幕式上，亚美站在舞台中央，手持麦克风。")
        narrate("台下人山人海，她的声音清脆而有力。")
        narrate("当你在人群中对她竖起大拇指时，她的笑容变得更加灿烂。")
        narrate("开幕式结束后，她第一时间跑下舞台找到你。")
        print()
        dialogue("亚美", "怎么样怎么样？我刚才没有结巴吧？")
        dialogue("你", "完美。")
        dialogue("亚美", "真的？太好了！多亏了你昨晚说的话。")
        narrate("她笑着拍了拍你的肩膀，然后突然凑近你耳边。")
        dialogue("亚美", "谢谢你……今晚能陪我看闭幕式的烟火吗？就我们两个人。")
        print()
        narrate("学园祭的烟火在夜空绽放。")
        narrate("亚美的笑容在烟火的映照下，比任何时刻都要耀眼。")
    else:
        print()
        narrate("～～～ Normal Ending ～～～")
        print()
        narrate("第二天的开幕式上，亚美表现出色。")
        narrate("你在人群中远远地看着她，心里有些庆幸。")
        narrate("也许没有说出口的话，也是一种温柔。")

    print()
    narrate("~ 学园祭前夜 ~ 亚美路线 · 完 ~")
    wait("按 Enter 查看总结…")
    return "ending_summary"


# ─── 雨宫路线 ──────────────────────────────────────────────────────

def scene_amamiya_1():
    """雨宫路线第 1 场"""
    scene("操场的银色身影")
    narrate("你追出教学楼，来到空旷的操场上。")
    narrate("夕阳已经快要落山了，天边只剩最后一抹橘红色的光。")
    narrate("操场上只有一个身影——银色的长发在风中轻轻飘动。")
    print()
    dialogue("你", "那个——！刚才在走廊的，是你吗？")
    narrate("银发少女回过头，露出一张安静而美丽的面孔。")
    narrate("你认出来了——她是这学期刚转来的雨宫。几乎不和任何人说话，总是独来独往。")
    print()
    dialogue("雨宫", "……嗯。")
    dialogue("你", "你还没回去吗？")
    dialogue("雨宫", "我在看夕阳。")
    narrate("她的目光重新投向远方的地平线。")
    dialogue("雨宫", "夕阳很美。每一片都不一样。")
    print()

    choice = choose([
        "在她身边坐下来，一起看夕阳",
        "好奇地问她为什么总是一个人",
    ])

    if choice == 0:
        dialogue("你", "不介意的话，我也想看看。")
        narrate("你在她身边坐下来。她没有说话，但也没有躲开。")
        amamiya.change(12)
        narrate("两个人就这样安静地坐着，看太阳一点点沉入地平线。")
        dialogue("雨宫", "……谢谢你。这是转学以来，第一次有人陪我看夕阳。")
    else:
        dialogue("你", "你为什么总是一个人？")
        dialogue("雨宫", "……习惯了。")
        dialogue("你", "一个人不会寂寞吗？")
        dialogue("雨宫", "寂寞……是什么感觉？"
              "可能我已经分不清了。")
        amamiya.change(8)
        narrate("她的话语像是在自言自语，又像是在问你。")

    print()
    narrate("最后一丝光线消失，天色暗了下来。远处的校园亮起了灯。")
    dialogue("雨宫", "……天黑了。该回去了。")
    narrate("她站起身，拍了拍裙子上的灰尘。")
    dialogue("雨宫", "你叫什么名字？")
    dialogue("你", "我叫——（你说了自己的名字）。")
    dialogue("雨宫", "……我记住了。")
    narrate("她轻轻点了点头，转身走向校门。")
    narrate("走了几步，她回过头。")
    dialogue("雨宫", "明天……学园祭，你来吗？")
    dialogue("你", "来。")
    dialogue("雨宫", "……嗯。那，明天见。")
    wait()
    return "amamiya_2"


def scene_amamiya_2():
    """雨宫路线第 2 场：天文台"""
    scene("天 文 台")
    narrate("第二天学园祭，你如约来到校园。")
    narrate("到处都是热闹的摊位和欢快的笑声，但你一直在寻找那个银色的身影。")
    narrate("最后，你在校舍天台的天文台前找到了雨宫。")
    print()
    dialogue("雨宫", "你来了。")
    dialogue("你", "我答应过你的。")
    narrate("天文台的圆顶敞开着，望远镜指向天空。")
    dialogue("雨宫", "我负责天文社的学园祭展示……虽然只有我一个人。")
    dialogue("你", "只有一个人？"
          "社团没有其他成员吗？")
    dialogue("雨宫", "没有。不过……一个人就够了。"
          "星星不会嫌人少。")
    print()

    choice = choose([
        "那我今天当你的搭档吧",
        "星星真的很美，能教我用望远镜吗？",
    ])

    if choice == 0:
        dialogue("你", "那我今天就当你的搭档吧。两个人的天文社。")
        amamiya.change(15)
        narrate("她微微睁大了眼睛，然后嘴角浮现了一个很浅很浅的微笑。")
        dialogue("雨宫", "……好。两个人的天文社。")
    else:
        dialogue("你", "星星真的很美。能教我怎么用望远镜吗？")
        dialogue("雨宫", "……嗯。")
        amamiya.change(10)
        narrate("她耐心地教你调整望远镜的角度，讲解每一颗星星的名字。")
        narrate("平时沉默寡言的她，在谈起星空时，眼睛里有了光。")

    print()
    narrate("夜幕降临，天文台前聚集了越来越多的人。")
    narrate("雨宫站在望远镜旁，用她独有的温柔声音，向每个人介绍星座的故事。")
    narrate("你在她身边帮忙引导人群，偶尔和她相视而笑。")
    print()
    dialogue("雨宫", "今天的参观者……比预想的多。")
    dialogue("你", "因为你讲得很好。")
    dialogue("雨宫", "不……是因为有你在。")
    narrate("说完这句话，她似乎意识到了什么，别过了头。")
    dialogue("雨宫", "……我去准备关门了。")
    wait()
    return "amamiya_end"


def scene_amamiya_end():
    """雨宫结局"""
    scene("雨宫 · 结局")
    affection_bar(amamiya)

    if amamiya.ending() == "good":
        print()
        narrate("～～～ Good Ending ～～～")
        print()
        narrate("学园祭的最后一天，天文台前多了一块小黑板。")
        narrate("上面用漂亮的字体写着：「双人天文社 —— 欢迎加入」。")
        print()
        dialogue("你", "这是你写的？")
        dialogue("雨宫", "嗯……两个人的社团，需要更多的成员。")
        dialogue("你", "所以你不打算一个人了？")
        dialogue("雨宫", "……嗯。因为遇到了你。")
        narrate("银色的头发在夕阳下泛着微光，她的声音依然很轻，但不再像风一样遥远。")
        dialogue("雨宫", "你知道吗……在遇到你之前，我以为星星只能一个人看。")
        dialogue("你", "现在呢？")
        dialogue("雨宫", "现在……我想和你一起看。每一颗。")
        print()
        narrate("操场上，最后一缕夕阳沉入远方。")
        narrate("天文台的望远镜指向天际，第一颗星已经在暗蓝色的天空中亮起。")
        narrate("你和雨宫并肩站在一起，看着满天繁星缓缓铺展开来。")
        narrate("也许从今天开始，她不再是独自看星星的转学生。")
        narrate("而你，也不再是那个被留在教学楼里的普通人。")
    else:
        print()
        narrate("～～～ Normal Ending ～～～")
        print()
        narrate("学园祭结束了，雨宫依然安静地独来独往。")
        narrate("但你注意到，她偶尔会看向天文台的方向，嘴角带着淡淡的笑意。")
        narrate("也许，有些连接不需要言语，只需要一颗愿意停留的心。")

    print()
    narrate("~ 学园祭前夜 ~ 雨宫路线 · 完 ~")
    wait("按 Enter 查看总结…")
    return "ending_summary"


# ─── 总结画面 ──────────────────────────────────────────────────────

def scene_ending_summary():
    """游戏结束总结"""
    scene("游 戏 完 结")
    print()
    narrate("感谢游玩「学园祭前夜 ~Starlight Festival Eve~」")
    print()
    print("  ╔══════════════════════════════════════╗")
    print("  ║          最 终 好 感 度              ║")
    print("  ╠══════════════════════════════════════╣")
    affection_bar(yukiko)
    affection_bar(ami)
    affection_bar(amamiya)
    print("  ╚══════════════════════════════════════╝")
    print()

    endings = {
        yukiko: yukiko.ending(),
        ami: ami.ending(),
        amamiya: amamiya.ending(),
    }

    narrate("你经历的故事：")
    for char, ending in endings.items():
        label = "♥ Good" if ending == "good" else "Normal"
        narrate(f"  {char.name}：{label}（好感 {char.affection}）")

    print()
    narrate("共 3 条路线，每条路线有 Good / Normal 两种结局。")
    narrate("想体验其他结局的话，请重新开始吧！")
    print()
    narrate("—— 「学园祭前夜 ~Starlight Festival Eve~」 ——")
    wait("按 Enter 退出…")


# ══════════════════════════════════════════════════════════════════════
#  场景路由表
# ══════════════════════════════════════════════════════════════════════

SCENES = {
    "intro":         scene_intro,
    "choose_path":   scene_choose_path,
    "yukiko_1":      scene_yukiko_1,
    "yukiko_2":      scene_yukiko_2,
    "yukiko_end":    scene_yukiko_end,
    "ami_1":         scene_ami_1,
    "ami_2":         scene_ami_2,
    "ami_end":       scene_ami_end,
    "amamiya_1":     scene_amamiya_1,
    "amamiya_2":     scene_amamiya_2,
    "amamiya_end":   scene_amamiya_end,
    "ending_summary": scene_ending_summary,
}


# ══════════════════════════════════════════════════════════════════════
#  主循环
# ══════════════════════════════════════════════════════════════════════

def main():
    current = "intro"
    while current in SCENES:
        current = SCENES[current]()


if __name__ == "__main__":
    main()
