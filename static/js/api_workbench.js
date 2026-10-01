// API 测试工作台：OpenAPI 导入、接口规格、用例管理、执行（PR4 / D-019~021）
// 与 testbench.js 同页共存：本文件负责「API 规格」tab；testbench.js 负责用例集 tab
(function () {
    "use strict";

    const state = { specs: [], currentSpec: null, currentEndpoint: null, cases: [], dirty: false };
    const el = {};

    const ELEMENT_IDS = [
        "casesSection", "apisSection", "importSpecBtn", "apiSpecList", "apiSpecEmpty",
        "apiSpecDetail", "apiSpecBack", "apiSpecName", "apiSpecVisibility", "apiSpecMeta",
        "endpointTable", "endpointPanel", "endpointTitle", "generateCasesBtn", "aiSuggestBtn",
        "saveCasesBtn", "caseList", "runBaseUrl", "runStartBtn", "runResults", "runSummary",
        "runHistoryPanel", "runHistory", "importSpecModal", "importCloseBtn", "specNameInput",
        "specFormatSelect", "specContentInput", "importCancelBtn", "importConfirmBtn",
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
        el.apiSpecBack.addEventListener("click", (event) => {
            event.preventDefault();
            showSpecList();
        });
        el.generateCasesBtn.addEventListener("click", generateCases);
        el.aiSuggestBtn.addEventListener("click", aiSuggest);
        el.saveCasesBtn.addEventListener("click", saveCases);
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
            if (error.message !== "未登录") showMessage("加载 API 规格失败", "error");
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
            [`${spec.endpoint_count} 个接口`, `${spec.spec_title} v${spec.spec_version}`,
             spec.is_mine ? "我创建的" : `来自 ${spec.owner_username || "未知用户"}`,
             `更新于 ${formatTime(spec.updated_at)}`].forEach((text) => {
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
        if (!confirm(`确定删除 API 规格「${spec.name}」吗？接口清单与执行历史将一并删除。`)) return;
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

    async function importSpec() {
        const name = el.specNameInput.value.trim();
        const content = el.specContentInput.value;
        if (!name || !content.trim()) return showMessage("请填写规格名称与文档内容", "error");
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

    // ---------- 规格详情 ----------

    async function openSpec(specId) {
        try {
            const response = await apiFetch(`/api/api-specs/${specId}`);
            if (!response.ok) return showMessage("加载规格失败", "error");
            state.currentSpec = await response.json();
        } catch (error) {
            if (error.message !== "未登录") showMessage("加载规格失败", "error");
            return;
        }
        el.apiSpecList.hidden = true;
        el.apiSpecEmpty.hidden = true;
        el.apiSpecDetail.hidden = false;
        el.apiSpecName.textContent = state.currentSpec.name;
        el.apiSpecVisibility.textContent = state.currentSpec.visibility === "shared" ? "共享" : "私有";
        el.apiSpecVisibility.className = `badge ${state.currentSpec.visibility === "shared" ? "badge-shared" : "badge-private"}`;
        el.apiSpecMeta.textContent = [
            `${state.currentSpec.spec_title} v${state.currentSpec.spec_version}`,
            `${state.currentSpec.endpoint_count} 个接口`,
            state.currentSpec.is_mine ? "我创建的" : `来自 ${state.currentSpec.owner_username || "未知用户"}`,
        ].join(" · ");
        renderEndpointTable();
        closeEndpointPanel();
        loadRunHistory();
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

    function renderCases() {
        const list = el.caseList;
        list.innerHTML = "";
        state.cases.forEach((testCase, index) => {
            const row = document.createElement("div");
            row.className = "case-row";

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

            list.appendChild(row);
        });
        if (state.cases.length === 0) {
            const empty = document.createElement("p");
            empty.className = "diff-empty";
            empty.textContent = "暂无用例：点击「规则引擎生成」从 schema 生成，或「AI 业务建议」补充业务异常场景";
            list.appendChild(empty);
        }
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

    async function aiSuggest() {
        const instruction = prompt("补充指令（业务异常维度，如：权限、并发、脏数据）", "补充权限与并发异常场景");
        if (!instruction || !instruction.trim()) return;
        el.aiSuggestBtn.disabled = true;
        try {
            const response = await apiFetch(
                `/api/api-specs/${state.currentSpec.id}/endpoints/${state.currentEndpoint.id}/cases/ai-suggest`,
                {
                    method: "POST",
                    headers: { "Content-Type": "application/json" },
                    body: JSON.stringify({ instruction: instruction.trim() }),
                }
            );
            const data = await response.json().catch(() => ({}));
            if (!response.ok) return showMessage(data.error || "AI 建议失败", "error");
            // 两段式（D-017 同构）：AI 提案仅并入本地待保存列表，确认后经「保存用例」落库
            const proposals = (data.proposals || []).map((p) => ({
                name: p.name, request: p.request || {}, expected_status: p.expected_status,
                source_type: "ai", enabled: true,
            }));
            const seen = new Set(state.cases.map((c) => c.name));
            const merged = proposals.filter((p) => !seen.has(p.name));
            state.cases = state.cases.concat(merged);
            if (data.truncated) showMessage("AI 输出被截断，结果可能不完整", "error");
            markDirty();
            renderCases();
            showMessage(`AI 建议 ${merged.length} 条提案已并入，确认后点「保存用例」落库`, "success");
        } catch (error) {
            if (error.message !== "未登录") showMessage("AI 建议失败，请稍后重试", "error");
        } finally {
            el.aiSuggestBtn.disabled = false;
        }
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

        el.runResults.appendChild(row);
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
                const status = document.createElement("span");
                status.className = `badge verdict-${run.status === "completed" ? "passed" : "failed"}`;
                status.textContent = run.status === "completed" ? "已完成" : "失败";
                const counts = document.createElement("span");
                counts.className = "version-note";
                counts.textContent = `通过 ${run.passed} · 失败 ${run.failed} · 异常 ${run.errored}（共 ${run.total} 条）`;
                const time = document.createElement("span");
                time.className = "version-time";
                time.textContent = formatTime(run.finished_at || run.created_at);
                li.appendChild(status);
                li.appendChild(counts);
                li.appendChild(time);
                list.appendChild(li);
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
