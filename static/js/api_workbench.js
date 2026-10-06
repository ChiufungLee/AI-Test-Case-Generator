// 接口测试页：OpenAPI 导入（URL/粘贴）、同步、接口规格、用例管理（可编辑）、登录态、执行（D-019~025）
// 独立页面 /api-test（D-026：原「测试工作台」二级 tab 拆分为一级菜单）；testbench.js 负责用例集页
(function () {
    "use strict";

    const state = {
        specs: [], currentSpec: null,
        selectedEndpointIds: new Set(),  // 批量执行：勾选的接口（空 = 单接口面板路径）
        buffers: new Map(),  // endpointId -> { endpoint, cases, dirty, loaded, expanded, aiOpen, aiInstruction }
        panelEndpointId: null,  // 单接口面板正在编辑的端点（与勾选互斥：勾选的在卡片分组里编辑）
        proposals: [],  // 单接口面板的 AI 提案（卡片分组有自己的闭包内提案）
        runEvents: [],  // 本轮执行的 case_done 事件累积，供完成后失败分组
    };
    const el = {};

    const ELEMENT_IDS = [
        "importSpecBtn", "apiSpecList", "apiSpecEmpty",
        "apiSpecDetail", "apiSpecBack", "apiSpecName", "apiSpecMeta",
        "apiPageHeader", "specListHeader", "specSearchInput",
        "endpointTable", "batchPanel", "batchPanelTitle", "batchClearBtn", "selectedEndpointList",
        "saveAllBtn", "cardRulesToggleBtn", "cardRulesPanel",
        "endpointPanel", "endpointTitle", "generateCasesBtn", "aiSuggestBtn",
        "saveCasesBtn", "caseList", "runBaseUrl", "runStartBtn", "runResults", "runSummary", "runScopeHint", "batchGenerateBtn",
        "runHistoryPanel", "runHistory", "importSpecModal", "importCloseBtn", "specNameInput",
        "specFormatSelect", "specContentInput", "importCancelBtn", "importConfirmBtn",
        "rulesToggleBtn", "rulesPanel", "aiPanel", "aiCloseBtn", "aiInstructionInput",
        "aiGenerateProposalsBtn", "aiGeneratingHint", "aiProposalsArea", "aiProposalsList",
        "aiMergeBtn", "aiDiscardBtn",
        "specSyncBtn", "addCaseBtn", "importModeSelect", "specUrlGroup", "specUrlInput",
        "specReparseBtn",
        "specFormatGroup", "specContentGroup", "specDescInput",
        "specEditModal", "specEditNameInput", "specEditDescInput",
        "specEditSaveBtn", "specEditCancelBtn", "specEditCloseBtn",
        "authToggleBtn", "authSummaryLine", "authPanel", "authMethodSelect", "authPathInput",
        "authBodyTypeSelect", "authTokenFieldInput", "authBodyInput", "authSaveBtn",
        "authClearBtn", "authCloseBtn",
    ];

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

    async function apiFetch(url, options) {
        const response = await fetch(url, options);
        if (response.status === 401) {
            redirectToLogin();
            throw new Error("未登录");
        }
        return response;
    }

    function formatTime(value) {
        if (!value) return "-";
        const date = new Date(value);
        return isNaN(date.getTime()) ? "-" : date.toLocaleString("zh-CN", { hour12: false });
    }

    // ---------- 初始化与 tab ----------

    document.addEventListener("DOMContentLoaded", init);

    function init() {
        ELEMENT_IDS.forEach((id) => { el[id] = document.getElementById(id); });
        el.importSpecBtn.addEventListener("click", () => el.importSpecModal.classList.add("active"));
        el.importCloseBtn.addEventListener("click", closeImportModal);
        el.importCancelBtn.addEventListener("click", closeImportModal);
        el.importConfirmBtn.addEventListener("click", importSpec);
        el.importModeSelect.addEventListener("change", toggleImportMode);
        el.specSyncBtn.addEventListener("click", syncSpec);
        el.specReparseBtn.addEventListener("click", reparseSpec);
        el.addCaseBtn.addEventListener("click", addManualCase);
        el.authToggleBtn.addEventListener("click", () => {
            el.authPanel.hidden = !el.authPanel.hidden;
        });
        el.authCloseBtn.addEventListener("click", () => { el.authPanel.hidden = true; });
        el.authSaveBtn.addEventListener("click", saveAuthConfig);
        el.authClearBtn.addEventListener("click", clearAuthConfig);
        el.specSearchInput.addEventListener("input", () => renderSpecList(filterSpecs(el.specSearchInput.value.trim())));
        el.specEditSaveBtn.addEventListener("click", saveSpecMeta);
        el.specEditCancelBtn.addEventListener("click", closeSpecEditModal);
        el.specEditCloseBtn.addEventListener("click", closeSpecEditModal);
        el.authPathInput.addEventListener("change", syncAuthBodyType);
        el.authMethodSelect.addEventListener("change", syncAuthBodyType);
        el.apiSpecBack.addEventListener("click", (event) => {
            event.preventDefault();
            showSpecList();
        });
        el.generateCasesBtn.addEventListener("click", generateCases);
        el.aiSuggestBtn.addEventListener("click", openAiPanel);
        el.saveCasesBtn.addEventListener("click", saveCases);
        el.rulesToggleBtn.addEventListener("click", () => {
            el.rulesPanel.hidden = !el.rulesPanel.hidden;
        });
        el.aiCloseBtn.addEventListener("click", closeAiPanel);
        el.aiGenerateProposalsBtn.addEventListener("click", generateProposals);
        el.aiMergeBtn.addEventListener("click", mergeSelectedProposals);
        el.aiDiscardBtn.addEventListener("click", discardProposals);
        el.runStartBtn.addEventListener("click", startRun);
        el.batchGenerateBtn.addEventListener("click", batchGenerateCases);
        el.saveAllBtn.addEventListener("click", saveAllDirtyGroups);
        el.cardRulesToggleBtn.addEventListener("click", () => {
            el.cardRulesPanel.hidden = !el.cardRulesPanel.hidden;
        });
        el.batchClearBtn.addEventListener("click", () => {
            state.selectedEndpointIds = new Set();
            renderEndpointTable();
        });
        loadSpecs();
    }

    // ---------- 规格列表 ----------

    async function loadSpecs() {
        el.apiSpecList.innerHTML = '<div class="loading-state"><i class="fas fa-spinner fa-spin"></i><p>加载中...</p></div>';
        try {
            const response = await apiFetch("/api/api-specs");
            if (!response.ok) throw new Error(`HTTP ${response.status}`);
            state.specs = await response.json();
            renderSpecList(state.specs);
            el.apiSpecEmpty.hidden = state.specs.length > 0;
        } catch (error) {
            if (error.message !== "未登录") showMessage("加载接口文档失败", "error");
        }
    }

    function filterSpecs(keyword) {
        if (!keyword) return state.specs;
        const lower = keyword.toLowerCase();
        return state.specs.filter((spec) =>
            [spec.name, spec.description, spec.owner_username, spec.spec_title]
                .some((field) => (field || "").toLowerCase().includes(lower))
        );
    }

    function renderSpecList(list) {
        el.apiSpecList.innerHTML = "";
        if (list.length === 0 && el.specSearchInput.value.trim()) {
            const hint = document.createElement("div");
            hint.className = "no-match-hint";
            hint.textContent = "没有匹配的接口文档，换个关键词试试";
            el.apiSpecList.appendChild(hint);
            return;
        }
        list.forEach((spec) => {
            const card = document.createElement("div");
            card.className = "test-set-card";
            card.style.cursor = "pointer";
            card.addEventListener("click", () => openSpec(spec.id));

            const header = document.createElement("div");
            header.className = "card-header";
            const name = document.createElement("h3");
            name.className = "card-title";
            name.textContent = spec.name;
            // 徽章展示来源（可见性不在列表展示）
            const badge = document.createElement("span");
            badge.className = "badge badge-source";
            badge.textContent = spec.source_url ? "来自 URL 导入" : "手工创建";
            header.appendChild(name);
            header.appendChild(badge);
            card.appendChild(header);

            if (spec.description) {
                const desc = document.createElement("p");
                desc.className = "card-desc";
                desc.textContent = spec.description;
                card.appendChild(desc);
            }

            const meta = document.createElement("div");
            meta.className = "card-meta";
            const sourceHost = spec.source_url ? sourceLabel(spec.source_url) : null;
            [`${spec.endpoint_count} 个接口`, `${spec.spec_title} v${spec.spec_version}`, sourceHost]
                .filter(Boolean).forEach((text) => {
                    const span = document.createElement("span");
                    span.textContent = text;
                    meta.appendChild(span);
                });
            card.appendChild(meta);

            const ownerLine = document.createElement("div");
            ownerLine.className = "card-meta";
            ownerLine.textContent = `创建人: ${spec.owner_username || "未知用户"} | 更新于 ${formatTime(spec.updated_at)}`;
            card.appendChild(ownerLine);

            const actions = document.createElement("div");
            actions.className = "card-actions";
            const viewBtn = document.createElement("button");
            viewBtn.className = "btn primary";
            viewBtn.textContent = "查看详情";
            viewBtn.addEventListener("click", (event) => {
                event.stopPropagation();
                openSpec(spec.id);
            });
            actions.appendChild(viewBtn);

            if (spec.is_mine) {
                const editBtn = document.createElement("button");
                editBtn.className = "btn";
                editBtn.textContent = "编辑";
                editBtn.addEventListener("click", (event) => {
                    event.stopPropagation();
                    openSpecEditModal(spec);
                });
                actions.appendChild(editBtn);
                const deleteBtn = document.createElement("button");
                deleteBtn.className = "btn danger";
                deleteBtn.textContent = "删除";
                deleteBtn.addEventListener("click", (event) => {
                    event.stopPropagation();
                    deleteSpec(spec);
                });
                actions.appendChild(deleteBtn);
            }
            card.appendChild(actions);

            el.apiSpecList.appendChild(card);
        });
    }

    async function deleteSpec(spec) {
        if (!confirm(`确定删除接口文档「${spec.name}」吗？接口清单与执行历史将一并删除。`)) return;
        try {
            const response = await apiFetch(`/api/api-specs/${spec.id}`, { method: "DELETE" });
            if (!response.ok) {
                const data = await response.json().catch(() => ({}));
                return showMessage(data.error || "删除失败", "error");
            }
            showMessage("已删除", "success");
            await loadSpecs();
        } catch (error) {
            if (error.message !== "未登录") showMessage("删除失败，请稍后重试", "error");
        }
    }

    // ---------- 导入 ----------

    function closeImportModal() {
        el.importSpecModal.classList.remove("active");
    }

    function toggleImportMode() {
        const isUrl = el.importModeSelect.value === "url";
        el.specUrlGroup.hidden = !isUrl;
        el.specFormatGroup.hidden = isUrl;
        el.specContentGroup.hidden = isUrl;
    }

    async function importSpec() {
        const isUrl = el.importModeSelect.value === "url";
        const name = el.specNameInput.value.trim();
        if (isUrl) {
            const url = el.specUrlInput.value.trim();
            if (!url) return showMessage("请填写文档 URL", "error");
            el.importConfirmBtn.disabled = true;
            try {
                const response = await apiFetch("/api/api-specs/import-url", {
                    method: "POST",
                    headers: { "Content-Type": "application/json" },
                    body: JSON.stringify({ url, name: name || null, description: el.specDescInput.value.trim() || null }),
                });
                const data = await response.json().catch(() => ({}));
                if (!response.ok) return showMessage(data.error || "导入失败", "error");
                showMessage(`已从 URL 导入 ${data.endpoint_count} 个接口，可在详情页「同步」刷新`, "success");
                closeImportModal();
                el.specUrlInput.value = "";
                el.specNameInput.value = "";
                el.specDescInput.value = "";
                await loadSpecs();
                openSpec(data.id);
            } catch (error) {
                if (error.message !== "未登录") showMessage("导入失败，请稍后重试", "error");
            } finally {
                el.importConfirmBtn.disabled = false;
            }
            return;
        }

        const content = el.specContentInput.value;
        if (!name || !content.trim()) return showMessage("请填写文档名称与文档内容", "error");
        el.importConfirmBtn.disabled = true;
        try {
            const response = await apiFetch("/api/api-specs", {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify({
                    name, content, format: el.specFormatSelect.value || null,
                    description: el.specDescInput.value.trim() || null,
                }),
            });
            const data = await response.json().catch(() => ({}));
            if (!response.ok) return showMessage(data.error || "导入失败", "error");
            showMessage(`已导入 ${data.endpoint_count} 个接口`, "success");
            closeImportModal();
            el.specNameInput.value = "";
            el.specContentInput.value = "";
            el.specFormatSelect.value = "";
            el.specDescInput.value = "";
            await loadSpecs();
            openSpec(data.id);
        } catch (error) {
            if (error.message !== "未登录") showMessage("导入失败，请稍后重试", "error");
        } finally {
            el.importConfirmBtn.disabled = false;
        }
    }

    // 编辑接口文档名称/描述（owner-only）
    let editingSpec = null;

    function openSpecEditModal(spec) {
        editingSpec = spec;
        el.specEditNameInput.value = spec.name;
        el.specEditDescInput.value = spec.description || "";
        el.specEditModal.classList.add("active");
        el.specEditNameInput.focus();
    }

    function closeSpecEditModal() {
        editingSpec = null;
        el.specEditModal.classList.remove("active");
    }

    async function saveSpecMeta() {
        if (!editingSpec) return;
        const name = el.specEditNameInput.value.trim();
        if (!name) return showMessage("请输入文档名称", "error");
        el.specEditSaveBtn.disabled = true;
        try {
            const response = await apiFetch(`/api/api-specs/${editingSpec.id}`, {
                method: "PATCH",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify({ name, description: el.specEditDescInput.value.trim() }),
            });
            const data = await response.json().catch(() => ({}));
            if (!response.ok) return showMessage(data.error || "保存失败", "error");
            showMessage("已保存", "success");
            closeSpecEditModal();
            await loadSpecs();
        } catch (error) {
            if (error.message !== "未登录") showMessage("保存失败，请稍后重试", "error");
        } finally {
            el.specEditSaveBtn.disabled = false;
        }
    }

    // 同步：从导入 URL 重新拉取文档并按 (method, path) 刷新接口快照（用例保留，D-022）
    async function syncSpec() {
        if (!state.currentSpec || !state.currentSpec.source_url) return;
        el.specSyncBtn.disabled = true;
        try {
            const response = await apiFetch(`/api/api-specs/${state.currentSpec.id}/sync`, { method: "POST" });
            const data = await response.json().catch(() => ({}));
            if (!response.ok) return showMessage(data.error || "同步失败", "error");
            showMessage(`已同步 ${data.endpoint_count} 个接口`, "success");
            await openSpec(state.currentSpec.id);
        } catch (error) {
            if (error.message !== "未登录") showMessage("同步失败，请稍后重试", "error");
        } finally {
            el.specSyncBtn.disabled = false;
        }
    }

    // 重新解析（D-029）：用当前解析器重放已存储的文档内容，刷新接口快照（粘贴导入文档的同步等价物）
    async function reparseSpec() {
        if (!state.currentSpec) return;
        el.specReparseBtn.disabled = true;
        try {
            const response = await apiFetch(`/api/api-specs/${state.currentSpec.id}/reparse`, { method: "POST" });
            const data = await response.json().catch(() => ({}));
            if (!response.ok) return showMessage(data.error || "重新解析失败", "error");
            showMessage(`已重新解析 ${data.endpoint_count} 个接口，接口快照已刷新（用例保留）`, "success");
            await openSpec(state.currentSpec.id);  // 重建详情（openSpec 会重置用例缓冲并重挂按钮状态）
        } catch (error) {
            if (error.message !== "未登录") showMessage("重新解析失败，请稍后重试", "error");
        } finally {
            el.specReparseBtn.disabled = false;
        }
    }

    // ---------- 规格详情 ----------

    async function openSpec(specId) {
        try {
            const response = await apiFetch(`/api/api-specs/${specId}`);
            if (!response.ok) return showMessage("加载接口文档失败", "error");
            state.currentSpec = await response.json();
        } catch (error) {
            if (error.message !== "未登录") showMessage("加载接口文档失败", "error");
            return;
        }
        // 详情视图隐藏页头与导入行（D-026 后详情即页面主体）
        el.apiPageHeader.hidden = true;
        el.specListHeader.hidden = true;
        el.apiSpecList.hidden = true;
        el.apiSpecEmpty.hidden = true;
        el.apiSpecDetail.hidden = false;
        el.apiSpecName.textContent = state.currentSpec.name;
        // 描述紧跟标题，其余属性信息随后（两行展示）
        el.apiSpecMeta.innerHTML = "";
        if (state.currentSpec.description) {
            const descLine = document.createElement("div");
            descLine.className = "spec-desc";
            descLine.textContent = state.currentSpec.description;
            el.apiSpecMeta.appendChild(descLine);
        }
        const host = state.currentSpec.source_url ? sourceLabel(state.currentSpec.source_url) : null;
        const factsLine = document.createElement("div");
        factsLine.textContent = [
            `${state.currentSpec.spec_title} v${state.currentSpec.spec_version}`,
            `${state.currentSpec.endpoint_count} 个接口`,
            `创建人: ${state.currentSpec.owner_username || "未知用户"}`,
            host,
            `更新于 ${formatTime(state.currentSpec.updated_at)}`,
        ].filter(Boolean).join(" · ");
        el.apiSpecMeta.appendChild(factsLine);
        // 同步/重新解析：owner 可见、两键常显，按文档来源互斥置灰
        const isMine = state.currentSpec.is_mine;
        const fromUrl = !!state.currentSpec.source_url;
        el.specSyncBtn.hidden = !isMine;
        el.specSyncBtn.disabled = !fromUrl;
        el.specSyncBtn.title = fromUrl
            ? "从导入的 URL 重新拉取文档并刷新接口快照（保留用例）"
            : "粘贴导入的文档没有来源 URL，无法远程同步；请使用「重新解析」刷新接口快照";
        el.specReparseBtn.hidden = !isMine;
        el.specReparseBtn.disabled = fromUrl;
        el.specReparseBtn.title = fromUrl
            ? "URL 导入的文档请使用「同步」重新拉取并刷新接口快照"
            : "用当前解析器重新解析已存储的文档，刷新接口快照（保留用例）";
        // 切换文档即重置编辑缓冲（用例以服务端为准，重新进入时按需加载）
        state.buffers.clear();
        state.panelEndpointId = null;
        renderAuth();
        renderEndpointTable();
        closeEndpointPanel();
        loadRunHistory();
    }

    // ---------- 登录态前置请求（D-025） ----------

    function renderAuth() {
        const auth = state.currentSpec.auth;
        if (auth) {
            el.authSummaryLine.textContent = `登录态：${auth.method.toUpperCase()} ${auth.path}${auth.token_field ? "（+token）" : ""}`;
        } else {
            el.authSummaryLine.textContent = "登录态：未配置";
        }
        // 配置仅创建者可编辑；共享读者只读摘要（凭据不下发）
        el.authToggleBtn.hidden = !state.currentSpec.is_mine;
        if (!state.currentSpec.is_mine) el.authPanel.hidden = true;
        el.authMethodSelect.value = auth ? auth.method : "post";
        el.authPathInput.value = auth ? auth.path : "";
        el.authBodyTypeSelect.value = auth ? auth.body_type : "json";
        el.authTokenFieldInput.value = auth && auth.token_field ? auth.token_field : "";
        el.authBodyInput.value = auth && auth.body && Object.keys(auth.body).length
            ? JSON.stringify(auth.body, null, 2)
            : "";
    }

    // 登录路径匹配到规格中的接口时，按其声明的请求体媒体类型自动切换（表单端点收 JSON 必 422）
    function syncAuthBodyType() {
        const path = el.authPathInput.value.trim();
        if (!path || !state.currentSpec) return;
        const method = el.authMethodSelect.value;
        const eps = state.currentSpec.endpoints || [];
        const match = eps.find((e) => e.path === path && e.method === method)
            || eps.find((e) => e.path === path);
        if (!match || !match.request_body_media_type) return;
        const mt = match.request_body_media_type;
        const desired = (mt === "application/x-www-form-urlencoded" || mt === "multipart/form-data")
            ? "form"
            : (mt === "application/json" ? "json" : null);
        if (desired && el.authBodyTypeSelect.value !== desired) {
            el.authBodyTypeSelect.value = desired;
            showMessage(`已按接口声明将登录请求体设为${desired === "form" ? "表单请求体" : "JSON 请求体"}`, "info");
        }
    }

    async function saveAuthConfig() {
        const path = el.authPathInput.value.trim();
        if (!path) return showMessage("请填写登录接口路径", "error");
        let body = {};
        const raw = el.authBodyInput.value.trim();
        if (raw) {
            try {
                body = JSON.parse(raw);
                if (typeof body !== "object" || body === null || Array.isArray(body)) throw new Error();
            } catch (error) {
                return showMessage("登录请求体不是合法的 JSON 对象", "error");
            }
        }
        el.authSaveBtn.disabled = true;
        try {
            const response = await apiFetch(`/api/api-specs/${state.currentSpec.id}/auth`, {
                method: "PUT",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify({
                    auth: {
                        method: el.authMethodSelect.value,
                        path,
                        body,
                        body_type: el.authBodyTypeSelect.value,
                        token_field: el.authTokenFieldInput.value.trim() || null,
                    },
                }),
            });
            const data = await response.json().catch(() => ({}));
            if (!response.ok) return showMessage(data.error || "保存失败", "error");
            state.currentSpec.auth = data.auth;
            renderAuth();
            el.authPanel.hidden = true;
            showMessage("登录态已保存，执行时将先登录", "success");
        } catch (error) {
            if (error.message !== "未登录") showMessage("保存失败，请稍后重试", "error");
        } finally {
            el.authSaveBtn.disabled = false;
        }
    }

    async function clearAuthConfig() {
        el.authClearBtn.disabled = true;
        try {
            const response = await apiFetch(`/api/api-specs/${state.currentSpec.id}/auth`, {
                method: "PUT",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify({ auth: null }),
            });
            const data = await response.json().catch(() => ({}));
            if (!response.ok) return showMessage(data.error || "清除失败", "error");
            state.currentSpec.auth = null;
            renderAuth();
            showMessage("登录态已清除", "success");
        } catch (error) {
            if (error.message !== "未登录") showMessage("清除失败，请稍后重试", "error");
        } finally {
            el.authClearBtn.disabled = false;
        }
    }

    function sourceLabel(url) {
        try {
            return `来源 ${new URL(url).host}`;
        } catch (error) {
            return `来源 ${url}`;
        }
    }

    function showSpecList() {
        el.apiPageHeader.hidden = false;
        el.specListHeader.hidden = false;
        el.apiSpecDetail.hidden = true;
        el.apiSpecList.hidden = false;
        el.apiSpecEmpty.hidden = state.specs.length > 0;
        loadSpecs();
    }

    function renderEndpointTable() {
        const table = el.endpointTable;
        table.innerHTML = "";
        const endpoints = state.currentSpec.endpoints || [];
        // 清掉已不存在的接口选择（同步/刷新后 endpoint 集合可能变化）
        const validIds = new Set(endpoints.map((e) => e.id));
        state.selectedEndpointIds = new Set([...state.selectedEndpointIds].filter((id) => validIds.has(id)));

        const thead = document.createElement("thead");
        const headerRow = document.createElement("tr");
        const selectTh = document.createElement("th");
        selectTh.title = "勾选多个接口可批量执行";
        const selectAll = document.createElement("input");
        selectAll.type = "checkbox";
        selectAll.checked = endpoints.length > 0 && state.selectedEndpointIds.size === endpoints.length;
        selectAll.addEventListener("click", (e) => e.stopPropagation());
        selectAll.addEventListener("change", () => {
            state.selectedEndpointIds = selectAll.checked ? new Set(endpoints.map((e) => e.id)) : new Set();
            renderEndpointTable();
        });
        selectTh.appendChild(selectAll);
        headerRow.appendChild(selectTh);
        ["方法", "路径", "用例数", "说明"].forEach((text) => {
            const th = document.createElement("th");
            th.textContent = text;
            headerRow.appendChild(th);
        });
        thead.appendChild(headerRow);
        table.appendChild(thead);

        const tbody = document.createElement("tbody");
        endpoints.forEach((endpoint) => {
            const tr = document.createElement("tr");
            tr.style.cursor = "pointer";
            tr.title = state.selectedEndpointIds.has(endpoint.id)
                ? "已加入批量执行：点击定位到下方用例分组"
                : "点击打开用例面板编辑";
            tr.addEventListener("click", () => selectEndpoint(endpoint));

            const checkTd = document.createElement("td");
            const check = document.createElement("input");
            check.type = "checkbox";
            check.checked = state.selectedEndpointIds.has(endpoint.id);
            check.title = "加入批量执行";
            check.addEventListener("click", (e) => e.stopPropagation());
            check.addEventListener("change", () => toggleEndpointSelection(endpoint.id, check.checked));
            checkTd.appendChild(check);
            tr.appendChild(checkTd);

            const methodTd = document.createElement("td");
            const methodBadge = document.createElement("span");
            methodBadge.className = `method-badge method-${endpoint.method}`;
            methodBadge.textContent = endpoint.method.toUpperCase();
            methodTd.appendChild(methodBadge);

            const pathTd = document.createElement("td");
            pathTd.textContent = endpoint.path;
            const countTd = document.createElement("td");
            countTd.className = "endpoint-case-count";
            countTd.textContent = endpoint.case_count ?? 0;
            const summaryTd = document.createElement("td");
            summaryTd.textContent = endpoint.summary || endpoint.operation_id || "-";

            tr.appendChild(methodTd);
            tr.appendChild(pathTd);
            tr.appendChild(countTd);
            tr.appendChild(summaryTd);
            tbody.appendChild(tr);
        });
        table.appendChild(tbody);
        renderSelectedEndpoints();
        updateRunScopeHint();
    }

    // 选择变更的唯一入口：复选框与卡片头部的清空都走这里
    function toggleEndpointSelection(endpointId, checked) {
        if (checked) {
            state.selectedEndpointIds.add(endpointId);
            // 互斥：该接口若正在单接口面板中编辑，收起面板（缓冲保留，转入卡片分组）
            if (state.panelEndpointId === endpointId) closeEndpointPanel();
        } else {
            state.selectedEndpointIds.delete(endpointId);
            // 缓冲保留：未保存的修改不丢失，重新勾选后继续编辑
        }
        renderEndpointTable();
    }

    // 单接口用例数变化（生成/保存）后同步清单列与卡片的计数，避免整体刷新详情
    function syncEndpointCount(endpoint, count) {
        endpoint.case_count = count;
        const row = (state.currentSpec.endpoints || []).find((e) => e.id === endpoint.id);
        if (row) row.case_count = count;
        renderEndpointTable();
    }

    // ---------- 已选接口卡片：按端点分组的可编辑用例区 ----------
    // 互斥模型：勾选的接口在卡片分组里编辑，未勾选的点行打开面板编辑——同一接口永远只有一个编辑缓冲

    function panelBuffer() {
        return state.panelEndpointId ? state.buffers.get(state.panelEndpointId) : null;
    }

    // 取端点的用例缓冲：没有则创建并从服务端加载（已有未保存修改或加载中时不重复请求）
    async function ensureBuffer(endpoint) {
        let buffer = state.buffers.get(endpoint.id);
        if (buffer && (buffer.loaded || buffer.dirty || buffer.loading)) return buffer;
        if (!buffer) {
            buffer = { endpoint, cases: [], dirty: false, loaded: false, loading: false, expanded: true, aiOpen: false, aiInstruction: "" };
            state.buffers.set(endpoint.id, buffer);
        }
        buffer.loading = true;
        try {
            const response = await apiFetch(`/api/api-specs/${state.currentSpec.id}/endpoints/${endpoint.id}/cases`);
            if (!response.ok) {
                showMessage("加载用例失败", "error");
                return buffer;
            }
            buffer.cases = await response.json();
            buffer.loaded = true;
            rerenderBuffer(buffer);
        } catch (error) {
            if (error.message !== "未登录") showMessage("加载用例失败", "error");
        } finally {
            buffer.loading = false;
        }
        return buffer;
    }

    function markBufferDirty(buffer) {
        buffer.dirty = true;
        if (state.panelEndpointId === buffer.endpoint.id) el.saveCasesBtn.hidden = false;
        const saveBtn = el.selectedEndpointList.querySelector(`[data-group-id="${buffer.endpoint.id}"] .group-save-btn`);
        if (saveBtn) saveBtn.hidden = false;
        updateSaveAllVisibility();
        updateRunScopeHint();
    }

    function rerenderBuffer(buffer) {
        if (state.panelEndpointId === buffer.endpoint.id) {
            renderPanelCases();
            el.saveCasesBtn.hidden = !buffer.dirty;
            return;
        }
        if (state.selectedEndpointIds.has(buffer.endpoint.id)) renderSelectedEndpoints();
    }

    function updateSaveAllVisibility() {
        const hasDirty = [...state.selectedEndpointIds].some((id) => {
            const b = state.buffers.get(id);
            return b && b.dirty;
        });
        if (el.saveAllBtn) el.saveAllBtn.hidden = !hasDirty;
    }

    function renderSelectedEndpoints() {
        const wrap = el.selectedEndpointList;
        const endpoints = state.currentSpec ? (state.currentSpec.endpoints || []) : [];
        const selected = endpoints.filter((e) => state.selectedEndpointIds.has(e.id));
        el.batchPanel.hidden = selected.length === 0;
        el.batchPanelTitle.textContent = `已选接口（${selected.length}）`;
        updateSaveAllVisibility();
        wrap.innerHTML = "";
        selected.forEach((endpoint) => {
            const buffer = state.buffers.get(endpoint.id)
                || { endpoint, cases: [], dirty: false, loaded: false, loading: false, expanded: true, aiOpen: false, aiInstruction: "" };
            state.buffers.set(endpoint.id, buffer);
            wrap.appendChild(buildGroupElement(buffer));
            if (!buffer.loaded && !buffer.dirty) ensureBuffer(endpoint);
        });
    }

    function buildGroupElement(buffer) {
        const wrap = document.createElement("div");
        wrap.className = "case-group";
        wrap.dataset.groupId = buffer.endpoint.id;

        const header = document.createElement("div");
        header.className = "case-group-header";

        const toggle = document.createElement("button");
        toggle.className = "case-group-toggle";
        toggle.innerHTML = buffer.expanded ? '<i class="fas fa-chevron-down"></i>' : '<i class="fas fa-chevron-right"></i>';
        toggle.title = buffer.expanded ? "收起该接口的用例" : "展开该接口的用例";
        toggle.addEventListener("click", () => {
            buffer.expanded = !buffer.expanded;
            renderSelectedEndpoints();
        });
        header.appendChild(toggle);

        const badge = document.createElement("span");
        badge.className = `method-badge method-${buffer.endpoint.method}`;
        badge.textContent = buffer.endpoint.method.toUpperCase();
        header.appendChild(badge);

        const path = document.createElement("span");
        path.className = "case-group-path";
        path.textContent = buffer.endpoint.path;
        header.appendChild(path);

        const spacer = document.createElement("span");
        spacer.className = "case-group-spacer";
        header.appendChild(spacer);

        const count = document.createElement("span");
        count.className = "case-group-count";
        count.textContent = buffer.loaded || buffer.dirty
            ? `${buffer.cases.length} 条用例`
            : `${buffer.endpoint.case_count || 0} 条用例`;
        header.appendChild(count);

        const aiBtn = document.createElement("button");
        aiBtn.className = "btn small";
        aiBtn.textContent = "AI 建议";
        aiBtn.addEventListener("click", () => {
            buffer.aiOpen = !buffer.aiOpen;
            renderSelectedEndpoints();
            if (buffer.aiOpen) {
                const node = el.selectedEndpointList.querySelector(`[data-group-id="${buffer.endpoint.id}"] .ai-panel`);
                if (node) node.scrollIntoView({ behavior: "smooth" });
            }
        });
        header.appendChild(aiBtn);

        const addBtn = document.createElement("button");
        addBtn.className = "btn small";
        addBtn.textContent = "新增用例";
        addBtn.addEventListener("click", () => addManualCaseTo(buffer));
        header.appendChild(addBtn);

        const saveBtn = document.createElement("button");
        saveBtn.className = "btn small primary group-save-btn";
        saveBtn.textContent = "保存用例";
        saveBtn.hidden = !buffer.dirty;
        saveBtn.addEventListener("click", () => saveBuffer(buffer));
        header.appendChild(saveBtn);

        wrap.appendChild(header);

        const body = document.createElement("div");
        body.className = "case-group-body";
        body.hidden = !buffer.expanded;
        if (buffer.loaded || buffer.dirty) {
            renderCasesInto(body, buffer, "暂无用例：点「为选中接口生成用例」批量生成、组内「AI 建议」补充，或「新增用例」手工填写");
        } else {
            const loading = document.createElement("p");
            loading.className = "diff-empty";
            loading.textContent = "用例加载中...";
            body.appendChild(loading);
        }
        if (buffer.aiOpen) body.appendChild(buildGroupAiPanel(buffer));
        wrap.appendChild(body);
        return wrap;
    }

    // 用例行渲染（面板与卡片分组共用）：buffer 为该端点的用例缓冲
    function renderCasesInto(container, buffer, emptyHint) {
        container.innerHTML = "";
        buffer.cases.forEach((testCase, index) => {
            const row = document.createElement("div");
            row.className = "case-row";

            const expandBtn = document.createElement("button");
            expandBtn.className = "case-expand-btn";
            expandBtn.innerHTML = '<i class="fas fa-chevron-right"></i>';
            expandBtn.title = "展开查看/编辑请求参数";
            row.appendChild(expandBtn);

            const dimension = dimensionLabel(testCase);
            if (dimension) {
                const dimBadge = document.createElement("span");
                dimBadge.className = "badge badge-dimension";
                dimBadge.textContent = dimension;
                row.appendChild(dimBadge);
            }
            const badge = document.createElement("span");
            badge.className = `badge badge-source badge-${testCase.source_type}`;
            badge.textContent = SOURCE_LABELS[testCase.source_type] || testCase.source_type;
            row.appendChild(badge);

            const assertionCount = (testCase.assertions || []).length;
            if (assertionCount > 0) {
                const assertionBadge = document.createElement("span");
                assertionBadge.className = "badge badge-assertions";
                assertionBadge.textContent = `断言 ${assertionCount}`;
                assertionBadge.title = "含响应体字段断言";
                row.appendChild(assertionBadge);
            }

            const name = document.createElement("span");
            name.className = "case-name";
            name.textContent = testCase.name;
            row.appendChild(name);

            const expected = document.createElement("span");
            expected.className = "case-expected";
            expected.textContent = `预期 ${testCase.expected_status}`;
            row.appendChild(expected);

            const enabledLabel = document.createElement("label");
            enabledLabel.className = "case-enabled";
            const checkbox = document.createElement("input");
            checkbox.type = "checkbox";
            checkbox.checked = !!testCase.enabled;
            checkbox.addEventListener("change", () => {
                buffer.cases[index].enabled = checkbox.checked;
                markBufferDirty(buffer);
            });
            enabledLabel.appendChild(checkbox);
            enabledLabel.appendChild(document.createTextNode("启用"));
            row.appendChild(enabledLabel);

            const deleteBtn = document.createElement("button");
            deleteBtn.className = "btn small danger";
            deleteBtn.textContent = "删除";
            deleteBtn.addEventListener("click", () => {
                buffer.cases.splice(index, 1);
                markBufferDirty(buffer);
                rerenderBuffer(buffer);
            });
            row.appendChild(deleteBtn);

            const editor = buildCaseEditor(testCase, index, buffer);
            expandBtn.addEventListener("click", () => {
                editor.hidden = !editor.hidden;
                expandBtn.innerHTML = editor.hidden
                    ? '<i class="fas fa-chevron-right"></i>'
                    : '<i class="fas fa-chevron-down"></i>';
            });

            container.appendChild(row);
            container.appendChild(editor);
        });
        if (buffer.cases.length === 0) {
            const empty = document.createElement("p");
            empty.className = "diff-empty";
            empty.textContent = emptyHint
                || "暂无用例：点击「规则引擎生成」从 schema 生成，「AI 业务建议」补充业务场景，或「新增用例」手工填写";
            container.appendChild(empty);
        }
    }

    function renderPanelCases() {
        const buffer = panelBuffer();
        el.caseList.innerHTML = "";
        if (!buffer) return;
        if (!buffer.loaded && !buffer.dirty) {
            const loading = document.createElement("p");
            loading.className = "diff-empty";
            loading.textContent = "用例加载中...";
            el.caseList.appendChild(loading);
            return;
        }
        renderCasesInto(el.caseList, buffer);
    }

    // 组内 AI 建议（两段式 D-017 同构）：提案不落库，勾选并入后经组头「保存用例」落库
    function buildGroupAiPanel(buffer) {
        const panel = document.createElement("div");
        panel.className = "ai-panel";

        const note = document.createElement("p");
        note.className = "rules-note";
        note.textContent = `为 ${buffer.endpoint.method.toUpperCase()} ${buffer.endpoint.path} 生成业务异常用例提案，勾选后「并入所选」再「保存用例」落库：`;
        panel.appendChild(note);

        const instruction = document.createElement("textarea");
        instruction.rows = 2;
        instruction.maxLength = 2000;
        instruction.placeholder = "例如：补充权限越权与重复提交场景";
        instruction.value = buffer.aiInstruction || "";
        instruction.addEventListener("input", () => { buffer.aiInstruction = instruction.value; });
        panel.appendChild(instruction);

        const generateRow = document.createElement("div");
        generateRow.className = "ai-generate-row";
        const generateBtn = document.createElement("button");
        generateBtn.className = "btn small primary";
        generateBtn.textContent = "生成提案";
        const hint = document.createElement("span");
        hint.className = "ai-generating-hint";
        hint.hidden = true;
        hint.textContent = "AI 正在分析接口，通常需要十几秒…";
        generateRow.appendChild(generateBtn);
        generateRow.appendChild(hint);
        panel.appendChild(generateRow);

        const proposalsArea = document.createElement("div");
        proposalsArea.hidden = true;
        const proposalsNote = document.createElement("p");
        proposalsNote.className = "rules-note";
        proposalsNote.textContent = "勾选要并入的提案，点「并入所选」：";
        const proposalsList = document.createElement("div");
        const actions = document.createElement("div");
        actions.className = "btn-row";
        actions.style.marginTop = "8px";
        const mergeBtn = document.createElement("button");
        mergeBtn.className = "btn small primary";
        mergeBtn.textContent = "并入所选";
        const discardBtn = document.createElement("button");
        discardBtn.className = "btn small";
        discardBtn.textContent = "放弃";
        actions.appendChild(mergeBtn);
        actions.appendChild(discardBtn);
        proposalsArea.appendChild(proposalsNote);
        proposalsArea.appendChild(proposalsList);
        proposalsArea.appendChild(actions);
        panel.appendChild(proposalsArea);

        let proposals = [];

        generateBtn.addEventListener("click", async () => {
            const ai = instruction.value.trim();
            if (!ai) return showMessage("请先输入补充指令", "error");
            generateBtn.disabled = true;
            hint.hidden = false;
            try {
                const response = await apiFetch(
                    `/api/api-specs/${state.currentSpec.id}/endpoints/${buffer.endpoint.id}/cases/ai-suggest`,
                    {
                        method: "POST",
                        headers: { "Content-Type": "application/json" },
                        body: JSON.stringify({ instruction: ai }),
                    }
                );
                const data = await response.json().catch(() => ({}));
                if (!response.ok) return showMessage(data.error || "AI 建议失败", "error");
                proposals = (data.proposals || []).map((p) => ({ ...p, checked: true }));
                if (data.truncated) showMessage("AI 输出被截断，结果可能不完整", "error");
                renderGroupProposals();
                proposalsArea.hidden = proposals.length === 0;
                if (proposals.length === 0) showMessage("AI 未给出提案", "error");
            } catch (error) {
                if (error.message !== "未登录") showMessage("AI 建议失败，请稍后重试", "error");
            } finally {
                generateBtn.disabled = false;
                hint.hidden = true;
            }
        });

        function renderGroupProposals() {
            proposalsList.innerHTML = "";
            proposals.forEach((proposal, index) => {
                const row = document.createElement("div");
                row.className = "case-row";
                const checkbox = document.createElement("input");
                checkbox.type = "checkbox";
                checkbox.checked = proposal.checked;
                checkbox.addEventListener("change", () => { proposals[index].checked = checkbox.checked; });
                row.appendChild(checkbox);
                const name = document.createElement("span");
                name.className = "case-name";
                name.textContent = proposal.name;
                row.appendChild(name);
                const expected = document.createElement("span");
                expected.className = "case-expected";
                expected.textContent = `预期 ${proposal.expected_status}`;
                row.appendChild(expected);
                proposalsList.appendChild(row);
            });
        }

        mergeBtn.addEventListener("click", () => {
            const chosen = proposals.filter((p) => p.checked);
            if (chosen.length === 0) return showMessage("请先勾选要并入的提案", "error");
            const seen = new Set(buffer.cases.map((c) => c.name));
            const merged = chosen
                .filter((p) => !seen.has(p.name))
                .map((p) => ({
                    name: p.name, request: p.request || {}, expected_status: p.expected_status,
                    assertions: p.assertions || [], source_type: "ai", enabled: true,
                }));
            buffer.cases = buffer.cases.concat(merged);
            buffer.aiOpen = false;
            markBufferDirty(buffer);
            rerenderBuffer(buffer);
            showMessage(`已并入 ${merged.length} 条提案，点「保存用例」落库`, "success");
        });

        discardBtn.addEventListener("click", () => {
            proposals = [];
            proposalsArea.hidden = true;
        });

        return panel;
    }

    async function saveBuffer(buffer, options = {}) {
        try {
            const response = await apiFetch(
                `/api/api-specs/${state.currentSpec.id}/endpoints/${buffer.endpoint.id}/cases`,
                {
                    method: "PUT",
                    headers: { "Content-Type": "application/json" },
                    body: JSON.stringify({
                        cases: buffer.cases.map((c) => ({
                            name: c.name, request: c.request || {}, expected_status: c.expected_status,
                            assertions: c.assertions || [], source_type: c.source_type, enabled: !!c.enabled,
                        })),
                    }),
                }
            );
            const data = await response.json().catch(() => ({}));
            if (!response.ok) {
                showMessage(data.error || "保存失败", "error");
                return false;
            }
            buffer.cases = data;
            buffer.dirty = false;
            syncEndpointCount(buffer.endpoint, data.length);
            rerenderBuffer(buffer);
            if (!options.silent) showMessage("用例已保存", "success");
            return true;
        } catch (error) {
            if (error.message !== "未登录") showMessage("保存失败，请稍后重试", "error");
            return false;
        }
    }

    async function saveAllDirtyGroups() {
        const dirty = [...state.selectedEndpointIds]
            .map((id) => state.buffers.get(id))
            .filter((b) => b && b.dirty);
        if (dirty.length === 0) return;
        let saved = 0;
        for (const buffer of dirty) {
            if (await saveBuffer(buffer, { silent: true })) saved += 1;
        }
        showMessage(`已保存 ${saved}/${dirty.length} 个接口的用例修改`, saved === dirty.length ? "success" : "error");
    }

    function updateRunScopeHint() {
        if (!el.runScopeHint) return;
        const endpoints = state.currentSpec ? (state.currentSpec.endpoints || []) : [];
        if (endpoints.length === 0) {
            el.runScopeHint.textContent = "";
            return;
        }
        const total = endpoints.length;
        const selected = endpoints.filter((e) => state.selectedEndpointIds.has(e.id));
        const sumCases = (list) => list.reduce((n, e) => n + (e.case_count || 0), 0);
        const emptyCount = (list) => list.filter((e) => !(e.case_count > 0)).length;
        const dirtySelected = [...state.selectedEndpointIds].some((id) => {
            const b = state.buffers.get(id);
            return b && b.dirty;
        });
        const dirtySuffix = dirtySelected ? " · 有未保存的用例修改" : "";
        if (selected.length === total) {
            const skipped = emptyCount(selected);
            el.runScopeHint.textContent = `执行范围：全部 ${total} 个接口 · 共 ${sumCases(selected)} 条用例`
                + (skipped > 0 ? ` · ${skipped} 个接口尚无用例（将跳过）` : "") + dirtySuffix;
        } else if (selected.length > 0) {
            const skipped = emptyCount(selected);
            el.runScopeHint.textContent = `执行范围：选中 ${selected.length} 个接口 · 共 ${sumCases(selected)} 条用例`
                + (skipped > 0 ? ` · ${skipped} 个接口尚无用例（将跳过）` : "") + dirtySuffix;
        } else {
            const buffer = state.panelEndpointId ? state.buffers.get(state.panelEndpointId) : null;
            if (buffer) {
                const count = buffer.loaded || buffer.dirty ? buffer.cases.length : (buffer.endpoint.case_count || 0);
                el.runScopeHint.textContent = `执行范围：当前接口（${count} 条用例）· 勾选接口可批量执行`;
            } else {
                el.runScopeHint.textContent = "执行范围：请先选择接口（勾选可批量执行）";
            }
        }
    }

    // ---------- 端点用例（单接口面板：仅未勾选接口的编辑路径，缓冲与卡片分组共用存储） ----------

    function closeEndpointPanel() {
        el.endpointPanel.hidden = true;
        state.panelEndpointId = null;
        el.saveCasesBtn.hidden = true;
        el.runResults.innerHTML = "";
        el.runSummary.hidden = true;
        el.runHistoryPanel.hidden = true;
    }

    async function selectEndpoint(endpoint) {
        if (state.selectedEndpointIds.has(endpoint.id)) {
            // 已勾选：定位到卡片内的分组（互斥——不在面板重复编辑）
            const buffer = state.buffers.get(endpoint.id);
            if (buffer) buffer.expanded = true;
            renderSelectedEndpoints();
            const node = el.selectedEndpointList.querySelector(`[data-group-id="${endpoint.id}"]`);
            if (node) node.scrollIntoView({ behavior: "smooth" });
            return;
        }
        state.panelEndpointId = endpoint.id;
        el.endpointTitle.textContent = `${endpoint.method.toUpperCase()} ${endpoint.path}`;
        el.endpointPanel.hidden = false;
        el.runBaseUrl.value = el.runBaseUrl.value || "http://127.0.0.1:8000";
        el.endpointPanel.scrollIntoView({ behavior: "smooth" });
        renderPanelCases();  // 缓冲未加载时先显示占位
        const existing = state.buffers.get(endpoint.id);
        el.saveCasesBtn.hidden = !(existing && existing.dirty);
        await ensureBuffer(endpoint);  // 加载完成后经 rerenderBuffer 刷新面板
    }

    const SOURCE_LABELS = { rule_engine: "规则引擎", ai: "AI 建议", manual: "手工" };
    const REQUEST_CONTAINERS = ["path", "query", "headers", "body"];

    // 用例编辑器（D-024）：名称/预期状态 + path/query/headers/body 四个 JSON 编辑区 + 响应断言（D-027）
    function buildCaseEditor(testCase, index, buffer) {
        const editor = document.createElement("div");
        editor.className = "case-editor";
        editor.hidden = true;

        const metaRow = document.createElement("div");
        metaRow.className = "case-editor-meta";
        const nameLabel = document.createElement("label");
        nameLabel.textContent = "名称";
        const nameInput = document.createElement("input");
        nameInput.type = "text";
        nameInput.value = testCase.name;
        const statusLabel = document.createElement("label");
        statusLabel.textContent = "预期状态";
        const statusInput = document.createElement("input");
        statusInput.type = "number";
        statusInput.min = 100;
        statusInput.max = 599;
        statusInput.value = testCase.expected_status;
        metaRow.appendChild(nameLabel);
        metaRow.appendChild(nameInput);
        metaRow.appendChild(statusLabel);
        metaRow.appendChild(statusInput);
        editor.appendChild(metaRow);

        const grid = document.createElement("div");
        grid.className = "case-editor-grid";
        const inputs = {};
        REQUEST_CONTAINERS.forEach((key) => {
            const cell = document.createElement("div");
            const label = document.createElement("label");
            label.textContent = key === "body" ? "body（JSON，留空表示无请求体）" : `${key}（JSON）`;
            const textarea = document.createElement("textarea");
            textarea.rows = key === "body" ? 6 : 3;
            textarea.spellcheck = false;
            const value = testCase.request ? testCase.request[key] : undefined;
            textarea.value = value === undefined || value === null ? "" : JSON.stringify(value, null, 2);
            inputs[key] = textarea;
            cell.appendChild(label);
            cell.appendChild(textarea);
            grid.appendChild(cell);
        });
        // 响应断言（D-027）：JSON 数组，留空 = 仅状态码断言
        const assertionCell = document.createElement("div");
        const assertionLabel = document.createElement("label");
        assertionLabel.textContent = "响应断言（JSON 数组，留空=仅状态码断言）";
        const assertionInput = document.createElement("textarea");
        assertionInput.rows = 4;
        assertionInput.spellcheck = false;
        assertionInput.placeholder = '[{"target": "code", "op": "eq", "expected": 0}]\nop：eq 值相等 / exists 字段存在 / type 类型核对；target 用点路径（如 data.id）';
        const assertions = testCase.assertions || [];
        assertionInput.value = assertions.length ? JSON.stringify(assertions, null, 2) : "";
        inputs.assertions = assertionInput;
        assertionCell.appendChild(assertionLabel);
        assertionCell.appendChild(assertionInput);
        grid.appendChild(assertionCell);
        editor.appendChild(grid);

        const actions = document.createElement("div");
        actions.className = "btn-row case-editor-actions";
        const applyBtn = document.createElement("button");
        applyBtn.className = "btn small primary";
        applyBtn.textContent = "应用修改";
        applyBtn.addEventListener("click", () => applyCaseEdit(index, inputs, nameInput, statusInput, buffer));
        const hint = document.createElement("span");
        hint.className = "case-editor-hint";
        hint.textContent = "应用后还需点「保存用例」落库";
        actions.appendChild(applyBtn);
        actions.appendChild(hint);
        editor.appendChild(actions);
        return editor;
    }

    function applyCaseEdit(index, inputs, nameInput, statusInput, buffer) {
        const request = {};
        for (const key of REQUEST_CONTAINERS) {
            const raw = inputs[key].value.trim();
            if (!raw) continue;
            try {
                const parsed = JSON.parse(raw);
                if (typeof parsed !== "object" || parsed === null || Array.isArray(parsed)) {
                    throw new Error("不是对象");
                }
                request[key] = parsed;
            } catch (error) {
                return showMessage(`${key} 不是合法的 JSON 对象`, "error");
            }
        }
        const rawAssertions = inputs.assertions.value.trim();
        let assertions = [];
        if (rawAssertions) {
            try {
                const parsed = JSON.parse(rawAssertions);
                if (!Array.isArray(parsed)) throw new Error("不是数组");
                const invalid = parsed.find((a) => !a || typeof a !== "object"
                    || typeof a.target !== "string" || !a.target
                    || !["eq", "exists", "type"].includes(a.op)
                    || (a.op !== "exists" && a.expected === undefined));
                if (invalid) throw new Error("断言项缺少 target/op/expected");
                assertions = parsed;
            } catch (error) {
                return showMessage(`响应断言格式不正确：${error.message}`, "error");
            }
        }
        const name = nameInput.value.trim();
        if (!name) return showMessage("用例名称不能为空", "error");
        const status = parseInt(statusInput.value, 10);
        if (!(status >= 100 && status <= 599)) return showMessage("预期状态需在 100-599 之间", "error");
        buffer.cases[index] = { ...buffer.cases[index], name, request, expected_status: status, assertions };
        markBufferDirty(buffer);
        rerenderBuffer(buffer);
        showMessage("已应用修改，点「保存用例」落库", "success");
    }

    function addManualCase() {
        const buffer = panelBuffer();
        if (!buffer) return showMessage("请先选择接口", "error");
        addManualCaseTo(buffer);
    }

    function addManualCaseTo(buffer) {
        const existingNames = new Set(buffer.cases.map((c) => c.name));
        let name = "手工用例";
        let seq = buffer.cases.length + 1;
        while (existingNames.has(name)) {
            name = `手工用例 ${seq}`;
            seq += 1;
        }
        const request = { path: {}, query: {}, headers: {} };
        if (["post", "put", "patch"].includes(buffer.endpoint.method)) request.body = {};
        buffer.cases.push({ name, request, expected_status: 200, assertions: [], source_type: "manual", enabled: true });
        markBufferDirty(buffer);
        rerenderBuffer(buffer);
    }

    async function generateCases() {
        const buffer = panelBuffer();
        if (!buffer) return showMessage("请先选择接口", "error");
        el.generateCasesBtn.disabled = true;
        try {
            const response = await apiFetch(
                `/api/api-specs/${state.currentSpec.id}/endpoints/${buffer.endpoint.id}/cases/generate`,
                { method: "POST" }
            );
            const data = await response.json().catch(() => ({}));
            if (!response.ok) return showMessage(data.error || "生成失败", "error");
            buffer.cases = data;
            buffer.dirty = false;
            buffer.loaded = true;
            renderPanelCases();
            el.saveCasesBtn.hidden = true;
            syncEndpointCount(buffer.endpoint, data.length);
            const generated = data.filter((c) => c.source_type === "rule_engine").length;
            showMessage(`规则引擎生成 ${generated} 条用例`, "success");
        } catch (error) {
            if (error.message !== "未登录") showMessage("生成失败，请稍后重试", "error");
        } finally {
            el.generateCasesBtn.disabled = false;
        }
    }

    // AI 业务建议（两段式 D-017 同构）：页内面板生成提案 → 勾选评审 → 并入后经「保存用例」落库
    function openAiPanel() {
        el.aiPanel.hidden = false;
        el.aiProposalsArea.hidden = true;
        state.proposals = [];
        el.aiPanel.scrollIntoView({ behavior: "smooth" });
    }

    function closeAiPanel() {
        el.aiPanel.hidden = true;
        state.proposals = [];
    }

    async function generateProposals() {
        const buffer = panelBuffer();
        if (!buffer) return showMessage("请先选择接口", "error");
        const instruction = el.aiInstructionInput.value.trim();
        if (!instruction) return showMessage("请先输入补充指令", "error");
        el.aiGenerateProposalsBtn.disabled = true;
        el.aiGeneratingHint.hidden = false;
        try {
            const response = await apiFetch(
                `/api/api-specs/${state.currentSpec.id}/endpoints/${buffer.endpoint.id}/cases/ai-suggest`,
                {
                    method: "POST",
                    headers: { "Content-Type": "application/json" },
                    body: JSON.stringify({ instruction }),
                }
            );
            const data = await response.json().catch(() => ({}));
            if (!response.ok) return showMessage(data.error || "AI 建议失败", "error");
            state.proposals = (data.proposals || []).map((p) => ({ ...p, checked: true }));
            if (data.truncated) showMessage("AI 输出被截断，结果可能不完整", "error");
            renderProposals();
            el.aiProposalsArea.hidden = state.proposals.length === 0;
            if (state.proposals.length === 0) showMessage("AI 未给出提案", "error");
        } catch (error) {
            if (error.message !== "未登录") showMessage("AI 建议失败，请稍后重试", "error");
        } finally {
            el.aiGenerateProposalsBtn.disabled = false;
            el.aiGeneratingHint.hidden = true;
        }
    }

    function renderProposals() {
        const list = el.aiProposalsList;
        list.innerHTML = "";
        state.proposals.forEach((proposal, index) => {
            const row = document.createElement("div");
            row.className = "case-row";
            const checkbox = document.createElement("input");
            checkbox.type = "checkbox";
            checkbox.checked = proposal.checked;
            checkbox.addEventListener("change", () => {
                state.proposals[index].checked = checkbox.checked;
            });
            row.appendChild(checkbox);
            const name = document.createElement("span");
            name.className = "case-name";
            name.textContent = proposal.name;
            row.appendChild(name);
            const expected = document.createElement("span");
            expected.className = "case-expected";
            expected.textContent = `预期 ${proposal.expected_status}`;
            row.appendChild(expected);
            list.appendChild(row);
        });
    }

    function mergeSelectedProposals() {
        const buffer = panelBuffer();
        if (!buffer) return showMessage("请先选择接口", "error");
        const selected = state.proposals.filter((p) => p.checked);
        if (selected.length === 0) return showMessage("请先勾选要并入的提案", "error");
        const seen = new Set(buffer.cases.map((c) => c.name));
        const merged = selected
            .filter((p) => !seen.has(p.name))
            .map((p) => ({
                name: p.name, request: p.request || {}, expected_status: p.expected_status,
                assertions: p.assertions || [], source_type: "ai", enabled: true,
            }));
        buffer.cases = buffer.cases.concat(merged);
        markBufferDirty(buffer);
        renderPanelCases();
        closeAiPanel();
        showMessage(`已并入 ${merged.length} 条提案，点「保存用例」落库`, "success");
    }

    function discardProposals() {
        state.proposals = [];
        el.aiProposalsArea.hidden = true;
    }

    // 规则引擎维度徽章：按用例名前缀映射（与 api_case_engine 命名约定对齐）
    const DIMENSION_RULES = [
        ["缺失必填", "缺失必填"],
        ["类型错误", "类型错误"],
        ["超出上限", "边界越界"],
        ["低于下限", "边界越界"],
        ["非法枚举", "非法枚举"],
        ["违反格式", "违反格式"],
    ];

    function dimensionLabel(testCase) {
        if (testCase.source_type !== "rule_engine") return null;
        for (const [prefix, label] of DIMENSION_RULES) {
            if (testCase.name === prefix || testCase.name.startsWith(prefix)) return label;
        }
        return null;
    }

    async function saveCases() {
        const buffer = panelBuffer();
        if (!buffer) return showMessage("请先选择接口", "error");
        el.saveCasesBtn.disabled = true;
        try {
            await saveBuffer(buffer);
        } finally {
            el.saveCasesBtn.disabled = false;
        }
    }

    // ---------- 批量生成（选中接口逐个跑规则引擎，D-027 配套：先有用例才能批量执行） ----------

    async function batchGenerateCases() {
        const endpoints = (state.currentSpec.endpoints || []).filter((e) => state.selectedEndpointIds.has(e.id));
        if (endpoints.length === 0) return showMessage("请先在接口清单勾选要生成用例的接口", "error");
        el.batchGenerateBtn.disabled = true;
        el.batchGenerateBtn.textContent = "生成中...";
        const results = [];
        try {
            for (const endpoint of endpoints) {
                const label = `${endpoint.method.toUpperCase()} ${endpoint.path}`;
                try {
                    const response = await apiFetch(
                        `/api/api-specs/${state.currentSpec.id}/endpoints/${endpoint.id}/cases/generate`,
                        { method: "POST" }
                    );
                    const data = await response.json().catch(() => ({}));
                    if (!response.ok) {
                        results.push({ label, ok: false, message: data.error || "生成失败" });
                        continue;
                    }
                    results.push({ label, ok: true, count: data.length });
                    // 生成结果直接进该端点的缓冲（组随 refreshSpecDetail 重建后立即展示）
                    const buffer = state.buffers.get(endpoint.id);
                    if (buffer) {
                        buffer.cases = data;
                        buffer.dirty = false;
                        buffer.loaded = true;
                    } else {
                        state.buffers.set(endpoint.id, {
                            endpoint, cases: data, dirty: false, loaded: true, expanded: true, aiOpen: false, aiInstruction: "",
                        });
                    }
                } catch (error) {
                    if (error.message === "未登录") throw error;
                    results.push({ label, ok: false, message: "请求失败" });
                }
            }
        } catch (error) {
            if (error.message !== "未登录") showMessage("批量生成失败，请稍后重试", "error");
            return;
        } finally {
            el.batchGenerateBtn.disabled = false;
            el.batchGenerateBtn.textContent = "为选中接口生成用例";
        }
        await refreshSpecDetail();
        const okItems = results.filter((r) => r.ok);
        const failItems = results.filter((r) => !r.ok);
        const parts = okItems.map((r) => `${r.label} 生成 ${r.count} 条`);
        parts.push(...failItems.map((r) => `${r.label} 失败：${r.message}`));
        showMessage(parts.join("；"), failItems.length ? "error" : "success");
    }

    async function refreshSpecDetail() {
        try {
            const response = await apiFetch(`/api/api-specs/${state.currentSpec.id}`);
            if (!response.ok) return;
            state.currentSpec = await response.json();
            const endpoints = state.currentSpec.endpoints || [];
            // 重挂缓冲的端点引用（case_count 等随详情刷新），已消失端点的缓冲一并清除
            endpoints.forEach((e) => {
                const b = state.buffers.get(e.id);
                if (b) b.endpoint = e;
            });
            [...state.buffers.keys()].forEach((id) => {
                if (!endpoints.some((e) => e.id === id)) state.buffers.delete(id);
            });
            renderEndpointTable();
        } catch (error) {
            if (error.message !== "未登录") showMessage("刷新接口清单失败", "error");
        }
    }

    // ---------- 执行（SSE 流式） ----------

    const VERDICT_LABELS = { passed: "通过", failed: "失败", error: "异常" };
    const ASSERTION_OP_LABELS = { eq: "值相等", exists: "字段存在", type: "类型核对" };

    async function startRun() {
        const baseUrl = el.runBaseUrl.value.trim();
        if (!baseUrl) return showMessage("请填写被测服务 base_url", "error");
        const endpoints = state.currentSpec.endpoints || [];
        const useAll = endpoints.length > 0 && state.selectedEndpointIds.size === endpoints.length;
        const selectedIds = [...state.selectedEndpointIds];
        const panelBuf = state.panelEndpointId ? state.buffers.get(state.panelEndpointId) : null;
        if (!useAll && selectedIds.length === 0 && !state.panelEndpointId) return showMessage("请先选择接口", "error");
        if (!useAll && selectedIds.length === 0 && panelBuf && !panelBuf.loaded) {
            return showMessage("用例加载中，请稍候再执行", "error");
        }
        if (!useAll && selectedIds.length === 0 && (!panelBuf || panelBuf.cases.length === 0)) {
            return showMessage("该接口还没有用例：请先「规则引擎生成」或「新增用例」", "error");
        }

        el.runStartBtn.disabled = true;
        el.runResults.innerHTML = "";
        el.runSummary.hidden = true;
        state.runEvents = [];
        try {
            const response = await fetch(`/api/api-specs/${state.currentSpec.id}/runs`, {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify({
                    base_url: baseUrl,
                    // 全选 → 缺省（后端跑全部启用用例）；部分勾选 → 所选接口；未勾选 → 当前接口
                    endpoint_ids: useAll ? null : (selectedIds.length > 0 ? selectedIds : [state.panelEndpointId]),
                }),
            });
            if (response.status === 401) return redirectToLogin();
            if (!response.ok || !response.body) {
                const data = await response.json().catch(() => ({}));
                return showMessage(data.error || "执行失败", "error");
            }
            // SSE 流式消费（fetch + getReader，与 workflow.js streamEvents 同模式）
            const reader = response.body.getReader();
            const decoder = new TextDecoder("utf-8");
            let buffer = "";
            while (true) {
                const { done, value } = await reader.read();
                if (done) break;
                buffer += decoder.decode(value, { stream: true });
                const parts = buffer.split("\n\n");
                buffer = parts.pop();
                for (const part of parts) {
                    const line = part.split("\n").find((l) => l.startsWith("data: "));
                    if (!line) continue;
                    const payload = line.slice(6).trim();
                    if (payload === "[DONE]") continue;
                    handleRunEvent(JSON.parse(payload));
                }
            }
        } catch (error) {
            if (error.message !== "未登录") showMessage("执行连接中断", "error");
        } finally {
            el.runStartBtn.disabled = false;
        }
    }

    function handleRunEvent(event) {
        if (event.event === "run_started") {
            el.runResults.innerHTML = "";
            // 本轮目标接口（PR4-UX 反馈 4：历史与实时面板都展示接口）
            const header = document.createElement("div");
            header.className = "run-target";
            header.textContent = `本轮目标：${(event.endpoints || []).join("、") || "-"}`;
            el.runResults.appendChild(header);
            return;
        }
        if (event.event === "auth_done") {
            const line = document.createElement("div");
            line.className = event.ok ? "run-target run-auth-ok" : "run-target run-auth-fail";
            line.textContent = event.ok ? `✓ ${event.message}` : `✗ ${event.message}`;
            el.runResults.appendChild(line);
            return;
        }
        if (event.event === "case_done") {
            state.runEvents.push(event);
            appendCaseResult(event);
            return;
        }
        if (event.event === "completed" || event.event === "failed") {
            showRunSummary(event);
            loadRunHistory();
        }
    }

    function appendCaseResult(event) {
        const row = document.createElement("div");
        row.className = `run-row run-${event.verdict}`;

        const verdict = document.createElement("span");
        verdict.className = `badge verdict-${event.verdict}`;
        verdict.textContent = VERDICT_LABELS[event.verdict] || event.verdict;
        row.appendChild(verdict);

        const name = document.createElement("span");
        name.className = "case-name";
        name.textContent = event.case_name;
        row.appendChild(name);

        if (event.endpoint) {
            const endpointLabel = document.createElement("span");
            endpointLabel.className = "run-endpoint";
            endpointLabel.textContent = event.endpoint;
            row.appendChild(endpointLabel);
        }

        const status = document.createElement("span");
        status.className = "run-status";
        status.textContent = `预期 ${event.expected_status} / 实际 ${event.actual_status ?? "-"}`;
        row.appendChild(status);

        const duration = document.createElement("span");
        duration.className = "run-duration";
        duration.textContent = `${event.duration_ms} ms`;
        row.appendChild(duration);

        // 可点击展开：失败原因 + 响应体快照（诊断执行失败，PR4-UX 反馈 3）
        row.style.cursor = "pointer";
        row.title = "点击查看详情";
        const detail = document.createElement("div");
        detail.className = "run-detail";
        detail.hidden = true;
        if (event.failure_reason) {
            const reason = document.createElement("p");
            reason.className = `run-reason run-reason-${event.verdict}`;
            reason.textContent = `⚠ ${event.failure_reason}`;
            detail.appendChild(reason);
        }
        // 响应断言明细（D-027）：逐条 ✓/✗ + 期望 vs 实际
        if ((event.assertions || []).length > 0) {
            const label = document.createElement("p");
            label.className = "run-detail-label";
            label.textContent = "响应断言：";
            detail.appendChild(label);
            event.assertions.forEach((a) => {
                const line = document.createElement("p");
                line.className = `run-assertion ${a.passed ? "run-assertion-ok" : "run-assertion-fail"}`;
                line.textContent = assertionLine(a);
                detail.appendChild(line);
            });
        }
        // 4xx 失败多为请求参数不满足服务端校验：引导展开用例编辑后重试
        if (event.verdict === "failed" && event.actual_status >= 400 && event.actual_status < 500) {
            const editHint = document.createElement("p");
            editHint.className = "run-reason";
            editHint.textContent = "可在上方用例中展开编辑请求参数与预期状态，保存后重试";
            detail.appendChild(editHint);
        }
        // 3xx 提示：执行器不自动跟随重定向，目标地址在响应头 location
        if (event.verdict === "failed" && event.actual_status >= 300 && event.actual_status < 400) {
            const redirectHint = document.createElement("p");
            redirectHint.className = "run-reason";
            redirectHint.textContent = "响应为重定向（3xx）：执行器不自动跟随，目标地址见响应头 location";
            detail.appendChild(redirectHint);
        }
        // 失败时展示完整响应信息（响应头 + 响应体，空体也明确提示）；通过且有体时同样可看
        if (event.response && (event.verdict !== "passed" || (event.response.body || ""))) {
            appendResponseInfo(detail, event.response);
        }
        row.addEventListener("click", () => {
            detail.hidden = !detail.hidden;
        });

        el.runResults.appendChild(row);
        el.runResults.appendChild(detail);
    }

    function showRunSummary(event) {
        el.runSummary.hidden = false;
        el.runSummary.innerHTML = "";
        const text = document.createElement("p");
        text.className = event.event === "failed" ? "run-summary-error" : "run-summary-ok";
        text.textContent = event.event === "failed"
            ? `执行异常：${event.error || "未知错误"}`
            : `执行完成：共 ${event.total} 条 · 通过 ${event.passed} · 失败 ${event.failed} · 异常 ${event.errored}`;
        el.runSummary.appendChild(text);
        // 确定性失败分组：failed 按实际状态码 × 接口聚类，error 归「异常」组
        if (event.event === "completed") {
            const groups = groupFailures(state.runEvents);
            groups.forEach((label) => {
                const line = document.createElement("p");
                line.className = "run-failure-group";
                line.textContent = label;
                el.runSummary.appendChild(line);
            });
        }
    }

    function groupFailures(events) {
        const groups = new Map();
        events.filter((e) => e.verdict !== "passed").forEach((e) => {
            const key = e.verdict === "error" ? "异常" : `HTTP ${e.actual_status ?? "-"}`;
            if (!groups.has(key)) groups.set(key, []);
            groups.get(key).push(`${e.endpoint || "-"}「${e.case_name}」`);
        });
        return [...groups.entries()].map(([key, items]) => {
            const shown = items.slice(0, 5).join("、");
            return `${key} × ${items.length}：${shown}${items.length > 5 ? ` 等 ${items.length} 条` : ""}`;
        });
    }

    function assertionLine(a) {
        const op = ASSERTION_OP_LABELS[a.op] || a.op;
        let text = `${a.passed ? "✓" : "✗"} ${a.target}［${op}］`;
        if (a.op !== "exists") text += ` 期望 ${a.expected ?? "-"}，实际 ${a.actual ?? "-"}`;
        if (a.message) text += `（${a.message}）`;
        return text;
    }

    // 响应信息块（响应头 + 响应体）：失败诊断的关键线索（重定向 location、错误详情等）；空体也明确提示
    function appendResponseInfo(container, response) {
        const headers = (response && response.headers) || {};
        const headerKeys = Object.keys(headers);
        if (headerKeys.length > 0) {
            const headerLabel = document.createElement("p");
            headerLabel.className = "run-detail-label";
            headerLabel.textContent = "响应头：";
            container.appendChild(headerLabel);
            const headerPre = document.createElement("pre");
            headerPre.className = "run-body run-headers";
            headerPre.textContent = headerKeys.map((k) => `${k}: ${headers[k]}`).join("\n");
            container.appendChild(headerPre);
        }
        const bodyLabel = document.createElement("p");
        bodyLabel.className = "run-detail-label";
        bodyLabel.textContent = "响应体：";
        container.appendChild(bodyLabel);
        const bodyPre = document.createElement("pre");
        bodyPre.className = "run-body";
        bodyPre.textContent = (response && response.body) || "（响应体为空）";
        container.appendChild(bodyPre);
    }

    async function loadRunHistory() {
        try {
            const response = await apiFetch(`/api/api-specs/${state.currentSpec.id}/runs`);
            if (!response.ok) return;
            const runs = await response.json();
            el.runHistoryPanel.hidden = false;
            const list = el.runHistory;
            list.innerHTML = "";
            runs.slice(0, 10).forEach((run) => {
                const li = document.createElement("li");
                li.className = "version-row";
                li.style.cursor = "pointer";
                li.title = "点击查看逐条结果";

                const status = document.createElement("span");
                status.className = `badge verdict-${run.status === "completed" ? "passed" : "failed"}`;
                status.textContent = run.status === "completed" ? "已完成" : "失败";

                const endpoints = document.createElement("span");
                endpoints.className = "version-note";
                endpoints.textContent = (run.endpoints || []).join("、") || "-";

                const runner = document.createElement("span");
                runner.className = "run-history-runner";
                runner.textContent = `执行人 ${run.created_by_username || "-"}`;

                const counts = document.createElement("span");
                counts.className = "run-history-counts";
                counts.textContent = `通过 ${run.passed} · 失败 ${run.failed} · 异常 ${run.errored}（共 ${run.total} 条）`;

                if (run.status === "failed" && run.error) {
                    const err = document.createElement("span");
                    err.className = "run-history-error";
                    err.textContent = run.error;
                    li.appendChild(err);
                }

                const time = document.createElement("span");
                time.className = "version-time";
                time.textContent = formatTime(run.finished_at || run.created_at);

                li.appendChild(status);
                li.appendChild(endpoints);
                li.appendChild(counts);
                li.appendChild(runner);
                li.appendChild(time);

                // 可点击展开：拉取逐条结果（PR4-UX 反馈 4）
                const detail = document.createElement("div");
                detail.className = "run-detail";
                detail.hidden = true;
                li.addEventListener("click", async () => {
                    if (!detail.hidden) {
                        detail.hidden = true;
                        return;
                    }
                    if (!detail.childNodes.length) {
                        detail.textContent = "加载中...";
                        try {
                            const response = await apiFetch(`/api/test-runs/${run.id}`);
                            if (!response.ok) {
                                detail.textContent = "加载失败";
                                return;
                            }
                            const view = await response.json();
                            detail.innerHTML = "";
                            (view.results || []).forEach((result) => {
                                const line = document.createElement("div");
                                line.className = `run-row run-${result.verdict}`;
                                const verdict = document.createElement("span");
                                verdict.className = `badge verdict-${result.verdict}`;
                                verdict.textContent = VERDICT_LABELS[result.verdict] || result.verdict;
                                const name = document.createElement("span");
                                name.className = "case-name";
                                name.textContent = result.case_name;
                                line.appendChild(verdict);
                                line.appendChild(name);
                                if (result.endpoint) {
                                    const endpointLabel = document.createElement("span");
                                    endpointLabel.className = "run-endpoint";
                                    endpointLabel.textContent = result.endpoint;
                                    line.appendChild(endpointLabel);
                                }
                                const statusText = document.createElement("span");
                                statusText.className = "run-status";
                                statusText.textContent = `预期 ${result.expected_status} / 实际 ${result.actual_status ?? "-"}`;
                                const duration = document.createElement("span");
                                duration.className = "run-duration";
                                duration.textContent = `${result.duration_ms} ms`;
                                line.appendChild(statusText);
                                line.appendChild(duration);
                                if (result.failure_reason) {
                                    const reason = document.createElement("p");
                                    reason.className = `run-reason run-reason-${result.verdict}`;
                                    reason.textContent = `⚠ ${result.failure_reason}`;
                                    line.appendChild(reason);
                                }
                                // 历史行内的断言摘要：全部通过给一行 ✓，失败只列未通过项
                                if ((result.assertions || []).length > 0) {
                                    const failedAssertions = result.assertions.filter((a) => !a.passed);
                                    const assertionsText = failedAssertions.length === 0
                                        ? `✓ 断言 ${result.assertions.length} 项全部通过`
                                        : failedAssertions.map(assertionLine).join("；");
                                    const assertionsLine = document.createElement("p");
                                    assertionsLine.className = failedAssertions.length
                                        ? "run-assertion run-assertion-fail"
                                        : "run-assertion run-assertion-ok";
                                    assertionsLine.textContent = assertionsText;
                                    line.appendChild(assertionsLine);
                                }
                                // 失败用例附完整响应信息（响应头 + 响应体），空体也明确提示
                                if (result.verdict !== "passed" && result.response) {
                                    const responseBlock = document.createElement("div");
                                    responseBlock.className = "run-response-block";
                                    appendResponseInfo(responseBlock, result.response);
                                    line.appendChild(responseBlock);
                                }
                                detail.appendChild(line);
                            });
                            if (!(view.results || []).length) {
                                detail.textContent = "无结果记录";
                            }
                        } catch (error) {
                            if (error.message !== "未登录") detail.textContent = "加载失败";
                        }
                    }
                    detail.hidden = false;
                });

                list.appendChild(li);
                list.appendChild(detail);
            });
            if (runs.length === 0) {
                const li = document.createElement("li");
                li.className = "version-row";
                li.textContent = "暂无执行历史";
                list.appendChild(li);
            }
        } catch (error) {
            if (error.message !== "未登录") showMessage("加载执行历史失败", "error");
        }
    }
})();
