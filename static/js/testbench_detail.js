// 测试用例集详情页：用例查看/编辑、版本历史、回滚、版本对比
const state = {
    setId: window.testCaseSetId,
    canEdit: window.canEdit === true,
    asset: null,
    currentContent: null,
    versions: [],
};

// 编辑态：任何新版本都基于进入编辑时的当前版本（base_version 乐观锁）
const editor = {
    active: false,
    baseVersion: null,
    cases: [],
    deletedCaseIds: [],
};

const SOURCE_TYPE_LABELS = {
    publish: "发布",
    manual_edit: "人工编辑",
    rollback: "回滚",
    ai_edit: "AI 修改",
};

const FIELD_LABELS = {
    title: "测试标题",
    preconditions: "前置条件",
    steps: "操作步骤",
    expected_results: "预期结果",
    priority: "优先级",
    automation: "自动化",
    requirement_refs: "需求追溯",
    rationale: "覆盖说明",
};

const CASE_TABLE_HEADERS = ["用例编号", "测试标题", "前置条件", "操作步骤", "预期结果", "优先级", "自动化", "需求追溯", "覆盖说明"];

const el = {};
["setName", "setVersion", "setMeta", "detailActions", "ownerActions", "editBtn",
 "exportBtn", "readonlyHint", "casesPanel", "viewingLabel",
 "backToCurrentBtn", "casesTable", "editorPanel", "editorBaseVersion", "addCaseBtn",
 "cancelEditBtn", "saveBtn", "editNote", "caseEditorList", "versionsPanel", "versionList",
 "diffFrom", "diffTo", "diffBtn", "diffPanel", "diffLabel", "closeDiffBtn", "diffView",
 "aiEditBtn", "aiPanel", "aiBaseVersion", "aiInstruction", "aiGenerateBtn", "aiCloseBtn",
 "aiGeneratingHint", "aiPreviewArea", "aiSaveBtn", "aiTruncatedWarn", "aiDiffView", "aiNote",
].forEach((id) => { el[id] = document.getElementById(id); });

document.addEventListener("DOMContentLoaded", init);

async function init() {
    el.editBtn.addEventListener("click", enterEditMode);
    el.aiEditBtn.addEventListener("click", enterAiEdit);
    el.aiCloseBtn.addEventListener("click", exitAiEdit);
    el.aiGenerateBtn.addEventListener("click", generateAiEdit);
    el.aiSaveBtn.addEventListener("click", confirmAiEdit);
    el.exportBtn.addEventListener("click", exportCsv);
    el.backToCurrentBtn.addEventListener("click", backToCurrent);
    el.addCaseBtn.addEventListener("click", addCase);
    el.cancelEditBtn.addEventListener("click", exitEditMode);
    el.saveBtn.addEventListener("click", saveEdit);
    el.diffBtn.addEventListener("click", renderDiff);
    el.closeDiffBtn.addEventListener("click", () => { el.diffPanel.hidden = true; });

    el.ownerActions.hidden = !state.canEdit;
    el.readonlyHint.hidden = state.canEdit;

    await loadDetail();
    await loadVersions();
}

function redirectToLogin() {
    window.location.href = "/login?logout=true";
}

function showMessage(message, type = "info") {
    const existing = document.querySelector(".message-alert");
    if (existing) existing.remove();
    const alertDiv = document.createElement("div");
    alertDiv.className = `message-alert message-${type}`;
    const span = document.createElement("span");
    span.textContent = message;
    const closeBtn = document.createElement("button");
    closeBtn.className = "message-close";
    closeBtn.innerHTML = "&times;";
    closeBtn.addEventListener("click", () => alertDiv.remove());
    alertDiv.appendChild(span);
    alertDiv.appendChild(closeBtn);
    document.body.appendChild(alertDiv);
    setTimeout(() => alertDiv.remove(), 4000);
}

function formatPriorityStats(stats) {
    const s = stats || {};
    return `P0:${s.P0 ?? 0} / P1:${s.P1 ?? 0} / P2:${s.P2 ?? 0} / P3:${s.P3 ?? 0}`;
}

function formatTime(value) {
    if (!value) return "-";
    const date = new Date(value);
    return isNaN(date.getTime()) ? "-" : date.toLocaleString("zh-CN", { hour12: false });
}

async function apiFetch(url, options) {
    const response = await fetch(url, options);
    if (response.status === 401) {
        redirectToLogin();
        throw new Error("未登录");
    }
    return response;
}

// ---------- 数据加载 ----------

async function loadDetail() {
    const response = await apiFetch(`/api/test-sets/${state.setId}`);
    if (!response.ok) {
        showMessage("加载用例集失败", "error");
        return;
    }
    state.asset = await response.json();
    state.currentContent = state.asset.current_content || { test_cases: [] };
    renderHeader();
    renderCases(state.currentContent.test_cases || []);
    el.viewingLabel.textContent = `v${state.asset.current_version}`;
}

async function loadVersions() {
    const response = await apiFetch(`/api/test-sets/${state.setId}/versions`);
    if (!response.ok) return;
    state.versions = await response.json();
    renderVersions();
    fillDiffSelects();
}

// ---------- 头部与操作 ----------

function renderHeader() {
    const asset = state.asset;
    el.setName.textContent = asset.name;
    el.setVersion.textContent = `v${asset.current_version}`;
    el.setVersion.className = "badge badge-version";

    el.setMeta.innerHTML = "";
    const facts = [`用例总数：${asset.case_count} | 用例分布：${formatPriorityStats(asset.priority_stats)}`];
    if (!asset.is_mine) facts.push(`创建者：${asset.owner_username || "未知用户"}`);
    if (asset.source_workflow_id) facts.push("来自测试任务");
    facts.push(`更新于 ${formatTime(asset.updated_at)}`);
    facts.forEach((text, index) => {
        if (index > 0) el.setMeta.appendChild(document.createTextNode(" · "));
        el.setMeta.appendChild(document.createTextNode(text));
    });
    if (asset.description) {
        el.setMeta.appendChild(document.createElement("br"));
        el.setMeta.appendChild(document.createTextNode(asset.description));
    }

}

function exportCsv() {
    window.location.href = `/api/test-sets/${state.setId}/export`;
}

// ---------- 用例表（查看模式） ----------

function renderCases(cases) {
    const table = el.casesTable;
    table.innerHTML = "";

    const thead = document.createElement("thead");
    const headerRow = document.createElement("tr");
    CASE_TABLE_HEADERS.forEach((text) => {
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
            (c.expected_results || []).join("\n"),
            c.priority,
            c.automation,
            (c.requirement_refs || []).join(", "),
            c.rationale || "",
        ];
        cells.forEach((value) => {
            const td = document.createElement("td");
            td.textContent = value ?? "";
            tr.appendChild(td);
        });
        tbody.appendChild(tr);
    });
    table.appendChild(tbody);
}

async function viewVersion(version) {
    const response = await apiFetch(`/api/test-sets/${state.setId}/versions/${version}`);
    if (!response.ok) return showMessage("加载历史版本失败", "error");
    const data = await response.json();
    renderCases((data.content && data.content.test_cases) || []);
    const isCurrent = version === state.asset.current_version;
    el.viewingLabel.textContent = isCurrent ? `v${version}` : `v${version}（历史版本）`;
    el.backToCurrentBtn.hidden = isCurrent;
}

function backToCurrent() {
    renderCases(state.currentContent.test_cases || []);
    el.viewingLabel.textContent = `v${state.asset.current_version}`;
    el.backToCurrentBtn.hidden = true;
}

// ---------- 编辑模式 ----------

function enterEditMode() {
    editor.active = true;
    editor.baseVersion = state.asset.current_version;
    editor.cases = (state.currentContent.test_cases || []).map((c) => ({ ...c, isNew: false }));
    editor.deletedCaseIds = [];
    el.editorBaseVersion.textContent = editor.baseVersion;
    el.editNote.value = "";
    el.casesPanel.hidden = true;
    el.editorPanel.hidden = false;
    el.aiPanel.hidden = true;  // 与 AI 修改互斥
    renderEditor();
}

function exitEditMode() {
    editor.active = false;
    el.editorPanel.hidden = true;
    el.casesPanel.hidden = false;
}

// ---------- AI 修改（两段式：生成建议 → diff 确认，D-017） ----------

const aiEdit = { baseVersion: null, proposed: null };

function enterAiEdit() {
    if (editor.active) exitEditMode();
    aiEdit.baseVersion = state.asset.current_version;
    aiEdit.proposed = null;
    el.aiBaseVersion.textContent = aiEdit.baseVersion;
    el.aiPreviewArea.hidden = true;
    el.aiTruncatedWarn.hidden = true;
    el.casesPanel.hidden = true;
    el.editorPanel.hidden = true;
    el.aiPanel.hidden = false;
    el.aiPanel.scrollIntoView({ behavior: "smooth" });
}

function exitAiEdit() {
    el.aiPanel.hidden = true;
    el.aiPreviewArea.hidden = true;
    el.casesPanel.hidden = false;
}

async function generateAiEdit() {
    const instruction = el.aiInstruction.value.trim();
    if (!instruction) return showMessage("请先输入修改指令", "error");

    el.aiGenerateBtn.disabled = true;
    el.aiGeneratingHint.hidden = false;
    try {
        const response = await apiFetch(`/api/test-sets/${state.setId}/ai-edit/preview`, {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ instruction }),
        });
        if (!response.ok) {
            const data = await response.json().catch(() => ({}));
            return showMessage(data.error || "AI 修改失败，请稍后重试", "error");
        }
        const payload = await response.json();
        aiEdit.baseVersion = payload.base_version;
        aiEdit.proposed = payload.proposed;
        el.aiBaseVersion.textContent = payload.base_version;
        el.aiTruncatedWarn.hidden = !payload.truncated;
        renderDiffInto(el.aiDiffView, payload.diff);
        el.aiPreviewArea.hidden = false;
    } catch (error) {
        if (error.message !== "未登录") {
            console.error("AI 修改失败:", error);
            showMessage("AI 修改失败，请稍后重试", "error");
        }
    } finally {
        el.aiGenerateBtn.disabled = false;
        el.aiGeneratingHint.hidden = true;
    }
}

async function confirmAiEdit() {
    if (!aiEdit.proposed) return;
    el.aiSaveBtn.disabled = true;
    try {
        const response = await apiFetch(`/api/test-sets/${state.setId}/ai-edit/confirm`, {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({
                content: aiEdit.proposed,
                base_version: aiEdit.baseVersion,
                note: el.aiNote.value.trim() || null,
            }),
        });
        const data = await response.json().catch(() => ({}));
        if (response.status === 409) {
            return showMessage("用例集已被其他人修改，AI 建议已过期，请重新生成", "error");
        }
        if (!response.ok) {
            return showMessage(data.error || "保存失败", "error");
        }
        exitAiEdit();
        await loadDetail();
        await loadVersions();
        showMessage(`已保存为新版本 v${data.version.version}（AI 修改）`, "success");
    } catch (error) {
        if (error.message !== "未登录") {
            console.error("保存 AI 修改失败:", error);
            showMessage("保存失败，请稍后重试", "error");
        }
    } finally {
        el.aiSaveBtn.disabled = false;
    }
}

function suggestNextId() {
    const existing = new Set(editor.cases.map((c) => c.id));
    let prefix = "TC-NEW";
    let maxNum = 0;
    let width = 3;
    editor.cases.forEach((c) => {
        const match = c.id.match(/^(.*?)(\d+)$/);
        if (match) {
            const num = parseInt(match[2], 10);
            if (num > maxNum) {
                maxNum = num;
                width = match[2].length;
                prefix = match[1];
            }
        }
    });
    let candidate = `${prefix}${String(maxNum + 1).padStart(width, "0")}`;
    while (existing.has(candidate)) {
        maxNum += 1;
        candidate = `${prefix}${String(maxNum + 1).padStart(width, "0")}`;
    }
    return candidate;
}

function addCase() {
    const emptyCase = {
        id: suggestNextId(),
        title: "",
        preconditions: [],
        steps: [],
        expected_results: [],
        priority: "P1",
        automation: "Manual",
        requirement_refs: [],
        rationale: "",
        isNew: true,
    };
    editor.cases.push(emptyCase);
    renderEditor();
}

function removeCase(index) {
    const target = editor.cases[index];
    if (!target) return;
    if (!target.isNew) editor.deletedCaseIds.push(target.id);
    editor.cases.splice(index, 1);
    renderEditor();
}

function renderEditor() {
    const list = el.caseEditorList;
    list.innerHTML = "";
    editor.cases.forEach((c, index) => {
        list.appendChild(renderCaseEditorCard(c, index));
    });
    if (editor.cases.length === 0) {
        const empty = document.createElement("p");
        empty.className = "editor-tip";
        empty.textContent = "当前没有用例。可点击「新增用例」，或取消编辑后回滚到历史版本。";
        list.appendChild(empty);
    }
    if (editor.deletedCaseIds.length > 0) {
        const tip = document.createElement("p");
        tip.className = "editor-tip delete-tip";
        tip.textContent = `本次显式删除的用例编号：${editor.deletedCaseIds.join("、")}`;
        list.appendChild(tip);
    }
}

function renderCaseEditorCard(c, index) {
    const card = document.createElement("div");
    card.className = "case-edit-card";
    card.dataset.index = index;

    // 已有用例的编号不可修改（D-015），仅新增用例的编号可编辑
    const idGroup = buildFieldRow("用例编号", "id", c.id, "text", !c.isNew);
    const titleGroup = buildFieldRow("测试标题", "title", c.title, "text", false);
    const row1 = document.createElement("div");
    row1.className = "case-edit-row";
    row1.appendChild(idGroup);
    row1.appendChild(titleGroup);
    card.appendChild(row1);

    const row2 = document.createElement("div");
    row2.className = "case-edit-row";
    row2.appendChild(buildSelectRow("优先级", "priority", ["P0", "P1", "P2"], c.priority));
    row2.appendChild(buildSelectRow("自动化", "automation", ["Manual", "Auto"], c.automation));
    row2.appendChild(buildFieldRow("需求追溯（逗号分隔）", "requirement_refs", (c.requirement_refs || []).join(", "), "text", false));
    card.appendChild(row2);

    const row3 = document.createElement("div");
    row3.className = "case-edit-row";
    row3.appendChild(buildTextareaRow("前置条件（每行一条）", "preconditions", (c.preconditions || []).join("\n")));
    row3.appendChild(buildTextareaRow("操作步骤（每行一步）", "steps", (c.steps || []).join("\n")));
    row3.appendChild(buildTextareaRow("预期结果（每行一条）", "expected_results", (c.expected_results || []).join("\n")));
    card.appendChild(row3);

    card.appendChild(buildFieldRow("覆盖说明", "rationale", c.rationale || "", "text", false));

    const actions = document.createElement("div");
    actions.className = "case-edit-actions";
    const deleteBtn = document.createElement("button");
    deleteBtn.type = "button";
    deleteBtn.className = "btn small danger";
    deleteBtn.textContent = c.isNew ? "移除新用例" : "删除用例";
    deleteBtn.addEventListener("click", () => removeCase(index));
    actions.appendChild(deleteBtn);
    card.appendChild(actions);

    return card;
}

function buildFieldRow(label, field, value, type, disabled) {
    const group = document.createElement("div");
    group.className = "form-group";
    const labelEl = document.createElement("label");
    labelEl.textContent = label;
    const input = document.createElement("input");
    input.type = type;
    input.dataset.field = field;
    input.value = value ?? "";
    input.disabled = disabled;
    if (disabled) input.title = "已有用例的编号不可修改";
    group.appendChild(labelEl);
    group.appendChild(input);
    return group;
}

function buildTextareaRow(label, field, value) {
    const group = document.createElement("div");
    group.className = "form-group";
    const labelEl = document.createElement("label");
    labelEl.textContent = label;
    const textarea = document.createElement("textarea");
    textarea.rows = 3;
    textarea.dataset.field = field;
    textarea.value = value ?? "";
    group.appendChild(labelEl);
    group.appendChild(textarea);
    return group;
}

function buildSelectRow(label, field, options, value) {
    const group = document.createElement("div");
    group.className = "form-group";
    const labelEl = document.createElement("label");
    labelEl.textContent = label;
    const select = document.createElement("select");
    select.dataset.field = field;
    options.forEach((option) => {
        const optionEl = document.createElement("option");
        optionEl.value = option;
        optionEl.textContent = option;
        if (option === value) optionEl.selected = true;
        select.appendChild(optionEl);
    });
    group.appendChild(labelEl);
    group.appendChild(select);
    return group;
}

function collectEditorCases() {
    const cases = [];
    el.caseEditorList.querySelectorAll(".case-edit-card").forEach((card) => {
        const index = parseInt(card.dataset.index, 10);
        const source = editor.cases[index];
        const read = (field) => card.querySelector(`[data-field="${field}"]`);
        cases.push({
            id: read("id").value.trim(),
            title: read("title").value.trim(),
            preconditions: splitLines(read("preconditions").value),
            steps: splitLines(read("steps").value),
            expected_results: splitLines(read("expected_results").value),
            priority: read("priority").value,
            automation: read("automation").value,
            requirement_refs: splitCommas(read("requirement_refs").value),
            rationale: read("rationale").value.trim(),
            isNew: source ? source.isNew : false,
        });
    });
    return cases;
}

function splitLines(value) {
    return value.split("\n").map((line) => line.trim()).filter((line) => line !== "");
}

function splitCommas(value) {
    return value.split(/[,，]/).map((part) => part.trim()).filter((part) => part !== "");
}

async function saveEdit() {
    const cases = collectEditorCases();

    const emptyId = cases.find((c) => !c.id);
    if (emptyId) return showMessage("存在未填写编号的用例", "error");
    const emptyTitle = cases.find((c) => !c.title);
    if (emptyTitle) return showMessage("存在未填写标题的用例", "error");

    const ids = cases.map((c) => c.id);
    const duplicated = ids.find((id, i) => ids.indexOf(id) !== i);
    if (duplicated) return showMessage(`用例编号重复：${duplicated}`, "error");

    el.saveBtn.disabled = true;
    try {
        const response = await apiFetch(`/api/test-sets/${state.setId}/content`, {
            method: "PUT",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({
                content: { test_cases: cases.map(({ isNew, ...rest }) => rest) },
                base_version: editor.baseVersion,
                note: el.editNote.value.trim() || null,
                deleted_case_ids: editor.deletedCaseIds,
            }),
        });
        const data = await response.json().catch(() => ({}));
        if (response.status === 409) {
            if (confirm("用例集已被其他人修改（版本冲突）。是否放弃本次编辑并加载最新版本？")) {
                exitEditMode();
                await loadDetail();
                await loadVersions();
            }
            return;
        }
        if (!response.ok) {
            return showMessage(data.error || "保存失败", "error");
        }
        showMessage(`已保存为新版本 v${data.version.version}`, "success");
        exitEditMode();
        await loadDetail();
        await loadVersions();
    } catch (error) {
        if (error.message !== "未登录") {
            console.error("保存失败:", error);
            showMessage("保存失败，请稍后重试", "error");
        }
    } finally {
        el.saveBtn.disabled = false;
    }
}

// ---------- 版本历史与对比 ----------

function renderVersions() {
    const list = el.versionList;
    list.innerHTML = "";
    state.versions.forEach((version) => {
        const li = document.createElement("li");
        li.className = "version-row";

        const label = document.createElement("span");
        label.className = "version-num";
        label.textContent = `v${version.version}`;
        li.appendChild(label);

        const type = document.createElement("span");
        type.className = `badge badge-source badge-${version.source_type}`;
        type.textContent = SOURCE_TYPE_LABELS[version.source_type] || version.source_type;
        li.appendChild(type);

        const note = document.createElement("span");
        note.className = "version-note";
        note.textContent = version.note || "";
        li.appendChild(note);

        const time = document.createElement("span");
        time.className = "version-time";
        time.textContent = formatTime(version.created_at);
        li.appendChild(time);

        const actions = document.createElement("span");
        actions.className = "version-actions";
        const viewBtn = document.createElement("button");
        viewBtn.className = "btn small";
        viewBtn.textContent = "查看";
        viewBtn.addEventListener("click", () => viewVersion(version.version));
        actions.appendChild(viewBtn);

        if (state.canEdit && version.version !== state.asset.current_version) {
            const rollbackBtn = document.createElement("button");
            rollbackBtn.className = "btn small";
            rollbackBtn.textContent = "回滚到此版本";
            rollbackBtn.addEventListener("click", () => rollbackTo(version.version));
            actions.appendChild(rollbackBtn);
        }
        li.appendChild(actions);
        list.appendChild(li);
    });
    if (state.versions.length === 0) {
        const li = document.createElement("li");
        li.className = "version-row";
        li.textContent = "暂无版本记录";
        list.appendChild(li);
    }
}

async function rollbackTo(sourceVersion) {
    if (!confirm(`确定回滚到 v${sourceVersion} 吗？将以其内容另存为新版本，历史版本保留。`)) return;
    const response = await apiFetch(`/api/test-sets/${state.setId}/rollback`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ source_version: sourceVersion }),
    });
    const data = await response.json().catch(() => ({}));
    if (!response.ok) return showMessage(data.error || "回滚失败", "error");
    showMessage(`已回滚，生成新版本 v${data.version.version}`, "success");
    el.diffPanel.hidden = true;
    await loadDetail();
    await loadVersions();
}

function fillDiffSelects() {
    const numbers = state.versions.map((v) => v.version);
    fillSelect(el.diffFrom, numbers);
    fillSelect(el.diffTo, numbers);
    if (numbers.length > 1) {
        el.diffFrom.value = String(numbers[1]);
        el.diffTo.value = String(numbers[0]);
    }
}

function fillSelect(select, numbers) {
    select.innerHTML = "";
    numbers.forEach((num) => {
        const option = document.createElement("option");
        option.value = num;
        option.textContent = `v${num}`;
        select.appendChild(option);
    });
}

async function renderDiff() {
    const from = el.diffFrom.value;
    const to = el.diffTo.value;
    if (!from || !to) return;
    const response = await apiFetch(`/api/test-sets/${state.setId}/diff?from_version=${from}&to_version=${to}`);
    if (!response.ok) {
        const data = await response.json().catch(() => ({}));
        return showMessage(data.error || "对比失败", "error");
    }
    const diff = await response.json();
    el.diffPanel.hidden = false;
    el.diffLabel.textContent = `v${diff.from_version} → v${diff.to_version}`;
    renderDiffInto(el.diffView, diff);
    el.diffPanel.scrollIntoView({ behavior: "smooth" });
}

// 版本对比与 AI 修改预览共用同一渲染（含字符级高亮切片）
function renderDiffInto(container, diff) {
    container.innerHTML = "";

    container.appendChild(buildDiffGroup("新增用例", diff.added, "diff-added"));
    container.appendChild(buildDiffGroup("删除用例", diff.removed, "diff-removed"));

    const changedWrap = document.createElement("div");
    changedWrap.className = "diff-group";
    const changedTitle = document.createElement("h3");
    changedTitle.textContent = `修改用例（${diff.changed.length}）`;
    changedWrap.appendChild(changedTitle);
    diff.changed.forEach((change) => {
        const card = document.createElement("div");
        card.className = "diff-changed-card";
        const head = document.createElement("p");
        head.textContent = `${change.case_id} ${change.title}`;
        card.appendChild(head);
        const table = document.createElement("table");
        table.className = "diff-table";
        const thead = document.createElement("thead");
        const headRow = document.createElement("tr");
        ["字段", "变更前", "变更后"].forEach((text) => {
            const th = document.createElement("th");
            th.textContent = text;
            headRow.appendChild(th);
        });
        thead.appendChild(headRow);
        table.appendChild(thead);
        const tbody = document.createElement("tbody");
        Object.entries(change.fields).forEach(([field, pair]) => {
            const tr = document.createElement("tr");
            const labelTd = document.createElement("td");
            labelTd.textContent = FIELD_LABELS[field] || field;
            tr.appendChild(labelTd);
            tr.appendChild(buildDiffValueCell(pair.before_segments, pair.before, "diff-before"));
            tr.appendChild(buildDiffValueCell(pair.after_segments, pair.after, "diff-after"));
            tbody.appendChild(tr);
        });
        table.appendChild(tbody);
        card.appendChild(table);
        changedWrap.appendChild(card);
    });
    if (diff.changed.length === 0) {
        const none = document.createElement("p");
        none.className = "diff-empty";
        none.textContent = "无修改用例";
        changedWrap.appendChild(none);
    }
    container.appendChild(changedWrap);
}

function buildDiffGroup(title, cases, className) {
    const group = document.createElement("div");
    group.className = `diff-group ${className}`;
    const heading = document.createElement("h3");
    heading.textContent = `${title}（${cases.length}）`;
    group.appendChild(heading);
    cases.forEach((c) => {
        const item = document.createElement("p");
        item.className = "diff-case";
        item.textContent = `${c.id} ${c.title}`;
        group.appendChild(item);
    });
    if (cases.length === 0) {
        const none = document.createElement("p");
        none.className = "diff-empty";
        none.textContent = "无";
        group.appendChild(none);
    }
    return group;
}

function formatValue(value) {
    if (Array.isArray(value)) return value.join("\n");
    return value === null || value === undefined ? "" : String(value);
}

// 按 service 返回的差异切片渲染单元格：changed 片段加深标记，一眼看出改了哪几个字
function buildDiffValueCell(segments, fallbackText, className) {
    const td = document.createElement("td");
    td.className = className;
    if (Array.isArray(segments) && segments.length > 0) {
        segments.forEach((seg) => {
            const span = document.createElement("span");
            if (seg.changed) span.className = "diff-mark";
            span.textContent = seg.text;
            td.appendChild(span);
        });
    } else {
        td.textContent = formatValue(fallbackText);
    }
    return td;
}
