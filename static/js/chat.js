// 当前应用状态
const appState = {
    currentScenario: 'requirement_clarification',  // 默认场景
    currentConversation: null,
    userId: null,
    username: null,
    isProcessing: false,
    currentKnowledgeBaseId: null,  // 当前选中的知识库ID
    pendingFile: null  // 待发送的附件（单个 PDF）
};

// DOM 元素引用
const elements = {
    historyContainer: document.getElementById('historyContainer'),
    chatMessages: document.getElementById('chatMessages'),
    chatHeaderTitle: document.getElementById('chatHeaderTitle'),
    chatInput: document.getElementById('chatInput'),
    sendBtn: document.getElementById('sendBtn'),
    newChatBtn: document.getElementById('newChatBtn'),
    attachBtn: document.getElementById('attachBtn'),
    fileInput: document.getElementById('fileInput'),
    attachmentBar: document.getElementById('attachmentBar')
};

const MAX_ATTACHMENT_BYTES = 50 * 1024 * 1024;  // 与后端 MAX_UPLOAD_SIZE 一致

// 无活跃对话时 header 显示的引导文案（提示信息已从聊天区移到 header）
const DEFAULT_CHAT_HEADER_TITLE = '选择场景和知识库进行对话';

function setChatHeaderTitle(title) {
    if (!elements.chatHeaderTitle) return;
    const text = (title || '').trim();
    elements.chatHeaderTitle.textContent = text || DEFAULT_CHAT_HEADER_TITLE;
    elements.chatHeaderTitle.dataset.tip = text || DEFAULT_CHAT_HEADER_TITLE;
}

function resetChatHeaderTitle() {
    setChatHeaderTitle(null);
}

const tipsText = '';

// 轻量悬浮提示：替代原生 title（仅当文本被截断时显示，样式统一）
function initFloatingTip() {
    const tip = document.createElement('div');
    tip.className = 'floating-tip';
    document.body.appendChild(tip);
    let current = null;

    function hide() {
        tip.classList.remove('show');
        current = null;
    }

    function show(target) {
        current = target;
        tip.textContent = target.dataset.tip;
        tip.classList.add('show');
        const rect = target.getBoundingClientRect();
        // 先渲染再测量气泡尺寸
        tip.style.left = '0px';
        tip.style.top = '0px';
        let x = Math.min(Math.max(8, rect.left), window.innerWidth - tip.offsetWidth - 8);
        let y = rect.top - tip.offsetHeight - 8;
        if (y < 8) {
            y = rect.bottom + 8;
        }
        tip.style.left = `${x}px`;
        tip.style.top = `${y}px`;
    }

    document.addEventListener('mouseover', (e) => {
        const target = e.target.closest('[data-tip]');
        if (!target) {
            hide();
            return;
        }
        if (target === current) return;
        // 文本没有溢出时不显示提示
        if (target.scrollWidth <= target.clientWidth + 1) {
            hide();
            return;
        }
        show(target);
    });

    document.addEventListener('mouseout', (e) => {
        const from = e.target.closest('[data-tip]');
        const to = e.relatedTarget && e.relatedTarget.closest ? e.relatedTarget.closest('[data-tip]') : null;
        if (from && from !== to) {
            hide();
        }
    });

    // 滚动时收起，避免提示钉在原位置
    document.addEventListener('scroll', hide, true);
    window.addEventListener('resize', hide);
}

initFloatingTip();

// 初始化应用
document.addEventListener('DOMContentLoaded', async () => {

    // 为服务端渲染的默认标题补充悬浮提示属性
    resetChatHeaderTitle();

    // 加载知识库列表
    await loadKnowledgeBases();

    await loadHistory(appState.currentScenario, appState.currentKnowledgeBaseId);

    setupEventListeners();
    
    elements.chatInput.addEventListener('input', () => {
        elements.sendBtn.disabled = elements.chatInput.value.trim() === '' || appState.isProcessing;
    });

    // 附件选择
    elements.attachBtn.addEventListener('click', () => elements.fileInput.click());
    elements.fileInput.addEventListener('change', () => {
        const file = elements.fileInput.files[0];
        elements.fileInput.value = '';
        if (!file) return;

        if (!file.name.toLowerCase().endsWith('.pdf')) {
            alert('仅支持上传 PDF 文件');
            return;
        }
        if (file.size > MAX_ATTACHMENT_BYTES) {
            alert('文件大小不能超过 50MB');
            return;
        }
        appState.pendingFile = file;
        renderAttachmentChip();
    });


    
});

let currentRequestController = null;
let historyMenuListenerBound = false;
let latestHistoryRequestId = 0;

// 页面刷新前提示（beforeunload 需 preventDefault；返回字符串是已废弃写法）
window.addEventListener('beforeunload', (event) => {
    if (appState.isProcessing) {
        event.preventDefault();
        event.returnValue = '';
    }
});

// 加载历史记录
async function loadHistory(scenario, knowledgeBaseId = null) {
    const requestId = ++latestHistoryRequestId;
    elements.historyContainer.innerHTML = '<div class="loader">加载历史记录中...</div>';
    try {
        // 构建查询参数
        const params = new URLSearchParams();
        params.append('scenario', scenario);
        if (knowledgeBaseId) {
            params.append('knowledge_base_id', knowledgeBaseId);
        }

        const response = await apiFetch(`/api/history?${params.toString()}`, {
            method: 'GET',
            credentials: 'include'
        });

        if (requestId !== latestHistoryRequestId) {
            return;
        }

        if (response.ok) {
            const historyData = await response.json();
            if (requestId !== latestHistoryRequestId) {
                return;
            }
            renderHistory(historyData);
        } else {
            console.error('加载历史记录失败');
            elements.historyContainer.innerHTML = '<div class="empty-state">无法加载历史记录</div>';
        }
    } catch (error) {
        if (requestId !== latestHistoryRequestId) {
            return;
        }
        console.error('加载历史记录时出错:', error);
        elements.historyContainer.innerHTML = '<div class="empty-state">加载历史记录时出错</div>';
    }
}

// 渲染知识库选择菜单（第一项固定为"普通对话"，即不使用 RAG）
function renderKbMenu(knowledgeBases) {
    const kbMenu = document.getElementById('kbMenu');
    if (!kbMenu) return;

    kbMenu.innerHTML = '';
    const items = [{ id: '', name: '普通对话', visibility: 'private' }, ...knowledgeBases];
    const currentKbId = appState.currentKnowledgeBaseId || '';

    items.forEach(kb => {
        const option = document.createElement('button');
        option.type = 'button';
        option.className = 'kb-option' + (kb.id === currentKbId ? ' active' : '');
        option.dataset.kbId = kb.id;
        option.setAttribute('role', 'option');
        option.setAttribute('aria-selected', String(kb.id === currentKbId));

        const icon = document.createElement('i');
        icon.className = 'kb-icon fas fa-book';

        const name = document.createElement('span');
        name.className = 'kb-name';
        // 共享知识库以 [共享] 后缀标识；名称超长省略，悬停显示全文
        const fullName = kb.name + (kb.visibility === 'shared' ? ' [共享]' : '');
        name.textContent = fullName;
        name.dataset.tip = fullName;

        const check = document.createElement('i');
        check.className = 'fas fa-check kb-check';

        option.append(icon, name, check);
        kbMenu.appendChild(option);
    });
}

async function loadKnowledgeBases() {
    try {
        const response = await apiFetch('/api/knowledge-bases/');
        if (!response.ok) {
            throw new Error(`HTTP error! status: ${response.status}`);
        }

        const knowledgeBases = await response.json();

        // 当前选中的知识库可能已被删除，先校验再渲染
        const hasCurrentKnowledgeBase = knowledgeBases.some(kb => kb.id === appState.currentKnowledgeBaseId);
        if (!hasCurrentKnowledgeBase) {
            appState.currentKnowledgeBaseId = null;
        }
        renderKbMenu(knowledgeBases);

    } catch (error) {
        console.error('加载知识库失败:', error);
        // 可以选择显示一个错误提示，但不影响主要功能
    }
}

async function refreshHistoryAndHighlightCurrentConversation() {
    await loadHistory(appState.currentScenario, appState.currentKnowledgeBaseId);
    document.querySelectorAll('.conversation-item').forEach(item => {
        item.classList.toggle('active', item.dataset.id === appState.currentConversation);
    });
}

// 渲染历史记录
function renderHistory(historyData) {

    elements.historyContainer.innerHTML = '';
    
    if (!historyData || !historyData.groups || historyData.groups.length === 0) {
        const emptyState = document.createElement('div');
        emptyState.className = 'empty-state';
        const text = document.createElement('p');
        text.textContent = '暂无历史对话记录';
        emptyState.appendChild(text);
        elements.historyContainer.appendChild(emptyState);
        return;
    }
    
    historyData.groups.forEach(group => {
        const groupElement = document.createElement('div');
        groupElement.className = 'history-section';

        const sectionTitle = document.createElement('div');
        sectionTitle.className = 'section-title';
        sectionTitle.textContent = group.time_group;
        groupElement.appendChild(sectionTitle);
        
        group.conversations.forEach(conversation => {
            const item = document.createElement('div');
            item.className = 'conversation-item';
            if (appState.currentConversation === conversation.id) {
                item.classList.add('active');
            }
            item.dataset.id = conversation.id;
            const titleElement = document.createElement('div');
            titleElement.className = 'conversation-title';
            titleElement.textContent = conversation.title;
            titleElement.dataset.tip = conversation.title;

            const actionsElement = document.createElement('div');
            actionsElement.className = 'conversation-actions';

            const moreBtnElement = document.createElement('button');
            moreBtnElement.className = 'more-btn';
            moreBtnElement.textContent = '···';

            const dropdownMenuElement = document.createElement('div');
            dropdownMenuElement.className = 'dropdown-menu';

            const renameButton = document.createElement('button');
            renameButton.className = 'dropdown-item rename-btn';
            renameButton.dataset.id = conversation.id;
            renameButton.textContent = '重命名';

            const deleteButton = document.createElement('button');
            deleteButton.className = 'dropdown-item delete-btn';
            deleteButton.dataset.id = conversation.id;
            deleteButton.textContent = '删除';

            dropdownMenuElement.appendChild(renameButton);
            dropdownMenuElement.appendChild(deleteButton);
            actionsElement.appendChild(moreBtnElement);
            actionsElement.appendChild(dropdownMenuElement);
            item.appendChild(titleElement);
            item.appendChild(actionsElement);
        // 点击加载对话
        item.addEventListener('click', (e) => {

            if (!e.target.closest('.conversation-actions')) {
                
                document.querySelectorAll('.conversation-item').forEach(el => {
                    el.classList.remove('active');
                });

                item.classList.add('active');

                loadConversation(conversation.id, conversation.title);
            }
        });

            // 更多按钮点击事件
            const moreBtn = moreBtnElement;
            const dropdownMenu = dropdownMenuElement;
            
            moreBtn.addEventListener('click', (e) => {
                e.stopPropagation(); // 阻止冒泡
                
                document.querySelectorAll('.dropdown-menu').forEach(menu => {
                    if (menu !== dropdownMenu) {
                        menu.classList.remove('show');
                    }
                });
                
                dropdownMenu.classList.toggle('show');
            });

            // 重命名按钮事件
            const renameBtn = renameButton;
            renameBtn.addEventListener('click', async (e) => {
                e.stopPropagation();
                dropdownMenu.classList.remove('show');
                
                const conversationId = e.target.dataset.id;
                const newTitle = prompt('请输入新的对话标题:', conversation.title);
                
                if (newTitle && newTitle.trim() !== '') {
                    try {
                        const response = await apiFetch(`/api/conversation/${conversationId}/rename`, {
                            method: 'POST',
                            headers: {
                                'Content-Type': 'application/json'
                            },
                            body: JSON.stringify({ title: newTitle.trim() }),
                            credentials: 'include'
                        });
                        
                        if (response.ok) {
                            const conversationTitleElement = item.querySelector('.conversation-title');
                            conversationTitleElement.textContent = newTitle.trim();
                            conversationTitleElement.dataset.tip = newTitle.trim();

                            if (appState.currentConversation === conversationId) {
                                setChatHeaderTitle(newTitle.trim());
                            }
                        } else {
                            alert('重命名失败，请稍后再试');
                        }
                    } catch (error) {
                        console.error('重命名请求失败:', error);
                        alert('重命名请求失败');
                    }
                }
            });
            
            // 删除按钮事件
            const deleteBtn = deleteButton;
            deleteBtn.addEventListener('click', async (e) => {
                e.stopPropagation();
                dropdownMenu.classList.remove('show');
                
                if (confirm('确定要删除这个对话吗？此操作不可恢复。')) {
                    const conversationId = e.target.dataset.id;
                    
                    try {
                        const response = await apiFetch(`/api/conversation/${conversationId}`, {
                            method: 'DELETE',
                            credentials: 'include'
                        });
                        
                        if (response.ok) {
                            item.remove();
                            
                            // 如果删除的是当前对话，重置状态
                            if (appState.currentConversation === conversationId) {
                                appState.currentConversation = null;
                                elements.chatMessages.innerHTML = '';
                                resetChatHeaderTitle();
                            }
                        } else {
                            alert('删除失败，请稍后再试');
                        }
                    } catch (error) {
                        console.error('删除请求失败:', error);
                        alert('删除请求失败');
                    }
                }
            });           
            
            groupElement.appendChild(item);
        });
        
        elements.historyContainer.appendChild(groupElement);
    });

    if (!historyMenuListenerBound) {
        document.addEventListener('click', (e) => {
            if (!e.target.closest('.dropdown-menu') && !e.target.closest('.more-btn')) {
                document.querySelectorAll('.dropdown-menu').forEach(menu => {
                    menu.classList.remove('show');
                });
            }
        });
        historyMenuListenerBound = true;
    }

}

// 加载对话内容（标题由历史列表传入，详情接口不返回 title）
async function loadConversation(conversationId, title = null) {
    appState.currentConversation = conversationId;
    try {
        const response = await apiFetch(`/api/conversation/${conversationId}`, {
            method: 'GET',
            credentials: 'include'
        });
        if (response.ok) {
            const conversationData = await response.json();

            // 检查返回的数据结构
            if (Array.isArray(conversationData.messages)) {
                // 正常情况：messages 是数组
                renderConversation(conversationData);
                setChatHeaderTitle(title);
            } else {
                console.error("对话不存在或出错:", conversationData.messages);
                elements.chatMessages.innerHTML = '';
                // 添加场景特定的欢迎消息
                elements.chatMessages.innerHTML = tipsText;
                resetChatHeaderTitle();
            }


        } else {
            console.error('加载对话内容失败');
        }
    } catch (error) {
        console.error('加载对话内容时出错:', error);
    }    
}

// 渲染对话内容
function renderConversation(conversation) {

    elements.chatMessages.innerHTML = '';

    conversation.messages.forEach(message => {
        addMessageToChat(message);
    });

    // 为最后一条AI消息添加重新生成按钮
    if (appState.currentConversation) {
        const aiMessages = elements.chatMessages.querySelectorAll('.ai-message');
        if (aiMessages.length > 0) {
            const lastAiMessage = aiMessages[aiMessages.length - 1].closest('.message-container');
            if (lastAiMessage) {
                addRegenerateButton(lastAiMessage);
                addEditButton(lastAiMessage);
            }
        }
    }

    // scrollToBottom();
}

// 添加消息到聊天区域
function addMessageToChat(message) {
    const messageContainer = document.createElement('div');
    messageContainer.className = 'message-container';

    const isUser = message.role === 'user';
    const wrapper = document.createElement('div');
    wrapper.className = `message ${isUser ? 'user-message' : 'ai-message'}`;

    const header = document.createElement('div');
    header.className = 'message-header';

    const avatar = document.createElement('div');
    avatar.className = `avatar ${isUser ? 'user-avatar-small' : 'ai-avatar'}`;
    avatar.setAttribute('aria-label', isUser ? '用户头像' : 'AI头像');
    avatar.textContent = isUser ? 'U' : 'AI';

    const senderName = document.createElement('div');
    senderName.className = 'sender-name';
    senderName.textContent = isUser ? '你' : '智能助手';

    const contentElement = document.createElement('div');
    contentElement.className = 'message-content';
    if (isUser) {
        contentElement.textContent = message.content;
    } else {
        contentElement.innerHTML = DOMPurify.sanitize(marked.parse(message.content));
    }

    const actions = document.createElement('div');
    actions.className = 'message-actions';

    header.appendChild(avatar);
    header.appendChild(senderName);
    wrapper.appendChild(header);
    // 附件标记放在内容区外，编辑模式重写 contentElement 时不会丢
    if (isUser && message.attachment_name) {
        const attachmentChip = document.createElement('div');
        attachmentChip.className = 'message-attachment';
        attachmentChip.textContent = `📎 ${message.attachment_name}`;
        wrapper.appendChild(attachmentChip);
    }
    wrapper.appendChild(contentElement);
    wrapper.appendChild(actions);
    messageContainer.appendChild(wrapper);

    elements.chatMessages.appendChild(messageContainer);

    // 如果是AI消息且是测试用例场景，添加导出按钮
    if (!isUser && appState.currentScenario === 'testcase_generation') {
        addExportButton(messageContainer);
    }

    scrollToBottom();
}

// 滚动到底部
function scrollToBottom() {
    elements.chatMessages.scrollTop = elements.chatMessages.scrollHeight;
}

// 智能滚动：仅在用户处于底部附近时自动滚动
function smartScrollToBottom() {
    const el = elements.chatMessages;
    const nearBottom = el.scrollHeight - el.scrollTop - el.clientHeight < 100;
    if (nearBottom) {
        el.scrollTop = el.scrollHeight;
    }
}

// 设置事件监听器
function setupEventListeners() {
    // 场景选择器（输入框内）
    const scenarioSelector = document.getElementById('scenarioSelector');
    const scenarioTrigger = document.getElementById('scenarioTrigger');
    const scenarioMenu = document.getElementById('scenarioMenu');

    function closeScenarioMenu() {
        if (!scenarioSelector || !scenarioTrigger) return;
        scenarioSelector.classList.remove('open');
        scenarioTrigger.setAttribute('aria-expanded', 'false');
    }

    function updateScenarioTrigger(option) {
        const iconEl = option.querySelector('.scenario-icon');
        const nameEl = option.querySelector('.scenario-name');
        const triggerIcon = document.getElementById('scenarioTriggerIcon');
        const triggerLabel = document.getElementById('scenarioTriggerLabel');
        if (iconEl && triggerIcon) {
            triggerIcon.className = iconEl.className;
        }
        if (nameEl && triggerLabel) {
            triggerLabel.textContent = nameEl.textContent;
        }
    }

    if (scenarioSelector && scenarioTrigger && scenarioMenu) {
        scenarioTrigger.addEventListener('click', (event) => {
            event.stopPropagation();
            const isOpen = scenarioSelector.classList.toggle('open');
            scenarioTrigger.setAttribute('aria-expanded', String(isOpen));
            if (isOpen) {
                closeKbMenu();
            }
        });

        scenarioMenu.addEventListener('click', (event) => event.stopPropagation());
        document.addEventListener('click', closeScenarioMenu);

        scenarioMenu.querySelectorAll('.scenario-option').forEach(option => {
            option.addEventListener('click', async () => {
                closeScenarioMenu();
                if (option.classList.contains('active')) return;

                if (currentRequestController) {
                    currentRequestController.abort();
                    currentRequestController = null;
                    appState.isProcessing = false;
                    elements.chatInput.disabled = false;
                    elements.sendBtn.disabled = false;
                }

                // 更新选中态
                scenarioMenu.querySelectorAll('.scenario-option').forEach(el => {
                    el.classList.remove('active');
                    el.setAttribute('aria-selected', 'false');
                });
                option.classList.add('active');
                option.setAttribute('aria-selected', 'true');
                updateScenarioTrigger(option);

                // 更新当前场景
                appState.currentScenario = option.dataset.scenario;

                // 重置当前对话
                appState.currentConversation = null;
                clearAttachment();

                // 清空聊天区域并显示欢迎消息
                elements.chatMessages.innerHTML = tipsText;
                resetChatHeaderTitle();
                // 根据当前选中的知识库加载新场景的历史记录
                await loadHistory(appState.currentScenario, appState.currentKnowledgeBaseId);
            });
        });
    }

    // 知识库选择器（输入框内）
    const kbSelector = document.getElementById('kbSelector');
    const kbTrigger = document.getElementById('kbTrigger');
    const kbMenu = document.getElementById('kbMenu');

    function closeKbMenu() {
        if (!kbSelector || !kbTrigger) return;
        kbSelector.classList.remove('open');
        kbTrigger.setAttribute('aria-expanded', 'false');
    }

    // 更新知识库 pill 的文案与高亮态
    function updateKbTrigger() {
        const label = document.getElementById('kbTriggerLabel');
        if (!label || !kbTrigger) return;
        const activeOption = kbMenu
            ? kbMenu.querySelector(`.kb-option[data-kb-id="${appState.currentKnowledgeBaseId || ''}"] .kb-name`)
            : null;
        if (appState.currentKnowledgeBaseId) {
            label.textContent = activeOption ? activeOption.textContent : '知识库';
            kbTrigger.classList.add('kb-active');
        } else {
            label.textContent = '普通对话';
            kbTrigger.classList.remove('kb-active');
        }
        // 名称超长省略时悬停可看全文
        label.dataset.tip = label.textContent;
    }

    if (kbSelector && kbTrigger && kbMenu) {
        kbTrigger.addEventListener('click', (event) => {
            event.stopPropagation();
            const isOpen = kbSelector.classList.toggle('open');
            kbTrigger.setAttribute('aria-expanded', String(isOpen));
            if (isOpen) {
                closeScenarioMenu();
            }
        });

        kbMenu.addEventListener('click', (event) => event.stopPropagation());

        kbMenu.addEventListener('click', async (event) => {
            const option = event.target.closest('.kb-option');
            if (!option) return;
            closeKbMenu();

            const selectedKbId = option.dataset.kbId || null;
            if ((appState.currentKnowledgeBaseId || null) === selectedKbId) return;

            appState.currentKnowledgeBaseId = selectedKbId;
            kbMenu.querySelectorAll('.kb-option').forEach(el => el.classList.remove('active'));
            option.classList.add('active');
            updateKbTrigger();

            // 重置当前对话并加载新知识库的历史记录
            appState.currentConversation = null;
            clearAttachment();
            elements.chatMessages.innerHTML = tipsText;
            resetChatHeaderTitle();
            await loadHistory(appState.currentScenario, appState.currentKnowledgeBaseId);
        });

        document.addEventListener('click', closeKbMenu);
        updateKbTrigger();
    }

    // 发送消息
    elements.sendBtn.addEventListener('click', sendMessage);
    elements.chatInput.addEventListener('keydown', e => {
        if (e.key === 'Enter' && !e.shiftKey) {
            e.preventDefault();
            if (!elements.sendBtn.disabled) {
                sendMessage();
            }
        }
    });
    
    elements.newChatBtn.addEventListener('click', async () => {
        // 仅清空聊天区域，不在数据库创建对话记录
        // 对话记录会在用户实际发送第一条消息时由 sendMessage() 懒创建
        appState.currentConversation = null;
        clearAttachment();
        elements.chatMessages.innerHTML = tipsText;
        resetChatHeaderTitle();

        // 刷新历史列表以移除旧对话的高亮状态
        await loadHistory(appState.currentScenario, appState.currentKnowledgeBaseId);
        document.querySelectorAll('.conversation-item').forEach(item => {
            item.classList.remove('active');
        });
    });

    // 点击历史项时在移动端自动关闭侧栏抽屉（抽屉开关由 nav.js 统一处理）
    document.querySelectorAll('.conversation-item').forEach(item => {
        item.addEventListener('click', () => {
            if (window.innerWidth <= 768) {
                document.querySelector('.sidebar')?.classList.remove('active');
            }
        });
    });
}


// 附件 chip 渲染与清理
function renderAttachmentChip() {
    elements.attachmentBar.innerHTML = '';
    if (!appState.pendingFile) {
        elements.attachmentBar.hidden = true;
        return;
    }

    const chip = document.createElement('div');
    chip.className = 'attachment-chip';

    const icon = document.createElement('span');
    icon.className = 'attachment-icon';
    icon.textContent = '📄';

    const name = document.createElement('span');
    name.className = 'attachment-name';
    name.textContent = appState.pendingFile.name;
    name.title = appState.pendingFile.name;

    const removeBtn = document.createElement('button');
    removeBtn.className = 'attachment-remove';
    removeBtn.setAttribute('aria-label', '移除附件');
    removeBtn.textContent = '✕';
    removeBtn.onclick = clearAttachment;

    chip.appendChild(icon);
    chip.appendChild(name);
    chip.appendChild(removeBtn);
    elements.attachmentBar.appendChild(chip);
    elements.attachmentBar.hidden = false;
}

function clearAttachment() {
    appState.pendingFile = null;
    elements.attachmentBar.innerHTML = '';
    elements.attachmentBar.hidden = true;
}

// 发送消息事件
async function sendMessage() {

    // 取消之前的请求
    if (currentRequestController) {
        currentRequestController.abort();
        currentRequestController = null;
    }

    const message = elements.chatInput.value.trim();
    if (!message || appState.isProcessing) return;

    // 禁用输入和发送按钮
    appState.isProcessing = true;
    elements.chatInput.disabled = true;
    elements.sendBtn.disabled = true;

    const attachedFileName = appState.pendingFile ? appState.pendingFile.name : null;
    const userMessage = {
        role: 'user',
        content: message,
        attachment_name: attachedFileName
    };
    addMessageToChat(userMessage);

    elements.chatInput.value = '';
    
    // 显示AI正在输入
    const aiTypingElement = createTypingIndicator();
    elements.chatMessages.appendChild(aiTypingElement);
    scrollToBottom();
    
    try {

        let createdConversation = false;

        // 如果当前没有对话，先创建一个新对话
        if (!appState.currentConversation) {
            try {
                const formData = new FormData();
                formData.append('scenario', appState.currentScenario);
                if (appState.currentKnowledgeBaseId) {
                    formData.append('knowledge_base_id', appState.currentKnowledgeBaseId);
                }

                const createResponse = await apiFetch('/api/conversation/new', {
                    method: 'POST',
                    credentials: 'include',
                    body: formData
                });

                if (createResponse.ok) {
                    const data = await createResponse.json();
                    appState.currentConversation = data.conversation_id;
                    createdConversation = true;
                    await refreshHistoryAndHighlightCurrentConversation();
                } else {
                    throw new Error('创建对话失败');
                }
            } catch (createError) {
                console.error('创建对话时出错:', createError);
                throw new Error('无法创建新对话');
            }
        }
        
        const guide_text = document.querySelector('.guide-text');
        if (guide_text) {
        guide_text.classList.add("hidden");
        }

        // 创建AI消息容器（用于流式内容）
        const aiMessageContainer = document.createElement('div');
        aiMessageContainer.className = 'message-container';

        const aiWrapper = document.createElement('div');
        aiWrapper.className = 'message ai-message';
        const aiHeader = document.createElement('div');
        aiHeader.className = 'message-header';
        const aiAvatar = document.createElement('div');
        aiAvatar.className = 'avatar ai-avatar';
        aiAvatar.setAttribute('aria-label', 'AI头像');
        aiAvatar.textContent = 'O';
        const aiSender = document.createElement('div');
        aiSender.className = 'sender-name';
        aiSender.textContent = '智能助手';
        const contentElement = document.createElement('div');
        contentElement.className = 'message-content';
        const actionsElement = document.createElement('div');
        actionsElement.className = 'message-actions';

        aiHeader.appendChild(aiAvatar);
        aiHeader.appendChild(aiSender);
        aiWrapper.appendChild(aiHeader);
        aiWrapper.appendChild(contentElement);
        aiWrapper.appendChild(actionsElement);
        aiMessageContainer.appendChild(aiWrapper);

        elements.chatMessages.appendChild(aiMessageContainer);
        scrollToBottom();

        // 移除正在输入指示器
        aiTypingElement.remove();

        // 添加初始光标
        let cursor = document.createElement('span');
        cursor.className = 'typing-cursor';
        cursor.textContent = '思考中...';
        contentElement.appendChild(cursor);

        currentRequestController = new AbortController();
        let lastRenderTime = 0;

        // 构建 multipart 请求：文本字段 + 可选附件
        const formData = new FormData();
        formData.append('message', message);
        formData.append('scenario', appState.currentScenario);
        formData.append('conversation_id', appState.currentConversation);
        if (appState.currentKnowledgeBaseId) {
            formData.append('knowledge_base_id', appState.currentKnowledgeBaseId);
        }
        if (appState.pendingFile) {
            formData.append('file', appState.pendingFile);
        }

        const response = await apiFetch('/api/chat', {
            method: 'POST',
            body: formData,
            signal: currentRequestController.signal
        });

        if (!response.ok) {
            let detail = '请求失败';
            try {
                const err = await response.json();
                detail = err.detail || err.error || detail;
            } catch (e) { /* 响应体不是 JSON 时使用默认提示 */ }
            throw new Error(detail);
        }
        
        // 读取流式响应（跨 chunk 缓冲 + 完整事件解析）
        let aiResponse = "";
        let conversationTitle = null;
        let streamError = null;
        let streamNotice = null;

        await readSseStream(response, (dataStr) => {
            // 结束标记
            if (dataStr === '[DONE]') {
                return true;
            }

            try {
                const data = JSON.parse(dataStr);
                if (data.token) {
                    aiResponse += data.token;

                    // 节流渲染：最多每120ms渲染一次，减少表格跳动
                    const now = Date.now();
                    if (now - lastRenderTime >= 120) {
                        contentElement.innerHTML = DOMPurify.sanitize(marked.parse(aiResponse));
                        smartScrollToBottom();
                        lastRenderTime = now;
                    }
                }

                if (data.conversation_title) {
                    conversationTitle = data.conversation_title;
                    setChatHeaderTitle(conversationTitle);
                }

                if (data.error) {
                    streamError = data.error;
                }
                if (data.attachment_processing) {
                    streamNotice = `《${data.attachment_processing}》已开始后台处理；文档就绪前检索不到其内容，完成后可再次提问`;
                }
            } catch (e) {
                console.error('解析JSON失败:', e);
            }
            return false;
        });

        // 流结束后做一次最终渲染，确保最后一批 token 被显示
        contentElement.innerHTML = DOMPurify.sanitize(marked.parse(aiResponse));
        if (streamError) {
            appendStreamError(contentElement, streamError);
        }
        if (streamNotice && !aiResponse) {
            contentElement.textContent = streamNotice;
        }
        smartScrollToBottom();

                // 确保添加导出按钮（如果未在流中处理）
        if (appState.currentScenario === 'testcase_generation') {
            addExportButton(aiMessageContainer);
        }
        addRegenerateButton(aiMessageContainer);
        addEditButton(aiMessageContainer);

        // 发送成功后清空附件（失败时保留 chip 供重试）
        clearAttachment();

        if (createdConversation || conversationTitle) {
            await refreshHistoryAndHighlightCurrentConversation();
        }
        
    } catch (error) {
        if (error.name === 'AbortError') {
            console.log('请求被取消');
        } else {
            console.error('发送消息时出错:', error);

            // 展示后端返回的具体错误（如文档处理失败）
            if (error.message) {
                alert(error.message);
            }

            // 显示错误消息
            const errorMessage = {
                role: 'assistant',
                content: '处理您的请求时出错，请稍后再试。'
            };
            addMessageToChat(errorMessage);
        }
    } finally {
        // 重新启用输入和发送按钮
        appState.isProcessing = false;
        elements.chatInput.disabled = false;
        elements.chatInput.focus();
        currentRequestController = null;
    }
}
// 创建AI正在输入的指示器
function createTypingIndicator() {
    const container = document.createElement('div');
    container.className = 'message-container';

    const wrapper = document.createElement('div');
    wrapper.className = 'message ai-message';
    const header = document.createElement('div');
    header.className = 'message-header';
    const avatar = document.createElement('div');
    avatar.className = 'avatar ai-avatar';
    avatar.setAttribute('aria-label', 'AI头像');
    avatar.textContent = 'O';
    const sender = document.createElement('div');
    sender.className = 'sender-name';
    sender.textContent = '智能助手';
    const content = document.createElement('div');
    content.className = 'message-content';
    const typingIndicator = document.createElement('div');
    typingIndicator.className = 'typing-indicator';

    for (let i = 0; i < 3; i++) {
        const dot = document.createElement('div');
        dot.className = 'typing-dot';
        typingIndicator.appendChild(dot);
    }

    header.appendChild(avatar);
    header.appendChild(sender);
    content.appendChild(typingIndicator);
    wrapper.appendChild(header);
    wrapper.appendChild(content);
    container.appendChild(wrapper);

    return container;
}
// 添加导出按钮的函数
function addExportButton(messageContainer) {
    // const messageHeader = messageContainer.querySelector('.message-header');
    const messageActions = messageContainer.querySelector('.message-actions');
    if (!messageActions) return;
    
    if (messageActions.querySelector('.export-btn')) return;

    // 创建导出按钮
    const exportBtn = document.createElement('button');
    exportBtn.className = 'export-btn';
    exportBtn.innerHTML = '📥 导出用例';
    exportBtn.title = '导出本对话最新的测试用例表格（CSV）';
    exportBtn.onclick = function(e) {
        e.stopPropagation();
        exportTestCases(messageContainer);
    };
    
    // 将按钮添加到消息头部
    messageActions.appendChild(exportBtn);
}

// 添加重新生成按钮（仅在测试用例生成场景）
function addRegenerateButton(messageContainer) {
    if (appState.currentScenario !== 'testcase_generation') return;

    const messageActions = messageContainer.querySelector('.message-actions');
    if (!messageActions) return;

    if (messageActions.querySelector('.regenerate-btn')) return;

    const regenerateBtn = document.createElement('button');
    regenerateBtn.className = 'regenerate-btn';
    regenerateBtn.innerHTML = '🔄 重新生成';
    regenerateBtn.title = '重新生成回复';
    regenerateBtn.onclick = function(e) {
        e.stopPropagation();
        regenerateResponse(messageContainer);
    };

    messageActions.appendChild(regenerateBtn);
}

// 添加编辑问题按钮（仅在测试用例生成场景，显示在AI消息上）
function addEditButton(messageContainer) {
    if (appState.currentScenario !== 'testcase_generation') return;

    const messageActions = messageContainer.querySelector('.message-actions');
    if (!messageActions) return;

    if (messageActions.querySelector('.edit-btn')) return;

    const editBtn = document.createElement('button');
    editBtn.className = 'edit-btn';
    editBtn.innerHTML = '✏️ 编辑问题';
    editBtn.title = '编辑问题后重新生成';
    editBtn.onclick = function(e) {
        e.stopPropagation();
        enterEditMode(messageContainer);
    };

    messageActions.appendChild(editBtn);
}

// 进入编辑模式：定位到对应的用户消息并使其可编辑
function enterEditMode(aiMessageContainer) {
    // 找到对应的用户消息容器（前一个.message-container）
    const userMessageContainer = aiMessageContainer.previousElementSibling;
    if (!userMessageContainer || !userMessageContainer.querySelector('.user-message')) return;

    const contentElement = userMessageContainer.querySelector('.message-content');
    if (!contentElement) return;

    const originalText = contentElement.textContent;

    // 滚动到用户消息处
    userMessageContainer.scrollIntoView({ behavior: 'smooth', block: 'center' });

    // 替换为编辑区域
    contentElement.innerHTML = `
        <textarea class="edit-textarea">${escapeHtml(originalText)}</textarea>
        <div class="edit-actions">
            <button class="edit-cancel-btn">取消</button>
            <button class="edit-submit-btn">重新生成</button>
        </div>
    `;

    const textarea = contentElement.querySelector('.edit-textarea');
    textarea.focus();
    textarea.setSelectionRange(textarea.value.length, textarea.value.length);

    // 取消按钮
    contentElement.querySelector('.edit-cancel-btn').onclick = function() {
        contentElement.textContent = originalText;
    };

    // 重新生成按钮
    contentElement.querySelector('.edit-submit-btn').onclick = function() {
        const newMessage = textarea.value.trim();
        if (!newMessage) return;
        contentElement.textContent = newMessage;
        // 调用重新生成，传入修改后的消息
        regenerateResponse(aiMessageContainer, newMessage);
    };
}

function escapeHtml(text) {
    const div = document.createElement('div');
    div.textContent = text;
    return div.innerHTML;
}

// 逐块读取 SSE 流并解析事件：维护跨 chunk 缓冲，事件被网络切成多段时
// 也能拼出完整事件再解析（否则 JSON.parse 会失败、token 丢失）。
// onEvent 收到每个事件的 data 字符串，返回 true 表示结束读取（如收到 [DONE]）
async function readSseStream(response, onEvent) {
    const reader = response.body.getReader();
    const decoder = new TextDecoder('utf-8');
    let buffer = '';

    const handleEvent = (event) => {
        if (!event.startsWith('data: ')) return false;
        return onEvent(event.replace('data: ', '').trim()) === true;
    };

    while (true) {
        const { value, done } = await reader.read();
        if (done) break;

        buffer += decoder.decode(value, { stream: true });
        const events = buffer.split('\n\n');
        // 最后一段可能不完整（还没等到事件分隔符），留到下一轮拼接
        buffer = events.pop();

        for (const event of events) {
            if (handleEvent(event)) return;
        }
    }

    // 流结束时处理缓冲中残留的最后一个事件
    const leftover = buffer.trim();
    if (leftover) handleEvent(leftover);
}

// 在消息内容区追加失败提示（服务端经 {"error": ...} 事件下发的通用文案）
function appendStreamError(contentElement, message) {
    const errBox = document.createElement('div');
    errBox.className = 'stream-error';
    errBox.textContent = message;
    contentElement.appendChild(errBox);
}

// 重新生成响应
async function regenerateResponse(messageContainer, editedMessage = null) {
    if (appState.isProcessing) return;
    if (!appState.currentConversation) return;

    appState.isProcessing = true;
    elements.chatInput.disabled = true;
    elements.sendBtn.disabled = true;

    // 中断之前的请求（如果有）
    if (currentRequestController) {
        currentRequestController.abort();
    }
    currentRequestController = new AbortController();

    const contentElement = messageContainer.querySelector('.message-content');
    const oldContent = contentElement.innerHTML;

    // 显示加载状态
    contentElement.innerHTML = '<div class="typing-indicator"><div class="typing-dot"></div><div class="typing-dot"></div><div class="typing-dot"></div></div>';

    // 移除旧按钮
    const exportBtn = messageContainer.querySelector('.export-btn');
    if (exportBtn) exportBtn.remove();
    const regenerateBtn = messageContainer.querySelector('.regenerate-btn');
    if (regenerateBtn) regenerateBtn.remove();
    const editBtn = messageContainer.querySelector('.edit-btn');
    if (editBtn) editBtn.remove();

    let lastRenderTime = 0;
    let aiResponse = '';

    try {
        const requestBody = { conversation_id: appState.currentConversation };
        if (editedMessage) {
            requestBody.message = editedMessage;
        }

        const response = await apiFetch('/api/chat/regenerate', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(requestBody),
            credentials: 'include',
            signal: currentRequestController.signal,
        });

        if (!response.ok) {
            throw new Error('重新生成请求失败');
        }

        let streamError = null;

        await readSseStream(response, (dataStr) => {
            if (dataStr === '[DONE]') return true;

            try {
                const data = JSON.parse(dataStr);
                if (data.token) {
                    aiResponse += data.token;
                    const now = Date.now();
                    if (now - lastRenderTime >= 120) {
                        contentElement.innerHTML = DOMPurify.sanitize(marked.parse(aiResponse));
                        smartScrollToBottom();
                        lastRenderTime = now;
                    }
                }
                if (data.error) {
                    streamError = data.error;
                }
            } catch (e) {
                console.error('解析JSON失败:', e);
            }
            return false;
        });

        // 最终渲染
        contentElement.innerHTML = DOMPurify.sanitize(marked.parse(aiResponse));
        if (streamError) {
            appendStreamError(contentElement, streamError);
        }

    } catch (error) {
        if (error.name !== 'AbortError') {
            console.error('重新生成失败:', error);
            contentElement.innerHTML = oldContent;
            alert('重新生成失败，请稍后再试');
        }
    } finally {
        // 重新添加按钮
        if (appState.currentScenario === 'testcase_generation') {
            addExportButton(messageContainer);
        }
        addRegenerateButton(messageContainer);
        addEditButton(messageContainer);

        appState.isProcessing = false;
        elements.chatInput.disabled = false;
        elements.chatInput.focus();
        currentRequestController = null;
    }
}

// 导出测试用例：走后端端点（服务端统一做表格提取、CSV 公式注入清洗与 utf-8-sig 编码，
// 与工作流/用例集导出口径一致；避免前端复刻一套易漂移的实现）
function exportTestCases() {
    if (!appState.currentConversation) {
        showMessage('请先选择或创建一个对话', 'error');
        return;
    }
    window.location.href = `/api/export/testcases?conversation_id=${encodeURIComponent(appState.currentConversation)}`;
}

// 从Markdown文本中提取表格数据
function about() {
    alert("AI 智能测试平台");
}