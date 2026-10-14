/* 测试任务详情页逻辑（/workflow-detail?workflow_id=...）：SSE 跟踪节点进度（含 loading）→ 两个人工确认点（需求分析 / 测试点与设计策略）→ 按节点查看产物 → 重新生成。新建任务在 /workflow-new（static/js/workflow_new.js） */

// 步骤条展示的 6 步（与图节点非一一对应：测试点设计/设计策略/测试点确认合并为一步）
const STEP_ORDER = [
    "load_requirement",
    "retrieve_knowledge",
    "human_review",
    "human_review_design",
    "test_case_generation_agent",
    "coverage_check",
];

// 图节点 → 步骤条步骤（SSE 事件与 current_step 都是节点名，展示前先归一）
const NODE_TO_STEP = {
    load_requirement: "load_requirement",
    retrieve_knowledge: "retrieve_knowledge",
    requirement_analysis_agent: "human_review",
    human_review: "human_review",
    test_point_design_agent: "human_review_design",
    test_design_plan: "human_review_design",
    human_review_design: "human_review_design",
    test_case_generation_agent: "test_case_generation_agent",
    coverage_check: "coverage_check",
};

function toStep(node) {
    return NODE_TO_STEP[node] || node || null;
}

// 节点 → 产物面板映射（点击步骤条节点时切换右侧展示）
const PANEL_BY_NODE = {
    load_requirement: "requirementPanel",
    retrieve_knowledge: "retrievedPanel",
    requirement_analysis_agent: "reviewPanel",
    human_review: "reviewPanel",
    test_point_design_agent: "testPointPanel",
    test_design_plan: "testPointPanel",
    human_review_design: "testPointPanel",
    test_case_generation_agent: "casesPanel",
    coverage_check: "coveragePanel",
};

// 测试点编辑：维度选项与服务端 schemas.workflow_schemas.TEST_POINT_DIMENSIONS 保持一致
const DIMENSION_OPTIONS = [
    ["function", "功能"],
    ["flow", "业务流程"],
    ["state", "状态流转"],
    ["permission", "权限"],
    ["data", "数据"],
    ["boundary", "边界"],
    ["exception", "异常容错"],
    ["business_rule", "业务规则"],
    ["nonfunctional", "非功能质量"],
];

const STATUS_TEXT = {
    created: "未开始",
    analyzing: "进行中",
    waiting_review: "待人工确认",
    generating: "进行中",
    completed: "已完成",
    failed: "失败",
};

// 需求文档上传与新建任务表单在 /workflow-new 页（static/js/workflow_new.js）

const appState = {
    currentWorkflowId: null,
    currentWorkflow: null,
    isStreaming: false,
    // 流式运行期的步骤状态：done 已完成节点集合，loading 当前进行中的节点
    runtime: null,
    // 当前在展示产物的节点（点击步骤条切换）
    activeArtifactNode: null,
    // 重新生成模式：从两个确认点之一进入（cases 从测试点确认进入，test_points 从需求分析确认进入）
    regenerateMode: false,
    regenerateTarget: "cases",
    // 编辑态：editing 开关 + 编辑对象（"analysis" | "test_points"）+ 表单("form")/JSON("json") 两种编辑模式
    editing: false,
    editingTarget: "analysis",
    editMode: "form",
    // 本页面加载后已尝试自动续跑过的任务（防止失败后无限重试）
    autoResumed: new Set(),
    // 从新建页跳来并已自动开始过（防止刷新触发二次启动）
    autoStarted: false,
};

const elements = {};

document.addEventListener("DOMContentLoaded", init);
document.addEventListener("DOMContentLoaded", init);

/* ---------- 初始化（任务 id 来自 URL，不再依赖左侧列表选中） ---------- */

async function init() {
    [
        "detailLoading", "detailLoadError", "detailLoadErrorText",
        "detailView", "wfTitle", "wfStatus",
        "wfError", "stepBar", "requirementPanel", "requirementView",
        "requirementSource",
        "retrievedPanel", "retrievedVersion", "retrievedView",
        "reviewPanel", "analysisVersion", "analysisView", "analysisForm",
        "analysisEditor", "reviewActions", "editBtn", "switchEditorBtn",
        "cancelEditBtn", "approveBtn",
        "testPointPanel", "testPointVersion", "testPointPlanVersion", "testPointView",
        "testPointForm", "testPointActions", "tpEditBtn", "tpCancelEditBtn", "tpApproveBtn",
        "casesPanel", "casesVersion", "casesTable", "regenerateBtn", "regeneratePointsBtn",
        "exportBtn", "publishBtn", "casesTruncatedWarn", "casesFailedWarn",
        "coveragePanel", "coverageVersion", "coverageView",
        "startBtn",
    ].forEach((id) => {
        elements[id] = document.getElementById(id);
    });

    elements.editBtn.addEventListener("click", enterEditMode);
    elements.switchEditorBtn.addEventListener("click", switchEditor);
    elements.cancelEditBtn.addEventListener("click", exitEditMode);
    elements.approveBtn.addEventListener("click", approveWorkflow);
    elements.tpEditBtn.addEventListener("click", enterTestPointEditMode);
    elements.tpCancelEditBtn.addEventListener("click", exitTestPointEditMode);
    elements.tpApproveBtn.addEventListener("click", approveWorkflow);
    elements.regenerateBtn.addEventListener("click", () => enterRegenerateMode("cases"));
    elements.regeneratePointsBtn.addEventListener("click", () => enterRegenerateMode("test_points"));
    elements.exportBtn.addEventListener("click", exportCsv);
    elements.publishBtn.addEventListener("click", publishTestSet);
    elements.startBtn.addEventListener("click", () => startWorkflow(appState.currentWorkflowId));

    elements.stepBar.querySelectorAll("li").forEach((li) => {
        li.addEventListener("click", () => selectArtifactNode(li.dataset.step));
    });

    appState.currentWorkflowId =
        window.workflowId || new URLSearchParams(window.location.search).get("workflow_id");
    if (!appState.currentWorkflowId) {
        window.location.replace("/workflows");
        return;
    }

    // 从新建页跳来时带上 autostart=1：创建即开跑，避免任务停在 created 无人启动
    const autostart = new URLSearchParams(window.location.search).get("autostart") === "1";
    await loadWorkflowDetail({ showLoading: true, autostart });
}

/**
 * 拉取任务详情并渲染；运行中的任务自动接上后台事件流。
 *
 * - showLoading：首次进入时展示加载态（运行结束后的刷新不闪加载态）
 * - autostart：status=created 且来自新建页时自动开始
 * - autoResume：status 为 analyzing/generating 时接续事件流（每个任务每次加载只尝试一次）
 */
async function loadWorkflowDetail(opts = {}) {
    const { showLoading = false, autostart = false, autoResume = true } = opts;
    if (showLoading) {
        elements.detailLoading.hidden = false;
        elements.detailLoadError.hidden = true;
    }
    try {
        const response = await fetch(`/api/workflows/${appState.currentWorkflowId}`);
        if (response.status === 401) return redirectToLogin();
        if (!response.ok) throw new Error(`HTTP ${response.status}`);
        const workflow = await response.json();
        appState.currentWorkflowId = workflow.id;
        appState.currentWorkflow = workflow;
        appState.regenerateMode = false;
        appState.regenerateTarget = "cases";
        appState.editing = false;
        appState.editingTarget = "analysis";
        elements.detailLoading.hidden = true;
        elements.detailLoadError.hidden = true;
        renderDetail(workflow);

        const running = ["analyzing", "generating"].includes(workflow.status);
        if (autoResume && running && !appState.autoResumed.has(workflow.id)) {
            appState.autoResumed.add(workflow.id);
            await resumeWorkflow(workflow.id);
            return;
        }
        if (autostart && workflow.status === "created" && !appState.autoStarted) {
            appState.autoStarted = true;
            await startWorkflow(workflow.id);
        }
    } catch (error) {
        console.error("加载测试工作流失败:", error);
        elements.detailLoading.hidden = true;
        elements.detailLoadErrorText.textContent = "测试工作流加载失败，请稍后重试";
        elements.detailLoadError.hidden = false;
    }
}

function latestArtifact(workflow, type) {
    const artifacts = (workflow.artifacts || []).filter((a) => a.artifact_type === type);
    return artifacts.length ? artifacts[artifacts.length - 1] : null;
}

function nodeHasContent(node, workflow) {
    if (!workflow) return node === "load_requirement";
    switch (node) {
        case "load_requirement":
            return true;
        case "retrieve_knowledge":
            return !!latestArtifact(workflow, "retrieved_context");
        case "requirement_analysis_agent":
        case "human_review":
            return !!latestArtifact(workflow, "requirement_analysis");
        case "test_point_design_agent":
        case "test_design_plan":
        case "human_review_design":
            return !!latestArtifact(workflow, "test_point_set");
        case "test_case_generation_agent":
            return !!latestArtifact(workflow, "test_case_set");
        case "coverage_check":
            return !!latestArtifact(workflow, "coverage_report");
        default:
            return false;
    }
}

function showArtifactPanel(node) {
    [
        "requirementPanel",
        "retrievedPanel",
        "reviewPanel",
        "testPointPanel",
        "casesPanel",
        "coveragePanel",
    ].forEach((id) => {
        elements[id].hidden = true;
    });
    const panelId = PANEL_BY_NODE[node] || PANEL_BY_NODE[toStep(node)];
    if (panelId && elements[panelId]) elements[panelId].hidden = false;
    renderSteps(appState.currentWorkflow);
}

function renderDetail(workflow) {
    elements.detailView.hidden = false;
    elements.wfTitle.textContent = workflow.name || "未命名工作流";

    const statusBadge = elements.wfStatus;
    statusBadge.className = `status-badge status-${workflow.status}`;
    statusBadge.textContent = STATUS_TEXT[workflow.status] || workflow.status;

    // created 未启动 / failed 可重跑：给一个显式入口，任务不会停在无人启动的状态
    const canStart = ["created", "failed"].includes(workflow.status);
    elements.startBtn.hidden = !canStart;
    elements.startBtn.textContent = workflow.status === "failed" ? "重新运行" : "开始运行";

    if (workflow.error) {
        elements.wfError.textContent = workflow.error;
        elements.wfError.hidden = false;
    } else {
        elements.wfError.hidden = true;
    }

    renderAllPanelContents(workflow);

    // 默认展示最深的有内容节点；用户已选择时保持其选择
    if (
        !appState.activeArtifactNode ||
        !nodeHasContent(appState.activeArtifactNode, workflow)
    ) {
        appState.activeArtifactNode =
            STEP_ORDER.slice().reverse().find((s) => nodeHasContent(s, workflow)) ||
            "load_requirement";
    }
    showArtifactPanel(appState.activeArtifactNode);
    setStreamingUI();
}

function renderAllPanelContents(workflow) {
    const retrievedArtifact = latestArtifact(workflow, "retrieved_context");
    const analysisArtifact = latestArtifact(workflow, "requirement_analysis");
    const testPointArtifact = latestArtifact(workflow, "test_point_set");
    const testPlanArtifact = latestArtifact(workflow, "test_design_plan");
    const casesArtifact = latestArtifact(workflow, "test_case_set");
    const coverageArtifact = latestArtifact(workflow, "coverage_report");

    // 需求原文（节点1）：经需求文档上传创建的任务标出正文来源，便于回溯
    elements.requirementView.textContent = workflow.requirement_text || "";
    if (workflow.requirement_file_name) {
        elements.requirementSource.textContent = `来自文档：${workflow.requirement_file_name}`;
        elements.requirementSource.hidden = false;
    } else {
        elements.requirementSource.hidden = true;
    }

    // 知识库检索结果（节点2）
    if (retrievedArtifact) {
        elements.retrievedVersion.textContent = `v${retrievedArtifact.version}`;
        renderRetrieved(retrievedArtifact.content);
    }

    // 需求分析结果（节点3/4）：编辑态下不重建表单/JSON，避免覆盖用户未提交的修改
    if (analysisArtifact) {
        elements.analysisVersion.textContent = `v${analysisArtifact.version}`;
        if (!appState.editing || appState.editingTarget !== "analysis") {
            renderAnalysis(analysisArtifact.content);
            elements.analysisEditor.value = JSON.stringify(analysisArtifact.content, null, 2);
        }
        refreshReviewPanel(workflow);
    }

    // 测试点与设计策略（节点5/6/7）：同样避免覆盖未提交的编辑
    if (testPointArtifact) {
        elements.testPointVersion.textContent = `测试点 v${testPointArtifact.version}`;
        if (!appState.editing || appState.editingTarget !== "test_points") {
            renderTestPoints(testPointArtifact.content);
        }
    } else {
        elements.testPointVersion.textContent = "";
    }
    if (testPlanArtifact) {
        elements.testPointPlanVersion.textContent = `设计策略 v${testPlanArtifact.version}`;
        renderTestDesignPlan(testPlanArtifact.content);
    } else {
        elements.testPointPlanVersion.textContent = "";
        if (!testPointArtifact) elements.testPointView.innerHTML = "";
    }
    refreshTestPointPanel(workflow);

    // 测试用例（节点8）
    if (casesArtifact) {
        elements.casesVersion.textContent = `v${casesArtifact.version}`;
        elements.casesTruncatedWarn.hidden = !casesArtifact.content.truncated;
        const failedPoints = casesArtifact.content.failed_test_points || [];
        if (failedPoints.length) {
            elements.casesFailedWarn.textContent = `⚠ 以下测试点的用例生成失败；建议编辑测试点或需求分析结果微调后重跑：${failedPoints.join("、")}`;
            elements.casesFailedWarn.hidden = false;
        } else {
            elements.casesFailedWarn.hidden = true;
        }
        fillCasesTable(casesArtifact.content.test_cases || []);
        // 用例产物一旦生成就能用：跑完的与失败的都允许导出 / 保存到用例集 / 回到上一确认点。
        // 失败任务手里只有已生成的部分用例，保存前会先确认（见 publishTestSet）。
        const canUseCases = ["completed", "failed"].includes(workflow.status);
        elements.exportBtn.hidden = !canUseCases;
        elements.publishBtn.hidden = !canUseCases;
        elements.regenerateBtn.hidden = !canUseCases;
        elements.regeneratePointsBtn.hidden = !canUseCases;
    }

    // 结果汇总（节点9）
    if (coverageArtifact) {
        elements.coverageVersion.textContent = `v${coverageArtifact.version}`;
        renderCoverage(coverageArtifact.content);
    }
}

/* ---------- 两个确认点的可编辑性判定（D-036：靠 current_step 区分当前 gate） ---------- */

// 当前待人工确认的确认点：null 表示不在确认状态
function currentGate(workflow) {
    if (!workflow || workflow.status !== "waiting_review") return null;
    return workflow.current_step === "human_review_design" ? "test_points" : "analysis";
}

function canEditAnalysis(workflow) {
    if (currentGate(workflow) === "analysis") return true;
    // 重新设计测试点时，需求分析是阶段 2 的输入，允许一并修订
    return appState.regenerateMode && appState.regenerateTarget === "test_points";
}

function canEditTestPoints(workflow) {
    if (currentGate(workflow) === "test_points") return true;
    // 重新生成用例时，允许先修订测试点（服务端会确定性重算设计策略）
    return appState.regenerateMode && appState.regenerateTarget === "cases";
}

function refreshReviewPanel(workflow) {
    const editable = canEditAnalysis(workflow);
    const editingHere = appState.editing && appState.editingTarget === "analysis";

    elements.analysisView.hidden = editingHere;
    elements.analysisForm.hidden = !(editingHere && appState.editMode === "form");
    elements.analysisEditor.hidden = !(editingHere && appState.editMode === "json");

    elements.reviewActions.hidden = !editable;
    elements.editBtn.hidden = editingHere || !editable;
    elements.switchEditorBtn.hidden = !editingHere;
    elements.switchEditorBtn.textContent =
        appState.editMode === "form" ? "切换 JSON 编辑" : "切换表单编辑";
    elements.cancelEditBtn.hidden = !editingHere;
    elements.approveBtn.textContent =
        currentGate(workflow) === "analysis"
            ? "确认并继续"
            : "确认并重新设计测试点";
}

function refreshTestPointPanel(workflow) {
    const editable = canEditTestPoints(workflow);
    const editingHere = appState.editing && appState.editingTarget === "test_points";
    const hasPoints = !!latestArtifact(workflow, "test_point_set");

    elements.testPointView.hidden = editingHere;
    elements.testPointForm.hidden = !editingHere;
    elements.testPointActions.hidden = !editable && !editingHere;
    elements.tpEditBtn.hidden = editingHere || !editable || !hasPoints;
    elements.tpCancelEditBtn.hidden = !editingHere;
    elements.tpApproveBtn.hidden = !editable;
    elements.tpApproveBtn.textContent =
        currentGate(workflow) === "test_points" ? "确认并继续" : "确认并重新生成用例";
}

function selectArtifactNode(node) {
    if (!nodeHasContent(node, appState.currentWorkflow)) return;
    appState.activeArtifactNode = node;
    showArtifactPanel(node);
}

function computeStepStates(workflow) {
    const done = new Set();

    if (latestArtifact(workflow, "requirement_analysis")) {
        done.add("load_requirement");
        done.add("retrieve_knowledge");
        done.add("human_review");
    }
    if (latestArtifact(workflow, "test_point_set")) {
        done.add("human_review_design");
    }
    if (latestArtifact(workflow, "test_case_set")) {
        done.add("test_case_generation_agent");
    }
    if (latestArtifact(workflow, "coverage_report")) {
        done.add("coverage_check");
    }

    let active = null;
    if (workflow.status === "waiting_review") {
        active = toStep(workflow.current_step) || "human_review";
        // 待确认的步骤要显示为 active，而不是"已完成"
        done.delete(active);
    } else if (["analyzing", "generating"].includes(workflow.status)) {
        active = toStep(workflow.current_step) || null;
    }
    return { done, active };
}

function renderSteps(workflow) {
    const { done, active } = computeStepStates(workflow);

    // 流式运行期间以 SSE 事件驱动的 runtime 状态为准；loading 优先于 done
    if (appState.isStreaming && appState.runtime) {
        appState.runtime.done.forEach((s) => done.add(s));
        if (appState.runtime.loading) {
            done.delete(appState.runtime.loading);
            return applyStepClasses(done, appState.runtime.loading, true);
        }
    }
    applyStepClasses(done, active, false);
}

function applyStepClasses(done, active, asLoading) {
    const workflow = appState.currentWorkflow;
    elements.stepBar.querySelectorAll("li").forEach((li) => {
        const step = li.dataset.step;
        li.classList.remove("done", "active", "loading", "selected");
        if (done.has(step)) li.classList.add("done");
        else if (step === active) li.classList.add(asLoading ? "loading" : "active");
        if (nodeHasContent(step, workflow)) li.classList.add("has-content");
        if (step === appState.activeArtifactNode && nodeHasContent(step, workflow)) {
            li.classList.add("selected");
        }
    });
}

function beginRuntimeTracking() {
    const { done } = appState.currentWorkflow
        ? computeStepStates(appState.currentWorkflow)
        : { done: new Set() };
    appState.runtime = {
        done: new Set(done),
        loading: STEP_ORDER.find((s) => !done.has(s)) || null,
    };

    // 断点续跑场景下，服务端记录的 current_step 比推算更精确
    const workflow = appState.currentWorkflow;
    const currentStep = workflow ? toStep(workflow.current_step) : null;
    if (
        workflow &&
        ["analyzing", "generating"].includes(workflow.status) &&
        STEP_ORDER.includes(currentStep)
    ) {
        appState.runtime.loading = currentStep;
    }

    // 状态徽章立即切到"进行中"，不等 SSE 结束
    if (workflow) {
        workflow.status = "analyzing";
    }
    elements.wfStatus.className = "status-badge status-analyzing";
    elements.wfStatus.textContent = STATUS_TEXT.analyzing;
    // 运行期不允许再次启动（开始任务按钮已随状态隐藏，这里再兜一层滚动/竞态）
    elements.startBtn.hidden = true;

    renderSteps(appState.currentWorkflow);
}

async function resumeWorkflow(workflowId) {
    // 刷新/断连后的恢复：start 端点对运行中状态会从 checkpoint 断点续跑
    setStreamingUI(true);
    beginRuntimeTracking();
    await streamEvents(`/api/workflows/${workflowId}/start`, {
        method: "POST",
    });
}

function renderRetrieved(content) {
    const view = elements.retrievedView;
    view.innerHTML = "";
    (content.documents || []).forEach((doc, index) => {
        const details = document.createElement("details");
        const summary = document.createElement("summary");
        summary.textContent = `${index + 1}. ${doc.source}`;
        const pre = document.createElement("pre");
        pre.className = "doc-content";
        pre.textContent = doc.content;
        details.appendChild(summary);
        details.appendChild(pre);
        view.appendChild(details);
    });
}

function renderAnalysis(analysis) {
    const view = elements.analysisView;
    view.innerHTML = "";

    const summary = document.createElement("p");
    summary.className = "analysis-summary";
    summary.textContent = analysis.summary || "";
    view.appendChild(summary);

    appendSection(view, "测试范围", analysis.scope);
    appendSection(view, "涉及角色", analysis.roles);
    appendRequirementItems(view, analysis.functional_requirements || []);
    appendSection(view, "业务规则", analysis.business_rules);
    appendSection(view, "约束与限制", analysis.constraints);
    appendSection(view, "业务前置条件", analysis.preconditions);
    appendSection(view, "验收标准", analysis.acceptance_criteria);
    appendRiskItems(view, analysis.risks || []);
    appendSection(view, "歧义、冲突与待确认项", analysis.assumptions);
}

/* ---------- 测试点与设计策略（节点 5/6/7） ---------- */

const DIMENSION_LABEL = Object.fromEntries(DIMENSION_OPTIONS);

function renderTestPoints(content) {
    const view = elements.testPointView;
    view.innerHTML = "";

    if (content.coverage_target) {
        const target = document.createElement("p");
        target.className = "analysis-summary";
        target.textContent = `覆盖目标：${content.coverage_target}`;
        view.appendChild(target);
    }

    appendSection(view, "覆盖取舍说明", content.notes);

    const points = content.test_points || [];
    const heading = document.createElement("h3");
    heading.className = "section-title";
    heading.textContent = `测试点（${points.length}）`;
    view.appendChild(heading);

    const table = document.createElement("table");
    table.className = "cases-table";
    const thead = document.createElement("thead");
    const headerRow = document.createElement("tr");
    ["编号", "测试点", "维度", "优先级", "风险", "关联需求"].forEach((text) => {
        const th = document.createElement("th");
        th.textContent = text;
        headerRow.appendChild(th);
    });
    thead.appendChild(headerRow);
    table.appendChild(thead);

    const tbody = document.createElement("tbody");
    points.forEach((point) => {
        const tr = document.createElement("tr");
        [
            point.id || "",
            point.title || "",
            DIMENSION_LABEL[point.dimension] || point.dimension || "",
            point.priority || "",
            point.risk_level || "",
            (point.requirement_refs || []).join(", "),
        ].forEach((value) => {
            const td = document.createElement("td");
            td.textContent = value;
            tr.appendChild(td);
        });
        tbody.appendChild(tr);
    });
    table.appendChild(tbody);
    view.appendChild(table);

    const dropped = content.dropped_test_points || [];
    if (dropped.length) {
        const line = document.createElement("p");
        line.className = "warn";
        line.textContent = `已被丢弃的测试点：${dropped
            .map((d) => `${d.id || "?"}（${d.reason}）`)
            .join("；")}`;
        view.appendChild(line);
    }
    if (content.issues && content.issues.length) {
        const issues = document.createElement("p");
        issues.className = "warn";
        issues.textContent = `覆盖提示：${content.issues.join("；")}`;
        view.appendChild(issues);
    }
}

function renderTestDesignPlan(plan) {
    const view = elements.testPointView;
    const items = plan.items || [];
    if (!items.length) return;

    const heading = document.createElement("h3");
    heading.className = "section-title";
    heading.textContent = `测试设计策略（确定性生成，共 ${plan.total_budget ?? 0} 条用例预算）`;
    view.appendChild(heading);

    const list = document.createElement("ul");
    list.className = "plan-list";
    items.forEach((item) => {
        const li = document.createElement("li");
        const techniques = (item.techniques || []).join("、");
        const coverage = (item.coverage_items || [])
            .map((c) => `${c.id} ${c.description}`)
            .join("；");
        li.textContent = `${item.test_point_id}｜技术：${techniques || "—"}｜预算：${item.case_budget} 条｜覆盖项：${coverage || "—"}`;
        list.appendChild(li);
    });
    view.appendChild(list);

    if (plan.notes && plan.notes.length) {
        const notes = document.createElement("p");
        notes.className = "muted";
        notes.textContent = plan.notes.join("；");
        view.appendChild(notes);
    }
}

/* ---------- 测试点编辑（表单） ---------- */

/* 字段控件与折叠卡片的通用构造器（editInput / editSelect / editTextarea / editField、
   autoGrowTextarea、setCardCollapsed、buildCollapseToggle、buildRowRemoveButton、summarySpan）
   都在 common.js：工作流详情与用例集详情的编辑器共用同一套。 */

function buildTestPointRow(point) {
    const card = document.createElement("div");
    card.className = "edit-card collapsed";

    // 折叠态：编号 + 标题 + 维度·优先级·风险 + 描述摘要（测试点也常有几十条，全展开就是一片文本框）
    const summary = document.createElement("div");
    summary.className = "edit-summary";
    summary.appendChild(summarySpan("edit-summary-id", point.id || "编号待分配"));
    summary.appendChild(summarySpan("edit-summary-title", point.title || "（未填标题）"));
    summary.appendChild(summarySpan("edit-summary-props", ""));
    summary.appendChild(summarySpan("edit-summary-desc", point.description || "（未填说明）"));
    summary.appendChild(buildCollapseToggle(card));
    summary.appendChild(buildRowRemoveButton(card));
    card.appendChild(summary);

    const expanded = document.createElement("div");
    expanded.className = "edit-body";

    // 头行：编号 + 标题 + 展开/收起 + 删除
    const head = document.createElement("div");
    head.className = "edit-head";
    head.appendChild(editInput("id", point.id || "", "edit-input edit-id", "TP-001"));
    head.appendChild(editInput("title", point.title || "", "edit-input edit-title", "测试点标题"));
    head.appendChild(buildCollapseToggle(card));
    head.appendChild(buildRowRemoveButton(card));
    expanded.appendChild(head);

    // 属性行：三个短控件并排，各带小标签（原来是无标签的三个下拉，看不出哪个是哪个）
    const props = document.createElement("div");
    props.className = "edit-props";
    props.appendChild(
        editField("维度", editSelect("dimension", DIMENSION_OPTIONS, point.dimension || "function"), true)
    );
    props.appendChild(
        editField(
            "优先级",
            editSelect("priority", [["P0", "P0"], ["P1", "P1"], ["P2", "P2"]], point.priority || "P1"),
            true
        )
    );
    props.appendChild(
        editField("风险", editSelect("risk_level", RISK_LEVEL_OPTIONS, point.risk_level || "medium"), true)
    );
    expanded.appendChild(props);

    // 长文本字段：用多行文本域，长描述不再被单行输入框截断
    expanded.appendChild(
        editField(
            "要点描述",
            editTextarea("description", point.description || "", "edit-input edit-desc", "要验证的可测方面", 2)
        )
    );
    expanded.appendChild(
        editField(
            "关联需求",
            editInput(
                "requirement_refs",
                (point.requirement_refs || []).join(", "),
                "edit-input edit-refs",
                "如 REQ-001, REQ-002"
            )
        )
    );
    expanded.appendChild(
        editField(
            "判定理由",
            editInput("rationale", point.rationale || "", "edit-input edit-rationale", "为什么需要这个测试点")
        )
    );
    card.appendChild(expanded);

    card.syncSummary = syncCardSummary;  // 收起时由 common.js 回调，把最新值写回摘要行
    setCardCollapsed(card, true);
    return card;
}

function buildTestPointForm(content) {
    const form = elements.testPointForm;
    form.innerHTML = "";

    const hint = document.createElement("p");
    hint.className = "edit-hint";
    hint.textContent =
        "测试点默认折叠成一行摘要（编号 + 标题 + 维度·优先级·风险），点条目右侧「展开」编辑该条；需求引用必须真实存在，否则确认时会被拒绝。设计策略由系统按测试点自动重算，不接受手工编辑。";
    form.appendChild(hint);

    // 覆盖目标常写成一句话或几条清单：用多行文本域，长文本不再被单行输入框截断
    form.appendChild(formLabel("覆盖目标"));
    const target = editTextarea(
        "coverage_target",
        content.coverage_target || "",
        "edit-input",
        "如：覆盖消息中心全部 10 个功能",
        2
    );
    // collectFormTestPoints 按 data-field 取值（editTextarea 写的是 data-sub，这里补上）
    target.dataset.field = "coverage_target";
    form.appendChild(target);

    form.appendChild(formLabel("测试点"));
    const wrap = document.createElement("div");
    wrap.className = "edit-reqs";
    wrap.dataset.field = "test_points";
    (content.test_points || []).forEach((point) => wrap.appendChild(buildTestPointRow(point)));
    form.appendChild(wrap);
    form.appendChild(
        buildAddButton("＋ 添加测试点", () => {
            const card = buildTestPointRow({});
            wrap.appendChild(card);
            // 新增的测试点直接展开，省掉"先展开再输入"这一步
            setCardCollapsed(card, false);
            const first = card.querySelector("input");
            if (first) first.focus();
        })
    );
}

function collectFormTestPoints() {
    const form = elements.testPointForm;
    const targetEl = form.querySelector('[data-field="coverage_target"]');
    const points = [];
    const usedIds = new Set();

    form
        .querySelectorAll('.edit-reqs[data-field="test_points"] .edit-card')
        .forEach((row, index) => {
            const value = (sub) => {
                const el = row.querySelector(`[data-sub="${sub}"]`);
                return el ? (el.value || "").trim() : "";
            };
            const title = value("title");
            const description = value("description");
            if (!title && !description) return;
            let id = value("id");
            if (!id || usedIds.has(id)) id = allocateId(usedIds, "TP", index);
            usedIds.add(id);
            points.push({
                id,
                title,
                description,
                requirement_refs: value("requirement_refs")
                    .split(/[,，、\s]+/)
                    .filter(Boolean),
                dimension: value("dimension") || "function",
                priority: value("priority") || "P1",
                risk_level: value("risk_level") || "medium",
                rationale: value("rationale"),
            });
        });

    return {
        test_points: points,
        coverage_target: targetEl ? targetEl.value.trim() : "",
        notes: [],
    };
}

function appendSection(container, title, items) {
    if (!items || !items.length) return;
    const h3 = document.createElement("h3");
    h3.textContent = title;
    container.appendChild(h3);
    const ul = document.createElement("ul");
    items.forEach((item) => {
        const li = document.createElement("li");
        li.textContent = item;
        ul.appendChild(li);
    });
    container.appendChild(ul);
}

function appendRequirementItems(container, items) {
    if (!items.length) return;
    const h3 = document.createElement("h3");
    h3.textContent = "功能需求点";
    container.appendChild(h3);
    const ul = document.createElement("ul");
    items.forEach((item) => {
        const li = document.createElement("li");
        li.textContent = `${item.id} ${item.title}` + (item.description ? `：${item.description}` : "");
        ul.appendChild(li);
    });
    container.appendChild(ul);
}

function appendRiskItems(container, items) {
    if (!items.length) return;
    const h3 = document.createElement("h3");
    h3.textContent = "风险";
    container.appendChild(h3);
    const ul = document.createElement("ul");
    items.forEach((item) => {
        const li = document.createElement("li");
        const tag = document.createElement("span");
        tag.className = `tag ${item.level || "medium"}`;
        tag.textContent = (item.level || "medium").toUpperCase();
        li.appendChild(tag);
        li.appendChild(document.createTextNode(item.description || ""));
        ul.appendChild(li);
    });
    container.appendChild(ul);
}

function fillCasesTable(cases) {
    const table = elements.casesTable;
    table.innerHTML = "";

    const thead = document.createElement("thead");
    const headerRow = document.createElement("tr");
    ["用例编号", "测试标题", "前置条件", "操作步骤", "测试数据", "预期结果", "优先级", "自动化", "需求追溯", "测试点/覆盖项", "覆盖说明"].forEach((text) => {
        const th = document.createElement("th");
        th.textContent = text;
        headerRow.appendChild(th);
    });
    thead.appendChild(headerRow);
    table.appendChild(thead);

    const tbody = document.createElement("tbody");
    cases.forEach((c) => {
        const tr = document.createElement("tr");
        const cells = [
            c.id,
            c.title,
            (c.preconditions || []).join("\n"),
            (c.steps || []).map((s, i) => `${i + 1}. ${s}`).join("\n"),
            c.test_data || "",
            (c.expected_results || []).join("\n"),
            c.priority,
            c.automation,
            (c.requirement_refs || []).join(", "),
            formatTestCaseTrace(c.test_point_refs, c.covered_items),
            c.rationale || "",
        ];
        cells.forEach((value) => {
            const td = document.createElement("td");
            // 长文本列（操作步骤/测试数据/预期结果/覆盖说明等）折叠并可展开，见 common.js
            fillCaseCell(td, value);
            tr.appendChild(td);
        });
        tbody.appendChild(tr);
    });
    table.appendChild(tbody);
}

function renderCoverage(report) {
    const view = elements.coverageView;
    view.innerHTML = "";

    const metric = document.createElement("span");
    metric.className = "coverage-metric";
    metric.textContent = `用例总数：${report.total_cases}`;
    view.appendChild(metric);

    const metricPoints = document.createElement("span");
    metricPoints.className = "coverage-metric";
    metricPoints.textContent = `测试点：${report.covered_test_points?.length || 0}/${report.total_test_points || 0} 已覆盖`;
    view.appendChild(metricPoints);

    const metricPriority = document.createElement("span");
    metricPriority.className = "coverage-metric";
    const priorityText = Object.entries(report.priority_summary || {})
        .map(([p, n]) => `${p}:${n}`)
        .join(" / ") || "无";
    metricPriority.textContent = `优先级分布：${priorityText}`;
    view.appendChild(metricPriority);

    const dimensionText = Object.entries(report.dimension_summary || {})
        .map(([d, n]) => `${DIMENSION_LABEL[d] || d}:${n}`)
        .join(" / ");
    if (dimensionText) {
        const metricDimension = document.createElement("span");
        metricDimension.className = "coverage-metric";
        metricDimension.textContent = `测试点维度分布：${dimensionText}`;
        view.appendChild(metricDimension);
    }

    const uncoveredPoints = report.uncovered_test_points || [];
    const uncoveredPointLine = document.createElement("p");
    if (uncoveredPoints.length) {
        uncoveredPointLine.className = "warn";
        uncoveredPointLine.textContent = `未被任何用例覆盖的测试点：${uncoveredPoints.join("、")}`;
    } else {
        uncoveredPointLine.className = "ok";
        uncoveredPointLine.textContent = "所有测试点均已被用例覆盖 ✔";
    }
    view.appendChild(uncoveredPointLine);

    const uncovered = report.uncovered_requirements || [];
    const uncoveredLine = document.createElement("p");
    if (uncovered.length) {
        uncoveredLine.className = "warn";
        uncoveredLine.textContent = `未被覆盖的需求点：${uncovered.join("、")}`;
    } else {
        uncoveredLine.className = "ok";
        uncoveredLine.textContent = "所有需求点均已被用例覆盖 ✔";
    }
    view.appendChild(uncoveredLine);

    const uncoveredItems = report.uncovered_coverage_items || [];
    if (uncoveredItems.length) {
        const itemLine = document.createElement("p");
        itemLine.className = "warn";
        itemLine.textContent = `没有用例声明的覆盖项：${uncoveredItems.join("、")}`;
        view.appendChild(itemLine);
    }

    if (report.note) {
        const noteLine = document.createElement("p");
        noteLine.className = "warn";
        noteLine.textContent = report.note;
        view.appendChild(noteLine);
    }

    const invalid = report.invalid_refs || [];
    if (invalid.length) {
        const invalidLine = document.createElement("p");
        invalidLine.className = "bad";
        invalidLine.textContent = `非法需求引用（用例引用了不存在的需求点）：${invalid.join("、")}`;
        view.appendChild(invalidLine);
    }

    const invalidPoints = report.invalid_test_point_refs || [];
    if (invalidPoints.length) {
        const invalidPointLine = document.createElement("p");
        invalidPointLine.className = "bad";
        invalidPointLine.textContent = `非法测试点引用（用例引用了不存在的测试点）：${invalidPoints.join("、")}`;
        view.appendChild(invalidPointLine);
    }

    const failedPoints = report.failed_test_points || [];
    if (failedPoints.length) {
        const failedLine = document.createElement("p");
        failedLine.className = "bad";
        failedLine.textContent = `用例生成失败的测试点（建议编辑测试点或需求分析结果微调后重跑）：${failedPoints.join("、")}`;
        view.appendChild(failedLine);
    }

    const duplicates = report.duplicates || [];
    if (duplicates.length) {
        const dupLine = document.createElement("p");
        dupLine.className = "warn";
        dupLine.textContent = `疑似重复用例：${duplicates
            .map((d) => `${d.case_a} ≈ ${d.case_b}（${Math.round(d.similarity * 100)}%）`)
            .join("；")}`;
        view.appendChild(dupLine);
    }

    const duplicatePoints = report.duplicate_test_points || [];
    if (duplicatePoints.length) {
        const dupPointLine = document.createElement("p");
        dupPointLine.className = "warn";
        dupPointLine.textContent = `疑似重复测试点：${duplicatePoints
            .map((d) => `${d.case_a} ≈ ${d.case_b}`)
            .join("；")}`;
        view.appendChild(dupPointLine);
    }

    const qualityIssues = report.quality_issues || [];
    if (qualityIssues.length) {
        const issueHeading = document.createElement("p");
        issueHeading.className = "warn";
        issueHeading.textContent = `质量问题清单（${qualityIssues.length} 项）：`;
        view.appendChild(issueHeading);
        const issueList = document.createElement("ul");
        issueList.className = "plan-list";
        qualityIssues.forEach((issue) => {
            const li = document.createElement("li");
            li.textContent = issue;
            issueList.appendChild(li);
        });
        view.appendChild(issueList);
    }
}

/* ---------- 需求分析编辑：表单模式（默认）与 JSON 模式互切 ---------- */

function enterEditMode() {
    const workflow = appState.currentWorkflow;
    const artifact = workflow && latestArtifact(workflow, "requirement_analysis");
    if (!artifact) return;
    appState.editing = true;
    appState.editingTarget = "analysis";
    appState.editMode = "form";
    buildAnalysisForm(artifact.content);
    renderAllPanelContents(workflow);
    // 面板此时才可见：文本域要按内容长高（隐藏时 scrollHeight 为 0，量不出来）
    autoGrowTextareas(elements.analysisForm);
    elements.analysisForm.scrollIntoView({ behavior: "smooth", block: "start" });
}

function enterTestPointEditMode() {
    const workflow = appState.currentWorkflow;
    const artifact = workflow && latestArtifact(workflow, "test_point_set");
    if (!artifact) return;
    appState.editing = true;
    appState.editingTarget = "test_points";
    buildTestPointForm(artifact.content);
    renderAllPanelContents(workflow);
    // 覆盖目标是常显的文本域，同样要按内容长高
    autoGrowTextareas(elements.testPointForm);
    elements.testPointForm.scrollIntoView({ behavior: "smooth", block: "start" });
}

function exitEditMode() {
    appState.editing = false;
    // 取消编辑时回到需求分析展示
    appState.activeArtifactNode = "human_review";
    renderAllPanelContents(appState.currentWorkflow);
    showArtifactPanel("human_review");
}

function exitTestPointEditMode() {
    appState.editing = false;
    appState.activeArtifactNode = "human_review_design";
    renderAllPanelContents(appState.currentWorkflow);
    showArtifactPanel("human_review_design");
}

function switchEditor() {
    if (!appState.editing) return;
    if (appState.editMode === "form") {
        const obj = collectFormAnalysis();
        appState.editMode = "json";
        elements.analysisEditor.value = JSON.stringify(obj, null, 2);
    } else {
        let obj;
        try {
            obj = JSON.parse(elements.analysisEditor.value);
        } catch (e) {
            alert("JSON 格式有误，请修正后再切换回表单");
            return;
        }
        if (!obj || typeof obj !== "object" || Array.isArray(obj)) {
            alert("需求分析必须是 JSON 对象（不能是 null / 数组），请修正后再切换回表单");
            return;
        }
        appState.editMode = "form";
        buildAnalysisForm(obj);
    }
    refreshReviewPanel(appState.currentWorkflow);
}

function formLabel(text) {
    const label = document.createElement("div");
    label.className = "form-label";
    label.textContent = text;
    return label;
}

function buildAddButton(text, onClick) {
    const btn = document.createElement("button");
    btn.type = "button";
    btn.className = "btn small";
    btn.textContent = text;
    btn.addEventListener("click", onClick);
    return btn;
}

function buildAnalysisForm(analysis) {
    const form = elements.analysisForm;
    form.innerHTML = "";

    const hint = document.createElement("p");
    hint.className = "edit-hint";
    hint.textContent =
        "区块默认折叠，只显示摘要行，点区块标题右侧「展开」才出现字段；列表类字段每行一条，条目再点各自的「展开」编辑。完成后点「确认并继续」。";
    form.appendChild(hint);

    // 条目卡区块：区块头（标题 + 展开）+ 若干条目卡 + 添加按钮
    const addItemSection = (titleText, field, items, builder, addLabel) => {
        const section = buildAnalysisCard(titleText);
        form.appendChild(section.card);
        const wrap = document.createElement("div");
        wrap.className = "edit-reqs";
        wrap.dataset.field = field;
        (items || []).forEach((item) => wrap.appendChild(builder(item)));
        section.body.appendChild(wrap);
        section.body.appendChild(
            buildAddButton(addLabel, () => {
                const card = builder({});
                wrap.appendChild(card);
                // 新增的条目直接展开，省掉"先展开再输入"这一步
                setCardCollapsed(card, false);
                const first = card.querySelector("input");
                if (first) first.focus();
            })
        );
    };

    const overview = buildAnalysisCard("概览");
    overview.body.appendChild(
        editField("需求概述", editTextarea("summary", analysis.summary || "", "edit-input", "一句话概述本次需求", 3))
    );
    overview.body.appendChild(
        buildAnalysisLines("scope", "测试范围", "每行一条，如：消息中心全部 10 个功能", analysis.scope)
    );
    overview.body.appendChild(
        buildAnalysisLines("roles", "涉及角色", "每行一条，如：普通用户 / 管理员", analysis.roles)
    );
    form.appendChild(overview.card);

    addItemSection(
        "功能需求点（编号留空自动分配，供用例追溯引用）",
        "functional_requirements",
        analysis.functional_requirements,
        buildRequirementCard,
        "＋ 添加需求点"
    );

    const rules = buildAnalysisCard("规则与验收");
    rules.body.appendChild(
        buildAnalysisLines(
            "business_rules",
            "业务规则",
            "每行一条，如：连续 5 次验证码错误锁定 30 分钟",
            analysis.business_rules
        )
    );
    rules.body.appendChild(
        buildAnalysisLines("constraints", "约束与限制", "每行一条，如：仅支持手机号登录", analysis.constraints)
    );
    rules.body.appendChild(
        buildAnalysisLines(
            "preconditions",
            "业务前置条件",
            "每行一条，如：账号已注册且未锁定",
            analysis.preconditions
        )
    );
    rules.body.appendChild(
        buildAnalysisLines("acceptance_criteria", "验收标准", "每行一条，怎样算通过", analysis.acceptance_criteria)
    );
    rules.body.appendChild(
        buildAnalysisLines("assumptions", "歧义与待确认项", "每行一条，歧义 / 冲突 / 待确认", analysis.assumptions)
    );
    form.appendChild(rules.card);

    addItemSection("风险", "risks", analysis.risks, buildRiskCard, "＋ 添加风险");

    // 区块一律先收起（收起时会顺带算出"里面有什么"的摘要行）
    form.querySelectorAll(".edit-section").forEach((card) => setSectionCollapsed(card, true));
}

/* 需求分析的区块卡、字段与条目卡：和测试点编辑共用 .tp-* 视觉语言。
   原来是 contenteditable 就地编辑，一屏几十个无标签文字块、列表靠 Enter/Backspace 增删，
   既看不出字段含义也不好发现操作；现在字段带标签、列表每行一条、条目一条一张卡。 */
const RISK_LEVEL_OPTIONS = [
    ["high", "高"],
    ["medium", "中"],
    ["low", "低"],
];

/* 区块卡：默认折叠成一行摘要（"里面有什么"），点「展开」才出现字段。
   需求分析字段多（概览 3 项、规则与验收 5 项、需求点 / 风险各几十条），全展开就是一片编辑框。 */
function buildAnalysisCard(titleText) {
    const card = document.createElement("div");
    card.className = "edit-section collapsed";

    const head = document.createElement("div");
    head.className = "edit-head";
    head.appendChild(summarySpan("edit-section-title", titleText));
    const toggle = document.createElement("button");
    toggle.type = "button";
    toggle.className = "edit-section-toggle";
    toggle.addEventListener("click", () => setSectionCollapsed(card, !card.classList.contains("collapsed")));
    head.appendChild(toggle);
    card.appendChild(head);

    const digest = document.createElement("div");
    digest.className = "edit-digest";
    card.appendChild(digest);

    const body = document.createElement("div");
    body.className = "edit-section-body";
    card.appendChild(body);

    return { card, body };
}

function setSectionCollapsed(card, collapsed) {
    card.classList.toggle("collapsed", collapsed);
    // 折叠时顺带重算摘要行，保证它讲的是当前内容；展开时让字段里的文本域按内容长高
    if (collapsed) syncSectionDigest(card);
    else autoGrowTextareas(card);
    card.querySelectorAll(".edit-section-toggle").forEach((button) => {
        button.textContent = collapsed ? "展开" : "收起";
        button.dataset.tip = collapsed ? "展开后可编辑这一区块" : "收起这一区块";
    });
}

/* 摘要行是折叠态唯一可见的内容，所以要把"里面有什么"讲清楚：
   条目区块给条数 + 首条标题；字段区块逐字段给「标签 + 条数」，
   「需求概述」这种单句字段直接显示正文。 */
function syncSectionDigest(card) {
    const digest = card.querySelector(".edit-digest");
    if (!digest) return;
    digest.innerHTML = "";

    const list = card.querySelector(".edit-reqs");
    if (list) {
        const cards = list.querySelectorAll(".edit-card");
        digest.appendChild(summarySpan("edit-digest-item", `共 ${cards.length} 条`));
        const firstTitle = cards.length ? cards[0].querySelector('[data-sub="title"]') : null;
        const firstText = firstTitle ? (firstTitle.value || "").trim() : "";
        if (firstText) digest.appendChild(summarySpan("edit-digest-item", `首条：${firstText}`));
        return;
    }

    card.querySelectorAll(".edit-section-body > .edit-field").forEach((field) => {
        const label = field.querySelector(".edit-label");
        const control = field.querySelector("input, textarea, select");
        if (!label || !control) return;
        const raw = control.value || "";
        if (control.dataset.sub === "summary") {
            const text = raw.trim().replace(/\s+/g, " ");
            const excerpt = text.length > 48 ? `${text.slice(0, 48)}…` : text;
            digest.appendChild(summarySpan("edit-digest-item", excerpt || "需求概述未填写"));
            return;
        }
        const count = raw
            .split("\n")
            .map((line) => line.trim())
            .filter(Boolean).length;
        digest.appendChild(summarySpan("edit-digest-item", `${label.textContent} ${count}`));
    });
}

/* 列表类字段：每行一条的多行文本域。比"可编辑行 + Enter 新增"更好发现，也方便整段粘贴 */
function buildAnalysisLines(field, label, placeholder, items) {
    return editField(label, editTextarea(field, (items || []).join("\n"), "edit-input", placeholder, 3));
}

const RISK_LEVEL_LABEL = Object.fromEntries(RISK_LEVEL_OPTIONS);

/* 条目卡（需求点 / 风险 / 测试点）默认折叠成一行纯文本，点「展开」才出现输入框：这类条目
   常有几十条，每条都摆一个说明框会把整页淹掉。折叠机制在 common.js（setCardCollapsed），
   本页只提供摘要写法——收起时由 common.js 通过卡片上的 syncSummary 回调触发。 */
function syncCardSummary(card) {
    const value = (sub) => {
        const el = card.querySelector(`[data-sub="${sub}"]`);
        return el ? (el.value || "").trim() : "";
    };
    const id = card.querySelector(".edit-summary-id");
    const title = card.querySelector(".edit-summary-title");
    const desc = card.querySelector(".edit-summary-desc");
    const level = card.querySelector(".edit-summary-level");
    const props = card.querySelector(".edit-summary-props");
    if (id) id.textContent = value("id") || "编号待分配";
    if (title) title.textContent = value("title") || "（未填标题）";
    if (desc) desc.textContent = value("description") || "（未填说明）";
    if (props) {
        // 测试点：维度 · 优先级 · 风险，折叠态一眼能看出这条点的属性
        const parts = [
            DIMENSION_LABEL[value("dimension")] || "",
            value("priority"),
            RISK_LEVEL_LABEL[value("risk_level")] || "",
        ];
        props.textContent = parts.filter(Boolean).join(" · ");
    }
    if (level) {
        const current = value("level") || "medium";
        level.textContent = RISK_LEVEL_LABEL[current] || current;
        level.classList.remove("high", "medium", "low");
        level.classList.add(current);
    }
}

/* 折叠态的一行：编号 + [等级] + [标题] + 说明摘要 + 展开 + ✕（风险没有标题，不带标题项） */
function buildItemSummary(item, { withLevel = false, withTitle = true } = {}) {
    const summary = document.createElement("div");
    summary.className = "edit-summary";
    summary.appendChild(summarySpan("edit-summary-id", item.id || "编号待分配"));
    if (withLevel) summary.appendChild(summarySpan("tag edit-summary-level medium", ""));
    if (withTitle) summary.appendChild(summarySpan("edit-summary-title", item.title || "（未填标题）"));
    summary.appendChild(summarySpan("edit-summary-desc", item.description || "（未填说明）"));
    return summary;
}

function buildRequirementCard(item) {
    const card = document.createElement("div");
    card.className = "edit-card collapsed";
    const summary = buildItemSummary(item);
    summary.appendChild(buildCollapseToggle(card));
    summary.appendChild(buildRowRemoveButton(card));
    card.appendChild(summary);

    const expanded = document.createElement("div");
    expanded.className = "edit-body";
    const head = document.createElement("div");
    head.className = "edit-head";
    head.appendChild(editInput("id", item.id || "", "edit-input edit-id", "REQ-001"));
    head.appendChild(editInput("title", item.title || "", "edit-input edit-title", "需求点标题"));
    head.appendChild(buildCollapseToggle(card));
    head.appendChild(buildRowRemoveButton(card));
    expanded.appendChild(head);
    expanded.appendChild(
        editField("需求说明", editTextarea("description", item.description || "", "edit-input", "这条需求要求什么", 2))
    );
    card.appendChild(expanded);

    card.syncSummary = syncCardSummary;  // 收起时由 common.js 回调，把最新值写回摘要行
    setCardCollapsed(card, true);
    return card;
}

function buildRiskCard(item) {
    const card = document.createElement("div");
    card.className = "edit-card collapsed";
    const summary = buildItemSummary(item, { withLevel: true, withTitle: false });
    summary.appendChild(buildCollapseToggle(card));
    summary.appendChild(buildRowRemoveButton(card));
    card.appendChild(summary);

    const expanded = document.createElement("div");
    expanded.className = "edit-body";
    const head = document.createElement("div");
    head.className = "edit-head";
    head.appendChild(editInput("id", item.id || "", "edit-input edit-id", "RISK-001"));
    head.appendChild(editField("等级", editSelect("level", RISK_LEVEL_OPTIONS, item.level || "medium"), true));
    head.appendChild(buildCollapseToggle(card));
    head.appendChild(buildRowRemoveButton(card));
    expanded.appendChild(head);
    expanded.appendChild(
        editField("风险描述", editTextarea("description", item.description || "", "edit-input", "这个风险是什么", 2))
    );
    card.appendChild(expanded);

    card.syncSummary = syncCardSummary;  // 收起时由 common.js 回调，把最新值写回摘要行
    setCardCollapsed(card, true);
    return card;
}

function allocateId(existing, prefix, index) {
    let n = index + 1;
    let id = `${prefix}-${String(n).padStart(3, "0")}`;
    while (existing.has(id)) {
        n += 1;
        id = `${prefix}-${String(n).padStart(3, "0")}`;
    }
    return id;
}

function collectFormAnalysis() {
    const form = elements.analysisForm;
    // 以原产物为基底，只覆盖表单管理的字段：schema 新增字段（或 JSON 模式里手写的额外字段）
    // 不会被表单保存悄悄抹掉——此前表单漏了 roles / constraints / preconditions 三项
    const artifact = latestArtifact(appState.currentWorkflow, "requirement_analysis");
    const base = artifact && artifact.content;
    const analysis = base && typeof base === "object" ? JSON.parse(JSON.stringify(base)) : {};

    const value = (sub) => {
        const el = form.querySelector(`[data-sub="${sub}"]`);
        return el ? (el.value || "").trim() : "";
    };
    // 列表类字段：每个非空行一条
    const lines = (sub) =>
        value(sub)
            .split("\n")
            .map((line) => line.trim())
            .filter(Boolean);

    analysis.summary = value("summary");
    analysis.scope = lines("scope");
    analysis.roles = lines("roles");
    analysis.business_rules = lines("business_rules");
    analysis.constraints = lines("constraints");
    analysis.preconditions = lines("preconditions");
    analysis.acceptance_criteria = lines("acceptance_criteria");
    analysis.assumptions = lines("assumptions");

    analysis.functional_requirements = [];
    const usedIds = new Set();
    form
        .querySelectorAll('.edit-reqs[data-field="functional_requirements"] .edit-card')
        .forEach((card, index) => {
            const title = card.querySelector('[data-sub="title"]').value.trim();
            const description = card.querySelector('[data-sub="description"]').value.trim();
            if (!title && !description) return;
            let id = card.querySelector('[data-sub="id"]').value.trim();
            if (!id || usedIds.has(id)) id = allocateId(usedIds, "REQ", index);
            usedIds.add(id);
            analysis.functional_requirements.push({ id, title, description });
        });

    analysis.risks = [];
    const usedRiskIds = new Set();
    form.querySelectorAll('.edit-reqs[data-field="risks"] .edit-card').forEach((card, index) => {
        const description = card.querySelector('[data-sub="description"]').value.trim();
        if (!description) return;
        let id = card.querySelector('[data-sub="id"]').value.trim();
        if (!id || usedRiskIds.has(id)) id = allocateId(usedRiskIds, "RISK", index);
        usedRiskIds.add(id);
        analysis.risks.push({
            id,
            description,
            level: card.querySelector('[data-sub="level"]').value || "medium",
        });
    });

    return analysis;
}


/* ---------- 任务流程动作 ---------- */


async function startWorkflow(workflowId) {
    if (appState.isStreaming) {
        alert("当前有测试工作流正在运行，请稍候");
        return;
    }
    setStreamingUI(true);
    beginRuntimeTracking();
    await streamEvents(`/api/workflows/${workflowId}/start`, {
        method: "POST",
    });
}

async function approveWorkflow() {
    const workflowId = appState.currentWorkflowId;
    if (!workflowId || appState.isStreaming) return;

    const workflow = appState.currentWorkflow;
    const gate = currentGate(workflow);
    // 当前这次提交针对哪个产物：重生成模式看目标，否则看停在哪个确认点
    const target = appState.regenerateMode
        ? appState.regenerateTarget
        : gate === "test_points"
          ? "test_points"
          : "analysis";

    let payload = {};
    if (target === "test_points") {
        let testPointSet = null;
        if (appState.editing && appState.editingTarget === "test_points") {
            testPointSet = collectFormTestPoints();
            if (!testPointSet.test_points.length) {
                alert("请至少保留一个测试点");
                return;
            }
        }
        payload.test_point_set = testPointSet;
        if (appState.regenerateMode) payload.analysis = null;
    } else {
        let analysis = null;
        if (appState.editing && appState.editingTarget === "analysis") {
            if (appState.editMode === "form") {
                analysis = collectFormAnalysis();
                if (!analysis.summary) {
                    alert("请填写需求概述");
                    return;
                }
            } else {
                const raw = elements.analysisEditor.value.trim();
                if (raw) {
                    try {
                        analysis = JSON.parse(raw);
                    } catch (e) {
                        alert("需求分析 JSON 格式有误，请检查后重试");
                        return;
                    }
                }
            }
        }
        payload.analysis = analysis;
    }

    if (appState.regenerateMode) payload.target = target;

    const url = appState.regenerateMode
        ? `/api/workflows/${workflowId}/regenerate`
        : `/api/workflows/${workflowId}/approve`;

    setStreamingUI(true);
    beginRuntimeTracking();
    if (appState.regenerateMode) {
        // 重生成只重跑确认点之后的节点；显式指定 spinner 位置
        appState.runtime.loading =
            target === "cases" ? "test_case_generation_agent" : "human_review_design";
        renderSteps(appState.currentWorkflow);
    }
    // 确认后回到顶部，方便观察步骤条的状态变化（滚动在整页，不再由卡片内部滚动）
    window.scrollTo({ top: 0, behavior: "smooth" });
    await streamEvents(url, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload),
    });
}

function enterRegenerateMode(target = "cases") {
    if (appState.isStreaming) return;
    const workflow = appState.currentWorkflow;
    if (!workflow || !latestArtifact(workflow, "requirement_analysis")) return;

    appState.regenerateMode = true;
    appState.regenerateTarget = target;
    appState.editing = false;

    if (target === "cases") {
        appState.activeArtifactNode = "human_review_design";
        const points = latestArtifact(workflow, "test_point_set");
        if (points) {
            // 直接进入表单编辑，方便用户调整测试点后重跑用例
            appState.editing = true;
            appState.editingTarget = "test_points";
            buildTestPointForm(points.content);
        }
        renderDetail(workflow);
        elements.testPointPanel.scrollIntoView({ behavior: "smooth", block: "start" });
    } else {
        appState.activeArtifactNode = "human_review";
        appState.editing = true;
        appState.editingTarget = "analysis";
        appState.editMode = "form";
        buildAnalysisForm(latestArtifact(workflow, "requirement_analysis").content);
        renderDetail(workflow);
        elements.reviewPanel.scrollIntoView({ behavior: "smooth", block: "start" });
    }
}

// 事件流断线重连：服务端每 15s 发一条注释行保活，正常不会走到这里；
// 真的被代理/网络掐断时，后台运行不受影响，且晚接入的订阅会重放该运行已发的事件
// （/start 对运行中的任务是幂等的：返回既有运行并重放），所以续订对用户是无感的。
const STREAM_MAX_ATTEMPTS = 3;
const STREAM_RETRY_DELAY_MS = 1200;

async function streamEvents(url, options) {
    let requestUrl = url;
    let requestOptions = options;
    try {
        for (let attempt = 1; attempt <= STREAM_MAX_ATTEMPTS; attempt++) {
            try {
                await readEventStream(requestUrl, requestOptions);
                return;
            } catch (error) {
                console.warn(`事件流第 ${attempt}/${STREAM_MAX_ATTEMPTS} 次中断:`, error);
                if (attempt === STREAM_MAX_ATTEMPTS) {
                    alert("订阅连接中断；测试工作流仍在后台执行，稍后刷新页面即可查看最新进度");
                    return;
                }
                await new Promise((resolve) => setTimeout(resolve, STREAM_RETRY_DELAY_MS * attempt));
                // 续订前先看任务是否还在跑：已经跑完/停在确认点时不必再连，
                // 直接返回让下面 finally 里的详情刷新渲染最终状态
                const workflow = await fetchWorkflowStatus();
                if (!workflow || !["analyzing", "generating"].includes(workflow.status)) return;
                // 只走 /start 续订（幂等）：绝不对 approve/regenerate 这类有副作用的入口重发
                requestUrl = `/api/workflows/${workflow.id}/start`;
                requestOptions = { method: "POST" };
            }
        }
    } finally {
        await finishStreaming();
    }
}

/** 静默拉一次任务详情（只更新状态，不动界面）：用于判断续订是否还有意义 */
async function fetchWorkflowStatus() {
    try {
        const response = await fetch(`/api/workflows/${appState.currentWorkflowId}`);
        if (response.status === 401) {
            redirectToLogin();
            return null;
        }
        if (!response.ok) return null;
        const workflow = await response.json();
        appState.currentWorkflowId = workflow.id;
        appState.currentWorkflow = workflow;
        return workflow;
    } catch (error) {
        console.warn("续订前查询状态失败:", error);
        return null;
    }
}

async function readEventStream(url, options) {
    const response = await fetch(url, options);
    if (response.status === 401) return redirectToLogin();
    if (!response.ok) {
        const err = await response.json().catch(() => ({}));
        alert(err.error || "请求失败，请稍后重试");
        return;
    }

    const reader = response.body.getReader();
    const decoder = new TextDecoder("utf-8");
    // 跨 chunk 缓冲：事件被网络切成多段时先拼接，只解析完整事件
    let buffer = "";

    const handleEvent = (event) => {
        if (!event.startsWith("data: ")) return false;
        const dataStr = event.replace("data: ", "").trim();
        if (dataStr === "[DONE]") return true;
        try {
            handleWorkflowEvent(JSON.parse(dataStr));
        } catch (e) {
            console.error("解析事件失败:", e);
        }
        return false;
    };

    while (true) {
        const { value, done } = await reader.read();
        if (done) break;

        buffer += decoder.decode(value, { stream: true });
        const events = buffer.split("\n\n");
        // 最后一段可能不完整，留到下一轮拼接
        buffer = events.pop();

        let stopped = false;
        for (const event of events) {
            if (handleEvent(event)) {
                stopped = true;
                break;
            }
        }
        if (stopped) return;
    }

    // 流结束时处理缓冲中残留的最后一个事件
    const leftover = buffer.trim();
    if (leftover) handleEvent(leftover);
}

/** 事件流收尾：运行结束后重新拉一次详情，把界面切到最终状态 */
async function finishStreaming() {
    appState.runtime = null;
    appState.regenerateMode = false;
    appState.regenerateTarget = "cases";
    appState.editing = false;
    appState.editingTarget = "analysis";
    setStreamingUI(false);
    if (!appState.currentWorkflowId) return;
    // 运行结束后重新拉一次详情（不再有列表需要刷新）；
    // autoResume:false 防止续跑流本身失败时形成"失败→自动续跑→失败"的循环
    await loadWorkflowDetail({ autoResume: false });
    const status = appState.currentWorkflow ? appState.currentWorkflow.status : null;
    if (status === "completed") {
        appState.activeArtifactNode = "test_case_generation_agent";
        showArtifactPanel(appState.activeArtifactNode);
    } else if (status === "waiting_review") {
        // 停在哪个确认点由 current_step 决定（两个 gate 的状态都是 waiting_review）
        appState.activeArtifactNode =
            toStep(appState.currentWorkflow.current_step) || "human_review";
        showArtifactPanel(appState.activeArtifactNode);
    }
}

function handleWorkflowEvent(data) {
    if (data.event === "node_done" && data.node) {
        const step = toStep(data.node);
        if (appState.runtime) {
            appState.runtime.done.add(step);
            appState.runtime.loading =
                STEP_ORDER.find((s) => !appState.runtime.done.has(s)) || null;
            renderSteps(appState.currentWorkflow);
        }
        if (data.artifact) mergeStreamArtifact(data.artifact);
        // 节点完成即切换到该节点的产物并立即渲染，跟随运行进度
        if (data.node !== "load_requirement") {
            appState.activeArtifactNode = data.node;
            renderAllPanelContents(appState.currentWorkflow);
            showArtifactPanel(data.node);
        }
    } else if (data.event === "waiting_review") {
        const reviewType = data.review_type === "test_point_review" ? "测试点" : "需求分析";
        console.info(`${reviewType}已完成，等待人工确认`);
    } else if (data.event === "failed" && data.error) {
        alert(`运行失败：${data.error}`);
    }
}

function mergeStreamArtifact(artifact) {
    // 把流中下发的节点产物合并进当前任务对象，让面板无需等详情刷新即可渲染。
    // 同一类型只保留一条：重连/重放会重复收到 node_done，追加会产生重复产物；
    // SSE 载荷本身不带版本号（服务端在落库时自增），因此不再客户端伪造 max+1。
    const workflow = appState.currentWorkflow;
    if (!workflow || !artifact) return;
    if (!Array.isArray(workflow.artifacts)) workflow.artifacts = [];
    const existing = workflow.artifacts.find(
        (a) => a.artifact_type === artifact.artifact_type
    );
    if (existing) {
        existing.content = artifact.content;
        if (artifact.version) existing.version = artifact.version;
        return;
    }
    workflow.artifacts.push({
        artifact_type: artifact.artifact_type,
        version: artifact.version || 1,
        parent_artifact_id: null,
        content: artifact.content,
    });
}

function setStreamingUI(streaming) {
    if (typeof streaming === "boolean") appState.isStreaming = streaming;
    const busy = appState.isStreaming;
    elements.approveBtn.disabled = busy;
    elements.editBtn.disabled = busy;
    elements.switchEditorBtn.disabled = busy;
    elements.cancelEditBtn.disabled = busy;
    elements.tpEditBtn.disabled = busy;
    elements.tpCancelEditBtn.disabled = busy;
    elements.tpApproveBtn.disabled = busy;
    elements.regenerateBtn.disabled = busy;
    elements.regeneratePointsBtn.disabled = busy;
    // 用例产物相关的动作在运行期间一并禁掉：重新生成时旧产物还在，此时导出/保存拿到的是上一轮结果
    // （服务端对保存另有一道"运行已结束"的门槛，这里只是别让用户白点一次）
    elements.exportBtn.disabled = busy;
    elements.publishBtn.disabled = busy;
    elements.startBtn.disabled = busy;
}

function exportCsv() {
    if (appState.currentWorkflowId) {
        window.location.href = `/api/workflows/${appState.currentWorkflowId}/export`;
    }
}

// 把当前任务最新用例保存到可长期管理的测试用例集（同一任务重复保存由服务端追加新版本）
async function publishTestSet() {
    if (!appState.currentWorkflowId) return;

    // 未跑完的任务手里只有已生成的部分用例：先讲清楚，别让人以为存下的是完整用例集
    const workflow = appState.currentWorkflow;
    if (workflow && workflow.status === "failed") {
        const artifact = latestArtifact(workflow, "test_case_set");
        const count = ((artifact && artifact.content && artifact.content.test_cases) || []).length;
        if (!confirm(`该测试工作流未跑完，将只保存当前已生成的 ${count} 条用例。是否继续？`)) return;
    }

    elements.publishBtn.disabled = true;
    try {
        const response = await fetch("/api/test-sets/publish", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ workflow_id: appState.currentWorkflowId }),
        });
        if (response.status === 401) return redirectToLogin();
        const data = await response.json().catch(() => ({}));
        if (!response.ok) {
            alert(data.error || "保存失败，请稍后重试");
            return;
        }
        const setName = data.test_set?.name || "";
        const version = data.version?.version ?? 1;
        if (confirm(`已保存到测试用例集「${setName}」（v${version}）。是否前往测试用例集查看？`)) {
            window.location.href = `/testbench-detail?set_id=${data.test_set.id}`;
        }
    } catch (error) {
        console.error("保存到用例集失败:", error);
        alert("保存失败，请稍后重试");
    } finally {
        elements.publishBtn.disabled = false;
    }
}