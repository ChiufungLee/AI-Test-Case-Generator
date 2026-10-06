"""响应体断言求值器（D-027）：op 语义、点路径、非 JSON 响应、失败摘要"""

import json

from services import api_assertions


def test_eq_passes_and_fails_on_value():
    spec = [{"target": "code", "op": "eq", "expected": 0}]

    assert api_assertions.evaluate_assertions(spec, '{"code": 0, "msg": "ok"}')[0]["passed"] is True
    failed = api_assertions.evaluate_assertions(spec, '{"code": -1}')[0]
    assert failed["passed"] is False
    assert failed["actual"] == "-1"  # 入库前紧凑化为字符串


def test_eq_bool_not_equal_to_integer():
    """JSON 语义下 true 不等于 1（防 Python 隐式相等）"""
    assert api_assertions.evaluate_assertions([{"target": "flag", "op": "eq", "expected": 1}], '{"flag": true}')[0]["passed"] is False
    assert api_assertions.evaluate_assertions([{"target": "flag", "op": "eq", "expected": True}], '{"flag": 1}')[0]["passed"] is False
    assert api_assertions.evaluate_assertions([{"target": "flag", "op": "eq", "expected": True}], '{"flag": true}')[0]["passed"] is True


def test_exists_checks_path_reachability():
    spec = [{"target": "data.user", "op": "exists", "expected": None}]

    assert api_assertions.evaluate_assertions(spec, '{"data": {"user": null}}')[0]["passed"] is True  # null 视为存在
    missing = api_assertions.evaluate_assertions(spec, '{"data": {}}')[0]
    assert missing["passed"] is False
    assert missing["message"] == "字段不存在"


def test_dotted_path_supports_array_index():
    spec = [{"target": "data.items.1.id", "op": "eq", "expected": 2}]

    assert api_assertions.evaluate_assertions(spec, '{"data": {"items": [{"id": 1}, {"id": 2}]}}')[0]["passed"] is True
    assert api_assertions.evaluate_assertions(spec, '{"data": {"items": [{"id": 1}]}}')[0]["passed"] is False


def test_type_passes_and_distinguishes():
    cases = [
        ('{"n": 3}', "integer", True),
        ('{"n": 3.0}', "integer", True),  # JSON 数字无整型/浮点之分
        ('{"n": 3.5}', "integer", False),
        ('{"n": 3}', "number", True),  # integer 满足 number
        ('{"n": 3.5}', "number", True),
        ('{"n": "x"}', "string", True),
        ('{"n": []}', "array", True),
        ('{"n": {}}', "object", True),
        ('{"n": true}', "boolean", True),
        ('{"n": null}', "null", True),
        ('{"n": true}', "integer", False),  # bool 不是 integer
    ]
    for body, expected_type, passed in cases:
        result = api_assertions.evaluate_assertions(
            [{"target": "n", "op": "type", "expected": expected_type}], body
        )[0]
        assert result["passed"] is passed, (body, expected_type)
        if not passed:
            assert result["message"] == f"实际类型 {api_assertions._json_type(json.loads(body)['n'])}"


def test_non_json_body_fails_all_assertions():
    spec = [
        {"target": "code", "op": "eq", "expected": 0},
        {"target": "data", "op": "exists", "expected": None},
    ]

    results = api_assertions.evaluate_assertions(spec, "<html>boom</html>")

    assert all(r["passed"] is False for r in results)
    assert results[0]["message"] == "响应体不是合法 JSON"


def test_empty_spec_returns_empty():
    assert api_assertions.evaluate_assertions([], '{"a": 1}') == []


def test_summarize_failures_composes_and_caps():
    spec = [
        {"target": "code", "op": "eq", "expected": 0},
        {"target": "data.id", "op": "exists", "expected": None},
        {"target": "data.name", "op": "type", "expected": "string"},
        {"target": "data.age", "op": "eq", "expected": 1},
    ]
    results = api_assertions.evaluate_assertions(spec, '{"code": -1, "data": {"name": 3, "age": 2}}')

    summary = api_assertions.summarize_failures(results)

    assert "code 期望 0，实际 -1" in summary
    assert "data.id 字段不存在" in summary
    assert "data.name 实际类型 integer" in summary
    assert "（另 1 项断言失败）" in summary

    ok_results = api_assertions.evaluate_assertions(spec[:1], '{"code": 0}')
    assert api_assertions.summarize_failures(ok_results) is None
