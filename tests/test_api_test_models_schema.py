"""test_runs.endpoints_json 列迁移回归：后补列（TEXT NULL），存量行 NULL 需回填 '[]'。"""
from sqlalchemy import inspect, text

from models.database import _ensure_schema_updates, get_engine, init_db


def test_schema_update_adds_endpoints_json_column(db_session):
    """模拟旧版库：删列后重跑迁移 → 列补回"""
    db_session.execute(text("ALTER TABLE test_runs DROP COLUMN endpoints_json"))
    db_session.commit()

    init_db()
    _ensure_schema_updates(get_engine())

    columns = {c["name"] for c in inspect(get_engine()).get_columns("test_runs")}
    assert "endpoints_json" in columns


def test_schema_update_backfills_null_endpoints_json(
    db_session, make_user, make_api_spec, make_api_endpoint, make_api_endpoint_case
):
    """模拟旧版库：删列后以不含该列的 SQL 插入存量行 → 迁移补列并回填 '[]'"""
    owner = make_user("legacy_owner", "secret123")
    spec = make_api_spec(owner.id, name="legacy spec")
    endpoint = make_api_endpoint(spec.id)
    make_api_endpoint_case(endpoint.id, name="正常请求", request_json="{}", expected_status=200)

    db_session.execute(text("ALTER TABLE test_runs DROP COLUMN endpoints_json"))
    db_session.commit()
    db_session.execute(text(
        "INSERT INTO test_runs (id, spec_id, base_url, status, total, passed, failed, errored, created_by, created_at) "
        "VALUES ('legacy-run', :spec_id, 'http://legacy.example', 'completed', 1, 1, 0, 0, :owner, CURRENT_TIMESTAMP)"
    ), {"spec_id": spec.id, "owner": owner.id})
    db_session.commit()

    init_db()
    _ensure_schema_updates(get_engine())

    value = db_session.execute(
        text("SELECT endpoints_json FROM test_runs WHERE id = 'legacy-run'")
    ).scalar()
    assert value == "[]"
