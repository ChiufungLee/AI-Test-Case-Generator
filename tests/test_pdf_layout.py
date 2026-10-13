"""PDF 版面还原（utils/pdf_layout）与 Markdown 表格渲染回归。

背景：本环境下 unstructured 的 fast 策略实测返回空 → PDF 退到 PyPDFLoader（pypdf），
按内容流顺序输出文本；Word/网页导出的文档常把表格按"列"写入内容流，
于是表格被拆成"一列一行"，字段名与取值失去对应关系。这里锁住修复后的行为：
- 按字形坐标恢复阅读顺序，按列写入的内容也还原成"一行一条记录"；
- 列起点对齐的多行输出 Markdown 表格，非表格行保持原样；
- 旋转水印（斜排文字）不进正文；
- 版面还原失败或取不到正文时回退 DocumentProcessor（含纯图片 PDF 的 OCR 路径）。
"""

from utils import data_handle
from utils.pdf_layout import (
    _Cell,
    _group_rows,
    _is_upright,
    _render_rows,
    _rows_align,
    extract_pdf_text_with_layout,
)
from services import knowledge_service


class FakeChar:
    """LTChar 的最小替身：只提供 pdf_layout 用到的几何与文本接口"""

    def __init__(self, text, x0, y0, width=12.0, height=12.0, rotated=False):
        self.text = text
        self.x0 = x0
        self.x1 = x0 + width
        self.y0 = y0
        self.y1 = y0 + height
        self.width = width
        self.height = height
        self.matrix = (
            (width, 3.0, -3.0, height, x0, y0) if rotated else (width, 0.0, 0.0, height, x0, y0)
        )

    def get_text(self):
        return self.text


def _text_chars(text: str, *, x0: float, y0: float, char_width: float = 12.0) -> list[FakeChar]:
    return [
        FakeChar(char, x0 + index * char_width, y0, width=char_width)
        for index, char in enumerate(text)
    ]


def test_column_major_chars_render_as_markdown_table():
    """内容流按列写入（一列接一列）时，也要还原成一行一条记录的 Markdown 表格"""
    chars = []
    for y, text in ((700, "名称"), (680, "消息类型名称"), (660, "说明")):
        chars.extend(_text_chars(text, x0=50, y0=y))
    for y, text in ((700, "描述"), (680, "规则同创建"), (660, "规则同创建")):
        chars.extend(_text_chars(text, x0=200, y0=y))
    chars.extend(_text_chars("备注", x0=400, y0=700))

    lines = _render_rows(_group_rows(chars))

    assert lines[0] == "| 名称 | 描述 | 备注 |"
    assert lines[1] == "| --- | --- | --- |"
    assert lines[2].startswith("| 消息类型名称 | 规则同创建")
    assert lines[3].startswith("| 说明 | 规则同创建")


def test_two_column_line_without_alignment_stays_plain_text():
    """只有一行、或列对不齐的多段文本不算表格（避免把页眉页脚当表格）"""
    chars = (
        _text_chars("文档名", x0=50, y0=700)
        + _text_chars("第 1 页", x0=450, y0=700)
        + _text_chars("正文第一行", x0=50, y0=660)
    )

    lines = _render_rows(_group_rows(chars))

    assert lines[0] == "文档名  第 1 页"
    assert lines[1] == "正文第一行"


def test_bullet_list_with_separate_bullet_text_is_not_a_table():
    """Word 把项目符号与正文拆成两段文本对象：要按列表输出，不能变成两列表格"""
    chars = []
    for y, text in ((700, "⚫"), (680, "⚫"), (660, "⚫")):
        chars.extend(_text_chars(text, x0=50, y0=y, char_width=12.0))
    for y, text in ((700, "软件版本：1.2.5"), (680, "操作系统：Ubuntu"), (660, "复现步骤：见附件")):
        chars.extend(_text_chars(text, x0=200, y0=y))

    lines = _render_rows(_group_rows(chars))

    assert lines == ["⚫ 软件版本：1.2.5", "⚫ 操作系统：Ubuntu", "⚫ 复现步骤：见附件"]


def test_rows_align_allows_missing_trailing_columns():
    anchors = [50.0, 200.0, 400.0]

    assert _rows_align(anchors, [_Cell(50.0, "a"), _Cell(200.0, "b")]) is True
    assert _rows_align(anchors, [_Cell(50.0, "a"), _Cell(400.0, "c")]) is True
    assert _rows_align(anchors, [_Cell(120.0, "a"), _Cell(200.0, "b")]) is False
    assert _rows_align(anchors, [_Cell(50.0, "a")] * 4) is False
    assert _rows_align(anchors, []) is False


def test_rotated_chars_are_recognized_as_watermark():
    assert _is_upright(FakeChar("水", 100, 100)) is True
    assert _is_upright(FakeChar("水", 100, 100, rotated=True)) is False


def test_to_markdown_table_pads_columns_and_escapes_pipes():
    markdown = data_handle.to_markdown_table([["名称", "描述"], ["a|b", "c"], ["d"]])

    assert markdown.splitlines() == [
        "| 名称 | 描述 |",
        "| --- | --- |",
        "| a\\|b | c |",
        "| d |  |",
    ]
    assert data_handle.to_markdown_table([]) == ""


def test_extract_pdf_text_with_layout_reorders_table_and_drops_watermark(
    tmp_path, make_layout_pdf
):
    """真实 PDF：表格按列写入内容流 + 45° 斜排水印"""
    pdf_path = make_layout_pdf(
        tmp_path / "table.pdf",
        rows=[["name", "desc", "note"], ["msg type", "same as create"], ["remark", "same as create"]],
    )

    text = extract_pdf_text_with_layout(str(pdf_path))
    lines = text.splitlines()

    assert lines[0] == "| name | desc | note |"
    assert lines[1] == "| --- | --- | --- |"
    assert "| msg type | same as create |  |" in lines
    assert "| remark | same as create |  |" in lines
    assert "CONFIDENTIAL" not in text


def test_load_pdf_text_prefers_layout_extraction(monkeypatch, document_processor):
    monkeypatch.setattr(knowledge_service, "extract_pdf_text_with_layout", lambda path: "版面还原文本")

    assert knowledge_service._load_pdf_text("any.pdf") == "版面还原文本"


def test_load_pdf_text_falls_back_when_no_text(monkeypatch, document_processor):
    """纯图片 PDF 取不到文字：回退 DocumentProcessor（含 OCR）路径"""
    monkeypatch.setattr(knowledge_service, "extract_pdf_text_with_layout", lambda path: "   ")

    assert knowledge_service._load_pdf_text("any.pdf") == "pdf content"


def test_load_pdf_text_falls_back_when_layout_extraction_raises(monkeypatch, document_processor):
    def _boom(path):
        raise RuntimeError("layout failed")

    monkeypatch.setattr(knowledge_service, "extract_pdf_text_with_layout", _boom)

    assert knowledge_service._load_pdf_text("any.pdf") == "pdf content"
