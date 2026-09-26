# 辅助函数：从Markdown中提取表格
import csv
import io


def extract_table_from_markdown(text: str) -> list:
    """
    从Markdown文本中提取表格数据
    
    返回:
        list: 二维列表，包含表格行和列数据
    """
    table_data = []
    in_table = False
    
    for line in text.split('\n'):
        line = line.strip()
        # 检测表格开始
        if line.startswith('|') and ('---' in line or '--' in line):
            in_table = True
            continue
        
        # 处理表格行
        if in_table and line.startswith('|'):
            # 移除首尾的|并分割单元格
            cells = [cell.strip() for cell in line.split('|')[1:-1]]
            table_data.append(cells)
        elif in_table and not line.startswith('|'):
            # 表格结束
            break
    
    return table_data

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