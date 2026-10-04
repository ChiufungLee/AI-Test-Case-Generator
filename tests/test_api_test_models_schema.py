"""test_runs/api_specs/api_endpoints 列迁移回归：后补列（幂等 ALTER + 回填）。"""
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


def test_schema_update_adds_source_url_and_media_type(db_session):
    """api_specs.source_url 与 api_endpoints.request_body_media_type 后补列"""
    db_session.execute(text("ALTER TABLE api_specs DROP COLUMN source_url"))
    db_session.execute(text("ALTER TABLE api_endpoints DROP COLUMN request_body_media_type"))
    db_session.commit()

    init_db()
    _ensure_schema_updates(get_engine())

    inspector = inspect(get_engine())
    assert "source_url" in {c["name"] for c in inspector.get_columns("api_specs")}
    assert "request_body_media_type" in {c["name"] for c in inspector.get_columns("api_endpoints")}


def test_schema_update_backfills_media_type_for_legacy_rows(
    db_session, make_user, make_api_spec, make_api_endpoint
):
    """存量行均为 JSON-only 解析产物：有请求体的回填 application/json，无请求体保持空串"""
    owner = make_user("legacy_owner", "secret123")
    spec = make_api_spec(owner.id, name="legacy spec")
    with_body = make_api_endpoint(spec.id, method="post", path="/users", request_body_json='{"type": "object"}')
    without_body = make_api_endpoint(spec.id, method="get", path="/ping")

    db_session.execute(text("ALTER TABLE api_endpoints DROP COLUMN request_body_media_type"))
    db_session.commit()
    db_session.execute(text(
        "INSERT INTO api_endpoints (id, spec_id, method, path, operation_id, summary, parameters_json, request_body_json, responses_json) "
        "VALUES ('legacy-ep', :spec_id, 'post', '/legacy', '', '', '[]', '{\"type\": \"object\"}', '{}')"
    ), {"spec_id": spec.id})
    db_session.commit()

    init_db()
    _ensure_schema_updates(get_engine())

    def _media(endpoint_id):
        return db_session.execute(
            text("SELECT request_body_media_type FROM api_endpoints WHERE id = :id"), {"id": endpoint_id}
        ).scalar()

    assert _media(with_body.id) == "application/json"  # 模型 default 在重跑迁移前已写入
    assert _media(without_body.id) == ""
    assert _media("legacy-ep") == "application/json"  # 存量行幂等回填


def test_schema_update_adds_auth_config_and_run_error(db_session, make_user, make_api_spec):
    """api_specs.auth_config_json 与 test_runs.error 后补列（D-025）；存量行 NULL 回填空串。

    MySQL 的 TEXT 列不允许 DEFAULT（错误 1101），因此以 NULL 加列后回填 ''。
    """
    owner = make_user("legacy_owner", "secret123")
    spec = make_api_spec(owner.id, name="legacy spec")

    db_session.execute(text("ALTER TABLE api_specs DROP COLUMN auth_config_json"))
    db_session.execute(text("ALTER TABLE test_runs DROP COLUMN error"))
    db_session.commit()
    db_session.execute(text(
        "INSERT INTO api_specs (id, owner_user_id, name, format, content, visibility, created_at, updated_at) "
        "VALUES ('legacy-spec', :owner, 'legacy', 'yaml', 'paths: {}', 'private', CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)"
    ), {"owner": owner.id})
    db_session.commit()

    init_db()
    _ensure_schema_updates(get_engine())

    inspector = inspect(get_engine())
    assert "auth_config_json" in {c["name"] for c in inspector.get_columns("api_specs")}
    assert "error" in {c["name"] for c in inspector.get_columns("test_runs")}
    value = db_session.execute(
        text("SELECT auth_config_json FROM api_specs WHERE id = 'legacy-spec'")
    ).scalar()
    assert value == ""


def test_schema_update_adds_description_column(db_session, make_user, make_api_spec):
    """api_specs.description 后补列（TEXT NULL + 回填空串，MySQL TEXT 不允许 DEFAULT）"""
    owner = make_user("legacy_owner", "secret123")
    spec = make_api_spec(owner.id, name="legacy spec")

    db_session.execute(text("ALTER TABLE api_specs DROP COLUMN description"))
    db_session.commit()
    db_session.execute(text(
        "INSERT INTO api_specs (id, owner_user_id, name, format, content, visibility, auth_config_json, created_at, updated_at) "
        "VALUES ('legacy-desc-spec', :owner, 'legacy', 'yaml', 'paths: {}', 'private', '', CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)"
    ), {"owner": owner.id})
    db_session.commit()

    init_db()
    _ensure_schema_updates(get_engine())

    assert "description" in {c["name"] for c in inspect(get_engine()).get_columns("api_specs")}
    value = db_session.execute(
        text("SELECT description FROM api_specs WHERE id = 'legacy-desc-spec'")
    ).scalar()
    assert value == ""
