// 页面公共工具：登录态跳转、带 401 处理的 fetch 包装、顶部消息提示。
// chat / knowledge / kb_detail 等页共用；样式在 static/css/nav.css 的 .message-alert 一节。
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

    window.redirectToLogin = redirectToLogin;
    window.apiFetch = apiFetch;
    window.isUnauthorized = isUnauthorized;
    window.showMessage = showMessage;
})();
