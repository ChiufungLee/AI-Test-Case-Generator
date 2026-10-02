// 测试用例集页：用例集列表、重命名、删除（D-026 拆分后的独立一级菜单页）
let testSets = [];
let renamingSet = null;

const listEl = document.getElementById("testSetList");
const emptyEl = document.getElementById("emptyState");
const searchInput = document.getElementById("searchInput");
const renameModal = document.getElementById("renameModal");
const renameInput = document.getElementById("renameInput");
const renameConfirmBtn = document.getElementById("renameConfirmBtn");
const renameCancelBtn = document.getElementById("renameCancelBtn");
const renameCloseBtn = document.getElementById("renameCloseBtn");

document.addEventListener("DOMContentLoaded", () => {
    searchInput.addEventListener("input", () => renderList(filterSets(searchInput.value.trim())));
    renameConfirmBtn.addEventListener("click", submitRename);
    renameCancelBtn.addEventListener("click", closeRenameModal);
    renameCloseBtn.addEventListener("click", closeRenameModal);
    renameInput.addEventListener("keydown", (event) => {
        if (event.key === "Enter") submitRename();
    });
    loadTestSets();
});

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

async function loadTestSets() {
    listEl.innerHTML = '<div class="loading-state"><i class="fas fa-spinner fa-spin"></i><p>加载用例集...</p></div>';
    try {
        const response = await fetch("/api/test-sets");
        if (response.status === 401) return redirectToLogin();
        if (!response.ok) throw new Error(`HTTP ${response.status}`);
        testSets = await response.json();
        renderList(testSets);
        emptyEl.hidden = testSets.length > 0;
    } catch (error) {
        console.error("加载用例集失败:", error);
        listEl.innerHTML = "";
        const errorState = document.createElement("div");
        errorState.className = "error-state";
        errorState.textContent = "加载失败，请刷新重试";
        listEl.appendChild(errorState);
    }
}

function filterSets(keyword) {
    if (!keyword) return testSets;
    const lower = keyword.toLowerCase();
    return testSets.filter((set) =>
        [set.name, set.description, set.owner_username]
            .some((field) => (field || "").toLowerCase().includes(lower))
    );
}

function renderList(sets) {
    listEl.innerHTML = "";
    if (sets.length === 0 && searchInput.value.trim()) {
        const hint = document.createElement("div");
        hint.className = "no-match-hint";
        hint.textContent = "没有匹配的用例集，换个关键词试试";
        listEl.appendChild(hint);
        return;
    }
    sets.forEach((set) => listEl.appendChild(renderCard(set)));
}

function renderCard(set) {
    const card = document.createElement("div");
    card.className = "test-set-card";

    const header = document.createElement("div");
    header.className = "card-header";
    const name = document.createElement("h3");
    name.className = "card-title";
    name.textContent = set.name;
    // 版本徽章紧跟名称，与详情页一致（badge-version 同款样式）
    const versionBadge = document.createElement("span");
    versionBadge.className = "badge badge-version";
    versionBadge.textContent = `v${set.current_version}`;
    // 徽章展示用例集来源（可见性在详情页管理）
    const badge = document.createElement("span");
    badge.className = "badge badge-source";
    badge.textContent = set.source_workflow_id ? "来自测试任务" : "手工创建";
    header.appendChild(name);
    header.appendChild(versionBadge);
    header.appendChild(badge);
    card.appendChild(header);

    if (set.description) {
        const desc = document.createElement("p");
        desc.className = "card-desc";
        desc.textContent = set.description;
        card.appendChild(desc);
    }

    // 信息两行：第一行用例统计，第二行创建人与更新时间
    const caseLine = document.createElement("div");
    caseLine.className = "card-meta";
    caseLine.textContent = `用例总数: ${set.case_count} ${formatPriorityStats(set.priority_stats)}`;
    card.appendChild(caseLine);

    const meta = document.createElement("div");
    meta.className = "card-meta";
    meta.textContent = `创建人: ${set.owner_username || "未知用户"} | 更新于 ${formatTime(set.updated_at)}`;
    card.appendChild(meta);

    const actions = document.createElement("div");
    actions.className = "card-actions";

    const viewBtn = document.createElement("a");
    viewBtn.className = "btn primary";
    viewBtn.href = `/testbench-detail?set_id=${set.id}`;
    viewBtn.textContent = "查看详情";
    actions.appendChild(viewBtn);

    if (set.is_mine) {
        const renameBtn = document.createElement("button");
        renameBtn.className = "btn";
        renameBtn.textContent = "重命名";
        renameBtn.addEventListener("click", () => openRenameModal(set));
        actions.appendChild(renameBtn);

        const deleteBtn = document.createElement("button");
        deleteBtn.className = "btn danger";
        deleteBtn.textContent = "删除";
        deleteBtn.addEventListener("click", () => deleteSet(set));
        actions.appendChild(deleteBtn);
    }

    card.appendChild(actions);
    return card;
}

function openRenameModal(set) {
    renamingSet = set;
    renameInput.value = set.name;
    renameModal.classList.add("active");
    renameInput.focus();
    renameInput.select();
}

function closeRenameModal() {
    renamingSet = null;
    renameModal.classList.remove("active");
}

async function submitRename() {
    if (!renamingSet) return;
    const name = renameInput.value.trim();
    if (!name) return showMessage("请输入用例集名称", "error");
    if (name === renamingSet.name) return closeRenameModal();
    renameConfirmBtn.disabled = true;
    try {
        const response = await fetch(`/api/test-sets/${renamingSet.id}`, {
            method: "PATCH",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ name }),
        });
        if (response.status === 401) return redirectToLogin();
        const data = await response.json().catch(() => ({}));
        if (!response.ok) return showMessage(data.error || "重命名失败", "error");
        showMessage("已重命名", "success");
        closeRenameModal();
        loadTestSets();
    } catch (error) {
        console.error("重命名失败:", error);
        showMessage("重命名失败，请稍后重试", "error");
    } finally {
        renameConfirmBtn.disabled = false;
    }
}

async function deleteSet(set) {
    if (!confirm(`确定删除用例集「${set.name}」吗？其全部版本历史将一并删除，且不可恢复。`)) return;
    try {
        const response = await fetch(`/api/test-sets/${set.id}`, { method: "DELETE" });
        if (response.status === 401) return redirectToLogin();
        if (!response.ok) {
            const data = await response.json().catch(() => ({}));
            return showMessage(data.error || "删除失败", "error");
        }
        showMessage("已删除", "success");
        loadTestSets();
    } catch (error) {
        console.error("删除失败:", error);
        showMessage("删除失败，请稍后重试", "error");
    }
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
