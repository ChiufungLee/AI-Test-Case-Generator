/* 新建测试任务页：需求原文 + 需求文档上传 + 知识库选择 → 创建后跳详情页自动开始 */

// 需求文档上传：与服务端 services/knowledge_service.py 保持一致（类型、大小、正文字符上限）
const REQUIREMENT_DOC_EXTENSIONS = [".pdf", ".docx", ".txt", ".md"];
const REQUIREMENT_FILE_MAX_BYTES = 50 * 1024 * 1024;
const REQUIREMENT_TEXT_MAX_CHARS = 50000;

const appState = {
    // 需求文档上传：已解析成功的来源文件名（创建任务时随请求上报）与最近一次解析的正文
    requirementFileName: null,
    requirementTextFromFile: null,
    requirementParsing: false,
    creating: false,
};

const elements = {};

document.addEventListener("DOMContentLoaded", init);

async function init() {
    [
        "workflowName", "workflowRequirement", "workflowKb", "createWorkflowBtn",
        "requirementUploadBtn", "requirementFileInput", "requirementFileBar",
        "requirementUploadStatus",
    ].forEach((id) => {
        elements[id] = document.getElementById(id);
    });

    elements.createWorkflowBtn.addEventListener("click", createWorkflow);
    elements.requirementUploadBtn.addEventListener("click", () => elements.requirementFileInput.click());
    elements.requirementFileInput.addEventListener("change", handleRequirementFileSelect);

    await loadKnowledgeBases();
}

async function createWorkflow() {
    if (appState.creating) return;
    const requirement = elements.workflowRequirement.value.trim();
    if (!requirement) {
        alert("请先粘贴需求原文或上传需求文档");
        return;
    }
    if (requirement.length > REQUIREMENT_TEXT_MAX_CHARS) {
        alert(`需求原文过长（上限 ${REQUIREMENT_TEXT_MAX_CHARS} 字符），请精简后再创建`);
        return;
    }
    const payload = {
        name: elements.workflowName.value.trim(),
        requirement_text: requirement,
        knowledge_base_id: elements.workflowKb.value || null,
        requirement_file_name: appState.requirementFileName,
    };

    appState.creating = true;
    elements.createWorkflowBtn.disabled = true;
    // 创建成功后要跳详情页：跳转期间保持按钮禁用，避免重复提交建出两个任务
    let created = false;
    try {
        const response = await fetch("/api/workflows", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify(payload),
        });
        if (response.status === 401) return redirectToLogin();
        if (!response.ok) {
            const err = await response.json().catch(() => ({}));
            alert(err.error || "创建任务失败");
            return;
        }
        const workflow = await response.json();
        created = true;
        // 启动与事件订阅统一由详情页负责（autostart=1）：SSE 只在一个页面消费，避免双订阅
        window.location.href = `/workflow-detail?workflow_id=${encodeURIComponent(workflow.id)}&autostart=1`;
    } catch (error) {
        console.error("创建任务失败:", error);
        alert("创建任务失败，请重试");
    } finally {
        if (!created) {
            appState.creating = false;
            elements.createWorkflowBtn.disabled = false;
        }
    }
}

async function loadKnowledgeBases() {
    try {
        const response = await fetch("/api/knowledge-bases/");
        if (response.status === 401) return redirectToLogin();
        if (!response.ok) return;
        const knowledgeBases = await response.json();
        knowledgeBases.forEach((kb) => {
            const option = document.createElement("option");
            option.value = kb.id;
            option.textContent = (kb.visibility === "shared" ? "【共享】 " : "") + kb.name;
            elements.workflowKb.appendChild(option);
        });
    } catch (error) {
        console.error("加载知识库失败:", error);
    }
}

/* ---------- 需求文档上传（新建面板） ---------- */

function handleRequirementFileSelect(event) {
    const file = event.target.files && event.target.files[0];
    // 立即清空 value：同一文件二次选择（改完文档重传）也要触发 change
    event.target.value = "";
    if (file) parseRequirementDocument(file);
}

function setRequirementUploadStatus(message, type = "info") {
    const status = elements.requirementUploadStatus;
    if (!message) {
        status.hidden = true;
        status.textContent = "";
        status.className = "upload-status";
        return;
    }
    status.textContent = message;
    status.className = `upload-status upload-status-${type}`;
    status.hidden = false;
}

function renderRequirementFileBar() {
    const bar = elements.requirementFileBar;
    bar.innerHTML = "";
    if (!appState.requirementFileName) {
        bar.hidden = true;
        return;
    }

    const chip = document.createElement("div");
    chip.className = "requirement-chip";

    const icon = document.createElement("span");
    icon.textContent = "📄";

    const name = document.createElement("span");
    name.className = "requirement-chip-name";
    name.textContent = appState.requirementFileName;
    name.dataset.tip = appState.requirementFileName;

    const removeBtn = document.createElement("button");
    removeBtn.type = "button";
    removeBtn.className = "requirement-chip-remove";
    removeBtn.setAttribute("aria-label", "移除需求文档标记");
    removeBtn.textContent = "✕";
    removeBtn.addEventListener("click", () => clearRequirementFile("已移除文档标记，需求原文内容保留"));

    chip.appendChild(icon);
    chip.appendChild(name);
    chip.appendChild(removeBtn);
    bar.appendChild(chip);
    bar.hidden = false;
}

// 只清「来源文档」标记（可选提示文案）；需求原文文本框的内容始终保留给用户处置
function clearRequirementFile(statusMessage) {
    appState.requirementFileName = null;
    appState.requirementTextFromFile = null;
    renderRequirementFileBar();
    setRequirementUploadStatus(statusMessage || null);
}

function fillRequirementText(text) {
    const area = elements.workflowRequirement;
    const current = area.value.trim();
    const previousFromFile = (appState.requirementTextFromFile || "").trim();

    if (current && current !== previousFromFile) {
        // 已有手工粘贴/编辑过的正文：不覆盖用户输入，文档正文接在其后
        area.value = `${current}\n\n${text}`;
    } else {
        // 空白或仍是上一次解析结果（未被编辑）：直接替换，避免重复堆叠
        area.value = text;
    }
    appState.requirementTextFromFile = text;
}

async function parseRequirementDocument(file) {
    if (appState.requirementParsing) return;

    const dotIndex = file.name.lastIndexOf(".");
    const ext = dotIndex === -1 ? "" : file.name.slice(dotIndex).toLowerCase();
    if (!REQUIREMENT_DOC_EXTENSIONS.includes(ext)) {
        setRequirementUploadStatus("仅支持 PDF、Word(.docx)、TXT、Markdown 需求文档", "error");
        return;
    }
    if (file.size > REQUIREMENT_FILE_MAX_BYTES) {
        setRequirementUploadStatus("文件大小不能超过 50MB", "error");
        return;
    }

    appState.requirementParsing = true;
    elements.requirementUploadBtn.disabled = true;
    setRequirementUploadStatus(`正在解析《${file.name}》，大文档请稍候…`, "info");

    try {
        const formData = new FormData();
        formData.append("file", file);
        const response = await apiFetch("/api/workflows/requirement-extract", {
            method: "POST",
            body: formData,
        });
        if (!response.ok) {
            const err = await response.json().catch(() => ({}));
            setRequirementUploadStatus(err.error || "需求文档解析失败，请重试", "error");
            return;
        }

        const data = await response.json();
        fillRequirementText(data.text);
        appState.requirementFileName = data.filename || file.name;
        renderRequirementFileBar();

        const truncatedNote = data.truncated
            ? `；文档过长，仅保留前 ${REQUIREMENT_TEXT_MAX_CHARS} 字符`
            : "";
        // 原文档的符号字体/复选框等解析不出字形，只会显示成方框，提取时已剔除
        const droppedNote = data.dropped_chars > 0
            ? `；已忽略 ${data.dropped_chars} 个无字形的符号`
            : "";
        setRequirementUploadStatus(
            `已解析《${appState.requirementFileName}》（${data.chars} 字符）并回填到需求原文${truncatedNote}${droppedNote}，可编辑后创建任务`,
            data.truncated || data.dropped_chars > 0 ? "warn" : "ok"
        );
    } catch (error) {
        if (isUnauthorized(error)) return;
        console.error("需求文档解析失败:", error);
        setRequirementUploadStatus("需求文档解析失败，请重试", "error");
    } finally {
        appState.requirementParsing = false;
        elements.requirementUploadBtn.disabled = false;
    }
}