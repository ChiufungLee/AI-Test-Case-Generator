"""回归：长文本列在 MySQL 上必须是 MEDIUMTEXT。

背景：MySQL 的 TEXT 上限是 65535 **字节**，utf8mb4 下约 2.1 万中文字即溢出；
而业务上限按**字符**计（接口文档 200 万字符、需求 5 万字符、消息 3.2 万字符）。
严格模式（STRICT_TRANS_TABLES）下超限直接 `1406 Data too long`（实测 MySQL 8.0.45），
表现为 500 / 任务 failed。

测试环境是 SQLite（TEXT 无长度限制），这类缺陷在既有用例里完全不可见，因此这里不断言
数据能否写入，而是断言"列类型在 MySQL 方言下的编译结果"，让 SQLite 环境也能守住这道线。
"""

from sqlalchemy.dialects import mysql, sqlite

import models.api_test_models  # noqa: F401  确保全部模型注册到 Base.metadata
import models.chat  # noqa: F401
import models.knowledge_models  # noqa: F401
import models.test_asset_models  # noqa: F401
import models.user  # noqa: F401
import models.workflow_models  # noqa: F401
from models.database import Base, _MEDIUMTEXT_UPGRADES


def _compiled_type(column, dialect) -> str:
    return str(column.type.compile(dialect=dialect)).upper()


def test_long_text_columns_compile_to_mediumtext_on_mysql():
    """清单里的每一列：MySQL 下编译为 MEDIUMTEXT，SQLite 下仍是 TEXT（测试库行为不变）"""
    mysql_dialect = mysql.dialect()
    sqlite_dialect = sqlite.dialect()

    for table_name, column_names in _MEDIUMTEXT_UPGRADES.items():
        table = Base.metadata.tables[table_name]
        for column_name in column_names:
            column = table.c[column_name]
            assert _compiled_type(column, mysql_dialect) == "MEDIUMTEXT", (
                f"{table_name}.{column_name} 在 MySQL 下必须是 MEDIUMTEXT"
            )
            assert _compiled_type(column, sqlite_dialect) == "TEXT", (
                f"{table_name}.{column_name} 在 SQLite 下应保持 TEXT"
            )


def test_mediumtext_upgrade_covers_every_long_text_column():
    """反向校验：metadata 里所有编译为 MEDIUMTEXT 的列都必须在存量升级清单中。

    新增长文本列却忘记同步 `models/database.py::_MEDIUMTEXT_UPGRADES` 时，
    老库不会被迁移（新库由 create_all 建对、老库仍是 TEXT）→ 本用例失败。
    """
    mysql_dialect = mysql.dialect()
    expected = {(table, column) for table, columns in _MEDIUMTEXT_UPGRADES.items() for column in columns}
    actual = {
        (table.name, column.name)
        for table in Base.metadata.tables.values()
        for column in table.columns
        if _compiled_type(column, mysql_dialect) == "MEDIUMTEXT"
    }

    missing = sorted(actual - expected)
    extra = sorted(expected - actual)
    assert actual == expected, f"未纳入迁移清单: {missing}；清单里的非 MEDIUMTEXT 列: {extra}"


def test_migration_alter_statements_keep_definition_intact():
    """迁移用 `MODIFY` 只重述类型与可空性，会丢弃列上未重述的 DB 级 DEFAULT / COMMENT。

    这里固化"这些列没有 DB 级默认值/注释"的前提：将来有人加了 DEFAULT，本用例失败，
    提醒同步更新迁移语句（而不是让迁移把默认值悄悄抹掉）。
    """
    for table_name, column_names in _MEDIUMTEXT_UPGRADES.items():
        table = Base.metadata.tables[table_name]
        for column_name in column_names:
            column = table.c[column_name]
            assert column.server_default is None, f"{table_name}.{column_name} 新增了 DB 级默认值，需同步迁移语句"
            assert not column.comment, f"{table_name}.{column_name} 新增了列注释，需同步迁移语句"
