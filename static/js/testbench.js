// 测试工作台列表页：用例集列表、可见性切换、删除、导出
let testSets = [];

const listEl = document.getElementById("testSetList");
const emptyEl = document.getElementById("emptyState");
const searchInput = document.getElementById("searchInput");

document.addEventListener("DOMContentLoaded", () => {
    searchInput.addEventListener("input", () => renderList(filterSets(searchInput.value.trim())));
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
    const badge = document.createElement("span");
    badge.className = `badge ${set.visibility === "shared" ? "badge-shared" : "badge-private"}`;
    badge.textContent = set.visibility === "shared" ? "共享" : "私有";
    header.appendChild(name);
    header.appendChild(badge);
    card.appendChild(header);

    if (set.description) {
        const desc = document.createElement("p");
        desc.className = "card-desc";
        desc.textContent = set.description;
        card.appendChild(desc);
    }

    const meta = document.createElement("div");
    meta.className = "card-meta";
    const facts = [
        `用例 ${set.case_count} 条`,
        `当前 v${set.current_version}`,
        set.is_mine ? "我创建的" : `来自 ${set.owner_username || "未知用户"}`,
        `更新于 ${formatTime(set.updated_at)}`,
    ];
    facts.forEach((text) => {
        const span = document.createElement("span");
        span.textContent = text;
        meta.appendChild(span);
    });
    card.appendChild(meta);

    if (set.source_workflow_id) {
        const source = document.createElement("a");
        source.className = "card-source";
        source.href = "/workflows";
        source.textContent = "来自测试任务";
        card.appendChild(source);
    }

    const actions = document.createElement("div");
    actions.className = "card-actions";

    const viewBtn = document.createElement("a");
    viewBtn.className = "btn primary";
    viewBtn.href = `/testbench-detail?set_id=${set.id}`;
    viewBtn.textContent = "查看详情";
    actions.appendChild(viewBtn);

    const exportBtn = document.createElement("button");
    exportBtn.className = "btn";
    exportBtn.textContent = "导出 CSV";
    exportBtn.addEventListener("click", () => {
        window.location.href = `/api/test-sets/${set.id}/export`;
    });
    actions.appendChild(exportBtn);

    if (set.is_mine) {
        const visibilityBtn = document.createElement("button");
        visibilityBtn.className = "btn";
        visibilityBtn.textContent = set.visibility === "shared" ? "设为私有" : "设为共享";
        visibilityBtn.addEventListener("click", () => toggleVisibility(set));
        actions.appendChild(visibilityBtn);

        const deleteBtn = document.createElement("button");
        deleteBtn.className = "btn danger";
        deleteBtn.textContent = "删除";
        deleteBtn.addEventListener("click", () => deleteSet(set));
        actions.appendChild(deleteBtn);
    }

    card.appendChild(actions);
    return card;
}

async function toggleVisibility(set) {
    const next = set.visibility === "shared" ? "private" : "shared";
    try {
        const response = await fetch(`/api/test-sets/${set.id}`, {
            method: "PATCH",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ visibility: next }),
        });
        if (response.status === 401) return redirectToLogin();
        if (!response.ok) {
            const data = await response.json().catch(() => ({}));
            return showMessage(data.error || "操作失败", "error");
        }
        showMessage(next === "shared" ? "已设为共享" : "已设为私有", "success");
        loadTestSets();
    } catch (error) {
        console.error("切换可见性失败:", error);
        showMessage("操作失败，请稍后重试", "error");
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

function formatTime(value) {
    if (!value) return "-";
    const date = new Date(value);
    return isNaN(date.getTime()) ? "-" : date.toLocaleString("zh-CN", { hour12: false });
}
