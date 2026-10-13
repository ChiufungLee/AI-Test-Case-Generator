"""测试设计确定性规则模块（D-036 阶段 2/3）。

纯函数、零 LLM、零 IO：技术适用性判定、覆盖项骨架、用例预算分配、测试点后处理与
编号归一。是 D-020「测试设计=确定性规则引擎为主，LLM 仅补业务语义」从 API 域到
工作流域的落地——规则引擎只回答"用什么技术、必须覆盖哪些覆盖项"，具体取值由阶段 4
的 LLM 依据需求文本填充。

本模块被 workflows/nodes.py 的节点调用，同时被测试直接单测。
"""

import re
from collections import Counter

from rapidfuzz import fuzz

from schemas.workflow_schemas import (
    CoverageItem,
    TestDesignPlan,
    TestDesignPlanItem,
)

# ---------- 规模常量 ----------

# 测试点数量上限：每需求点 3 个为基准，下限 6、上限 40（控制输出 token 与后续预算）
TEST_POINT_PER_REQUIREMENT = 3
TEST_POINT_LIMIT_MIN = 6
TEST_POINT_LIMIT_MAX = 40

# 用例总预算：每测试点 2 条为基准，下限 10、上限 40（与既有 _dynamic_case_limit 同量级）
CASE_PER_TEST_POINT = 2
CASE_BUDGET_MIN = 10
CASE_BUDGET_MAX = 40

# 单个测试点的用例数上限（分批生成的批大小）
CASE_BATCH_SIZE = 4

# 测试点查重阈值（与用例查重同口径）
TEST_POINT_DUPLICATE_THRESHOLD = 0.85

# 用例查重阈值（覆盖校验沿用）
DUPLICATE_SIMILARITY_THRESHOLD = 0.85

_PRIORITY_ORDER = {"P0": 0, "P1": 1, "P2": 2}
_RISK_ORDER = {"high": 0, "medium": 1, "low": 2}

# ---------- 技术判定的启发式词表 ----------

# 数值/次数/时长/上下限等"可边界化"的线索
_QUANTITY_PATTERN = re.compile(
    r"\d|次|分钟|小时|秒|天|条|个|位|长度|上限|下限|最大|最小|不超过|至少|至多|以内|以上"
)

_STATE_KEYWORDS = (
    "状态", "流转", "锁定", "解锁", "冻结", "解冻", "启用", "禁用", "过期", "失效", "激活",
)
_PERMISSION_KEYWORDS = (
    "权限", "角色", "越权", "未登录", "登录态", "鉴权", "授权", "管理员", "访客", "游客",
)
_RULE_KEYWORDS = ("且", "或", "同时", "满足", "组合", "条件", "规则")
_FLOW_KEYWORDS = ("流程", "步骤", "提交", "下单", "审批", "审核", "回调", "重试", "链路")
_ERROR_KEYWORDS = ("异常", "错误", "失败", "超时", "容错", "降级", "重复提交", "并发", "非法")

# 技术 → 覆盖项骨架（description, data_hint）。data_hint 只描述"要取什么数据"，
# 不含具体取值——取值属阶段 4（D-036 ④）。
_COVERAGE_TEMPLATES = {
    "equivalence_partitioning": (
        ("有效等价类输入", "取需求允许范围内的典型有效值"),
        ("无效等价类输入", "取需求明确不允许或格式非法的值"),
    ),
    "boundary_value": (
        ("下边界及其外侧", "取下限值与下限减一"),
        ("上边界及其外侧", "取上限值与上限加一"),
    ),
    "decision_table": (
        ("条件组合全部满足", "所有规则条件同时成立"),
        ("条件组合逐一不满足", "逐条使单个条件不成立"),
    ),
    "state_transition": (
        ("合法状态流转", "按需求允许的流转路径触发"),
        ("非法状态流转", "在需求不允许的状态下触发同一操作"),
    ),
    "scenario": (
        ("主流程贯通", "按需求主流程完整走通"),
        ("异常与中断分支", "在主流程中制造中断、回退或异常分支"),
    ),
    "error_guessing": (
        ("常见错误与容错", "重复提交、超时、并发、脏数据等易错场景"),
    ),
    "exploratory": (
        ("质量属性验证", "性能、安全、兼容性等非功能要求的验证点"),
    ),
}


# ---------- 规模与相似度 ----------


def dynamic_test_point_limit(requirement_count: int) -> int:
    """测试点数量上限随需求点数量动态计算（下限保证基础覆盖面，上限控制 token）"""
    return min(
        TEST_POINT_LIMIT_MAX,
        max(TEST_POINT_LIMIT_MIN, requirement_count * TEST_POINT_PER_REQUIREMENT),
    )


def dynamic_case_budget(test_point_count: int) -> int:
    """用例总预算随测试点数量动态计算（每个测试点至少一条，故下限 10 是兜底而非保证）"""
    return min(
        CASE_BUDGET_MAX,
        max(CASE_BUDGET_MIN, test_point_count * CASE_PER_TEST_POINT),
    )


def text_similarity(a: str, b: str) -> float:
    """标题类文本相似度：保序的 fuzz.ratio（对语序敏感）"""
    return fuzz.ratio(a or "", b or "") / 100.0


def case_similarity(a: dict, b: dict) -> float:
    """用例相似度：标题与步骤均用保序的 fuzz.ratio（按字符比较、对语序敏感）。

    token_sort_ratio 会把中文字符逐个当 token 排序后比较，等于只比字符集合，
    "输入正确用户名错误密码" 与 "输入错误用户名正确密码" 会被判 1.0——而这
    恰是两个不同的组合用例。步骤权重更高：标题相同但步骤序列不同不算重复。
    """
    title_sim = text_similarity(a.get("title"), b.get("title"))
    steps_a = "\n".join(a.get("steps") or [])
    steps_b = "\n".join(b.get("steps") or [])
    if not steps_a and not steps_b:
        return title_sim
    steps_sim = fuzz.ratio(steps_a, steps_b) / 100.0
    return 0.4 * title_sim + 0.6 * steps_sim


def requirement_ids_from_analysis(analysis: dict) -> set:
    """从需求分析产物中取出功能需求点编号集合"""
    return {
        item.get("id")
        for item in (analysis.get("functional_requirements") or [])
        if item.get("id")
    }


def short_code(test_point_id: str) -> str:
    """测试点编号 → 短码：TP-001 → TP001（用于派生用例与覆盖项编号）"""
    return re.sub(r"[^0-9A-Za-z]", "", test_point_id or "") or "TP"


def _test_point_sort_key(test_point: dict):
    return (
        _PRIORITY_ORDER.get(test_point.get("priority"), 2),
        _RISK_ORDER.get(test_point.get("risk_level"), 2),
        test_point.get("id") or "",
    )


def _contains(text: str, keywords) -> bool:
    return any(keyword in text for keyword in keywords)


# ---------- 测试点后处理 ----------


def _shares_requirement(a: dict, b: dict) -> bool:
    return bool(set(a.get("requirement_refs") or []) & set(b.get("requirement_refs") or []))


def _find_duplicate(candidate: dict, accepted: list) -> dict | None:
    """同需求点内标题相似的测试点视为重复（跨需求点的相似标题可能是不同业务分支）"""
    for other in accepted:
        if not _shares_requirement(candidate, other):
            continue
        if text_similarity(candidate.get("title"), other.get("title")) >= TEST_POINT_DUPLICATE_THRESHOLD:
            return other
    return None


def validate_test_points(test_points: list, requirement_ids: set) -> tuple:
    """测试点的确定性后处理：丢弃非法引用 → 同需求点内去重 → 超限裁剪 → 覆盖缺口提示。

    返回 (accepted, dropped, issues)。dropped 每项含 id/title/reason，随产物落库供回看。
    """
    accepted: list = []
    dropped: list = []
    issues: list = []

    for raw in test_points or []:
        if not isinstance(raw, dict):
            continue
        refs = [ref for ref in (raw.get("requirement_refs") or []) if ref in requirement_ids]
        if not refs:
            dropped.append(
                {"id": raw.get("id"), "title": raw.get("title"), "reason": "未关联到任何存在的需求点"}
            )
            continue
        candidate = {**raw, "requirement_refs": refs}
        duplicate = _find_duplicate(candidate, accepted)
        if duplicate is not None:
            dropped.append(
                {
                    "id": raw.get("id"),
                    "title": raw.get("title"),
                    "reason": f"与测试点 {duplicate.get('id')} 语义重复",
                }
            )
            continue
        accepted.append(candidate)

    limit = dynamic_test_point_limit(len(requirement_ids))
    if len(accepted) > limit:
        accepted.sort(key=_test_point_sort_key)
        overflow = accepted[limit:]
        accepted = accepted[:limit]
        for item in overflow:
            dropped.append(
                {"id": item.get("id"), "title": item.get("title"), "reason": f"超出测试点上限 {limit}"}
            )
        issues.append(f"测试点数量超过上限 {limit}，已按优先级与风险裁剪 {len(overflow)} 个")

    covered = {ref for item in accepted for ref in (item.get("requirement_refs") or [])}
    missing = sorted(requirement_ids - covered)
    if missing:
        issues.append("以下需求点没有任何测试点：" + "、".join(missing))
    if dropped:
        issues.append(f"共丢弃 {len(dropped)} 个测试点（关联失效/重复/超限）")

    return accepted, dropped, issues


def find_duplicate_test_points(test_points: list) -> list:
    """测试点查重（覆盖校验用）：同步产出疑似重复对"""
    pairs = []
    for i in range(len(test_points)):
        for j in range(i + 1, len(test_points)):
            score = text_similarity(test_points[i].get("title"), test_points[j].get("title"))
            if score >= TEST_POINT_DUPLICATE_THRESHOLD:
                pairs.append(
                    {
                        "case_a": test_points[i].get("id", ""),
                        "case_b": test_points[j].get("id", ""),
                        "similarity": round(score, 2),
                    }
                )
    return pairs


# ---------- 技术选型与覆盖项 ----------


def detect_techniques(test_point: dict) -> list:
    """按维度与文本线索判定测试技术（顺序即优先级，结果去重保序）"""
    dimension = test_point.get("dimension") or "function"
    text = " ".join(
        filter(
            None,
            [
                test_point.get("title"),
                test_point.get("description"),
                test_point.get("rationale"),
            ],
        )
    )
    techniques: list = []

    if dimension == "nonfunctional":
        techniques.append("exploratory")
    elif dimension == "boundary":
        techniques.extend(["boundary_value", "equivalence_partitioning"])
    elif dimension == "state" or _contains(text, _STATE_KEYWORDS):
        techniques.append("state_transition")
    elif dimension == "business_rule" or _contains(text, _RULE_KEYWORDS):
        techniques.append("decision_table")
    elif dimension == "permission":
        techniques.append("scenario")
    elif dimension == "exception" or _contains(text, _ERROR_KEYWORDS):
        techniques.append("error_guessing")
    elif dimension == "flow" or _contains(text, _FLOW_KEYWORDS):
        techniques.append("scenario")
    else:
        techniques.append("equivalence_partitioning")

    # 出现数值/次数/时长等可边界化线索时叠加边界值（不覆盖主技术）
    if "boundary_value" not in techniques and _QUANTITY_PATTERN.search(text):
        techniques.append("boundary_value")
    # 非功能维度不做等价类/边界值展开
    if dimension == "nonfunctional":
        techniques = [t for t in techniques if t == "exploratory"]

    seen = set()
    ordered = []
    for technique in techniques:
        if technique not in seen:
            seen.add(technique)
            ordered.append(technique)
    return ordered


def build_coverage_items(test_point: dict, techniques: list) -> list:
    """按技术生成覆盖项骨架：只给"必须覆盖什么"，取值由阶段 4 填充"""
    tp_id = test_point.get("id") or "TP"
    code = short_code(tp_id)
    title = test_point.get("title") or ""
    items = []
    seq = 0
    for technique in techniques:
        for description, data_hint in _COVERAGE_TEMPLATES.get(technique, ()):
            seq += 1
            items.append(
                CoverageItem(
                    id=f"CI-{code}-{seq:02d}",
                    test_point_id=tp_id,
                    technique=technique,
                    description=f"{title}：{description}" if title else description,
                    data_hint=data_hint,
                ).model_dump()
            )
    return items


# ---------- 用例预算与设计策略 ----------


def allocate_case_budget(test_points: list, total_budget: int) -> dict:
    """按 优先级 → 风险 分配用例预算：先保证每个测试点 1 条（覆盖优先于深度），
    剩余预算按同一顺序轮转加深，单点不超过 CASE_BATCH_SIZE。

    测试点数超过总预算时每个测试点仍保底 1 条（结果可能超出 total_budget，
    由调用方在 notes 中说明）——不漏测优先于预算字面值。
    """
    budget = {}
    ordered = sorted([tp for tp in (test_points or []) if tp.get("id")], key=_test_point_sort_key)
    for test_point in ordered:
        budget[test_point["id"]] = 1

    remaining = max(0, total_budget - len(ordered))
    index = 0
    while remaining > 0:
        if all(budget[tp["id"]] >= CASE_BATCH_SIZE for tp in ordered):
            break
        test_point = ordered[index % len(ordered)]
        if budget[test_point["id"]] < CASE_BATCH_SIZE:
            budget[test_point["id"]] += 1
            remaining -= 1
        index += 1
    return budget


def build_test_design_plan(test_points: list, total_budget: int | None = None) -> dict:
    """生成测试设计策略产物：每个测试点的技术、覆盖项清单与用例预算（确定性）"""
    test_points = [tp for tp in (test_points or []) if tp.get("id")]
    if total_budget is None:
        total_budget = dynamic_case_budget(len(test_points))

    budget = allocate_case_budget(test_points, total_budget)
    items = []
    for test_point in sorted(test_points, key=_test_point_sort_key):
        techniques = detect_techniques(test_point)
        items.append(
            TestDesignPlanItem(
                test_point_id=test_point["id"],
                techniques=techniques,
                coverage_items=[CoverageItem(**item) for item in build_coverage_items(test_point, techniques)],
                case_budget=budget.get(test_point["id"], 1),
                rationale=f"维度 {test_point.get('dimension') or 'function'} → " + "、".join(techniques),
            )
        )

    technique_summary = dict(
        sorted(Counter(technique for item in items for technique in item.techniques).items())
    )
    notes = []
    if len(test_points) > total_budget:
        notes.append(
            f"测试点 {len(test_points)} 个超过用例预算 {total_budget}，已按每点至少 1 条保证覆盖"
        )
    notes.append("覆盖项为确定性骨架，具体取值由用例生成阶段按需求文本填充")

    return TestDesignPlan(
        items=items,
        total_budget=sum(budget.values()),
        technique_summary=technique_summary,
        notes=notes,
    ).model_dump()


# ---------- 用例编号归一与质量检查 ----------


def normalize_case_ids(cases: list) -> list:
    """服务端统一分配用例编号（D-036 ⑧）：TC-<测试点短码>-<两位序号>。

    分批生成下 LLM 无法保证跨批编号唯一（每批都会从 001 起编），编号因此由服务端
    按最终顺序分配；无测试点引用的用例回退到 TC-GEN-NN。
    """
    counters: dict = {}
    normalized = []
    for case in cases or []:
        refs = case.get("test_point_refs") or []
        code = short_code(refs[0]) if refs else "GEN"
        counters[code] = counters.get(code, 0) + 1
        normalized.append({**case, "id": f"TC-{code}-{counters[code]:02d}"})
    return normalized


def case_quality_issues(cases: list) -> list:
    """用例字段完整性的确定性检查（覆盖校验的质检部分）"""
    issues = []
    for case in cases or []:
        case_id = case.get("id") or "未编号用例"
        if not (case.get("title") or "").strip():
            issues.append(f"{case_id} 缺少测试标题")
        if not (case.get("steps") or []):
            issues.append(f"{case_id} 缺少操作步骤")
        if not (case.get("expected_results") or []):
            issues.append(f"{case_id} 缺少预期结果")
        if not (case.get("test_point_refs") or []) and not (case.get("requirement_refs") or []):
            issues.append(f"{case_id} 未关联任何测试点或需求点")
    return issues
