// API 接口管理：OpenAPI 导入（URL/粘贴）、同步、接口规格、用例管理（可编辑）、执行（PR4 / D-019~024）
// 与 testbench.js 同页共存：本文件负责「API 接口管理」tab；testbench.js 负责用例集 tab
(function () {
    "use strict";

    const state = { specs: [], currentSpec: null, currentEndpoint: null, cases: [], dirty: false, proposals: [] };
    const el = {};

    const ELEMENT_IDS = [
        "casesSection", "apisSection", "importSpecBtn", "apiSpecList", "apiSpecEmpty",
        "apiSpecDetail", "apiSpecBack", "apiSpecName", "apiSpecVisibility", "apiSpecMeta",
        "endpointTable", "endpointPanel", "endpointTitle", "generateCasesBtn", "aiSuggestBtn",
        "saveCasesBtn", "caseList", "runBaseUrl", "runStartBtn", "runResults", "runSummary",
        "runHistoryPanel", "runHistory", "importSpecModal", "importCloseBtn", "specNameInput",
        "specFormatSelect", "specContentInput", "importCancelBtn", "importConfirmBtn",
        "rulesToggleBtn", "rulesPanel", "aiPanel", "aiCloseBtn", "aiInstructionInput",
        "aiGenerateProposalsBtn", "aiGeneratingHint", "aiProposalsArea", "aiProposalsList",
        "aiMergeBtn", "aiDiscardBtn",
        "specSyncBtn", "addCaseBtn", "importModeSelect", "specUrlGroup", "specUrlInput",
        "specFormatGroup", "specContentGroup",
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
        document.querySelectorAll(".tb-tab").forEach((tab) => {
            tab.addEventListener("click", () => switchTab(tab.dataset.tab));
        });
        el.importSpecBtn.addEventListener("click", () => el.importSpecModal.classList.add("active"));
        el.importCloseBtn.addEventListener("click", closeImportModal);
        el.importCancelBtn.addEventListener("click", closeImportModal);
        el.importConfirmBtn.addEventListener("click", importSpec);
        el.importModeSelect.addEventListener("change", toggleImportMode);
        el.specSyncBtn.addEventListener("click", syncSpec);
        el.addCaseBtn.addEventListener("click", addManualCase);
        el.authToggleBtn.addEventListener("click", () => {
            el.authPanel.hidden = !el.authPanel.hidden;
        });
        el.authCloseBtn.addEventListener("click", () => { el.authPanel.hidden = true; });
        el.authSaveBtn.addEventListener("click", saveAuthConfig);
        el.authClearBtn.addEventListener("click", clearAuthConfig);
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
        loadSpecs();
    }

    function switchTab(tab) {
        document.querySelectorAll(".tb-tab").forEach((t) => {
            t.classList.toggle("active", t.dataset.tab === tab);
        });
        el.casesSection.hidden = tab !== "cases";
        el.apisSection.hidden = tab !== "apis";
        if (tab === "apis") loadSpecs();
    }

    // ---------- 规格列表 ----------

    async function loadSpecs() {
        el.apiSpecList.innerHTML = '<div class="loading-state"><i class="fas fa-spinner fa-spin"></i><p>加载中...</p></div>';
        try {
            const response = await apiFetch("/api/api-specs");
            if (!response.ok) throw new Error(`HTTP ${response.status}`);
            state.specs = await response.json();
            renderSpecList();
            el.apiSpecEmpty.hidden = state.specs.length > 0;
        } catch (error) {
            if (error.message !== "未登录") showMessage("加载接口文档失败", "error");
        }
    }

    function renderSpecList() {
        el.apiSpecList.innerHTML = "";
        state.specs.forEach((spec) => {
            const card = document.createElement("div");
            card.className = "test-set-card";
            card.style.cursor = "pointer";
            card.addEventListener("click", () => openSpec(spec.id));

            const header = document.createElement("div");
            header.className = "card-header";
            const name = document.createElement("h3");
            name.className = "card-title";
            name.textContent = spec.name;
            const badge = document.createElement("span");
            badge.className = `badge ${spec.visibility === "shared" ? "badge-shared" : "badge-private"}`;
            badge.textContent = spec.visibility === "shared" ? "共享" : "私有";
            header.appendChild(name);
            header.appendChild(badge);
            card.appendChild(header);

            const meta = document.createElement("div");
            meta.className = "card-meta";
            const sourceHost = spec.source_url ? sourceLabel(spec.source_url) : null;
            [`${spec.endpoint_count} 个接口`, `${spec.spec_title} v${spec.spec_version}`,
             spec.is_mine ? "我创建的" : `来自 ${spec.owner_username || "未知用户"}`,
             sourceHost, `更新于 ${formatTime(spec.updated_at)}`].filter(Boolean).forEach((text) => {
                const span = document.createElement("span");
                span.textContent = text;
                meta.appendChild(span);
            });
            card.appendChild(meta);

            if (spec.is_mine) {
                const actions = document.createElement("div");
                actions.className = "card-actions";
                const deleteBtn = document.createElement("button");
                deleteBtn.className = "btn danger";
                deleteBtn.textContent = "删除";
                deleteBtn.addEventListener("click", (event) => {
                    event.stopPropagation();
                    deleteSpec(spec);
                });
                actions.appendChild(deleteBtn);
                card.appendChild(actions);
            }

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
                    body: JSON.stringify({ url, name: name || null }),
                });
                const data = await response.json().catch(() => ({}));
                if (!response.ok) return showMessage(data.error || "导入失败", "error");
                showMessage(`已从 URL 导入 ${data.endpoint_count} 个接口，可在详情页「同步」刷新`, "success");
                closeImportModal();
                el.specUrlInput.value = "";
                el.specNameInput.value = "";
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
                body: JSON.stringify({ name, content, format: el.specFormatSelect.value || null }),
            });
            const data = await response.json().catch(() => ({}));
            if (!response.ok) return showMessage(data.error || "导入失败", "error");
            showMessage(`已导入 ${data.endpoint_count} 个接口`, "success");
            closeImportModal();
            el.specNameInput.value = "";
            el.specContentInput.value = "";
            el.specFormatSelect.value = "";
            await loadSpecs();
            openSpec(data.id);
        } catch (error) {
            if (error.message !== "未登录") showMessage("导入失败，请稍后重试", "error");
        } finally {
            el.importConfirmBtn.disabled = false;
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
        el.apiSpecList.hidden = true;
        el.apiSpecEmpty.hidden = true;
        el.apiSpecDetail.hidden = false;
        el.apiSpecName.textContent = state.currentSpec.name;
        el.apiSpecVisibility.textContent = state.currentSpec.visibility === "shared" ? "共享" : "私有";
        el.apiSpecVisibility.className = `badge ${state.currentSpec.visibility === "shared" ? "badge-shared" : "badge-private"}`;
        const host = state.currentSpec.source_url ? sourceLabel(state.currentSpec.source_url) : null;
        el.apiSpecMeta.textContent = [
            `${state.currentSpec.spec_title} v${state.currentSpec.spec_version}`,
            `${state.currentSpec.endpoint_count} 个接口`,
            state.currentSpec.is_mine ? "我创建的" : `来自 ${state.currentSpec.owner_username || "未知用户"}`,
            host,
        ].filter(Boolean).join(" · ");
        // 同步仅 URL 导入的创建者可用（同步会覆盖文档与接口快照）
        el.specSyncBtn.hidden = !(state.currentSpec.source_url && state.currentSpec.is_mine);
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
        el.apiSpecDetail.hidden = true;
        el.apiSpecList.hidden = false;
        el.apiSpecEmpty.hidden = state.specs.length > 0;
        loadSpecs();
    }

    function renderEndpointTable() {
        const table = el.endpointTable;
        table.innerHTML = "";
        const thead = document.createElement("thead");
        const headerRow = document.createElement("tr");
        ["方法", "路径", "说明"].forEach((text) => {
            const th = document.createElement("th");
            th.textContent = text;
            headerRow.appendChild(th);
        });
        thead.appendChild(headerRow);
        table.appendChild(thead);

        const tbody = document.createElement("tbody");
        (state.currentSpec.endpoints || []).forEach((endpoint) => {
            const tr = document.createElement("tr");
            tr.style.cursor = "pointer";
            tr.addEventListener("click", () => selectEndpoint(endpoint));

            const methodTd = document.createElement("td");
            const methodBadge = document.createElement("span");
            methodBadge.className = `method-badge method-${endpoint.method}`;
            methodBadge.textContent = endpoint.method.toUpperCase();
            methodTd.appendChild(methodBadge);

            const pathTd = document.createElement("td");
            pathTd.textContent = endpoint.path;
            const summaryTd = document.createElement("td");
            summaryTd.textContent = endpoint.summary || endpoint.operation_id || "-";

            tr.appendChild(methodTd);
            tr.appendChild(pathTd);
            tr.appendChild(summaryTd);
            tbody.appendChild(tr);
        });
        table.appendChild(tbody);
    }

    // ---------- 端点用例 ----------

    function closeEndpointPanel() {
        el.endpointPanel.hidden = true;
        state.currentEndpoint = null;
        state.cases = [];
        state.dirty = false;
        el.saveCasesBtn.hidden = true;
        el.runResults.innerHTML = "";
        el.runSummary.hidden = true;
        el.runHistoryPanel.hidden = true;
    }

    async function selectEndpoint(endpoint) {
        state.currentEndpoint = endpoint;
        el.endpointTitle.textContent = `${endpoint.method.toUpperCase()} ${endpoint.path}`;
        el.endpointPanel.hidden = false;
        el.runBaseUrl.value = el.runBaseUrl.value || "http://127.0.0.1:8000";
        el.endpointPanel.scrollIntoView({ behavior: "smooth" });
        await loadCases();
    }

    async function loadCases() {
        try {
            const response = await apiFetch(
                `/api/api-specs/${state.currentSpec.id}/endpoints/${state.currentEndpoint.id}/cases`
            );
            if (!response.ok) return showMessage("加载用例失败", "error");
            state.cases = await response.json();
            state.dirty = false;
            el.saveCasesBtn.hidden = true;
            renderCases();
        } catch (error) {
            if (error.message !== "未登录") showMessage("加载用例失败", "error");
        }
    }

    const SOURCE_LABELS = { rule_engine: "规则引擎", ai: "AI 建议", manual: "手工" };
    const REQUEST_CONTAINERS = ["path", "query", "headers", "body"];

    function renderCases() {
        const list = el.caseList;
        list.innerHTML = "";
        state.cases.forEach((testCase, index) => {
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
                state.cases[index].enabled = checkbox.checked;
                markDirty();
            });
            enabledLabel.appendChild(checkbox);
            enabledLabel.appendChild(document.createTextNode("启用"));
            row.appendChild(enabledLabel);

            const deleteBtn = document.createElement("button");
            deleteBtn.className = "btn small danger";
            deleteBtn.textContent = "删除";
            deleteBtn.addEventListener("click", () => {
                state.cases.splice(index, 1);
                markDirty();
                renderCases();
            });
            row.appendChild(deleteBtn);

            const editor = buildCaseEditor(testCase, index);
            expandBtn.addEventListener("click", () => {
                editor.hidden = !editor.hidden;
                expandBtn.innerHTML = editor.hidden
                    ? '<i class="fas fa-chevron-right"></i>'
                    : '<i class="fas fa-chevron-down"></i>';
            });

            list.appendChild(row);
            list.appendChild(editor);
        });
        if (state.cases.length === 0) {
            const empty = document.createElement("p");
            empty.className = "diff-empty";
            empty.textContent = "暂无用例：点击「规则引擎生成」从 schema 生成，「AI 业务建议」补充业务场景，或「新增用例」手工填写";
            list.appendChild(empty);
        }
    }

    // 用例编辑器（D-024）：名称/预期状态 + path/query/headers/body 四个 JSON 编辑区
    function buildCaseEditor(testCase, index) {
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
        editor.appendChild(grid);

        const actions = document.createElement("div");
        actions.className = "btn-row case-editor-actions";
        const applyBtn = document.createElement("button");
        applyBtn.className = "btn small primary";
        applyBtn.textContent = "应用修改";
        applyBtn.addEventListener("click", () => applyCaseEdit(index, inputs, nameInput, statusInput));
        const hint = document.createElement("span");
        hint.className = "case-editor-hint";
        hint.textContent = "应用后还需点「保存用例」落库";
        actions.appendChild(applyBtn);
        actions.appendChild(hint);
        editor.appendChild(actions);
        return editor;
    }

    function applyCaseEdit(index, inputs, nameInput, statusInput) {
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
        const name = nameInput.value.trim();
        if (!name) return showMessage("用例名称不能为空", "error");
        const status = parseInt(statusInput.value, 10);
        if (!(status >= 100 && status <= 599)) return showMessage("预期状态需在 100-599 之间", "error");
        state.cases[index] = { ...state.cases[index], name, request, expected_status: status };
        markDirty();
        renderCases();
        showMessage("已应用修改，点「保存用例」落库", "success");
    }

    function addManualCase() {
        if (!state.currentEndpoint) return showMessage("请先选择接口", "error");
        const existingNames = new Set(state.cases.map((c) => c.name));
        let name = "手工用例";
        let seq = state.cases.length + 1;
        while (existingNames.has(name)) {
            name = `手工用例 ${seq}`;
            seq += 1;
        }
        const request = { path: {}, query: {}, headers: {} };
        if (["post", "put", "patch"].includes(state.currentEndpoint.method)) request.body = {};
        state.cases.push({ name, request, expected_status: 200, source_type: "manual", enabled: true });
        markDirty();
        renderCases();
    }

    function markDirty() {
        state.dirty = true;
        el.saveCasesBtn.hidden = false;
    }

    async function generateCases() {
        el.generateCasesBtn.disabled = true;
        try {
            const response = await apiFetch(
                `/api/api-specs/${state.currentSpec.id}/endpoints/${state.currentEndpoint.id}/cases/generate`,
                { method: "POST" }
            );
            const data = await response.json().catch(() => ({}));
            if (!response.ok) return showMessage(data.error || "生成失败", "error");
            state.cases = data;
            state.dirty = false;
            el.saveCasesBtn.hidden = true;
            renderCases();
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
        const instruction = el.aiInstructionInput.value.trim();
        if (!instruction) return showMessage("请先输入补充指令", "error");
        el.aiGenerateProposalsBtn.disabled = true;
        el.aiGeneratingHint.hidden = false;
        try {
            const response = await apiFetch(
                `/api/api-specs/${state.currentSpec.id}/endpoints/${state.currentEndpoint.id}/cases/ai-suggest`,
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
        const selected = state.proposals.filter((p) => p.checked);
        if (selected.length === 0) return showMessage("请先勾选要并入的提案", "error");
        const seen = new Set(state.cases.map((c) => c.name));
        const merged = selected
            .filter((p) => !seen.has(p.name))
            .map((p) => ({
                name: p.name, request: p.request || {}, expected_status: p.expected_status,
                source_type: "ai", enabled: true,
            }));
        state.cases = state.cases.concat(merged);
        markDirty();
        renderCases();
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
        el.saveCasesBtn.disabled = true;
        try {
            const response = await apiFetch(
                `/api/api-specs/${state.currentSpec.id}/endpoints/${state.currentEndpoint.id}/cases`,
                {
                    method: "PUT",
                    headers: { "Content-Type": "application/json" },
                    body: JSON.stringify({
                        cases: state.cases.map((c) => ({
                            name: c.name, request: c.request || {}, expected_status: c.expected_status,
                            source_type: c.source_type, enabled: !!c.enabled,
                        })),
                    }),
                }
            );
            const data = await response.json().catch(() => ({}));
            if (!response.ok) return showMessage(data.error || "保存失败", "error");
            state.cases = data;
            state.dirty = false;
            el.saveCasesBtn.hidden = true;
            renderCases();
            showMessage("用例已保存", "success");
        } catch (error) {
            if (error.message !== "未登录") showMessage("保存失败，请稍后重试", "error");
        } finally {
            el.saveCasesBtn.disabled = false;
        }
    }

    // ---------- 执行（SSE 流式） ----------

    const VERDICT_LABELS = { passed: "通过", failed: "失败", error: "异常" };

    async function startRun() {
        const baseUrl = el.runBaseUrl.value.trim();
        if (!baseUrl) return showMessage("请填写被测服务 base_url", "error");
        if (!state.currentEndpoint) return showMessage("请先选择接口", "error");
        if (state.cases.length === 0) return showMessage("该接口还没有用例：请先「规则引擎生成」或「新增用例」", "error");

        el.runStartBtn.disabled = true;
        el.runResults.innerHTML = "";
        el.runSummary.hidden = true;
        try {
            const response = await fetch(`/api/api-specs/${state.currentSpec.id}/runs`, {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify({
                    base_url: baseUrl,
                    endpoint_ids: [state.currentEndpoint.id],
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
        // 4xx 失败多为请求参数不满足服务端校验：引导展开用例编辑后重试
        if (event.verdict === "failed" && event.actual_status >= 400 && event.actual_status < 500) {
            const editHint = document.createElement("p");
            editHint.className = "run-reason";
            editHint.textContent = "可在上方用例中展开编辑请求参数与预期状态，保存后重试";
            detail.appendChild(editHint);
        }
        const body = (event.response && event.response.body) || "";
        if (body) {
            const bodyLabel = document.createElement("p");
            bodyLabel.className = "run-detail-label";
            bodyLabel.textContent = "响应体：";
            detail.appendChild(bodyLabel);
            const pre = document.createElement("pre");
            pre.className = "run-body";
            pre.textContent = body;
            detail.appendChild(pre);
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
                                const statusText = document.createElement("span");
                                statusText.className = "run-status";
                                statusText.textContent = `预期 ${result.expected_status} / 实际 ${result.actual_status ?? "-"}`;
                                const duration = document.createElement("span");
                                duration.className = "run-duration";
                                duration.textContent = `${result.duration_ms} ms`;
                                line.appendChild(verdict);
                                line.appendChild(name);
                                line.appendChild(statusText);
                                line.appendChild(duration);
                                if (result.failure_reason) {
                                    const reason = document.createElement("p");
                                    reason.className = `run-reason run-reason-${result.verdict}`;
                                    reason.textContent = `⚠ ${result.failure_reason}`;
                                    line.appendChild(reason);
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
