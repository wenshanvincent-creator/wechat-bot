"""Native inline WeChat expressions use text tokens, not image/sticker uploads."""
import re

NAMES=tuple(dict.fromkeys('''微笑 撇嘴 色 发呆 得意 流泪 害羞 闭嘴 睡 大哭 尴尬 发怒 调皮 呲牙 惊讶 难过 囧 抓狂 吐 偷笑 愉快 白眼 傲慢 困 惊恐 流汗 憨笑 悠闲 奋斗 咒骂 疑问 嘘 晕 衰 骷髅 敲打 再见 擦汗 抠鼻 鼓掌 坏笑 左哼哼 右哼哼 哈欠 鄙视 委屈 快哭了 亲亲 可怜 菜刀 西瓜 啤酒 咖啡 猪头 玫瑰 凋谢 嘴唇 爱心 心碎 蛋糕 炸弹 便便 月亮 太阳 拥抱 强 弱 握手 胜利 抱拳 勾引 拳头 OK 跳跳 发抖 怄火 转圈 嘿哈 捂脸 奸笑 机智 皱眉 耶 红包 發 福 旺柴 恐惧 失望 无语 笑脸 生病 脸红 破涕为笑 阴险 吃瓜 加油 汗 天啊 Emm 社会社会 好的 打脸 哇 翻白眼 666 让我看看 叹气 苦涩 裂开 合十 庆祝 礼物 烟花 爆竹'''.split()))
ALIASES={'狗头':'旺柴','doge':'旺柴','笑哭':'破涕为笑','发':'發','ok':'OK','emm':'Emm','社会':'社会社会'}


def token(value):
    if not isinstance(value,str):return None
    name=value.strip()
    if name.startswith('[') and name.endswith(']'):name=name[1:-1]
    name=ALIASES.get(name,ALIASES.get(name.lower(),name))
    return '['+name+']' if name in NAMES else None


def normalize_text(text):
    # Unknown bracketed prose remains unchanged; it may be code or a quotation.
    return re.sub(r'\[([^\[\]\r\n]{1,16})\]',lambda match:token(match[1]) or match[0],text)


def guidance():
    return ('微信默认小表情直接写在文字气泡里，可单发或混排。'
            '它们通过文字通道发送并由微信原生显示，不需要收藏、识图或图片上传；不要因收藏列表为空而说不能发默认小表情。'
            'sticker 对象专用于已收藏的大表情包。'
            '先理解对方情绪、你这句话的态度和双方关系，再决定是否需要表情，最后才选种类；不用每句配。'
            '轻松互损可用坏笑、旺柴或偷笑；真疑惑才用疑问，惊讶才用惊讶，别把疑问当万能句尾。'
            '委屈、失落、安慰、祝贺、感谢各用对应含义；认真难过或求助时别塞嘲讽、吃瓜、色或嬉笑。'
            '微笑、再见、鼓掌、白眼在不同关系里可能显得阴阳怪气，不只按字面理解。'
            '换着用是换贴切的表达，不是轮流打卡；没有合适的就不加，一般一个就够。'
            '默认小表情可选代码：'+''.join('['+name+']' for name in NAMES))
