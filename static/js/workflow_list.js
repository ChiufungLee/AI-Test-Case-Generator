/* 测试工作流列表页：服务端搜索 + 分页，点击卡片进入详情页，卡片可重命名/删除 */

const PAGE_SIZE = 12;
// 列表页有工作流在跑时轮询刷新（详情页订阅 SSE，列表页没有事件源）
const POLL_INTERVAL_MS = 8000;

const STATUS_TEXT = {
    created: "未开始",
    analyzing: "进行中",
    waiting_review: "待人工确认",
    generating: "进行中",
    completed: "已完成",
    failed: "失败",
};

const listState = {
    keyword: "",
    offset: 0,
    total: 0,
    workflows: [],
    // 上一次渲染的数据指纹：轮询结果未变化时不重绘，避免卡片闪烁
    signature: null,
    pollTimer: null,
    // 请求序号：搜索/翻页/轮询可能并发，只允许最新一次请求的结果落到界面上
    requestSeq: 0,
};

const elements = {};

// 当前正在重命名的测试工作流（弹窗打开期间有效）
let renamingWorkflow = null;
// 弹窗焦点归还用：记录打开前的焦点元素
let renameReturnFocus = null;

document.addEventListener("DOMContentLoaded", init);

function init() {
    [
        "workflowList", "searchInput", "emptyState", "emptyTitle", "emptyHint",
        "pager", "pageInfo", "prevPageBtn", "nextPageBtn",
        "renameModal", "renameInput", "renameConfirmBtn", "renameCancelBtn", "renameCloseBtn",
    ].forEach((id) => {
        elements[id] = document.getElementById(id);
    });

    elements.searchInput.addEventListener("input", onSearchInput);
    elements.prevPageBtn.addEventListener("click", () => changePage(-1));
    elements.nextPageBtn.addEventListener("click", () => changePage(1));
    elements.renameConfirmBtn.addEventListener("click", submitRename);
    elements.renameCancelBtn.addEventListener("click", closeRenameModal);
    elements.renameCloseBtn.addEventListener("click", closeRenameModal);
    elements.renameInput.addEventListener("keydown", (event) => {
        if (event.key === "Enter") submitRename();
    });
    // Esc 关闭弹窗（无障碍：仅靠鼠标点关闭按钮不够）
    document.addEventListener("keydown", (event) => {
        if (event.key === "Escape" && elements.renameModal.classList.contains("active")) {
            closeRenameModal();
        }
    });
    // 从详情页返回（浏览器前进/后退缓存）时刷新，避免看到过期状态
    window.addEventListener("pageshow", (event) => {
        if (event.persisted) loadList({ silent: true });
    });

    loadList();
}

let searchTimer = null;

function onSearchInput() {
    clearTimeout(searchTimer);
    // 输入即搜：300ms 防抖，避免每敲一个字都打一次接口
    searchTimer = setTimeout(() => {
        listState.keyword = elements.searchInput.value.trim();
        listState.offset = 0;
        loadList();
    }, 300);
}

function changePage(delta) {
    const next = listState.offset + delta * PAGE_SIZE;
    if (next < 0 || next >= listState.total) return;
    listState.offset = next;
    loadList();
}

async function loadList(opts = {}) {
    const { silent = false } = opts;
    const seq = ++listState.requestSeq;
    if (!silent) renderLoading();

    const params = new URLSearchParams({
        limit: String(PAGE_SIZE),
        offset: String(listState.offset),
    });
    if (listState.keyword) params.set("keyword", listState.keyword);

    try {
        const response = await fetch(`/api/workflows?${params.toString()}`);
        if (response.status === 401) return redirectToLogin();
        if (!response.ok) throw new Error(`HTTP ${response.status}`);
        const data = await response.json();
        // 已有更新的请求发出：这次响应过期，丢弃（否则会盖掉新关键词/新页的结果）
        if (seq !== listState.requestSeq) return;
        listState.workflows = data.workflows || [];
        listState.total = typeof data.total === "number" ? data.total : listState.workflows.length;

        // 任务在当前页被筛掉/越界：回退到最后一个有效页
        if (!listState.workflows.length && listState.offset > 0) {
            listState.offset = Math.max(0, Math.ceil(listState.total / PAGE_SIZE) - 1) * PAGE_SIZE;
            return loadList({ silent });
        }
        renderList();
    } catch (error) {
        if (seq !== listState.requestSeq) return;
        console.error("加载测试工作流列表失败:", error);
        renderError();
    }
}

function renderLoading() {
    // 重置指纹：加载态/错误态不是"已渲染列表"，之后的同名结果也必须重建
    listState.signature = null;
    elements.emptyState.hidden = true;
    elements.pager.hidden = true;
    elements.workflowList.innerHTML =
        '<div class="loading-state"><i class="fas fa-spinner fa-spin"></i><p>加载测试工作流...</p></div>';
}

function renderError() {
    stopPolling();
    listState.signature = null;
    elements.emptyState.hidden = true;
    elements.pager.hidden = true;
    elements.workflowList.innerHTML =
        '<div class="error-state"><i class="fas fa-triangle-exclamation"></i>' +
        "<p>测试工作流列表加载失败，请稍后重试</p>" +
        '<button type="button" class="btn small" id="retryBtn">重新加载</button></div>';
    const retry = document.getElementById("retryBtn");
    if (retry) retry.addEventListener("click", () => loadList());
}

function renderList() {
    const signature = listState.workflows
        .map((wf) => `${wf.id}:${wf.status}:${wf.updated_at}:${wf.name}`)
        .join("|");
    // 轮询到相同结果时不重绘：保留滚动位置与卡片悬停状态
    if (signature === listState.signature) {
        renderPager();
        if (!listState.workflows.length) stopPolling();
        else schedulePolling();
        return;
    }
    listState.signature = signature;

    elements.workflowList.innerHTML = "";
    if (!listState.workflows.length) {
        elements.emptyState.hidden = false;
        const searching = Boolean(listState.keyword);
        elements.emptyTitle.textContent = searching ? "没有匹配的测试工作流" : "暂无测试工作流";
        elements.emptyHint.textContent = searching
            ? "换个关键词试试，或清空搜索框查看全部测试工作流"
            : "点击「新建测试工作流」创建您的第一个测试工作流";
        renderPager();
        stopPolling();
        return;
    }

    elements.emptyState.hidden = true;
    listState.workflows.forEach((wf) => elements.workflowList.appendChild(buildCard(wf)));
    renderPager();
    schedulePolling();
}

function buildCard(workflow) {
    const card = document.createElement("div");
    card.className = "list-card";

    // 卡片主体是整块可点的链接；重命名/删除按钮不能嵌在 <a> 里（嵌套交互元素非法，
    // 点按钮会连带跳转），所以按钮单独放在卡片底部的操作行（结构见 nav.css「列表卡片」）
    const link = document.createElement("a");
    link.className = "list-card-link";
    link.href = `/workflow-detail?workflow_id=${encodeURIComponent(workflow.id)}`;

    const header = document.createElement("div");
    header.className = "task-card-header";

    const title = document.createElement("h3");
    title.className = "task-card-title";
    title.textContent = workflow.name || "未命名工作流";

    const badge = document.createElement("span");
    badge.className = `status-badge status-${workflow.status}`;
    badge.textContent = STATUS_TEXT[workflow.status] || workflow.status;

    header.appendChild(title);
    header.appendChild(badge);

    const excerpt = document.createElement("p");
    excerpt.className = "task-card-excerpt";
    excerpt.textContent = workflow.requirement_excerpt || "（无需求原文）";

    const meta = document.createElement("div");
    meta.className = "task-card-meta";
    meta.appendChild(buildMetaItem("fa-clock", formatTime(workflow.created_at)));
    if (workflow.requirement_file_name) {
        meta.appendChild(
            buildMetaItem("fa-file-lines", workflow.requirement_file_name, "meta-file")
        );
    }

    link.appendChild(header);
    link.appendChild(excerpt);
    link.appendChild(meta);

    card.appendChild(link);
    // 操作行与另外三个列表页同一个构造器（工作流都是自己的，不存在只读态，无需只读提示）
    card.appendChild(
        buildCardActions([
            { label: "重命名", onClick: () => openRenameModal(workflow) },
            { label: "删除", danger: true, onClick: () => deleteWorkflow(workflow) },
        ])
    );
    return card;
}

/* ---------- 重命名 / 删除 ---------- */

function openRenameModal(workflow) {
    renamingWorkflow = workflow;
    renameReturnFocus = document.activeElement;
    elements.renameInput.value = workflow.name || "";
    elements.renameModal.classList.add("active");
    elements.renameInput.focus();
    elements.renameInput.select();
}

function closeRenameModal() {
    renamingWorkflow = null;
    elements.renameModal.classList.remove("active");
    if (renameReturnFocus && typeof renameReturnFocus.focus === "function") {
        renameReturnFocus.focus();
    }
    renameReturnFocus = null;
}

async function submitRename() {
    if (!renamingWorkflow) return;
    const name = elements.renameInput.value.trim();
    if (!name) return showMessage("请输入测试工作流名称", "error");
    if (name === renamingWorkflow.name) return closeRenameModal();

    elements.renameConfirmBtn.disabled = true;
    try {
        const response = await fetch(
            `/api/workflows/${encodeURIComponent(renamingWorkflow.id)}`,
            {
                method: "PATCH",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify({ name }),
            }
        );
        if (response.status === 401) return redirectToLogin();
        const data = await response.json().catch(() => ({}));
        if (!response.ok) return showMessage(data.error || "重命名失败", "error");
        closeRenameModal();
        showMessage("已重命名", "success");
        refreshList();
    } catch (error) {
        console.error("重命名测试工作流失败:", error);
        showMessage("重命名失败，请稍后重试", "error");
    } finally {
        elements.renameConfirmBtn.disabled = false;
    }
}

async function deleteWorkflow(workflow) {
    const name = workflow.name || "未命名工作流";
    if (
        !confirm(
            `确定删除测试工作流「${name}」吗？` +
                "其全部产物（需求分析、测试点、用例、覆盖报告）将一并删除，且不可恢复。" +
                "已保存到用例集的用例不受影响。"
        )
    ) {
        return;
    }
    try {
        const response = await fetch(
            `/api/workflows/${encodeURIComponent(workflow.id)}`,
            { method: "DELETE" }
        );
        if (response.status === 401) return redirectToLogin();
        if (!response.ok) {
            const data = await response.json().catch(() => ({}));
            // 运行中的任务不给删：直接把服务端的说明提示给用户
            return showMessage(data.error || "删除失败", "error");
        }
        showMessage("已删除", "success");
        refreshList();
    } catch (error) {
        console.error("删除测试工作流失败:", error);
        showMessage("删除失败，请稍后重试", "error");
    }
}

/* 本地改动（重命名 / 删除）后强制重绘一次再取数：
   指纹里的 updated_at 只有秒级精度，同一秒内的改名不重置指纹就会被
   「轮询结果未变化不重绘」吞掉（卡片仍显示旧名称） */
function refreshList() {
    listState.signature = null;
    loadList({ silent: true });
}

function buildMetaItem(iconClass, text, extraClass = "") {
    const item = document.createElement("span");
    if (extraClass) item.className = extraClass;
    const icon = document.createElement("i");
    icon.className = `fas ${iconClass}`;
    item.appendChild(icon);
    item.appendChild(document.createTextNode(text));
    return item;
}

function formatTime(value) {
    if (!value) return "时间未知";
    const date = new Date(value);
    if (Number.isNaN(date.getTime())) return "时间未知";
    const pad = (num) => String(num).padStart(2, "0");
    return (
        `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())} ` +
        `${pad(date.getHours())}:${pad(date.getMinutes())}`
    );
}

function renderPager() {
    const showPager = listState.total > PAGE_SIZE;
    elements.pager.hidden = !showPager;
    if (!showPager) return;
    const pageCount = Math.max(1, Math.ceil(listState.total / PAGE_SIZE));
    const current = Math.floor(listState.offset / PAGE_SIZE) + 1;
    elements.pageInfo.textContent = `第 ${current} / ${pageCount} 页 · 共 ${listState.total} 个测试工作流`;
    elements.prevPageBtn.disabled = listState.offset === 0;
    elements.nextPageBtn.disabled = listState.offset + PAGE_SIZE >= listState.total;
}

/* ---------- 运行中任务的轮询 ---------- */

function hasRunningTask() {
    return listState.workflows.some((wf) => ["analyzing", "generating"].includes(wf.status));
}

function schedulePolling() {
    if (!hasRunningTask()) {
        stopPolling();
        return;
    }
    if (listState.pollTimer) return;
    listState.pollTimer = setInterval(() => {
        if (!document.hidden) loadList({ silent: true });
    }, POLL_INTERVAL_MS);
}

function stopPolling() {
    if (!listState.pollTimer) return;
    clearInterval(listState.pollTimer);
    listState.pollTimer = null;
}
