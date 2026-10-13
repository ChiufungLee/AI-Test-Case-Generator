"""PDF 版面还原（utils/pdf_layout）与 Markdown 表格渲染回归。

背景：本环境下 unstructured 的 fast 策略实测返回空 → PDF 退到 PyPDFLoader（pypdf），
按内容流顺序输出文本；Word/网页导出的文档常把表格按"列"写入内容流，
于是表格被拆成"一列一行"，字段名与取值失去对应关系。这里锁住修复后的行为：
- 按字形坐标恢复阅读顺序，按列写入的内容也还原成"一行一条记录"；
- 列起点对齐的多行输出 Markdown 表格，非表格行保持原样；
- 旋转水印（斜排文字）不进正文；
- 版面还原失败或取不到正文时回退 DocumentProcessor（含纯图片 PDF 的 OCR 路径）。
"""

import pytest
from langchain_core.documents import Document

from utils import data_handle
from utils.pdf_layout import (
    _Cell,
    _group_rows,
    _is_upright,
    _render_rows,
    _rows_align,
    extract_pdf_pages_with_layout,
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


def _rows_text(rows):
    """只取文本（忽略每行归属的页码）"""
    return [text for _, text in _render_rows(rows)]


def test_column_major_chars_render_as_markdown_table():
    """内容流按列写入（一列接一列）时，也要还原成一行一条记录的 Markdown 表格"""
    chars = []
    for y, text in ((700, "名称"), (680, "消息类型名称"), (660, "说明")):
        chars.extend(_text_chars(text, x0=50, y0=y))
    for y, text in ((700, "描述"), (680, "规则同创建"), (660, "规则同创建")):
        chars.extend(_text_chars(text, x0=200, y0=y))
    chars.extend(_text_chars("备注", x0=400, y0=700))

    lines = _rows_text(_group_rows(chars))

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

    lines = _rows_text(_group_rows(chars))

    assert lines[0] == "文档名  第 1 页"
    assert lines[1] == "正文第一行"


def test_bullet_list_with_separate_bullet_text_is_not_a_table():
    """Word 把项目符号与正文拆成两段文本对象：要按列表输出，不能变成两列表格"""
    chars = []
    for y, text in ((700, "⚫"), (680, "⚫"), (660, "⚫")):
        chars.extend(_text_chars(text, x0=50, y0=y, char_width=12.0))
    for y, text in ((700, "软件版本：1.2.5"), (680, "操作系统：Ubuntu"), (660, "复现步骤：见附件")):
        chars.extend(_text_chars(text, x0=200, y0=y))

    lines = _rows_text(_group_rows(chars))

    assert lines == ["⚫ 软件版本：1.2.5", "⚫ 操作系统：Ubuntu", "⚫ 复现步骤：见附件"]


def test_rows_align_allows_missing_trailing_columns():
    anchors = [50.0, 200.0, 400.0]

    assert _rows_align(anchors, [_Cell(50.0, "a"), _Cell(200.0, "b")]) is True
    assert _rows_align(anchors, [_Cell(50.0, "a"), _Cell(400.0, "c")]) is True
    assert _rows_align(anchors, [_Cell(120.0, "a"), _Cell(200.0, "b")]) is False
    assert _rows_align(anchors, [_Cell(50.0, "a")] * 4) is False
    assert _rows_align(anchors, []) is False


def test_rows_align_requires_first_column():
    """只有后续列有内容 = 上一格换行，不能当成"首列为空的新行"（会把表格撑成空行）"""
    anchors = [50.0, 200.0, 400.0]

    assert _rows_align(anchors, [_Cell(200.0, "b")]) is False
    assert _rows_align(anchors, [_Cell(50.0, "a")]) is True


def test_multiline_cell_folds_into_previous_row():
    """单元格内多行（换行/嵌套列表）：折叠回该单元格，且后续行仍在同一张表格里"""
    chars = []
    # 表头 + 第一行（描述列内容有 3 行）+ 下一个正常行
    chars.extend(_text_chars("name", x0=50, y0=700))
    chars.extend(_text_chars("desc", x0=200, y0=700))
    chars.extend(_text_chars("bulk", x0=50, y0=660))
    chars.extend(_text_chars("single", x0=200, y0=660))
    chars.extend(_text_chars("1.all success", x0=200, y0=644, char_width=6.0))
    chars.extend(_text_chars("2.partial ok", x0=200, y0=628, char_width=6.0))
    chars.extend(_text_chars("channel", x0=50, y0=580))
    chars.extend(_text_chars("multi", x0=200, y0=580))
    chars.extend(_text_chars("a.nested item", x0=230, y0=564, char_width=6.0))

    lines = _rows_text(_group_rows(chars))

    assert lines[0] == "| name | desc |"
    assert lines[1] == "| --- | --- |"
    assert lines[2] == "| bulk | single<br>1.all success<br>2.partial ok |"
    assert lines[3] == "| channel | multi<br>a.nested item |"


def test_line_left_of_table_or_far_below_ends_table():
    """表格左边界之外的行、以及行距明显变大的行都不属于表格（否则正文会被吸进表里）"""
    chars = []
    chars.extend(_text_chars("name", x0=50, y0=700))
    chars.extend(_text_chars("desc", x0=200, y0=700))
    chars.extend(_text_chars("row1", x0=50, y0=670))
    chars.extend(_text_chars("value1", x0=200, y0=670))
    chars.extend(_text_chars("row2", x0=50, y0=640))
    chars.extend(_text_chars("value2", x0=200, y0=640))
    # 左边界之外（28 < 50-4）→ 表格结束
    chars.extend(_text_chars(">> done", x0=28, y0=610, char_width=6.0))
    # 行距明显变大 → 即使 x 在表内也不算续行
    chars.extend(_text_chars("later paragraph", x0=200, y0=500, char_width=6.0))

    lines = _rows_text(_group_rows(chars))

    assert lines == [
        "| name | desc |",
        "| --- | --- |",
        "| row1 | value1 |",
        "| row2 | value2 |",
        ">> done",
        "later paragraph",
    ]


def test_cross_page_table_continues(monkeypatch):
    """跨页续排的表格：换页后仍接在同一张表里（不因页边界被截断成正文）"""
    chars = []
    chars.extend(_text_chars("name", x0=50, y0=100, char_width=6.0))
    chars.extend(_text_chars("desc", x0=200, y0=100, char_width=6.0))
    chars.extend(_text_chars("row1", x0=50, y0=70, char_width=6.0))
    chars.extend(_text_chars("value1", x0=200, y0=70, char_width=6.0))
    first_page_rows = _group_rows(chars)
    for row in first_page_rows:
        row.page = 0

    second_chars = []
    second_chars.extend(_text_chars("1.wrapped", x0=200, y0=800, char_width=6.0))
    second_chars.extend(_text_chars("row2", x0=50, y0=740, char_width=6.0))
    second_chars.extend(_text_chars("value2", x0=200, y0=740, char_width=6.0))
    second_page_rows = _group_rows(second_chars)
    for row in second_page_rows:
        row.page = 1

    lines = _rows_text(first_page_rows + second_page_rows)

    assert lines[0] == "| name | desc |"
    assert lines[1] == "| --- | --- |"
    assert lines[2] == "| row1 | value1<br>1.wrapped |"
    assert lines[3] == "| row2 | value2 |"


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


def test_extract_pdf_text_with_layout_folds_multiline_cells(tmp_path, make_layout_pdf):
    """真实 PDF：单元格内多行折叠进单元格，后续行仍留在同一张表里"""
    pdf_path = make_layout_pdf(
        tmp_path / "wrapped.pdf",
        rows=[
            ["name", "desc", "note"],
            ["bulk", ["single", "1.all success", "2.partial ok"]],
            ["channel", ["multi", "1.from config"]],
        ],
    )

    lines = extract_pdf_text_with_layout(str(pdf_path)).splitlines()

    assert lines[0] == "| name | desc | note |"
    assert lines[1] == "| --- | --- | --- |"
    assert lines[2] == "| bulk | single<br>1.all success<br>2.partial ok |  |"
    assert lines[3] == "| channel | multi<br>1.from config |  |"


def test_extract_pdf_pages_with_layout_keeps_page_numbers(tmp_path, make_layout_pdf):
    """逐页还原：每页一份文本（RAG 分块要按页溯源）"""
    pdf_path = make_layout_pdf(
        tmp_path / "pages.pdf",
        rows=[["name", "desc"], ["row1", "value1"]],
    )

    pages = extract_pdf_pages_with_layout(str(pdf_path))

    assert len(pages) >= 1
    assert pages[0].splitlines()[0] == "| name | desc |"


def test_document_processor_uses_layout_extraction(tmp_path, make_layout_pdf):
    """知识库/聊天附件入库：优先用版面还原分块（表格保留为 Markdown、带页码 metadata）"""
    from utils.file_handle import get_document_processor

    pdf_path = make_layout_pdf(
        tmp_path / "kb.pdf",
        rows=[["name", "desc"], ["row1", "value1"], ["row2", "value2"]],
    )

    documents = get_document_processor().load_pdf(str(pdf_path))

    assert documents, "版面还原应能产出分块"
    assert all(doc.metadata["source"] == "kb.pdf" for doc in documents)
    assert all(doc.metadata["page"] == 1 for doc in documents)
    merged = "\n".join(doc.page_content for doc in documents)
    assert "| name | desc |" in merged
    assert "| --- | --- |" in merged


def test_document_processor_falls_back_when_layout_unavailable(monkeypatch, tmp_path):
    """版面还原取不到正文（如纯图片 PDF）时回退既有链路（unstructured → PyPDFLoader → OCR）"""
    from utils import file_handle

    monkeypatch.setattr(file_handle, "extract_pdf_pages_with_layout", lambda path: [])
    monkeypatch.setattr(file_handle, "partition_pdf", lambda **kwargs: [])

    class _Loader:
        def __init__(self, path):
            self.path = path

        def load(self):
            return [Document(page_content="fallback page", metadata={"source": self.path})]

    monkeypatch.setattr("langchain_community.document_loaders.PyPDFLoader", _Loader)

    documents = file_handle.get_document_processor().load_pdf(str(tmp_path / "scan.pdf"))

    assert documents
    assert "fallback page" in documents[0].page_content


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


@pytest.mark.asyncio
async def test_chat_attachment_text_is_cleaned(monkeypatch, test_env):
    """对话页面上传的普通附件：与需求文档共用同一套字符清理（部首归一 + 噪声剔除）"""
    import io

    class _Upload:
        filename = "附件.pdf"

        def __init__(self):
            self.file = io.BytesIO(b"%PDF-1.4\nstub\n")

    monkeypatch.setattr(
        knowledge_service,
        "_load_pdf_text",
        lambda path: "需求\ufffd：租\u2f3e、\u2edc\u4e66\u0007",
    )

    text = await knowledge_service.extract_pdf_text(_Upload())

    assert text == "需求：租户、飞书"
