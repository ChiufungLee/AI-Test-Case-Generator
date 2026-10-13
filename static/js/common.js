// 页面公共工具：登录态跳转、带 401 处理的 fetch 包装、顶部消息提示、用例表长文本单元格。
// chat / knowledge / kb_detail 等页共用；样式在 static/css/nav.css 的
// .message-alert（消息提示）与 .cell-clamp / .cell-toggle（用例表折叠）两节。
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

    window.redirectToLogin = redirectToLogin;
    window.apiFetch = apiFetch;
    window.isUnauthorized = isUnauthorized;
    window.showMessage = showMessage;
    window.fillCaseCell = fillCaseCell;
    window.formatTestCaseTrace = formatTestCaseTrace;
})();
