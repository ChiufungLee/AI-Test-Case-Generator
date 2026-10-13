"""PDF 版面还原：按字形坐标重建阅读顺序，并把对齐的多列区块输出为 Markdown 表格。

为什么需要这个模块：本环境下 `unstructured.partition_pdf(strategy="fast")` 实测返回 0 个元素
（`DocumentProcessor.load_pdf` 因此落到 PyPDFLoader 回退），而 pypdf 按**内容流顺序**输出文本——
Word/网页导出的文档常把表格按"列"写入内容流，于是表格被拆成"一列一行"，
字段名与其取值失去对应关系（需求分析、用例生成的输入质量直接受损）。

这里改用项目已依赖的 pdfminer.six 取字形坐标：
1. 递归收集 LTTextLine 内的字形并读取 bbox；
2. 丢弃旋转字形（水印多为斜排文字），避免水印混进正文；
3. 按纵向位置聚成视觉行、行内按 x 排序 → 恢复"从左到右、从上到下"的阅读顺序；
4. 行内按字间距切分单元格；连续多行、列起点对齐的区块渲染成 Markdown 表格；
5. **单元格内的多行内容**（换行/嵌套列表）按列区间折叠回所在单元格，跨页续排的表格也接续
   （表格常因单元格内容换行或分页而"断行"，不折叠就会把表格拦腰截断成正文）。

解析不出正文时返回空串、解析异常向上抛，都由调用方决定是否回退到既有解析路径。
"""

import logging
from dataclasses import dataclass
from pathlib import Path

from pdfminer.high_level import extract_pages
from pdfminer.layout import LAParams, LTChar, LTContainer, LTTextLine

from utils.data_handle import to_markdown_table

logger = logging.getLogger(__name__)

# 同一视觉行的判定：纵向重叠超过较矮字形高度的一半即视为同行
ROW_OVERLAP_RATIO = 0.5
# 单元格切分：相邻字形水平间距超过 max(下限, 字宽中位数 × 比例) 即认为是不同单元格
CELL_GAP_MIN_PT = 8.0
CELL_GAP_WIDTH_RATIO = 1.2
# 列起点对齐容差（pt）
COLUMN_TOLERANCE_PT = 6.0
# 认定表格所需的最少行数与列数
MIN_TABLE_ROWS = 2
MIN_TABLE_COLUMNS = 2
# 续行判定：文本起点仍在表格横向范围内（左边界允许的悬挂缩进），
# 且与上一行的行距（顶到顶，与字号无关）不超过"表内最近行距"的该倍数（超过即认为表格已结束）；
# 倍数下限用于表格刚跨页接上、行距样本很小的情况
CONTINUATION_LEFT_TOLERANCE_PT = 4.0
CONTINUATION_GAP_RATIO = 1.8
CONTINUATION_GAP_MIN_PT = 24.0
# 判定"水平字形"的旋转分量阈值（LTChar.matrix 的 b/c 分量）
UPRIGHT_EPSILON = 0.01
# 项目符号：首列全是项目符号时按列表输出（Word 常把项目符号与正文拆成两段文本对象，
# 若当成两列表格会让 LLM 误以为存在"符号/正文"两列）
BULLET_CHARS = frozenset("•·◦●○⚫⚪▪▫■□◆◇‣⁃∙-–—*+")
BULLET_MAX_CHARS = 2
# 单个单元格/单行的字符上限：极端长文本不撑爆提示词（表格结构不受影响）
MAX_CELL_CHARS = 2000

@dataclass
class _Cell:
    """行内的一个单元格：列起点 + 文本"""

    x0: float
    text: str


@dataclass
class _Row:
    """一个视觉行：携带该行全部字形（按 x 排序后切分为单元格）"""

    top: float
    bottom: float
    chars: list[LTChar]
    page: int = 0

    def cells(self) -> list[_Cell]:
        """按字间距把一行字形切成单元格"""
        threshold = _cell_gap_threshold(self.chars)
        cells: list[_Cell] = []
        current: list[LTChar] = []
        for char in self.chars:
            if current and char.x0 - current[-1].x1 > threshold:
                cells.append(_build_cell(current))
                current = []
            current.append(char)
        if current:
            cells.append(_build_cell(current))
        return [cell for cell in cells if cell.text]


def _is_upright(char: LTChar) -> bool:
    """字形是否水平排布（旋转文本多为水印，不属正文）"""
    matrix = getattr(char, "matrix", None)
    if not matrix:
        return True
    return abs(matrix[1]) < UPRIGHT_EPSILON and abs(matrix[2]) < UPRIGHT_EPSILON


def _iter_chars(container) -> list[LTChar]:
    """递归收集容器内的水平字形"""
    if isinstance(container, LTTextLine):
        return [char for char in container if isinstance(char, LTChar) and _is_upright(char)]
    if isinstance(container, LTContainer):
        chars: list[LTChar] = []
        for child in container:
            chars.extend(_iter_chars(child))
        return chars
    return []


def _cell_gap_threshold(chars: list[LTChar]) -> float:
    widths = sorted(char.width for char in chars if char.width > 0)
    median = widths[len(widths) // 2] if widths else 0.0
    return max(CELL_GAP_MIN_PT, median * CELL_GAP_WIDTH_RATIO)


def _build_cell(chars: list[LTChar]) -> _Cell:
    text = "".join(char.get_text() for char in chars).strip()[:MAX_CELL_CHARS]
    return _Cell(x0=min(char.x0 for char in chars), text=text)


def _group_rows(chars: list[LTChar]) -> list[_Row]:
    """把字形按纵向位置聚成视觉行（自上而下）。

    同一表格行的多个单元格可能被 pdfminer 切成多个 LTTextLine，
    所以这里不用它的行分组，直接按字形 bbox 的纵向重叠聚合。
    """
    rows: list[_Row] = []
    for char in sorted(chars, key=lambda c: (-(c.y0 + c.y1) / 2, c.x0)):
        for row in rows:
            overlap = min(row.top, char.y1) - max(row.bottom, char.y0)
            if overlap > ROW_OVERLAP_RATIO * min(row.top - row.bottom, char.y1 - char.y0):
                row.chars.append(char)
                row.top = max(row.top, char.y1)
                row.bottom = min(row.bottom, char.y0)
                break
        else:
            rows.append(_Row(top=char.y1, bottom=char.y0, chars=[char]))

    for row in rows:
        row.chars.sort(key=lambda c: c.x0)
    return rows


def _rows_align(anchors: list[float], cells: list[_Cell]) -> bool:
    """行内每个单元格都能按顺序对位到参考列起点（允许尾部空列缺格）。

    首格必须落在第一列：只有后续列有内容时按"上一格换行"处理（单元格内多行/嵌套列表）。
    几何上无法区分它与"首列为空的新行"，按需求文档的实际情况（首列几乎总是字段名）取前者。
    """
    if not cells or len(cells) > len(anchors):
        return False
    if abs(cells[0].x0 - anchors[0]) > COLUMN_TOLERANCE_PT:
        return False
    cursor = 0
    for cell in cells:
        while cursor < len(anchors) and cell.x0 - anchors[cursor] > COLUMN_TOLERANCE_PT:
            cursor += 1
        if cursor >= len(anchors) or abs(cell.x0 - anchors[cursor]) > COLUMN_TOLERANCE_PT:
            return False
        cursor += 1
    return True


def _is_bullet_cell(cell: _Cell) -> bool:
    text = cell.text.strip()
    return bool(text) and len(text) <= BULLET_MAX_CHARS and all(ch in BULLET_CHARS for ch in text)


def _is_bullet_list(rows: list[list[_Cell]]) -> bool:
    """整块表格的首列是否全是项目符号（是则按列表输出，不渲染成两列表格）"""
    first_cells = [cells[0] for cells in rows if cells]
    return bool(first_cells) and all(_is_bullet_cell(cell) for cell in first_cells)


def _render_table(rows: list[list[_Cell]]) -> list[str]:
    """对齐的多列区块 → Markdown 表格（首行作表头）"""
    anchors = [cell.x0 for cell in rows[0]]
    table: list[list[str]] = []
    for cells in rows:
        line = ["" for _ in anchors]
        for cell in cells:
            index = min(range(len(anchors)), key=lambda i: abs(anchors[i] - cell.x0))
            line[index] = f"{line[index]} {cell.text}".strip() if line[index] else cell.text
        table.append(line)
    return to_markdown_table(table).splitlines()


def _join_cells(cells: list[_Cell]) -> str:
    """非表格行按原样拼接：项目符号与正文间用一个空格，其余多段文本用两个空格"""
    parts = [cell.text for cell in cells if cell.text]
    if not parts:
        return ""
    if _is_bullet_cell(cells[0]):
        return " ".join(parts)
    return "  ".join(parts)


def _column_index(anchors: list[float], x0: float) -> int:
    """按列区间把 x 落到某一列（取左侧最近的列起点，超出最后一列则归末列）"""
    index = 0
    for candidate, anchor in enumerate(anchors):
        if x0 >= anchor - COLUMN_TOLERANCE_PT:
            index = candidate
        else:
            break
    return index


def _within_table_span(cells: list[_Cell], anchors: list[float]) -> bool:
    """一行的文本是否都落在表格横向范围内（左侧允许悬挂缩进的容差）"""
    if not cells:
        return False
    left_edge = anchors[0] - CONTINUATION_LEFT_TOLERANCE_PT
    return all(cell.x0 >= left_edge for cell in cells)


def _line_gap(upper: _Row, lower: _Row) -> float:
    """两行行距（顶到顶度量，与字号无关，便于比较）"""
    return upper.top - lower.top


def _gap_is_close(row: _Row, last_row: _Row | None, inner_gap: float | None) -> bool:
    """与上一行的行距是否仍在"表内行距"量级。

    跨页时上一行在另一页（坐标不可比），此时不做间距判定；
    表内行距未知（如表格刚跨页接上）时同样放行。
    """
    if last_row is None or row.page != last_row.page or inner_gap is None:
        return True
    threshold = max(inner_gap * CONTINUATION_GAP_RATIO, CONTINUATION_GAP_MIN_PT)
    return _line_gap(last_row, row) <= threshold


def _fold_cells(target: list[_Cell], cells: list[_Cell], anchors: list[float]) -> None:
    """把续行文本按列区间并入目标行的对应单元格。

    单元格内的换行用 `<br>` 连接：Markdown 表格单元格里不能出现真实换行，
    `<br>` 是表格内换行的通行写法，也比空格拼接更接近原文档的版面。
    """
    for cell in cells:
        if not cell.text:
            continue
        index = _column_index(anchors, cell.x0)
        if index >= len(target):
            continue
        existing = target[index].text
        if len(existing) >= MAX_CELL_CHARS:
            continue
        merged = f"{existing}<br>{cell.text}"[:MAX_CELL_CHARS]
        target[index] = _Cell(x0=target[index].x0, text=merged)


def _render_rows(rows: list[_Row]) -> list[str]:
    """逐行输出正文；列对齐的多行渲染为 Markdown 表格，单元格内的续行折叠回该单元格"""
    lines: list[str] = []
    block: list[list[_Cell]] = []
    inner_gap: float | None = None
    last_row: _Row | None = None
    last_page: int | None = None

    def flush() -> None:
        nonlocal block, inner_gap
        if not block:
            return
        if (
            len(block) >= MIN_TABLE_ROWS
            and len(block[0]) >= MIN_TABLE_COLUMNS
            and not _is_bullet_list(block)
        ):
            lines.extend(_render_table(block))
        else:
            lines.extend(_join_cells(cells) for cells in block if _join_cells(cells))
        block = []
        inner_gap = None

    for row in rows:
        cells = row.cells()

        # 跨页时留一个空行（表格内部不留，否则 Markdown 表格会被截断）
        if last_page is not None and row.page != last_page and not block and lines and lines[-1]:
            lines.append("")

        anchors = [cell.x0 for cell in block[0]] if block else []

        # 与列起点对齐且不止一列 → 表格的新记录
        # （只有一列的行不当作新记录：几何上无法与"上一格换行"区分，按后者处理）
        if anchors and len(cells) >= MIN_TABLE_COLUMNS and _rows_align(anchors, cells):
            if last_row is not None and row.page == last_row.page:
                inner_gap = _line_gap(last_row, row)
            block.append(cells)
            last_row = row
            last_page = row.page
            continue

        # 单元格内容换行/嵌套列表：文本仍在表格范围内且间距接近表内行距 → 折叠进上一条记录
        if (
            len(block) >= MIN_TABLE_ROWS
            and anchors
            and _within_table_span(cells, anchors)
            and _gap_is_close(row, last_row, inner_gap)
        ):
            _fold_cells(block[-1], cells, anchors)
            if last_row is not None and row.page == last_row.page:
                inner_gap = _line_gap(last_row, row)
            last_row = row
            last_page = row.page
            continue

        flush()
        last_page = row.page
        if len(cells) >= MIN_TABLE_COLUMNS:
            block.append(cells)
            last_row = row
            continue

        text = _join_cells(cells)
        if text:
            lines.append(text)
        last_row = row

    flush()
    return lines


def extract_pdf_text_with_layout(file_path: str) -> str:
    """按版面还原 PDF 正文（表格以 Markdown 表格呈现，单元格内的换行折叠进单元格）。

    各页行按页序拼接后再统一渲染，这样跨页续排的表格不会被页边界截断。
    解析不出内容（如纯图片 PDF）时返回空串；解析异常向上抛，由调用方决定回退。
    """
    pages = extract_pages(str(file_path), laparams=LAParams())
    rows: list[_Row] = []
    for page_index, page in enumerate(pages):
        for row in _group_rows(_iter_chars(page)):
            row.page = page_index
            rows.append(row)

    lines = [line for line in _render_rows(rows) if line.strip()]
    text = "\n".join(lines).strip()
    logger.info("PDF 版面还原: %s -> %d 字符", Path(file_path).name, len(text))
    return text
