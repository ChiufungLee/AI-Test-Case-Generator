"""响应体断言求值（D-027）：纯函数、零 LLM，执行时对完整响应体逐项求值。

断言规格（ApiEndpointCase.assertions_json）：[{"target": "点路径", "op": "eq|exists|type", "expected": 值}]
评估结果（TestRunResult.assertions_json）：[{"target", "op", "expected", "actual", "passed", "message"?}]

op 语义：
- eq：值相等（bool 性必须一致，防 Python 的 True == 1 隐式相等）
- exists：点路径可达（段为字典键或数组下标，如 data.items.0.id；null 值视为存在）
- type：JSON 类型核对（object/array/string/number/integer/boolean/null；integer 满足 number）
"""

import json

_MAX_VALUE_CHARS = 200
_MISSING = object()  # 点路径不可达哨兵（区别于合法的 null 值）
_NOT_JSON = object()  # 响应体非合法 JSON 哨兵


def evaluate_assertions(assertions: list[dict], body_text: str | None) -> list[dict]:
    """对响应体逐项求值断言，返回与规格等长的结果列表（含 expected/actual 的紧凑快照）。"""
    if not assertions:
        return []
    try:
        body = json.loads(body_text) if body_text else None
    except (TypeError, ValueError):
        body = _NOT_JSON

    results = []
    for assertion in assertions:
        target = str(assertion.get("target") or "")
        op = str(assertion.get("op") or "")
        expected = assertion.get("expected")
        if body is _NOT_JSON:
            results.append(_result(target, op, expected, None, False, "响应体不是合法 JSON"))
            continue

        value = _resolve_path(body, target)
        if value is _MISSING:
            results.append(_result(target, op, expected, None, False, "字段不存在"))
        elif op == "exists":
            results.append(_result(target, op, expected, None, True))
        elif op == "type":
            actual_type = _json_type(value)
            passed = actual_type == expected or (expected == "number" and actual_type == "integer")
            results.append(
                _result(target, op, expected, actual_type, passed, None if passed else f"实际类型 {actual_type}")
            )
        else:  # eq
            passed = _values_equal(expected, value)
            results.append(_result(target, op, expected, value, passed))
    return results


def summarize_failures(results: list[dict], limit: int = 3) -> str | None:
    """把失败的断言结果摘要成一行文案；全部通过返回 None。超出 limit 折叠计数。"""
    failed = [r for r in results if not r.get("passed")]
    if not failed:
        return None
    parts = [f"{r['target']} {r['message']}" if r.get("message") else f"{r['target']} 期望 {_compact(r['expected'])}，实际 {_compact(r['actual'])}" for r in failed[:limit]]
    if len(failed) > limit:
        parts.append(f"（另 {len(failed) - limit} 项断言失败）")
    return "；".join(parts)


def _result(target: str, op: str, expected, actual, passed: bool, message: str | None = None) -> dict:
    item = {
        "target": target,
        "op": op,
        "expected": _compact(expected),
        "actual": _compact(actual),
        "passed": passed,
    }
    if message:
        item["message"] = message
    return item


def _resolve_path(node, target: str):
    """点路径取值：段为字典键或数组下标；中途缺失返回哨兵（null 值视为存在）"""
    if not target:
        return _MISSING
    for part in target.split("."):
        if isinstance(node, dict) and part in node:
            node = node[part]
        elif isinstance(node, list) and part.isdigit() and int(part) < len(node):
            node = node[int(part)]
        else:
            return _MISSING
    return node


def _values_equal(expected, actual) -> bool:
    # bool 性必须一致：JSON 语义下 true 不等于 1
    if isinstance(expected, bool) != isinstance(actual, bool):
        return False
    return expected == actual


def _json_type(value) -> str:
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, int):
        return "integer"
    if isinstance(value, float):
        # JSON 数字无整型/浮点之分，3.0 与 3 同为整数值
        return "integer" if value.is_integer() else "number"
    if isinstance(value, str):
        return "string"
    if isinstance(value, list):
        return "array"
    if isinstance(value, dict):
        return "object"
    return "null"


def _compact(value):
    """期望/实际值入库前的紧凑化：JSON 序列化并截断，防止大对象膨胀结果行"""
    if value is None:
        return None
    try:
        text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
    except (TypeError, ValueError):
        text = str(value)
    return text if len(text) <= _MAX_VALUE_CHARS else text[:_MAX_VALUE_CHARS] + "…"
