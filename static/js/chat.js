// 当前应用状态
const appState = {
    currentScenario: 'product_manual',  // 默认场景
    currentConversation: null,
    userId: null,
    username: null,
    isProcessing: false,
    currentKnowledgeBaseId: null,  // 当前选中的知识库ID
    pendingFile: null  // 待发送的附件（单个 PDF）
};

// DOM 元素引用
const elements = {
    scenarioGrid: document.getElementById('scenarioGrid'),
    historyContainer: document.getElementById('historyContainer'),
    chatMessages: document.getElementById('chatMessages'),
    chatInput: document.getElementById('chatInput'),
    sendBtn: document.getElementById('sendBtn'),
    newChatBtn: document.getElementById('newChatBtn'),
    attachBtn: document.getElementById('attachBtn'),
    fileInput: document.getElementById('fileInput'),
    attachmentBar: document.getElementById('attachmentBar')
    // chatTitle: document.getElementById('chatTitle')
};

const MAX_ATTACHMENT_BYTES = 50 * 1024 * 1024;  // 与后端 MAX_UPLOAD_SIZE 一致

const tipsText = `
    <div class="message-container guide-text">
        <div class="message ai-message">
            <div class="message-content">
                <p>请在左侧选择功能场景，默认不使用 RAG 知识库。</p>
                <p>在创建知识库并上传文档后，点击顶部的知识库列表进行切换。</p>
            </div>
        </div>
    </div>
`; 

// 初始化应用
document.addEventListener('DOMContentLoaded', async () => {

    // 加载知识库列表
    await loadKnowledgeBases();

    const selectElement = document.getElementById('knowledgeBaseSelect');
    appState.currentKnowledgeBaseId = selectElement.value || null;

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


    
    // 监听知识库选择变化
    document.getElementById('knowledgeBaseSelect').addEventListener('change', async function() {
        const selectedKbId = this.value || null;

        appState.currentKnowledgeBaseId = selectedKbId;
        appState.currentConversation = null;
        clearAttachment();
        await loadHistory(appState.currentScenario, selectedKbId);

        elements.chatMessages.innerHTML = '';
        elements.chatMessages.innerHTML = tipsText;
    });

});


const userInfo = document.getElementById('userInfo');
const dropdownContent = document.getElementById('dropdownContent');
let currentRequestController = null;
let historyMenuListenerBound = false;
let latestHistoryRequestId = 0;

userInfo.addEventListener('click', function(event) {
    event.stopPropagation();
    dropdownContent.classList.toggle('show');
});


document.addEventListener('click', function() {
    dropdownContent.classList.remove('show');
});

dropdownContent.addEventListener('click', function(event) {
    event.stopPropagation();
});

async function logout() {
    if (!confirm('确定要退出登录吗？')) {
        return;
    }

    try {
        const response = await fetch('/logout', {
            method: 'POST',
            credentials: 'include'
        });

        if (!response.ok && !response.redirected) {
            throw new Error('退出登录失败');
        }

        window.location.href = '/login?logout=true';
    } catch (error) {
        console.error('退出登录失败:', error);
        alert('退出登录失败，请稍后再试');
    }
}

// 页面刷新前保存状态
window.addEventListener('beforeunload', () => {
    if (appState.isProcessing) {
        // 提示用户
        return "AI正在响应中，确定要离开吗？";
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

        const response = await fetch(`/api/history?${params.toString()}`, {
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

async function loadKnowledgeBases() {
    try {
        const response = await fetch('/api/knowledge-bases/');
        if (!response.ok) {
            throw new Error(`HTTP error! status: ${response.status}`);
        }

        const knowledgeBases = await response.json();
        const selectElement = document.getElementById('knowledgeBaseSelect');
        const currentValue = selectElement.value;

        // 清空现有选项（保留默认选项）
        while (selectElement.options.length > 1) {
            selectElement.remove(1);
        }

        knowledgeBases.forEach(kb => {
            const option = document.createElement('option');
            option.value = kb.id;
            const prefix = kb.visibility === 'shared' ? '【共享】 ' : '';
            option.textContent = prefix + kb.name;
            selectElement.appendChild(option);
        });

        const hasCurrentKnowledgeBase = knowledgeBases.some(kb => kb.id === currentValue);
        selectElement.value = hasCurrentKnowledgeBase ? currentValue : '';
        appState.currentKnowledgeBaseId = selectElement.value || null;

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
            titleElement.title = conversation.title;

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

                loadConversation(conversation.id);
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
                        const response = await fetch(`/api/conversation/${conversationId}/rename`, {
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
                            conversationTitleElement.title = newTitle.trim();

                            // if (appState.currentConversation === conversationId) {
                            //     elements.chatTitle.textContent = newTitle.trim();
                            // }
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
                        const response = await fetch(`/api/conversation/${conversationId}`, {
                            method: 'DELETE',
                            credentials: 'include'
                        });
                        
                        if (response.ok) {
                            item.remove();
                            
                            // 如果删除的是当前对话，重置状态
                            if (appState.currentConversation === conversationId) {
                                appState.currentConversation = null;
                                elements.chatMessages.innerHTML = '';
                                // elements.chatTitle.textContent = "遇事不决怎么办";
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

// 加载对话内容
async function loadConversation(conversationId, knowledgeBaseId = null) {
    appState.currentConversation = conversationId;
    try {
        // 构建查询参数
        const params = new URLSearchParams();
        if (knowledgeBaseId) {
            params.append('knowledge_base_id', knowledgeBaseId);
        }
        
        const url = `/api/conversation/${conversationId}${params.toString() ? `?${params.toString()}` : ''}`;

        const response = await fetch(url, {
            method: 'GET',
            credentials: 'include'
        });
        console.log(response);
        if (response.ok) {
            const conversationData = await response.json();

            // 检查返回的数据结构
            if (Array.isArray(conversationData.messages)) {
                // 正常情况：messages 是数组
                renderConversation(conversationData);
            } else {
                console.error("对话不存在或出错:", conversationData.messages);
                elements.chatMessages.innerHTML = '';
                // 添加场景特定的欢迎消息
                elements.chatMessages.innerHTML = tipsText;
            }
            
            
            // elements.chatTitle.textContent = conversationData.title || "对话详情";
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
    wrapper.appendChild(contentElement);
    wrapper.appendChild(actions);
    messageContainer.appendChild(wrapper);

    elements.chatMessages.appendChild(messageContainer);

    // 如果是AI消息且是测试用例场景，添加导出按钮
    if (!isUser && appState.currentScenario === 'testcase_generation') {
        messageContainer.dataset.raw = message.content;
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
    // 场景切换
    document.querySelectorAll('.function-item').forEach(item => {
        item.addEventListener('click', async () => {
            if (currentRequestController) {
                currentRequestController.abort();
                currentRequestController = null;
                appState.isProcessing = false;
                elements.chatInput.disabled = false;
                elements.sendBtn.disabled = false;
            }

            // 更新活动状态
            document.querySelectorAll('.function-item').forEach(el => {
                el.classList.remove('active');
            });
            item.classList.add('active');
            
            // 更新当前场景
            const newScenario = item.dataset.scenario;
            appState.currentScenario = newScenario;

            // 重置当前对话
            appState.currentConversation = null;
            clearAttachment();
            // elements.chatTitle.textContent = "有问题就会有答案";
            
            // 清空聊天区域
            elements.chatMessages.innerHTML = '';
            // 添加场景特定的欢迎消息
            elements.chatMessages.innerHTML = tipsText           
            // 根据当前选中的知识库加载新场景的历史记录
            await loadHistory(newScenario, appState.currentKnowledgeBaseId);

        });
    });
    
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

        // 刷新历史列表以移除旧对话的高亮状态
        await loadHistory(appState.currentScenario, appState.currentKnowledgeBaseId);
        document.querySelectorAll('.conversation-item').forEach(item => {
            item.classList.remove('active');
        });
    });

    const mobileMenuBtn = document.getElementById('mobileMenuBtn');
    const historyOverlay = document.getElementById('historyOverlay');
    const closeHistoryBtn = document.createElement('button');

    // 打开历史记录侧边栏
    function openHistorySidebar() {
        document.querySelector('.app-container').classList.add('history-open');
    }

    // 关闭历史记录侧边栏
    function closeHistorySidebar() {
        document.querySelector('.app-container').classList.remove('history-open');
    }

    // 事件监听
    if (mobileMenuBtn) {
        mobileMenuBtn.addEventListener('click', openHistorySidebar);
    }

    if (closeHistoryBtn) {
        closeHistoryBtn.addEventListener('click', closeHistorySidebar);
    }

    if (historyOverlay) {
        historyOverlay.addEventListener('click', closeHistorySidebar);
    }

    // 点击历史项时在移动端自动关闭侧边栏
    document.querySelectorAll('.conversation-item').forEach(item => {
        item.addEventListener('click', () => {
            if (window.innerWidth <= 768) {
                closeHistorySidebar();
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
        // 与后端落库格式一致：带附件时加标记前缀
        content: attachedFileName ? `【附件: ${attachedFileName}】\n${message}` : message
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

                const createResponse = await fetch('/api/conversation/new', {
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

        const response = await fetch('/api/chat', {
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
        
        // 读取流式响应
        const reader = response.body.getReader();
        const decoder = new TextDecoder('utf-8');
        let aiResponse = "";
        let conversationTitle = null;
        
        while (true) {
            const { value, done } = await reader.read();
            if (done) break;
            
            // 解码并处理事件流
            const chunk = decoder.decode(value, { stream: true });
            const events = chunk.split('\n\n').filter(event => event.trim() !== '');
            
            for (const event of events) {
                if (event.startsWith('data: ')) {
                    const dataStr = event.replace('data: ', '').trim();
                    
                    // 结束标记
                    if (dataStr === '[DONE]') {
                        break;
                    }
                    
                    try {
                        const data = JSON.parse(dataStr);
                        if (data.token) {
                            // 添加token到响应
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
                        }
                        
                    } catch (e) {
                        console.error('解析JSON失败:', e);
                    }
                }
            }
        }

        // 流结束后做一次最终渲染，确保最后一批 token 被显示
        contentElement.innerHTML = DOMPurify.sanitize(marked.parse(aiResponse));
        smartScrollToBottom();

                // 确保添加导出按钮（如果未在流中处理）
        if (appState.currentScenario === 'testcase_generation') {
            aiMessageContainer.dataset.raw = aiResponse;
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
    exportBtn.title = '导出测试用例';
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
        // 更新 dataset 以备导出
        userMessageContainer.dataset.raw = newMessage;
        // 调用重新生成，传入修改后的消息
        regenerateResponse(aiMessageContainer, newMessage);
    };
}

function escapeHtml(text) {
    const div = document.createElement('div');
    div.textContent = text;
    return div.innerHTML;
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

        const response = await fetch('/api/chat/regenerate', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(requestBody),
            credentials: 'include',
            signal: currentRequestController.signal,
        });

        if (!response.ok) {
            throw new Error('重新生成请求失败');
        }

        const reader = response.body.getReader();
        const decoder = new TextDecoder('utf-8');

        while (true) {
            const { value, done } = await reader.read();
            if (done) break;

            const chunk = decoder.decode(value, { stream: true });
            const events = chunk.split('\n\n').filter(event => event.trim() !== '');

            for (const event of events) {
                if (event.startsWith('data: ')) {
                    const dataStr = event.replace('data: ', '').trim();
                    if (dataStr === '[DONE]') break;

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
                    } catch (e) {
                        console.error('解析JSON失败:', e);
                    }
                }
            }
        }

        // 最终渲染
        contentElement.innerHTML = DOMPurify.sanitize(marked.parse(aiResponse));

    } catch (error) {
        if (error.name !== 'AbortError') {
            console.error('重新生成失败:', error);
            contentElement.innerHTML = oldContent;
            alert('重新生成失败，请稍后再试');
        }
    } finally {
        // 更新导出数据
        messageContainer.dataset.raw = aiResponse || messageContainer.dataset.raw;

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

// 导出测试用例的函数
function exportTestCases(messageContainer) {
    // 从消息容器获取原始内容
    const rawContent = messageContainer.dataset.raw;
    if (!rawContent) {
        alert('未找到测试用例内容');
        return;
    }   
    
    // 提取表格数据
    const tableData = extractTableFromMarkdown(rawContent);
    if (!tableData || tableData.length === 0) {
        alert('未找到表格数据');
        return;
    }

    // 转换为CSV
    const csvContent = convertTableToCSV(tableData);

        // 创建下载链接
    const blob = new Blob([csvContent], { type: 'text/csv;charset=utf-8;' });
    const url = URL.createObjectURL(blob);
    const link = document.createElement('a');
    link.href = url;
    link.download = `testcases_${new Date().getTime()}.csv`;
    link.style.visibility = 'hidden';
    document.body.appendChild(link);
    link.click();
    document.body.removeChild(link);
}

// 从Markdown文本中提取表格数据
function extractTableFromMarkdown(text) {
    const lines = text.split('\n');
    const tableData = [];
    let inTable = false;
    
    for (let line of lines) {
        line = line.trim();
        if (line.startsWith('|') && line.endsWith('|')) {
            // 移除首尾的管道符，并分割单元格
            const cells = line.split('|').slice(1, -1).map(cell => cell.trim());
            // 跳过分隔行（如 | --- | --- |）
            if (cells.every(cell => /^:?-{3,}:?$/.test(cell))) {
                inTable = true;
                continue;
            }
            tableData.push(cells);
            inTable = true;
        } else if (inTable) {
            // 表格结束
            break;
        }
    }
    
    // 如果表格行数少于2（没有表头和数据），则返回空
    if (tableData.length < 2) {
        return [];
    }
    
    return tableData;
}

// 将表格数据转换为CSV格式的字符串
function convertTableToCSV(tableData) {
    let csvContent = '\uFEFF';
    csvContent += tableData.map(row => 
        row.map(cell => `"${cell.replace(/"/g, '""')}"`).join(',')
    ).join('\n');
    return csvContent;
}

function about() {
    alert("AI 智能测试平台");
}