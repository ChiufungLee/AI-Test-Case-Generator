"""API 用例规则引擎：从接口参数与请求体 Schema 确定性生成用例提案（零 LLM，D-020）。

设计原则（对齐 D-002 确定性优先）：schema 能推导的全部用代码生成——正常样例、
缺失必填、类型错误、越界、非法枚举、违反 pattern；LLM 只负责业务语义维度的补充。

提案行结构：{"name", "request": {"path", "query", "body", "headers"}, "expected_status", "source_type"}
每条提案的 request 均为深拷贝，异常变异互不污染。
组合 schema（allOf/oneOf/anyOf）先经 _normalize_schema 归一为普通对象 schema 再采样（D-024）。
"""

import copy

# 每个异常维度最多生成的用例数（避免大 schema 产生用例爆炸）
_MAX_PER_DIMENSION = 3
_MAX_NORMALIZE_DEPTH = 12

_TYPE_ERRORS = {
    "string": 12345,
    "integer": "不是数字",
    "number": "不是数字",
    "boolean": "yes",
    "array": "不是数组",
    "object": "不是对象",
}

_PATTERN_VIOLATION = "___pattern_violation___"
_ENUM_VIOLATION = "___invalid_enum___"


def generate_case_proposals(endpoint: dict) -> list[dict]:
    """从接口快照（parse 产出的 dict：method/path/parameters/request_body/responses）生成用例提案。

    正常样例的 expected_status 取声明响应里的首个 2xx（按码值升序），无声明则 200；
    异常样例预期 400（服务端实际可能返回 422，可在 UI 中调整后保存）。
    """
    method = endpoint.get("method", "get")
    parameters = [p for p in endpoint.get("parameters") or [] if isinstance(p, dict)]
    raw_body = endpoint.get("request_body") if isinstance(endpoint.get("request_body"), dict) else {}
    body_schema = _normalize_schema(raw_body)
    expected_ok = _first_success_status(endpoint.get("responses") or {})

    path_params = {p["name"]: p for p in parameters if p.get("in") == "path" and p.get("name")}
    query_params = [p for p in parameters if p.get("in") == "query" and p.get("name")]
    header_params = [p for p in parameters if p.get("in") == "header" and p.get("name")]

    normal_path = {name: _sample_value(schema, name) for name, schema in path_params.items()}
    normal_query = {p["name"]: _sample_value(p.get("schema") or {}, p["name"]) for p in query_params}
    normal_headers = {p["name"]: _sample_value(p.get("schema") or {}, p["name"]) for p in header_params}
    normal_body = _sample_object(body_schema) if method in ("post", "put", "patch") else {}

    def _request(query: dict | None = None, body: dict | None = None) -> dict:
        # 深拷贝：异常提案的字段变异不得污染正常样基线
        request = {
            "path": dict(normal_path),
            "query": copy.deepcopy(normal_query) if query is None else query,
            "headers": copy.deepcopy(normal_headers),
        }
        if method in ("post", "put", "patch"):
            request["body"] = copy.deepcopy(normal_body) if body is None else body
        return request

    proposals = [{
        "name": "正常请求",
        "request": _request(),
        "expected_status": expected_ok,
        "source_type": "rule_engine",
    }]

    # 缺失必填：body 缺全部 required 字段 + query 缺全部 required 参数（各一条）
    required_body = [k for k in (body_schema.get("required") or []) if k in normal_body]
    required_query = [p["name"] for p in query_params if p.get("required")]
    if required_body:
        partial_body = {k: v for k, v in normal_body.items() if k not in required_body}
        proposals.append({
            "name": "缺失必填字段 - 400",
            "request": _request(body=partial_body),
            "expected_status": 400,
            "source_type": "rule_engine",
        })
    if required_query:
        missing_query = {k: v for k, v in normal_query.items() if k not in required_query}
        proposals.append({
            "name": "缺失必填查询参数 - 400",
            "request": _request(query=missing_query),
            "expected_status": 400,
            "source_type": "rule_engine",
        })

    # 字段级异常：类型错误 / 越界 / 非法枚举 / 违反 pattern（body 与 query 各自扫描，逐维限量，目标容器显式标记）
    typed_fields = _typed_fields(body_schema.get("properties") or {}, normal_body, target="body")
    typed_fields += _typed_fields(
        {p["name"]: (p.get("schema") or {}) for p in query_params},
        normal_query,
        prefix="查询参数 ",
        target="query",
    )
    proposals.extend(_field_anomalies(typed_fields, _request))

    return proposals


def _normalize_schema(schema: dict, depth: int = 0) -> dict:
    """组合 schema 归一（D-024）：allOf 深合并、oneOf/anyOf 取首支，并递归归一 properties/items。

    解析层已解引用 $ref，这里补齐采样器读不懂的组合形态——否则 allOf/oneOf 请求体
    会采样成空对象，正常请求被服务端 422。超深度防御性返回 {}。
    """
    if not isinstance(schema, dict) or depth > _MAX_NORMALIZE_DEPTH:
        return schema if isinstance(schema, dict) else {}

    if "allOf" in schema:
        merged = {k: v for k, v in schema.items() if k != "allOf"}
        for sub in schema["allOf"]:
            sub = _normalize_schema(sub, depth + 1)
            for key, value in sub.items():
                if key == "required":
                    merged["required"] = sorted(set(merged.get("required") or []) | set(value))
                elif key == "properties":
                    props = dict(merged.get("properties") or {})
                    for name, sub_schema in value.items():
                        if isinstance(props.get(name), dict) and isinstance(sub_schema, dict):
                            props[name] = _normalize_schema({"allOf": [props[name], sub_schema]}, depth + 1)
                        else:
                            props[name] = sub_schema
                    merged["properties"] = props
                elif key not in merged:
                    merged[key] = value
        merged.pop("allOf", None)
        return _normalize_schema(merged, depth + 1)

    if "oneOf" in schema or "anyOf" in schema:
        branches = schema.get("oneOf") or schema.get("anyOf") or []
        first = next((b for b in branches if isinstance(b, dict)), None)
        base = {k: v for k, v in schema.items() if k not in ("oneOf", "anyOf")}
        if first is None:
            return base
        normalized = _normalize_schema(first, depth + 1)
        for key, value in base.items():
            normalized.setdefault(key, value)
        return normalized

    normalized = dict(schema)
    if isinstance(normalized.get("properties"), dict):
        normalized["properties"] = {
            name: _normalize_schema(sub, depth + 1) if isinstance(sub, dict) else sub
            for name, sub in normalized["properties"].items()
        }
    if isinstance(normalized.get("items"), dict):
        normalized["items"] = _normalize_schema(normalized["items"], depth + 1)
    return normalized


def _typed_fields(properties: dict, samples: dict, prefix: str = "", target: str = "body") -> list[tuple[str, dict, object, str]]:
    """收集字段级异常扫描所需的 (展示名, schema, 正常样例值, 目标容器) 列表"""
    fields = []
    for name, schema in properties.items():
        if not isinstance(schema, dict) or name not in samples:
            continue
        fields.append((f"{prefix}{name}", schema, samples[name], target))
    return fields


def _field_anomalies(fields: list[tuple[str, dict, object, str]], request_builder) -> list[dict]:
    """按 维度×字段 生成异常提案：类型错误、越界、非法枚举、违反 pattern，逐维限量"""
    proposals: list[dict] = []
    dimension_counts = {"type": 0, "range": 0, "enum": 0, "pattern": 0}

    for display_name, schema, sample, target in fields:
        props = schema.get("properties")
        # 对象字段下钻一层（取其必填属性做异常），数组字段跳过（items 异常交给元素类型）
        if isinstance(props, dict) and isinstance(sample, dict):
            nested = _typed_fields(props, sample, prefix=f"{display_name}.", target=target)
            proposals.extend(_field_anomalies(nested, request_builder))
            continue

        field_type = schema.get("type")
        if field_type in _TYPE_ERRORS and dimension_counts["type"] < _MAX_PER_DIMENSION:
            wrong = _TYPE_ERRORS[field_type]
            if not isinstance(wrong, type(sample)):
                proposals.append(_anomaly(f"类型错误 {display_name}", request_builder, display_name, wrong, "type", target))
                dimension_counts["type"] += 1

        if dimension_counts["range"] < _MAX_PER_DIMENSION:
            out_of_range = _out_of_range_value(schema)
            if out_of_range is not None:
                direction = "超出上限" if out_of_range[1] == "max" else "低于下限"
                proposals.append(_anomaly(f"{direction} {display_name}", request_builder, display_name, out_of_range[0], "range", target))
                dimension_counts["range"] += 1

        if "enum" in schema and dimension_counts["enum"] < _MAX_PER_DIMENSION:
            proposals.append(_anomaly(f"非法枚举 {display_name}", request_builder, display_name, _ENUM_VIOLATION, "enum", target))
            dimension_counts["enum"] += 1

        if "pattern" in schema and dimension_counts["pattern"] < _MAX_PER_DIMENSION:
            proposals.append(_anomaly(f"违反格式 {display_name}", request_builder, display_name, _PATTERN_VIOLATION, "pattern", target))
            dimension_counts["pattern"] += 1

    return proposals


def _anomaly(name: str, request_builder, field: str, value, dimension: str, target: str) -> dict:
    """构造单条异常提案：复制正常请求，把目标容器（body/query）中的字段值替换为异常值"""
    request = request_builder()
    parts = field.split(".")
    container = request.get(target) or {}
    for part in parts[:-1]:
        container = container.setdefault(part, {})
    container[parts[-1]] = value
    if not request.get(target):
        request[target] = container
    return {
        "name": f"{name} - 400",
        "request": request,
        "expected_status": 400,
        "source_type": "rule_engine",
    }


def _out_of_range_value(schema: dict) -> tuple[object, str] | None:
    """按声明约束构造越界值：优先向上（maximum+步长），其次向下（minimum/exclusiveMinimum-步长）、字符串超长"""
    if schema.get("type") in ("integer", "number"):
        step = 1 if schema.get("type") == "integer" else 0.5
        maximum = schema.get("maximum")
        if isinstance(maximum, (int, float)):
            return maximum + step, "max"
        minimum = schema.get("minimum")
        if isinstance(minimum, (int, float)):
            return minimum - step, "min"
        exclusive_min = schema.get("exclusiveMinimum")
        if isinstance(exclusive_min, (int, float)):
            return exclusive_min - step, "min"
        return None
    if schema.get("type") == "string":
        max_length = schema.get("maxLength")
        if isinstance(max_length, int):
            return "x" * (max_length + 1), "max"
        min_length = schema.get("minLength")
        if isinstance(min_length, int) and min_length > 0:
            return "", "min"
    return None


def _first_success_status(responses: dict) -> int:
    codes = []
    for code in responses:
        try:
            codes.append(int(code))
        except (TypeError, ValueError):
            continue
    success = sorted(code for code in codes if 200 <= code < 300)
    return success[0] if success else 200


def _sample_value(schema: dict, field_name: str = ""):
    """按 JSON Schema 约束构造合法样例值（enum 首值 > format 启发 > 类型默认，并满足 min/max）"""
    if not isinstance(schema, dict):
        return "test"
    if "enum" in schema and schema["enum"]:
        return schema["enum"][0]
    value_type = schema.get("type", "string")

    if value_type == "string":
        value = _string_sample(schema.get("format"), field_name)
        min_length = schema.get("minLength")
        if isinstance(min_length, int) and len(value) < min_length:
            value = "x" * min_length
        max_length = schema.get("maxLength")
        if isinstance(max_length, int) and len(value) > max_length:
            value = value[:max_length]
        return value
    if value_type == "integer":
        value = schema.get("minimum")
        if value is None:
            exclusive = schema.get("exclusiveMinimum")
            value = exclusive + 1 if isinstance(exclusive, (int, float)) else 1
        if isinstance(value, float):
            value = int(value)
        maximum = schema.get("maximum")
        if isinstance(maximum, (int, float)) and value > maximum:
            value = int(maximum)
        return value
    if value_type == "number":
        value = schema.get("minimum")
        if value is None:
            exclusive = schema.get("exclusiveMinimum")
            value = exclusive + 1 if isinstance(exclusive, (int, float)) else 1.0
        maximum = schema.get("maximum")
        if isinstance(maximum, (int, float)) and value > maximum:
            value = float(maximum)
        return float(value)
    if value_type == "boolean":
        return True
    if value_type in ("array", "file"):
        if value_type == "file":
            # 2.0 formData 文件字段：JSON 里只存占位文件名，执行层替换为占位文件内容
            return "test-file.bin"
        item_schema = schema.get("items") if isinstance(schema.get("items"), dict) else {}
        min_items = schema.get("minItems") or 1
        return [_sample_value(item_schema, field_name) for _ in range(max(min_items, 1))]
    if value_type == "object":
        return _sample_object(schema)
    return "test"


def _string_sample(value_format: str | None, field_name: str) -> str:
    if value_format == "binary":
        # 3.0 二进制字段：JSON 里只存占位文件名，执行层替换为占位文件内容
        return "test-file.bin"
    if value_format == "email":
        return "tester@example.com"
    if value_format == "date-time":
        return "2026-01-01T00:00:00Z"
    if value_format == "date":
        return "2026-01-01"
    if value_format == "uuid":
        return "3fa85f64-5717-4562-b3fc-2c963f66afa6"
    if value_format == "uri":
        return "https://example.com/fixture"
    return f"test-{field_name}" if field_name else "test"


def _sample_object(schema: dict) -> dict:
    """对象采样：只填 required 属性（嵌套对象递归），与「正常样例最小合法」原则一致"""
    if not isinstance(schema, dict):
        return {}
    properties = schema.get("properties") or {}
    required = schema.get("required") or []
    target = required if required else list(properties.keys())[:1]
    return {
        name: _sample_value(properties.get(name) or {}, name)
        for name in target
        if isinstance(properties.get(name), dict)
    }
