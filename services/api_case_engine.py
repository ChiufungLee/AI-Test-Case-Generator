"""API 用例规则引擎：从接口参数与请求体 Schema 确定性生成用例提案（零 LLM，D-020）。

设计原则（对齐 D-002 确定性优先）：schema 能推导的全部用代码生成——正常样例、
缺失必填、类型错误、越界、非法枚举、违反 pattern；LLM 只负责业务语义维度的补充。

提案行结构：{"name", "request": {"path", "query", "body", "headers"}, "expected_status", "assertions", "source_type"}
每条提案的 request 均为深拷贝，异常变异互不污染。
组合 schema（allOf/oneOf/anyOf）先经 _normalize_schema 归一为普通对象 schema 再采样（D-024）。
正常样例按响应体 schema 顶层 required 字段派生 exists/type 断言（D-027）；异常样例断言为空。
"""

import copy
import re

# 每个异常维度最多生成的用例数（避免大 schema 产生用例爆炸）
_MAX_PER_DIMENSION = 3
_MAX_NORMALIZE_DEPTH = 12
# 合成值的硬上限：maxLength/minLength/minItems 完全由文档作者控制，没有上限时
# 20 字节的 `maxLength: 500000000` 就能生成 500MB 字符串（进程内存 + 落库体积双爆）
_MAX_SYNTHETIC_LENGTH = 4096
_MAX_SYNTHETIC_ITEMS = 64
# 字段级异常下钻的最大层数（对象嵌套 + 数组元素），防止深嵌套 schema 递归过深
_MAX_ANOMALY_DEPTH = 6
# 正常用例派生响应断言的上限（exists + type 成对计入）
_MAX_DERIVED_ASSERTIONS = 8
_ASSERTION_TYPES = ("object", "array", "string", "number", "integer", "boolean", "null")

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

# "违反 pattern"的候选值：逐个校验确实不匹配该 pattern 才使用（见 _pattern_violation_value）
_PATTERN_VIOLATIONS = (
    _PATTERN_VIOLATION,
    "!!!invalid!!!",
    "1234567890",
    " ",
)

# 参数对象上的元数据键：2.0 的非 body 参数把类型约束直接写在参数上，
# 当 schema 用时必须剔除这些键，避免 name/in/required 被当成取值约束读取
_PARAM_META_KEYS = frozenset({
    "name", "in", "description", "required", "deprecated", "allowEmptyValue",
    "collectionFormat", "style", "explode", "allowReserved", "example", "examples",
})


def _param_schema(param: dict) -> dict:
    """参数的有效取值 schema（D-024 的两种形态）。

    3.x 把约束放在 param["schema"]；2.0 的 path/query/header 参数直接写 type/format/enum。
    此前 path 传的是整个参数对象、2.0 传空 dict，导致 integer/enum/uuid 参数一律退化成
    "test-xxx" 字符串，正常用例必然被服务端 404/422 拒绝。
    """
    schema = param.get("schema")
    if isinstance(schema, dict):
        return schema
    return {key: value for key, value in param.items() if key not in _PARAM_META_KEYS}


def generate_case_proposals(endpoint: dict) -> list[dict]:
    """从接口快照（parse 产出的 dict：method/path/parameters/request_body/responses/response_schemas）生成用例提案。

    正常样例的 expected_status 取声明响应里的首个 2xx（按码值升序），无声明则 200；
    异常样例预期 400（服务端实际可能返回 422，可在 UI 中调整后保存）。
    """
    method = endpoint.get("method", "get")
    parameters = [p for p in endpoint.get("parameters") or [] if isinstance(p, dict)]
    raw_body = endpoint.get("request_body") if isinstance(endpoint.get("request_body"), dict) else {}
    body_schema = _normalize_schema(raw_body)
    expected_ok = _first_success_status(endpoint.get("responses") or {})
    response_schemas = endpoint.get("response_schemas") if isinstance(endpoint.get("response_schemas"), dict) else {}

    path_params = {p["name"]: p for p in parameters if p.get("in") == "path" and p.get("name")}
    query_params = [p for p in parameters if p.get("in") == "query" and p.get("name")]
    header_params = [p for p in parameters if p.get("in") == "header" and p.get("name")]

    normal_path = {name: _sample_value(_param_schema(param), name) for name, param in path_params.items()}
    normal_query = {p["name"]: _sample_value(_param_schema(p), p["name"]) for p in query_params}
    normal_headers = {p["name"]: _sample_value(_param_schema(p), p["name"]) for p in header_params}
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
        "assertions": _derive_body_assertions(_response_schema_for(response_schemas, expected_ok)),
        "source_type": "rule_engine",
    }]

    # 缺失必填：body 缺全部 required 字段 + query 缺全部 required 参数（各一条）
    required_body = [
        k for k in (body_schema.get("required") or []) if isinstance(k, str) and k in normal_body
    ]
    required_query = [p["name"] for p in query_params if p.get("required")]
    if required_body:
        partial_body = {k: v for k, v in normal_body.items() if k not in required_body}
        proposals.append({
            "name": "缺失必填字段 - 400",
            "request": _request(body=partial_body),
            "expected_status": 400,
            "assertions": [],
            "source_type": "rule_engine",
        })
    if required_query:
        missing_query = {k: v for k, v in normal_query.items() if k not in required_query}
        proposals.append({
            "name": "缺失必填查询参数 - 400",
            "request": _request(query=missing_query),
            "expected_status": 400,
            "assertions": [],
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


def _typed_fields(
    properties: dict,
    samples: dict,
    prefix: str = "",
    path: tuple = (),
    target: str = "body",
) -> list[tuple]:
    """收集字段级异常扫描所需的 (展示名, 字段路径, schema, 正常样例值, 目标容器)。

    展示名用于用例命名，可能与真实属性名不同（属性名本身可以含 "."），
    因此变异位置必须用独立的路径元组表达，不能靠展示名 split(".") 反推。
    """
    fields = []
    for name, schema in properties.items():
        if not isinstance(schema, dict) or name not in samples:
            continue
        fields.append((f"{prefix}{name}", path + (name,), schema, samples[name], target))
    return fields


def _field_anomalies(
    fields: list[tuple],
    request_builder,
    counts: dict | None = None,
    depth: int = 0,
) -> list[dict]:
    """按 维度×字段 生成异常提案：类型错误、越界、非法枚举、违反 pattern，逐维限量。

    counts 由递归共用：每层新建计数器会让"逐维限量"在下钻时反复放行（每层每维各 3 条）。
    depth 限制下钻层数，避免深嵌套 schema 递归过深。
    """
    proposals: list[dict] = []
    if counts is None:
        counts = {"type": 0, "range": 0, "enum": 0, "pattern": 0}
    if depth > _MAX_ANOMALY_DEPTH:
        return proposals

    def add(name: str, path: tuple, value, target: str, dimension: str) -> None:
        """构造并收集提案；路径不可达（样例形状与 schema 不符）时静默跳过"""
        proposal = _anomaly(name, request_builder, path, value, target)
        if proposal is None:
            return
        proposals.append(proposal)
        counts[dimension] += 1

    for display_name, path, schema, sample, target in fields:
        props = schema.get("properties")
        # 对象字段下钻一层（取其必填属性做异常）
        if isinstance(props, dict) and isinstance(sample, dict):
            nested = _typed_fields(props, sample, prefix=f"{display_name}.", path=path, target=target)
            proposals.extend(_field_anomalies(nested, request_builder, counts, depth + 1))
            continue

        # 数组元素：对首个元素做字段级异常（items 的约束此前完全没有被覆盖）
        items = schema.get("items")
        if isinstance(items, dict) and isinstance(sample, list) and sample:
            element = (f"{display_name}[0]", path + (0,), items, sample[0], target)
            proposals.extend(_field_anomalies([element], request_builder, counts, depth + 1))
            continue

        field_type = schema.get("type")
        if field_type in _TYPE_ERRORS and counts["type"] < _MAX_PER_DIMENSION:
            wrong = _TYPE_ERRORS[field_type]
            if not isinstance(wrong, type(sample)):
                add(f"类型错误 {display_name}", path, wrong, target, "type")

        if counts["range"] < _MAX_PER_DIMENSION:
            out_of_range = _out_of_range_value(schema)
            if out_of_range is not None:
                direction = "超出上限" if out_of_range[1] == "max" else "低于下限"
                add(f"{direction} {display_name}", path, out_of_range[0], target, "range")

        if "enum" in schema and counts["enum"] < _MAX_PER_DIMENSION:
            add(f"非法枚举 {display_name}", path, _ENUM_VIOLATION, target, "enum")

        if "pattern" in schema and counts["pattern"] < _MAX_PER_DIMENSION:
            violation = _pattern_violation_value(schema.get("pattern"))
            if violation is not None:
                add(f"违反格式 {display_name}", path, violation, target, "pattern")

    return proposals


def _pattern_violation_value(pattern) -> str | None:
    """构造一个确实不匹配 pattern 的字符串；构造不出（含无效正则）返回 None。

    固定值 "___pattern_violation___" 会完整匹配 "^[a-z_]+$" 这类 pattern，
    于是"违反格式"用例预期 400 而实际 200，稳定误报；这里逐个候选校验后再使用。
    候选值只有 20 余字符，即使 pattern 有灾难性回溯也不构成 ReDoS。
    """
    if not isinstance(pattern, str) or not pattern:
        return None
    for candidate in _PATTERN_VIOLATIONS:
        try:
            if re.search(pattern, candidate) is None:
                return candidate
        except re.error:
            return None  # 无效正则：无法判定，跳过该维度而不是生成假用例
    return None


def _anomaly(name: str, request_builder, path: tuple, value, target: str) -> dict | None:
    """构造单条异常提案：复制正常请求，把目标容器中 path 指向的位置替换为异常值。

    path 的元素可以是对象键（str）或数组下标（int），因此不能只按点号切分展示名。
    """
    request = request_builder()
    container = request.get(target)
    if not isinstance(container, (dict, list)):
        container = {}
        request[target] = container
    for part in path[:-1]:
        container = _child_container(container, part)
        if container is None:
            return None  # 路径不可达（样例形状与 schema 不符）：放弃这条提案
    last = path[-1]
    if isinstance(container, list):
        if not isinstance(last, int) or not 0 <= last < len(container):
            return None
        container[last] = value
    else:
        container[last] = value
    return {
        "name": f"{name} - 400",
        "request": request,
        "expected_status": 400,
        "assertions": [],
        "source_type": "rule_engine",
    }


def _child_container(container, part):
    """取（必要时补建）路径上的下一层容器；int 段按数组下标处理，不可达返回 None"""
    if isinstance(container, list):
        if not isinstance(part, int) or not 0 <= part < len(container):
            return None
        child = container[part]
        if isinstance(child, (dict, list)):
            return child
        child = {}
        container[part] = child
        return child
    if isinstance(container, dict):
        child = container.get(part)
        if isinstance(child, (dict, list)):
            return child
        child = [] if isinstance(part, int) else {}
        container[part] = child
        return child
    return None


def _derive_body_assertions(schema) -> list[dict]:
    """正常用例的响应断言派生（D-027）：按响应 schema 顶层 required 字段生成 exists + type 断言。

    只做存在性与类型核对——schema 推不出确定的响应值，值断言（eq）留给 AI 建议与手工编辑；
    无 schema 或无 required 返回空，断言总量不超过 _MAX_DERIVED_ASSERTIONS。
    """
    if not isinstance(schema, dict):
        return []
    properties = schema.get("properties") if isinstance(schema.get("properties"), dict) else {}
    required = [r for r in (schema.get("required") or []) if isinstance(r, str)]
    assertions: list[dict] = []
    for name in required:
        if len(assertions) >= _MAX_DERIVED_ASSERTIONS:
            break
        assertions.append({"target": name, "op": "exists", "expected": None})
        sub_type = properties.get(name, {}).get("type") if isinstance(properties.get(name), dict) else None
        if sub_type in _ASSERTION_TYPES and len(assertions) < _MAX_DERIVED_ASSERTIONS:
            assertions.append({"target": name, "op": "type", "expected": sub_type})
    return assertions


def _effective_type(schema: dict) -> str:
    """schema 的有效类型：type 缺失（或写成数组，含 null 联合）时按结构推断。

    手写文档里 {"properties": {...}} / {"minimum": 0} 这类省略 type 的写法很常见，
    直接按字符串采样会得到 "test-x"，正常用例必被服务端 422。
    """
    declared = schema.get("type")
    if isinstance(declared, str):
        return declared
    if isinstance(declared, list):
        for candidate in declared:
            if isinstance(candidate, str) and candidate != "null":
                return candidate
    if "properties" in schema or "required" in schema:
        return "object"
    if "items" in schema:
        return "array"
    if any(
        key in schema
        for key in ("minimum", "maximum", "exclusiveMinimum", "exclusiveMaximum", "multipleOf")
    ):
        return "number"
    return "string"


def _out_of_range_value(schema: dict) -> tuple[object, str] | None:
    """按声明约束构造越界值：优先向上（maximum/exclusiveMaximum），其次向下（minimum/exclusiveMinimum）、字符串超长"""
    value_type = _effective_type(schema)
    if value_type in ("integer", "number"):
        step = 1 if value_type == "integer" else 0.5
        maximum = _numeric_bound(schema.get("maximum"))
        if maximum is not None:
            return maximum + step, "max"
        exclusive_max = _numeric_bound(schema.get("exclusiveMaximum"))
        if exclusive_max is not None:
            return exclusive_max, "max"  # 开区间：等于上界即越界
        minimum = _numeric_bound(schema.get("minimum"))
        if minimum is not None:
            return minimum - step, "min"
        exclusive_min = _numeric_bound(schema.get("exclusiveMinimum"))
        if exclusive_min is not None:
            return exclusive_min - step, "min"
        return None
    if value_type == "string":
        max_length = schema.get("maxLength")
        # 上限过大时跳过该维度：合成值有硬上限，无法在合理体积内构造"超长"值
        if isinstance(max_length, int) and 0 <= max_length < _MAX_SYNTHETIC_LENGTH:
            return "x" * (max_length + 1), "max"
        min_length = schema.get("minLength")
        if isinstance(min_length, int) and min_length > 0:
            return "", "min"
    return None


def _first_success_status(responses: dict) -> int:
    codes = []
    for code in responses:
        text = str(code).strip().upper()
        if text.isdigit():
            codes.append(int(text))
        elif len(text) == 3 and text[0].isdigit() and text[1:] == "XX":
            # 区间码（如 2XX）：取该区间的下界参与"首个 2xx"计算
            codes.append(int(text[0]) * 100)
    success = sorted(code for code in codes if 200 <= code < 300)
    return success[0] if success else 200


def _response_schema_for(response_schemas: dict, status: int):
    """按状态码取响应 schema：先精确码，再退回区间码（文档只声明 2XX 时也要能派生断言）"""
    if not isinstance(response_schemas, dict):
        return None
    schema = response_schemas.get(str(status))
    if schema is None:
        schema = response_schemas.get(f"{status // 100}XX")
    return schema


def _numeric_bound(value):
    """数值型约束取值；draft-04 的布尔 exclusiveMinimum/exclusiveMaximum 不是数值，返回 None"""
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return value
    return None


def _sample_value(schema: dict, field_name: str = ""):
    """按 JSON Schema 约束构造合法样例值（enum 首值 > format 启发 > 类型默认，并满足 min/max）"""
    if not isinstance(schema, dict):
        return "test"
    if "enum" in schema and schema["enum"]:
        return schema["enum"][0]
    value_type = _effective_type(schema)

    if value_type == "string":
        value = _string_sample(schema.get("format"), field_name)
        min_length = schema.get("minLength")
        if isinstance(min_length, int) and len(value) < min_length:
            # 按上限截断，避免文档里的天文数字变成超大字符串
            value = "x" * min(min_length, _MAX_SYNTHETIC_LENGTH)
        max_length = schema.get("maxLength")
        if isinstance(max_length, int) and 0 <= max_length < len(value):
            value = value[:max_length]
        return value
    if value_type == "integer":
        return int(_sample_numeric(schema, integer=True))
    if value_type == "number":
        return float(_sample_numeric(schema, integer=False))
    if value_type == "boolean":
        return True
    if value_type in ("array", "file"):
        if value_type == "file":
            # 2.0 formData 文件字段：JSON 里只存占位文件名，执行层替换为占位文件内容
            return "test-file.bin"
        item_schema = schema.get("items") if isinstance(schema.get("items"), dict) else {}
        min_items = schema.get("minItems")
        count = min_items if isinstance(min_items, int) and min_items > 0 else 1
        return [
            _sample_value(item_schema, field_name)
            for _ in range(min(count, _MAX_SYNTHETIC_ITEMS))
        ]
    if value_type == "object":
        return _sample_object(schema)
    return "test"


def _sample_numeric(schema: dict, integer: bool):
    """数值采样：满足更严的下界（minimum / exclusiveMinimum），并夹进上界之内。

    开区间下界必须加步长（minimum=0 + exclusiveMinimum=0 时采样 0 违反 >0），
    上界含 exclusiveMaximum；draft-04 的布尔 exclusiveMinimum 由 _numeric_bound 排除。
    """
    step = 1 if integer else 0.5
    lower = _numeric_bound(schema.get("minimum"))
    exclusive_lower = _numeric_bound(schema.get("exclusiveMinimum"))
    if exclusive_lower is not None and (lower is None or exclusive_lower >= lower):
        lower = exclusive_lower + step
    value = lower if lower is not None else (1 if integer else 1.0)

    upper = _numeric_bound(schema.get("maximum"))
    exclusive_upper = _numeric_bound(schema.get("exclusiveMaximum"))
    if exclusive_upper is not None and (upper is None or exclusive_upper <= upper):
        upper = exclusive_upper - step
    if upper is not None and value > upper:
        value = upper
    return value


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
    """对象采样：只填 required 属性（嵌套对象递归），与「正常样例最小合法」原则一致。

    required 里声明但 properties 未定义的字段给字符串占位——文档不完整时静默丢掉该字段，
    会让"正常用例"缺必填被 422，且"缺失必填"维度退化成与正常样例相同。
    """
    if not isinstance(schema, dict):
        return {}
    properties = schema.get("properties") if isinstance(schema.get("properties"), dict) else {}
    required = [name for name in (schema.get("required") or []) if isinstance(name, str)]
    target = required if required else list(properties.keys())[:1]
    sample = {}
    for name in target:
        sub_schema = properties.get(name)
        if not isinstance(sub_schema, dict):
            sub_schema = {"type": "string"}
        sample[name] = _sample_value(sub_schema, name)
    return sample
