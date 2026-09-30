from utils.data_handle import convert_table_to_csv, extract_table_from_markdown


SAMPLE_MULTI_TABLE = """# 测试用例

## 模块A：登录

| 用例编号 | 用例标题 | 输入 | 预期结果 |
| --- | --- | --- | --- |
| TC-001 | 正确登录 | 用户名+密码 | 登录成功 |
| TC-002 | 密码错误 | 输入 -- 空 | 提示错误 |

## 模块B：注销

| 用例编号 | 用例标题 | 输入 | 预期结果 |
| --- | --- | --- | --- |
| TC-003 | 退出登录 | 点击退出 | 返回首页 |
"""


def test_extract_keeps_header_row():
    """表头行（分隔行之前）不被丢掉"""
    text = "| 用例编号 | 用例标题 |\n| --- | --- |\n| TC-001 | 登录 |"

    table = extract_table_from_markdown(text)

    assert table[0] == ["用例编号", "用例标题"]
    assert table[1] == ["TC-001", "登录"]


def test_extract_data_row_containing_dashes_is_kept():
    """含 -- 的数据行（如“输入 -- 空”）不再被误判为分隔行"""
    text = "| 用例编号 | 输入 | 预期结果 |\n| --- | --- | --- |\n| TC-002 | 输入 -- 空 | 提示错误 |"

    table = extract_table_from_markdown(text)

    assert ["TC-002", "输入 -- 空", "提示错误"] in table


def test_extract_handles_escaped_pipe():
    """单元格内转义的 \\| 不被当作列分隔符"""
    text = "| 用例编号 | 输入 | 预期结果 |\n| --- | --- | --- |\n| TC-003 | a \\| b | 通过 |"

    table = extract_table_from_markdown(text)

    assert table[1] == ["TC-003", "a | b", "通过"]


def test_extract_merges_multiple_tables():
    """多张表按顺序合并，重复表头只保留第一份"""
    table = extract_table_from_markdown(SAMPLE_MULTI_TABLE)

    # 1 份表头 + 模块A 2 行 + 模块B 1 行
    assert table[0] == ["用例编号", "用例标题", "输入", "预期结果"]
    assert ["TC-001", "正确登录", "用户名+密码", "登录成功"] in table
    assert ["TC-002", "密码错误", "输入 -- 空", "提示错误"] in table
    assert ["TC-003", "退出登录", "点击退出", "返回首页"] in table
    headers = [row for row in table if row[0] == "用例编号"]
    assert len(headers) == 1


def test_extract_alignment_colons_in_separator():
    """分隔行带对齐冒号（:---:、---:）也能识别"""
    text = "| A | B |\n| :--- | ---: |\n| 1 | 2 |"

    table = extract_table_from_markdown(text)

    assert table == [["A", "B"], ["1", "2"]]


def test_extract_ignores_table_without_separator_row():
    """缺少分隔行的伪表格不导出（无法确定表头）"""
    text = "| A | B |\n| 1 | 2 |"

    assert extract_table_from_markdown(text) == []


def test_extract_ignores_text_outside_tables():
    text = "前置说明\n\n| A | B |\n| --- | --- |\n| 1 | 2 |\n\n结尾说明"

    assert extract_table_from_markdown(text) == [["A", "B"], ["1", "2"]]


def test_extract_empty_and_no_table():
    assert extract_table_from_markdown("") == []
    assert extract_table_from_markdown("没有任何表格") == []


def test_convert_table_to_csv_basic():
    csv_text = convert_table_to_csv([["用例编号", "预期结果"], ["TC-001", "登录成功"]])

    assert csv_text == "用例编号,预期结果\r\nTC-001,登录成功\r\n"


def test_convert_table_to_csv_empty():
    assert convert_table_to_csv([]) == ""
