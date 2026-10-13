// 页面公共工具：登录态跳转、带 401 处理的 fetch 包装、顶部消息提示、统一 tooltip、用例表长文本单元格。
// chat / knowledge / kb_detail 等页共用；样式在 static/css/nav.css 的
// .message-alert（消息提示）、.floating-tip（tooltip）、.cell-clamp / .cell-toggle（用例表折叠）各节。
(function () {
    "use strict";

    function redirectToLogin() {
        window.location.href = "/login?logout=true";
    }

    // 包装 fetch：401 统一跳登录页并抛出可识别的“未登录”错误，
    // 调用方在 catch 里用 isUnauthorized(error) 跳过重复报错
    async function apiFetch(url, options) {
        const response = await fetch(url, options);
        if (response.status === 401) {
            redirectToLogin();
            throw new Error("未登录");
        }
        return response;
    }

    function isUnauthorized(error) {
        return Boolean(error) && error.message === "未登录";
    }

    // 顶部消息提示：同名提示只保留一条，4 秒后自动消失
    function showMessage(message, type = "info") {
        const existing = document.querySelector(".message-alert");
        if (existing) existing.remove();

        const alertDiv = document.createElement("div");
        alertDiv.className = `message-alert message-${type}`;
        const span = document.createElement("span");
        span.textContent = message;
        const closeBtn = document.createElement("button");
        closeBtn.className = "message-close";
        closeBtn.setAttribute("aria-label", "关闭提示");
        closeBtn.innerHTML = "&times;";
        closeBtn.addEventListener("click", () => alertDiv.remove());
        alertDiv.appendChild(span);
        alertDiv.appendChild(closeBtn);
        document.body.appendChild(alertDiv);

        setTimeout(() => {
            alertDiv.classList.add("message-alert-hide");
            setTimeout(() => alertDiv.remove(), 300);
        }, 4000);
    }

    // 用例表长文本单元格：折叠到固定行数 + 「展开/收起」按钮（工作流页与测试用例集页共用）。
    // 判定用文本本身（字符数/换行数）而非布局测量：表格常渲染在隐藏面板里，
    // 隐藏时 scrollHeight 恒为 0，测量式判定会漏掉展开入口。
    const CELL_CLAMP_CHARS = 80;
    const CELL_CLAMP_LINES = 4;

    function fillCaseCell(td, text) {
        const raw = text == null ? "" : String(text);
        td.textContent = "";
        const needsClamp =
            raw.length > CELL_CLAMP_CHARS || raw.split("\n").length > CELL_CLAMP_LINES;
        if (!needsClamp) {
            td.textContent = raw;
            return;
        }

        const clamp = document.createElement("div");
        clamp.className = "cell-clamp";
        clamp.textContent = raw;
        td.appendChild(clamp);

        const toggle = document.createElement("button");
        toggle.type = "button";
        toggle.className = "cell-toggle";
        toggle.textContent = "展开";
        toggle.setAttribute("aria-expanded", "false");
        toggle.addEventListener("click", () => {
            const expanded = clamp.classList.toggle("expanded");
            toggle.textContent = expanded ? "收起" : "展开";
            toggle.setAttribute("aria-expanded", String(expanded));
        });
        td.appendChild(toggle);
    }

    // 测试点追溯 + 覆盖项合并为一列展示（表宽优化）：测试点编号在上、覆盖项编号在下，
    // 两者都为空则返回空串。数据模型、CSV 导出与编辑表单仍分列保存，这里只影响只读表格。
    function formatTestCaseTrace(testPoints, coveredItems) {
        const points = (testPoints || []).join(", ");
        const items = (coveredItems || []).join(", ");
        return [points, items].filter(Boolean).join("\n");
    }

    /* ---------- 统一 tooltip（替代浏览器默认的 title 提示） ----------
       用法：元素上加 data-tip="说明文字"。气泡是挂在 body 上的 .floating-tip（样式见 nav.css），
       由这里定位：先按目标中心水平居中，再往视口内收敛（左边缘的侧栏项、右边缘的操作按钮都不会被裁），
       上方空间不足时翻到元素下方并翻转箭头方向。因此同一样式对页面各处都成立，
       页面 CSS 不需要、也不要给 [data-tip] 加定位或 ::after/::before。 */
    const TIP_GAP = 10;
    const TIP_EDGE = 8;
    let tipNode = null;
    let tipTarget = null;

    function ensureTipNode() {
        if (tipNode) return tipNode;
        tipNode = document.createElement("div");
        tipNode.className = "floating-tip";
        tipNode.setAttribute("role", "tooltip");
        document.body.appendChild(tipNode);
        return tipNode;
    }

    // 命中最内层的 [data-tip]：嵌套提示（如卡片里的删除按钮）各显示各自的气泡
    function tipTargetOf(event) {
        const node = event.target;
        return node instanceof Element ? node.closest("[data-tip]") : null;
    }

    function showTip(target) {
        const text = target.getAttribute("data-tip");
        if (!text) return;
        tipTarget = target;

        const node = ensureTipNode();
        if (node.textContent !== text) node.textContent = text;
        node.classList.add("visible");

        const rect = target.getBoundingClientRect();
        const size = node.getBoundingClientRect();
        let left = rect.left + rect.width / 2 - size.width / 2;
        left = Math.max(TIP_EDGE, Math.min(left, window.innerWidth - size.width - TIP_EDGE));
        let top = rect.top - size.height - TIP_GAP;
        const below = top < TIP_EDGE;
        if (below) top = rect.bottom + TIP_GAP;

        node.classList.toggle("below", below);
        node.style.setProperty("--tip-arrow-x", `${Math.round(rect.left + rect.width / 2 - left)}px`);
        node.style.left = `${Math.round(left)}px`;
        node.style.top = `${Math.round(top)}px`;
    }

    function hideTip() {
        tipTarget = null;
        if (tipNode) tipNode.classList.remove("visible", "below");
    }

    document.addEventListener("mouseover", (event) => {
        const target = tipTargetOf(event);
        if (!target || target === tipTarget) return;
        showTip(target);
    });

    document.addEventListener("mouseout", (event) => {
        const target = tipTargetOf(event);
        // 在同一个提示元素内部移动不隐藏（子元素之间切换交给 mouseover 处理）
        if (!target || target.contains(event.relatedTarget)) return;
        hideTip();
    });

    document.addEventListener("focusin", (event) => {
        const target = tipTargetOf(event);
        if (target) showTip(target);
    });

    document.addEventListener("focusout", hideTip);
    // 点击、滚动（含容器内滚动）、改变窗口大小时收起：位置都是按视口算的，留着会错位
    document.addEventListener("click", hideTip, true);
    window.addEventListener("scroll", hideTip, true);
    window.addEventListener("resize", hideTip);

    /* ---------- 折叠卡片编辑器（工作流详情 / 用例集详情共用） ----------
       结构：.edit-card（加 .collapsed 表示折叠）> .edit-summary（折叠态一行纯文本）
       + .edit-body（展开态字段）。折叠只切 CSS，输入框始终留在 DOM 里，页面收集表单
       时不需要区分折叠态。摘要内容各页不同：卡片上挂 card.syncSummary(card) 回调，
       收起时由这里调用它把最新值写回摘要行（摘要因此不用手工维护）。 */
    function summarySpan(className, text) {
        const span = document.createElement("span");
        span.className = className;
        span.textContent = text;
        return span;
    }

    // onToggle 可选：展开状态需要持久化的页面（如接口测试的用例卡）在这里记一笔
    function buildCollapseToggle(card, onToggle = null) {
        const button = document.createElement("button");
        button.type = "button";
        button.className = "edit-toggle";
        button.addEventListener("click", () => {
            setCardCollapsed(card, !card.classList.contains("collapsed"));
            if (typeof onToggle === "function") onToggle(card);
        });
        return button;
    }

    function setCardCollapsed(card, collapsed) {
        card.classList.toggle("collapsed", collapsed);
        // 折叠时把输入框里的最新值写回摘要行；展开时让文本域按内容长高
        if (collapsed && typeof card.syncSummary === "function") card.syncSummary(card);
        if (!collapsed) autoGrowTextareas(card);
        card.querySelectorAll(".edit-toggle").forEach((button) => {
            button.textContent = collapsed ? "展开" : "收起";
            button.dataset.tip = collapsed ? "展开后可编辑这一条" : "收起这一条";
        });
    }

    // 行内删除按钮（卡片摘要行与卡头共用）：给了 onClick 就交给调用方处理（如"删除用例"要同时改模型）
    function buildRowRemoveButton(target, tip = "删除该条", onClick = null) {
        const remove = document.createElement("button");
        remove.type = "button";
        remove.className = "row-remove";
        remove.dataset.tip = tip;
        remove.textContent = "✕";
        remove.addEventListener("click", () => (onClick ? onClick() : target.remove()));
        return remove;
    }

    /* ---------- 折叠卡片里的字段控件（工作流详情 / 用例集详情共用） ---------- */
    function editInput(sub, value, className, placeholder) {
        const input = document.createElement("input");
        input.type = "text";
        input.className = className;
        input.dataset.sub = sub;
        input.value = value ?? "";
        if (placeholder) input.placeholder = placeholder;
        return input;
    }

    function editSelect(sub, options, value) {
        const select = document.createElement("select");
        select.className = "edit-select";
        select.dataset.sub = sub;
        options.forEach((option) => {
            // 选项写成 "P0" 或 ["high", "高"] 都支持：后者用于「值 ≠ 显示文案」的情况
            const [optionValue, label] = Array.isArray(option) ? option : [option, option];
            const optionEl = document.createElement("option");
            optionEl.value = optionValue;
            optionEl.textContent = label;
            if (optionValue === value) optionEl.selected = true;
            select.appendChild(optionEl);
        });
        return select;
    }

    function editTextarea(sub, value, className, placeholder, rows = 2) {
        const area = document.createElement("textarea");
        area.className = className;
        area.dataset.sub = sub;
        area.rows = rows;
        area.value = value ?? "";
        if (placeholder) area.placeholder = placeholder;
        area.addEventListener("input", () => autoGrowTextarea(area));
        return area;
    }

    /* 带标签的一行；inline=true 时标签与控件并排（维度/优先级/风险这类短控件），
       否则是「左标签 + 右控件」两列（长文本字段，标签对齐、纵向只占一行） */
    function editField(labelText, control, inline = false) {
        const row = document.createElement("div");
        row.className = inline ? "edit-inline" : "edit-field";
        const label = document.createElement("span");
        label.className = "edit-label";
        label.textContent = labelText;
        row.appendChild(label);
        row.appendChild(control);
        return row;
    }

    /* 标签在上的字段块：标签较长或控件是大段文本（如 JSON）时用，配合 .edit-grid 两列排布 */
    function editStackField(labelText, control) {
        const cell = document.createElement("div");
        cell.className = "edit-stack";
        const label = document.createElement("span");
        label.className = "edit-label";
        label.textContent = labelText;
        cell.appendChild(label);
        cell.appendChild(control);
        return cell;
    }

    /* 文本域随内容长高：固定行数在长内容时会出现内部滚动条，展开卡片后看起来又堵又碎。
       隐藏状态下量不出高度（scrollHeight 为 0），所以只在展开时与输入时重算——
       新增"展开/显示"入口时记得补一次 autoGrowTextareas(root)。 */
    function autoGrowTextarea(area) {
        if (area.offsetParent === null) return;
        area.style.height = "auto";
        area.style.height = `${area.scrollHeight}px`;
    }

    function autoGrowTextareas(root) {
        root.querySelectorAll("textarea.edit-input").forEach(autoGrowTextarea);
    }

    window.redirectToLogin = redirectToLogin;
    window.apiFetch = apiFetch;
    window.isUnauthorized = isUnauthorized;
    window.showMessage = showMessage;
    window.fillCaseCell = fillCaseCell;
    window.formatTestCaseTrace = formatTestCaseTrace;
    window.summarySpan = summarySpan;
    window.buildCollapseToggle = buildCollapseToggle;
    window.setCardCollapsed = setCardCollapsed;
    window.buildRowRemoveButton = buildRowRemoveButton;
    window.autoGrowTextarea = autoGrowTextarea;
    window.autoGrowTextareas = autoGrowTextareas;
    window.editInput = editInput;
    window.editSelect = editSelect;
    window.editTextarea = editTextarea;
    window.editField = editField;
    window.editStackField = editStackField;
})();
