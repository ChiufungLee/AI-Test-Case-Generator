# 辅助函数：从Markdown中提取表格
import csv
import io
import re


# 分隔行单元格的严格匹配：只允许可选对齐冒号 + 连字符 + 可选对齐冒号（如 ---、:---:、---:），
# 避免把含 "--" 的数据行（如"输入 -- 空"）误判为分隔行
_SEPARATOR_CELL_RE = re.compile(r"^\s*:?-+:?\s*$")


def _split_table_row(line: str) -> list:
    """按未转义的 | 切分表格行，去掉首尾管道产生的空单元格，并还原转义的 \\|"""
    cells = re.split(r"(?<!\\)\|", line)
    if cells and cells[0].strip() == "":
        cells = cells[1:]
    if cells and cells[-1].strip() == "":
        cells = cells[:-1]
    return [cell.strip().replace("\\|", "|") for cell in cells]


def _is_separator_row(cells: list) -> bool:
    return bool(cells) and all(_SEPARATOR_CELL_RE.match(cell) for cell in cells)


def _is_same_row(a: list, b: list) -> bool:
    return [c.strip() for c in a] == [c.strip() for c in b]


def extract_table_from_markdown(text: str) -> list:
    """
    从Markdown文本中提取表格数据

    - 表头行（分隔行之前的一行）一并返回，不会被丢掉；
    - 支持多张表：按出现顺序合并为一个二维列表，后续表头与第一张相同时跳过，
      不同时作为普通数据行保留；
    - 单元格内转义的 \\| 不会被视为列分隔符。

    返回:
        list: 二维列表，首行为表头，其余为数据行
    """
    lines = text.split("\n")
    tables = []

    i = 0
    while i < len(lines):
        line = lines[i].strip()
        # 表格 = 表头行 + 分隔行 + 若干数据行；分隔行必须紧跟表头行
        if line.startswith("|") and i + 1 < len(lines) and lines[i + 1].strip().startswith("|"):
            header_cells = _split_table_row(line)
            separator_cells = _split_table_row(lines[i + 1].strip())
            if header_cells and _is_separator_row(separator_cells):
                rows = [header_cells]
                i += 2
                while i < len(lines):
                    row_line = lines[i].strip()
                    if not row_line.startswith("|"):
                        break
                    rows.append(_split_table_row(row_line))
                    i += 1
                tables.append(rows)
                continue
        i += 1

    if not tables:
        return []

    first_header = tables[0][0]
    merged = list(tables[0])
    for rows in tables[1:]:
        data_rows = rows[1:] if _is_same_row(rows[0], first_header) else rows
        merged.extend(data_rows)

    return merged

# 以这些字符开头的单元格在 Excel 中会被当公式执行（CSV 公式注入），导出时加 ' 前缀
CSV_FORMULA_PREFIXES = ("=", "+", "-", "@", "\t", "\r")


def _sanitize_csv_cell(cell) -> str:
    text = str(cell)
    if text.startswith(CSV_FORMULA_PREFIXES):
        return "'" + text
    return text


# 辅助函数：将表格数据转换为CSV
def convert_table_to_csv(table_data: list) -> str:
    """
    将表格数据转换为CSV格式字符串

    参数:
        table_data: 二维表格数据

    返回:
        str: CSV格式的字符串
    """
    if not table_data:
        return ""

    # 创建CSV内容
    output = io.StringIO()
    writer = csv.writer(output)

    # 写入表头
    writer.writerow([_sanitize_csv_cell(cell) for cell in table_data[0]])

    # 写入数据行
    for row in table_data[1:]:
        writer.writerow([_sanitize_csv_cell(cell) for cell in row])

    return output.getvalue()
