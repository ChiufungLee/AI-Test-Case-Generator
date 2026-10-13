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

    window.redirectToLogin = redirectToLogin;
    window.apiFetch = apiFetch;
    window.isUnauthorized = isUnauthorized;
    window.showMessage = showMessage;
    window.fillCaseCell = fillCaseCell;
    window.formatTestCaseTrace = formatTestCaseTrace;
})();
