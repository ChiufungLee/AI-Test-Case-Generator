/* 测试任务页面逻辑：新建 → SSE 跟踪节点进度（含 loading）→ 两个人工确认点（需求分析 / 测试点与设计策略）→ 按节点查看产物 → 重新生成 */

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

// 需求文档上传：与服务端 services/knowledge_service.py 保持一致（类型、大小、正文字符上限）
const REQUIREMENT_DOC_EXTENSIONS = [".pdf", ".docx", ".txt", ".md"];
const REQUIREMENT_FILE_MAX_BYTES = 50 * 1024 * 1024;
const REQUIREMENT_TEXT_MAX_CHARS = 50000;

const appState = {
    currentWorkflowId: null,
    currentWorkflow: null,
    workflows: [],
    isStreaming: false,
    // 流式运行期的步骤状态：done 已完成节点集合，loading 当前进行中的节点
    runtime: null,
    // 当前在右侧展示产物的节点（点击步骤条切换）
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
    // 需求文档上传：已解析成功的来源文件名（创建任务时随请求上报）与最近一次解析的正文
    requirementFileName: null,
    requirementTextFromFile: null,
    requirementParsing: false,
};

const elements = {};

document.addEventListener("DOMContentLoaded", init);

async function init() {
    [
        "workflowList", "workflowName", "workflowRequirement", "workflowKb",
        "createWorkflowBtn", "emptyHint", "detailView", "wfTitle", "wfStatus",
        "wfError", "stepBar", "workflowMain", "requirementPanel", "requirementView",
        "requirementUploadBtn", "requirementFileInput", "requirementFileBar",
        "requirementUploadStatus", "requirementSource",
        "retrievedPanel", "retrievedVersion", "retrievedView",
        "reviewPanel", "analysisVersion", "analysisView", "analysisForm",
        "analysisEditor", "reviewActions", "editBtn", "switchEditorBtn",
        "cancelEditBtn", "approveBtn",
        "testPointPanel", "testPointVersion", "testPointPlanVersion", "testPointView",
        "testPointForm", "testPointActions", "tpEditBtn", "tpCancelEditBtn", "tpApproveBtn",
        "casesPanel", "casesVersion", "casesTable", "regenerateBtn", "regeneratePointsBtn",
        "exportBtn", "publishBtn", "casesTruncatedWarn", "casesFailedWarn",
        "coveragePanel", "coverageVersion", "coverageView",
    ].forEach((id) => {
        elements[id] = document.getElementById(id);
    });

    elements.createWorkflowBtn.addEventListener("click", createWorkflow);
    elements.requirementUploadBtn.addEventListener("click", () => elements.requirementFileInput.click());
    elements.requirementFileInput.addEventListener("change", handleRequirementFileSelect);
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

    elements.stepBar.querySelectorAll("li").forEach((li) => {
        li.addEventListener("click", () => selectArtifactNode(li.dataset.step));
    });

    await loadKnowledgeBases();
    await loadWorkflows();
}

// 公共工具（redirectToLogin 等）来自 common.js

async function loadKnowledgeBases() {
    try {
        const response = await fetch("/api/knowledge-bases/");
        if (response.status === 401) return redirectToLogin();
        if (!response.ok) return;
        const knowledgeBases = await response.json();
        knowledgeBases.forEach((kb) => {
            const option = document.createElement("option");
            option.value = kb.id;
            option.textContent = (kb.visibility === "shared" ? "【共享】 " : "") + kb.name;
            elements.workflowKb.appendChild(option);
        });
    } catch (error) {
        console.error("加载知识库失败:", error);
    }
}

/* ---------- 需求文档上传（新建面板） ---------- */

function handleRequirementFileSelect(event) {
    const file = event.target.files && event.target.files[0];
    // 立即清空 value：同一文件二次选择（改完文档重传）也要触发 change
    event.target.value = "";
    if (file) parseRequirementDocument(file);
}

function setRequirementUploadStatus(message, type = "info") {
    const status = elements.requirementUploadStatus;
    if (!message) {
        status.hidden = true;
        status.textContent = "";
        status.className = "upload-status";
        return;
    }
    status.textContent = message;
    status.className = `upload-status upload-status-${type}`;
    status.hidden = false;
}

function renderRequirementFileBar() {
    const bar = elements.requirementFileBar;
    bar.innerHTML = "";
    if (!appState.requirementFileName) {
        bar.hidden = true;
        return;
    }

    const chip = document.createElement("div");
    chip.className = "requirement-chip";

    const icon = document.createElement("span");
    icon.textContent = "📄";

    const name = document.createElement("span");
    name.className = "requirement-chip-name";
    name.textContent = appState.requirementFileName;
    name.title = appState.requirementFileName;

    const removeBtn = document.createElement("button");
    removeBtn.type = "button";
    removeBtn.className = "requirement-chip-remove";
    removeBtn.setAttribute("aria-label", "移除需求文档标记");
    removeBtn.textContent = "✕";
    removeBtn.addEventListener("click", () => clearRequirementFile("已移除文档标记，需求原文内容保留"));

    chip.appendChild(icon);
    chip.appendChild(name);
    chip.appendChild(removeBtn);
    bar.appendChild(chip);
    bar.hidden = false;
}

// 只清「来源文档」标记（可选提示文案）；需求原文文本框的内容始终保留给用户处置
function clearRequirementFile(statusMessage) {
    appState.requirementFileName = null;
    appState.requirementTextFromFile = null;
    renderRequirementFileBar();
    setRequirementUploadStatus(statusMessage || null);
}

function fillRequirementText(text) {
    const area = elements.workflowRequirement;
    const current = area.value.trim();
    const previousFromFile = (appState.requirementTextFromFile || "").trim();

    if (current && current !== previousFromFile) {
        // 已有手工粘贴/编辑过的正文：不覆盖用户输入，文档正文接在其后
        area.value = `${current}\n\n${text}`;
    } else {
        // 空白或仍是上一次解析结果（未被编辑）：直接替换，避免重复堆叠
        area.value = text;
    }
    appState.requirementTextFromFile = text;
}

async function parseRequirementDocument(file) {
    if (appState.requirementParsing) return;

    const dotIndex = file.name.lastIndexOf(".");
    const ext = dotIndex === -1 ? "" : file.name.slice(dotIndex).toLowerCase();
    if (!REQUIREMENT_DOC_EXTENSIONS.includes(ext)) {
        setRequirementUploadStatus("仅支持 PDF、Word(.docx)、TXT、Markdown 需求文档", "error");
        return;
    }
    if (file.size > REQUIREMENT_FILE_MAX_BYTES) {
        setRequirementUploadStatus("文件大小不能超过 50MB", "error");
        return;
    }

    appState.requirementParsing = true;
    elements.requirementUploadBtn.disabled = true;
    setRequirementUploadStatus(`正在解析《${file.name}》，大文档请稍候…`, "info");

    try {
        const formData = new FormData();
        formData.append("file", file);
        const response = await apiFetch("/api/workflows/requirement-extract", {
            method: "POST",
            body: formData,
        });
        if (!response.ok) {
            const err = await response.json().catch(() => ({}));
            setRequirementUploadStatus(err.error || "需求文档解析失败，请重试", "error");
            return;
        }

        const data = await response.json();
        fillRequirementText(data.text);
        appState.requirementFileName = data.filename || file.name;
        renderRequirementFileBar();

        const truncatedNote = data.truncated
            ? `；文档过长，仅保留前 ${REQUIREMENT_TEXT_MAX_CHARS} 字符`
            : "";
        // 原文档的符号字体/复选框等解析不出字形，只会显示成方框，提取时已剔除
        const droppedNote = data.dropped_chars > 0
            ? `；已忽略 ${data.dropped_chars} 个无字形的符号`
            : "";
        setRequirementUploadStatus(
            `已解析《${appState.requirementFileName}》（${data.chars} 字符）并回填到需求原文${truncatedNote}${droppedNote}，可编辑后创建任务`,
            data.truncated || data.dropped_chars > 0 ? "warn" : "ok"
        );
    } catch (error) {
        if (isUnauthorized(error)) return;
        console.error("需求文档解析失败:", error);
        setRequirementUploadStatus("需求文档解析失败，请重试", "error");
    } finally {
        appState.requirementParsing = false;
        elements.requirementUploadBtn.disabled = false;
    }
}

async function loadWorkflows() {
    try {
        const response = await fetch("/api/workflows");
        if (response.status === 401) return redirectToLogin();
        if (!response.ok) throw new Error(`HTTP ${response.status}`);
        const data = await response.json();
        appState.workflows = data.workflows || [];
        renderWorkflowList(appState.workflows);

        // 首次加载时若有任务正在运行（如刷新页面中断的运行），自动打开并续跑
        if (!appState.currentWorkflowId && !appState.isStreaming) {
            const running = appState.workflows.find((w) =>
                ["analyzing", "generating"].includes(w.status)
            );
            if (running) await selectWorkflow(running.id);
        }
    } catch (error) {
        console.error("加载任务列表失败:", error);
        elements.workflowList.innerHTML = '<li class="empty">加载失败</li>';
    }
}

function renderWorkflowList(workflows) {
    elements.workflowList.innerHTML = "";
    if (!workflows.length) {
        elements.workflowList.innerHTML = '<li class="empty">暂无任务，先创建一个吧</li>';
        return;
    }
    workflows.forEach((wf) => {
        const li = document.createElement("li");
        if (wf.id === appState.currentWorkflowId) li.classList.add("active");
        const name = document.createElement("span");
        name.className = "wf-name";
        name.textContent = wf.name || "未命名任务";
        const badge = document.createElement("span");
        badge.className = `status-badge status-${wf.status}`;
        badge.textContent = STATUS_TEXT[wf.status] || wf.status;
        li.appendChild(name);
        li.appendChild(badge);
        li.addEventListener("click", () => selectWorkflow(wf.id));
        elements.workflowList.appendChild(li);
    });
}

async function selectWorkflow(workflowId, opts = {}) {
    if (appState.isStreaming) {
        // 静默 return 会让用户以为点击失效：运行期切换任务给出明确反馈
        if (opts.autoResume !== false) alert("当前有任务正在运行，请稍候再切换");
        return;
    }
    try {
        const response = await fetch(`/api/workflows/${workflowId}`);
        if (response.status === 401) return redirectToLogin();
        if (!response.ok) throw new Error(`HTTP ${response.status}`);
        const workflow = await response.json();
        const switched = appState.currentWorkflowId !== workflowId;
        appState.currentWorkflowId = workflowId;
        appState.currentWorkflow = workflow;
        appState.regenerateMode = false;
        appState.regenerateTarget = "cases";
        appState.editing = false;
        appState.editingTarget = "analysis";
        if (switched) appState.activeArtifactNode = null;
        renderDetail(workflow);
        await loadWorkflows();

        // 任务处于运行中（如刷新页面导致流中断）时自动续跑；每个任务每次页面加载只尝试一次
        if (
            opts.autoResume !== false &&
            ["analyzing", "generating"].includes(workflow.status) &&
            !appState.autoResumed.has(workflowId)
        ) {
            appState.autoResumed.add(workflowId);
            await resumeWorkflow(workflowId);
        }
    } catch (error) {
        console.error("加载任务详情失败:", error);
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
    elements.emptyHint.hidden = true;
    elements.detailView.hidden = false;
    elements.wfTitle.textContent = workflow.name || "未命名任务";

    const statusBadge = elements.wfStatus;
    statusBadge.className = `status-badge status-${workflow.status}`;
    statusBadge.textContent = STATUS_TEXT[workflow.status] || workflow.status;

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
            elements.casesFailedWarn.textContent = `⚠ 以下测试点的用例生成失败，建议重新生成：${failedPoints.join("、")}`;
            elements.casesFailedWarn.hidden = false;
        } else {
            elements.casesFailedWarn.hidden = true;
        }
        fillCasesTable(casesArtifact.content.test_cases || []);
        elements.exportBtn.hidden = workflow.status !== "completed";
        elements.publishBtn.hidden = workflow.status !== "completed";
        const canRegenerate = ["completed", "failed"].includes(workflow.status);
        elements.regenerateBtn.hidden = !canRegenerate;
        elements.regeneratePointsBtn.hidden = !canRegenerate;
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

    // 状态徽章与列表立即切到"进行中"，不等 SSE 结束
    if (workflow) {
        workflow.status = "analyzing";
    }
    const entry = appState.workflows.find((w) => w.id === appState.currentWorkflowId);
    if (entry) entry.status = "analyzing";
    renderWorkflowList(appState.workflows);
    elements.wfStatus.className = "status-badge status-analyzing";
    elements.wfStatus.textContent = STATUS_TEXT.analyzing;

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

function tpInput(sub, value, className, placeholder) {
    const input = document.createElement("input");
    input.type = "text";
    input.className = className;
    input.dataset.sub = sub;
    input.value = value;
    if (placeholder) input.placeholder = placeholder;
    return input;
}

function tpSelect(sub, options, value) {
    const select = document.createElement("select");
    select.className = "tp-select";
    select.dataset.sub = sub;
    options.forEach(([optionValue, label]) => {
        const option = document.createElement("option");
        option.value = optionValue;
        option.textContent = label;
        if (optionValue === value) option.selected = true;
        select.appendChild(option);
    });
    return select;
}

function buildTestPointRow(point) {
    const wrap = document.createElement("div");
    wrap.className = "edit-req tp-row";

    const line = document.createElement("div");
    line.className = "req-line";
    line.appendChild(tpInput("id", point.id || "", "tp-id", "TP-001"));
    line.appendChild(tpInput("title", point.title || "", "tp-title", "测试点标题"));
    line.appendChild(
        tpSelect(
            "dimension",
            DIMENSION_OPTIONS,
            point.dimension || "function"
        )
    );
    line.appendChild(tpSelect("priority", [["P0", "P0"], ["P1", "P1"], ["P2", "P2"]], point.priority || "P1"));
    line.appendChild(
        tpSelect(
            "risk_level",
            [["high", "high"], ["medium", "medium"], ["low", "low"]],
            point.risk_level || "medium"
        )
    );
    line.appendChild(buildRemoveButtonFor(wrap));
    wrap.appendChild(line);
    wrap.appendChild(tpInput("description", point.description || "", "tp-desc", "要验证的可测方面"));
    wrap.appendChild(
        tpInput(
            "requirement_refs",
            (point.requirement_refs || []).join(", "),
            "tp-refs",
            "关联需求点编号，如 REQ-001, REQ-002"
        )
    );
    wrap.appendChild(tpInput("rationale", point.rationale || "", "tp-rationale", "为什么需要这个测试点"));
    return wrap;
}

function buildTestPointForm(content) {
    const form = elements.testPointForm;
    form.innerHTML = "";

    const hint = document.createElement("p");
    hint.className = "edit-hint";
    hint.textContent =
        "直接编辑测试点；需求引用必须真实存在，否则确认时会被拒绝。设计策略由系统按测试点自动重算，不接受手工编辑。";
    form.appendChild(hint);

    form.appendChild(formLabel("覆盖目标"));
    const target = document.createElement("input");
    target.type = "text";
    target.className = "tp-input";
    target.dataset.field = "coverage_target";
    target.value = content.coverage_target || "";
    form.appendChild(target);

    form.appendChild(formLabel("测试点"));
    const wrap = document.createElement("div");
    wrap.className = "edit-reqs";
    wrap.dataset.field = "test_points";
    (content.test_points || []).forEach((point) => wrap.appendChild(buildTestPointRow(point)));
    form.appendChild(wrap);
    form.appendChild(
        buildAddButton("＋ 添加测试点", () => wrap.appendChild(buildTestPointRow({})))
    );
}

function collectFormTestPoints() {
    const form = elements.testPointForm;
    const targetEl = form.querySelector('[data-field="coverage_target"]');
    const points = [];
    const usedIds = new Set();

    form
        .querySelectorAll('.edit-reqs[data-field="test_points"] .tp-row')
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
        failedLine.textContent = `用例生成失败的测试点（建议重新生成）：${failedPoints.join("、")}`;
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
        "点击任意文字直接修改；列表内按 Enter 新增一条，行尾 ✕ 删除；完成后点「确认并继续」。";
    form.appendChild(hint);

    form.appendChild(formLabel("需求概述"));
    form.appendChild(buildEditableBlock("summary", analysis.summary || "", "analysis-summary"));

    const listSections = [
        ["scope", "测试范围"],
        ["business_rules", "业务规则"],
        ["acceptance_criteria", "验收标准"],
        ["assumptions", "假设与待确认项"],
    ];
    listSections.forEach(([field, label]) => {
        form.appendChild(formLabel(label));
        form.appendChild(buildEditableList(field, analysis[field] || []));
    });

    form.appendChild(formLabel("功能需求点（编号留空将自动分配，供用例追溯引用）"));
    const reqWrap = document.createElement("div");
    reqWrap.className = "edit-reqs";
    reqWrap.dataset.field = "functional_requirements";
    (analysis.functional_requirements || []).forEach((item) =>
        reqWrap.appendChild(buildRequirementItem(item))
    );
    form.appendChild(reqWrap);
    form.appendChild(buildAddButton("＋ 添加需求点", () =>
        reqWrap.appendChild(buildRequirementItem({}))
    ));

    form.appendChild(formLabel("风险（点击等级标签切换严重程度）"));
    const riskWrap = document.createElement("div");
    riskWrap.className = "edit-reqs";
    riskWrap.dataset.field = "risks";
    (analysis.risks || []).forEach((item) => riskWrap.appendChild(buildRiskItem(item)));
    form.appendChild(riskWrap);
    form.appendChild(buildAddButton("＋ 添加风险", () => riskWrap.appendChild(buildRiskItem({}))));
}

function preventEnter(e) {
    if (e.key === "Enter") e.preventDefault();
}

function makeEditable(text, className, dataset = {}) {
    const el = document.createElement("div");
    el.className = className;
    el.contentEditable = "plaintext-only";
    if (el.contentEditable !== "plaintext-only") el.contentEditable = "true";
    el.textContent = text;
    Object.entries(dataset).forEach(([k, v]) => {
        el.dataset[k] = v;
    });
    return el;
}

function buildEditableBlock(field, text, className) {
    const el = makeEditable(text, className, { field });
    el.addEventListener("keydown", preventEnter);
    return el;
}

function buildEditableList(field, items) {
    const ul = document.createElement("ul");
    ul.className = "edit-list";
    ul.dataset.field = field;
    items.forEach((text) => ul.appendChild(buildListLine(text)));
    if (!items.length) ul.appendChild(buildListLine(""));
    return ul;
}

function buildListLine(text) {
    const li = document.createElement("li");
    const span = document.createElement("span");
    span.className = "edit-line";
    span.contentEditable = "plaintext-only";
    if (span.contentEditable !== "plaintext-only") span.contentEditable = "true";
    span.textContent = text;
    span.addEventListener("keydown", (e) => {
        if (e.key === "Enter") {
            // Enter 在下方新增一条并聚焦，符合"每行一条"的直觉
            e.preventDefault();
            const newline = buildListLine("");
            li.after(newline);
            newline.querySelector(".edit-line").focus();
        } else if (e.key === "Backspace" && span.textContent.trim() === "") {
            e.preventDefault();
            removeListLine(li);
        }
    });
    li.appendChild(span);
    const remove = document.createElement("button");
    remove.type = "button";
    remove.className = "row-remove";
    remove.title = "删除该条";
    remove.textContent = "✕";
    remove.addEventListener("click", () => removeListLine(li));
    li.appendChild(remove);
    return li;
}

function removeListLine(li) {
    const list = li.parentElement;
    // 列表至少保留一行占位，避免删空后无法再添加
    if (list && list.querySelectorAll("li").length <= 1) return;
    const prev = li.previousElementSibling;
    li.remove();
    if (prev) prev.querySelector(".edit-line").focus();
}

function buildRemoveButtonFor(target) {
    const remove = document.createElement("button");
    remove.type = "button";
    remove.className = "row-remove";
    remove.title = "删除该条";
    remove.textContent = "✕";
    remove.addEventListener("click", () => target.remove());
    return remove;
}

function buildRequirementItem(item) {
    const wrap = document.createElement("div");
    wrap.className = "edit-req";
    const line = document.createElement("div");
    line.className = "req-line";
    const id = makeEditable(item.id || "", "req-id", { sub: "id" });
    id.addEventListener("keydown", preventEnter);
    const title = makeEditable(item.title || "", "req-title", { sub: "title" });
    title.addEventListener("keydown", preventEnter);
    const desc = makeEditable(item.description || "", "req-desc", { sub: "description" });
    desc.addEventListener("keydown", preventEnter);
    line.appendChild(id);
    line.appendChild(title);
    line.appendChild(buildRemoveButtonFor(wrap));
    wrap.appendChild(line);
    wrap.appendChild(desc);
    return wrap;
}

const RISK_LEVEL_ORDER = ["high", "medium", "low"];

function buildRiskItem(item) {
    const wrap = document.createElement("div");
    wrap.className = "edit-req";
    const line = document.createElement("div");
    line.className = "req-line";
    const id = makeEditable(item.id || "", "req-id", { sub: "id" });
    id.addEventListener("keydown", preventEnter);
    const level = document.createElement("span");
    const initialLevel = RISK_LEVEL_ORDER.includes(item.level) ? item.level : "medium";
    level.className = `risk-tag ${initialLevel}`;
    level.dataset.sub = "level";
    level.dataset.value = initialLevel;
    level.title = "点击切换严重程度";
    level.textContent = initialLevel.toUpperCase();
    level.addEventListener("click", () => {
        const next = RISK_LEVEL_ORDER[(RISK_LEVEL_ORDER.indexOf(level.dataset.value) + 1) % RISK_LEVEL_ORDER.length];
        level.dataset.value = next;
        level.className = `risk-tag ${next}`;
        level.textContent = next.toUpperCase();
    });
    const desc = makeEditable(item.description || "", "req-desc", { sub: "description" });
    desc.addEventListener("keydown", preventEnter);
    line.appendChild(id);
    line.appendChild(level);
    line.appendChild(buildRemoveButtonFor(wrap));
    wrap.appendChild(line);
    wrap.appendChild(desc);
    return wrap;
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
    const pickList = (field) => {
        const container = form.querySelector(`[data-field="${field}"]`);
        if (!container) return [];
        return Array.from(container.querySelectorAll(".edit-line"))
            .map((s) => (s.innerText || "").trim())
            .filter(Boolean);
    };

    const summaryEl = form.querySelector('[data-field="summary"]');
    const analysis = {
        summary: summaryEl ? (summaryEl.innerText || "").trim() : "",
        scope: pickList("scope"),
        functional_requirements: [],
        business_rules: pickList("business_rules"),
        acceptance_criteria: pickList("acceptance_criteria"),
        risks: [],
        assumptions: pickList("assumptions"),
    };

    const usedIds = new Set();
    form.querySelectorAll('.edit-reqs[data-field="functional_requirements"] .edit-req').forEach((row, index) => {
        const title = row.querySelector('[data-sub="title"]').innerText.trim();
        const description = row.querySelector('[data-sub="description"]').innerText.trim();
        if (!title && !description) return;
        let id = row.querySelector('[data-sub="id"]').innerText.trim();
        if (!id || usedIds.has(id)) id = allocateId(usedIds, "REQ", index);
        usedIds.add(id);
        analysis.functional_requirements.push({ id, title, description });
    });

    const usedRiskIds = new Set();
    form.querySelectorAll('.edit-reqs[data-field="risks"] .edit-req').forEach((row, index) => {
        const description = row.querySelector('[data-sub="description"]').innerText.trim();
        if (!description) return;
        let id = row.querySelector('[data-sub="id"]').innerText.trim();
        if (!id || usedRiskIds.has(id)) id = allocateId(usedRiskIds, "RISK", index);
        usedRiskIds.add(id);
        analysis.risks.push({
            id,
            description,
            level: row.querySelector('[data-sub="level"]').dataset.value || "medium",
        });
    });

    return analysis;
}

/* ---------- 任务流程动作 ---------- */

async function createWorkflow() {
    const requirement = elements.workflowRequirement.value.trim();
    if (!requirement) {
        alert("请先粘贴需求原文或上传需求文档");
        return;
    }
    if (requirement.length > REQUIREMENT_TEXT_MAX_CHARS) {
        alert(`需求原文过长（上限 ${REQUIREMENT_TEXT_MAX_CHARS} 字符），请精简后再创建`);
        return;
    }
    const payload = {
        name: elements.workflowName.value.trim(),
        requirement_text: requirement,
        knowledge_base_id: elements.workflowKb.value || null,
        requirement_file_name: appState.requirementFileName,
    };

    try {
        const response = await fetch("/api/workflows", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify(payload),
        });
        if (response.status === 401) return redirectToLogin();
        if (!response.ok) {
            const err = await response.json().catch(() => ({}));
            alert(err.error || "创建任务失败");
            return;
        }
        const workflow = await response.json();
        elements.workflowName.value = "";
        elements.workflowRequirement.value = "";
        clearRequirementFile();
        await loadWorkflows();
        await selectWorkflow(workflow.id);
        await startWorkflow(workflow.id);
    } catch (error) {
        console.error("创建任务失败:", error);
        alert("创建任务失败，请重试");
    }
}

async function startWorkflow(workflowId) {
    if (appState.isStreaming) {
        alert("当前有任务正在运行，请稍候");
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
    // 确认后回到顶部，方便观察步骤条的状态变化
    elements.workflowMain.scrollTo({ top: 0, behavior: "smooth" });
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

async function streamEvents(url, options) {
    try {
        const response = await fetch(url, options);
        if (response.status === 401) return redirectToLogin();
        if (!response.ok) {
            const err = await response.json().catch(() => ({}));
            alert(err.error || "任务请求失败");
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
                console.error("解析任务事件失败:", e);
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
    } catch (error) {
        console.error("任务流式请求异常:", error);
        alert("订阅连接中断；任务仍在后台执行，稍后刷新页面即可查看最新进度");
    } finally {
        appState.runtime = null;
        appState.regenerateMode = false;
        appState.regenerateTarget = "cases";
        appState.editing = false;
        appState.editingTarget = "analysis";
        setStreamingUI(false);
        await loadWorkflows();
        if (appState.currentWorkflowId) {
            // autoResume:false 防止续跑流本身失败时形成"失败→自动续跑→失败"的循环
            await selectWorkflow(appState.currentWorkflowId, { autoResume: false });
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
        alert(`任务失败：${data.error}`);
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
    elements.createWorkflowBtn.disabled = busy;
    elements.approveBtn.disabled = busy;
    elements.editBtn.disabled = busy;
    elements.switchEditorBtn.disabled = busy;
    elements.cancelEditBtn.disabled = busy;
    elements.tpEditBtn.disabled = busy;
    elements.tpCancelEditBtn.disabled = busy;
    elements.tpApproveBtn.disabled = busy;
    elements.regenerateBtn.disabled = busy;
    elements.regeneratePointsBtn.disabled = busy;
}

function exportCsv() {
    if (appState.currentWorkflowId) {
        window.location.href = `/api/workflows/${appState.currentWorkflowId}/export`;
    }
}

// 把当前任务最新用例发布为测试工作台可长期管理的测试用例集
async function publishTestSet() {
    if (!appState.currentWorkflowId) return;
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
            alert(data.error || "发布失败，请稍后重试");
            return;
        }
        const setName = data.test_set?.name || "";
        const version = data.version?.version ?? 1;
        if (confirm(`已发布为测试用例集「${setName}」（v${version}）。是否前往测试用例集查看？`)) {
            window.location.href = `/testbench-detail?set_id=${data.test_set.id}`;
        }
    } catch (error) {
        console.error("发布失败:", error);
        alert("发布失败，请稍后重试");
    } finally {
        elements.publishBtn.disabled = false;
    }
}
