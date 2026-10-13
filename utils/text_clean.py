"""提取正文的字符清理：兼容汉字归一 + 无字形噪声剔除。

为什么单独成模块：同一份 PDF 会在两条链路上被解析——需求文档上传（`knowledge_service`）
与知识库/聊天附件入库（`utils/file_handle` 的 RAG 分块）。清理规则必须共用，
否则同一份文档在两处会得到不同文本（一处已归一、一处还是"方框"）。

两类问题都来自 PDF/Word 文本转换：
- 康熙部首（U+2F00–U+2FDF）、CJK 兼容汉字（U+F900–FAFF）：有 NFKC 分解；
- CJK 部首补充（U+2E80–U+2EFF）：Unicode 未给分解，按 Unicode 名称查表；
- 无字形字符：C0/C1 控制符、私用区（符号字体残留）、U+FFFD（字形映射缺失的占位）、
  零宽与双向控制符——它们在页面上只会显示成方框，对 LLM 与检索都是纯噪声。
"""

import re
import unicodedata

# 提取结果中的连续空行（3 个以上换行）压缩为单个空行
_MULTI_NEWLINE_RE = re.compile(r"\n{3,}")

# 无字形字符（浏览器只能画成方框/豆腐块）：零宽与双向控制符
_INVISIBLE_FORMAT_CHARS = frozenset(
    {"\u200b", "\u200c", "\u200d", "\u200e", "\u200f", "\ufeff"}
)
# 语义上是换行、但浏览器/文本框里常显示成方框的字符
_UNICODE_LINE_SEPARATORS = ("\u2028", "\u2029", "\u0085")

# CJK 部首补充（U+2E80–U+2EFF）在 Unicode 里没有 NFKC 分解，但 PDF 抽取常把"简体字形"
# 落在这段码位（实测需求文档里的 ⻜书、部⻔）。这里按 Unicode 名称逐字给出对应汉字。
_RADICAL_SUPPLEMENT_MAP = {
    0x2EB0: "纟",  # C-SIMPLIFIED SILK
    0x2EC5: "见",  # C-SIMPLIFIED SEE
    0x2EC6: "角",  # SIMPLIFIED HORN
    0x2EC8: "讠",  # C-SIMPLIFIED SPEECH
    0x2EC9: "贝",  # C-SIMPLIFIED SHELL
    0x2ECB: "车",  # C-SIMPLIFIED CART
    0x2ECC: "辶",  # SIMPLIFIED WALK
    0x2ED0: "钅",  # C-SIMPLIFIED GOLD
    0x2ED3: "长",  # C-SIMPLIFIED LONG
    0x2ED4: "门",  # C-SIMPLIFIED GATE
    0x2ED9: "韦",  # C-SIMPLIFIED TANNED LEATHER
    0x2EDA: "页",  # C-SIMPLIFIED LEAF
    0x2EDB: "风",  # C-SIMPLIFIED WIND
    0x2EDC: "飞",  # C-SIMPLIFIED FLY
    0x2EE0: "饣",  # C-SIMPLIFIED EAT
    0x2EE2: "马",  # C-SIMPLIFIED HORSE
    0x2EE5: "鱼",  # C-SIMPLIFIED FISH
    0x2EE6: "鸟",  # C-SIMPLIFIED BIRD
    0x2EE7: "卤",  # C-SIMPLIFIED SALT
    0x2EE8: "麦",  # SIMPLIFIED WHEAT
    0x2EE9: "黄",  # SIMPLIFIED YELLOW
    0x2EEA: "黾",  # C-SIMPLIFIED FROG
    0x2EEB: "齐",  # J-SIMPLIFIED EVEN
    0x2EEC: "齐",  # C-SIMPLIFIED EVEN
    0x2EED: "齿",  # J-SIMPLIFIED TOOTH
    0x2EEE: "齿",  # C-SIMPLIFIED TOOTH
    0x2EEF: "龙",  # J-SIMPLIFIED DRAGON
    0x2EF0: "龙",  # C-SIMPLIFIED DRAGON
    0x2EF2: "龟",  # J-SIMPLIFIED TURTLE
    0x2EF3: "龟",  # C-SIMPLIFIED TURTLE
}

# 康熙部首经 NFKC 归一后，少数落到繁体字形（⼾→戶、⾶→飛、⼀→一 无变化）。
# 这里只映射"由部首归一得到"的繁体字形，不作用于文档里本来就有的繁体正文
_TRADITIONAL_FROM_RADICAL = {
    "戶": "户", "門": "门", "車": "车", "馬": "马", "魚": "鱼", "鳥": "鸟", "頁": "页",
    "風": "风", "飛": "飞", "長": "长", "韋": "韦", "鹵": "卤", "麥": "麦", "黃": "黄",
    "黽": "黾", "齊": "齐", "齒": "齿", "龍": "龙", "龜": "龟", "貝": "贝", "見": "见",
    "靑": "青",
}


def _normalize_cjk_compat(text: str) -> str:
    """把"看着一样、码位不同"的部首字形还原为常规汉字。

    这两类码位是 PDF/Word 文本转换的常见产物：字形与常规汉字几乎一致、码位不同，
    在某些字体下没有字形就会显示成方框（例如 `租⼾` U+2F3E 实为 `租户`）。

    - 康熙部首（U+2F00–U+2FDF）与 CJK 兼容汉字（U+F900–U+FAFF）：走 NFKC 分解；
    - CJK 部首补充（U+2E80–U+2EFF）：Unicode 未给分解，按名称查表映射；
    - 逐字符处理，避免整体 NFKC 顺带把全角标点/数字也改成半角。
    """
    parts: list[str] = []
    for char in text:
        code = ord(char)
        if 0x2F00 <= code <= 0x2FDF or 0xF900 <= code <= 0xFAFF:
            mapped = unicodedata.normalize("NFKC", char)
            parts.append(_TRADITIONAL_FROM_RADICAL.get(mapped, mapped))
        else:
            parts.append(_RADICAL_SUPPLEMENT_MAP.get(code, char))
    return "".join(parts)


def _is_unrenderable_char(ch: str) -> bool:
    """判断是否为"没有字形可渲染"的解析噪声（页面上只会显示成方框）。

    - C0/C1 控制符（换行与制表保留）
    - 私用区码位（Wingdings/Symbol 等符号字体的残留，如 U+E000–U+F8FF）
    - 替换字符 U+FFFD（解析器拿不到字形映射时的占位）
    - 零宽字符与双向控制符（不可见，但会污染提示词与字符串比较）
    """
    if ch in "\n\t":
        return False
    code = ord(ch)
    if code < 0x20 or 0x7F <= code <= 0x9F:
        return True
    if 0xE000 <= code <= 0xF8FF or 0xF0000 <= code <= 0x10FFFD:
        return True
    if ch == "\ufffd" or ch in _INVISIBLE_FORMAT_CHARS:
        return True
    return 0x202A <= code <= 0x202E or 0x2066 <= code <= 0x2069


def normalize_extracted_text(text: str) -> tuple[str, int]:
    """统一换行、压缩连续空行、归一兼容汉字并剔除无字形噪声。

    返回 (正文, 被剔除的字符数)；剔除数量回显给用户，让"文档里有解析不出的符号"
    这件事可见，而不是静默丢字。
    """
    normalized = text.replace("\r\n", "\n").replace("\r", "\n")
    for separator in _UNICODE_LINE_SEPARATORS:
        normalized = normalized.replace(separator, "\n")

    normalized = _normalize_cjk_compat(normalized).replace("\u00a0", " ")
    cleaned = "".join(ch for ch in normalized if not _is_unrenderable_char(ch))
    return _MULTI_NEWLINE_RE.sub("\n\n", cleaned).strip(), len(normalized) - len(cleaned)


def sanitize_text(text: str) -> str:
    """只取清理后的正文（不需要剔除计数的调用方用这个）"""
    return normalize_extracted_text(text)[0]
