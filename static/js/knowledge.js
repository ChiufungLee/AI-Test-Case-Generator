// API基础URL（根据实际部署情况调整）
const API_BASE_URL = window.location.origin;

// 存储知识库数据
let knowledgeData = [];
let currentKnowledgeBase = null;

// DOM元素
const mobileMenuBtn = document.getElementById('mobileMenuBtn');
const sidebar = document.querySelector('.sidebar');
const knowledgeList = document.getElementById('knowledgeList');
const emptyState = document.getElementById('emptyState');
const createLibBtn = document.getElementById('createLibBtn');
const createModal = document.getElementById('createModal');
const closeModalBtn = document.getElementById('closeModalBtn');
const cancelBtn = document.getElementById('cancelBtn');
const createForm = document.getElementById('createForm');
const searchInput = document.getElementById('searchInput');
// const libCount = document.getElementById('libCount');

// 消息提示与带 401 处理的 fetch 包装由 static/js/common.js 提供（apiFetch / showMessage）

// 加载知识库列表
async function loadKnowledgeBases() {
    try {
        // 显示加载状态
        knowledgeList.innerHTML = '<div class="loading-state"><i class="fas fa-spinner fa-spin"></i><p>加载知识库...</p></div>';
        
        const response = await apiFetch(`${API_BASE_URL}/api/knowledge-bases/`);
        if (!response.ok) {
            throw new Error(`HTTP error! status: ${response.status}`);
        }
        
        knowledgeData = await response.json();
        renderKnowledgeList();
        
        // 如果没有数据，显示空状态
        if (knowledgeData.length === 0) {
            emptyState.style.display = 'block';
            knowledgeList.style.display = 'none';
        } else {
            emptyState.style.display = 'none';
            knowledgeList.style.display = 'grid';
        }
        
        // 更新计数
        // libCount.textContent = knowledgeData.length;
        
    } catch (error) {
        if (isUnauthorized(error)) return;
        console.error('加载知识库失败:', error);
        showMessage(`加载知识库失败: ${error.message}`, 'error');
        
        // 显示错误状态
        knowledgeList.innerHTML = '';
        const errorState = document.createElement('div');
        errorState.className = 'error-state';
        const errorIcon = document.createElement('i');
        errorIcon.className = 'fas fa-exclamation-triangle';
        const errorText = document.createElement('p');
        errorText.textContent = '加载知识库失败';
        const retryBtn = document.createElement('button');
        retryBtn.className = 'retry-btn';
        retryBtn.textContent = '重试';
        retryBtn.addEventListener('click', loadKnowledgeBases);
        errorState.appendChild(errorIcon);
        errorState.appendChild(errorText);
        errorState.appendChild(retryBtn);
        knowledgeList.appendChild(errorState);
        
        // 添加错误状态样式
    }
}

// 渲染知识库列表
function renderKnowledgeList(data = knowledgeData) {
    if (data.length === 0) {
        knowledgeList.style.display = 'none';
        emptyState.style.display = 'block';
        return;
    }

    knowledgeList.style.display = 'grid';
    emptyState.style.display = 'none';
    knowledgeList.innerHTML = '';

    const colors = [
        '#4361ee', '#3a0ca3', '#4cc9f0', '#f72585',
        '#7209b7', '#2a9d8f', '#f8961e', '#43aa8b'
    ];

    const icons = [
        'fas fa-database', 'fas fa-book', 'fas fa-code', 'fas fa-clipboard-list',
        'fas fa-comments', 'fas fa-network-wired', 'fas fa-graduation-cap', 'fas fa-file-alt'
    ];

    data.forEach((item, index) => {
        const color = colors[index % colors.length];
        const icon = icons[index % icons.length];
        const createdDate = new Date(item.created_at || item.updated_at).toLocaleDateString('zh-CN');
        const updatedDate = new Date(item.updated_at).toLocaleDateString('zh-CN');
        const isOwner = item.owner_user_id === window.currentUserId;

        const card = document.createElement('div');
        card.className = 'lib-card';
        card.dataset.id = item.id;

        const header = document.createElement('div');
        header.className = 'card-header';

        const iconWrap = document.createElement('div');
        iconWrap.className = 'lib-icon';
        iconWrap.style.background = `linear-gradient(135deg, ${color}, ${color}99)`;
        const iconEl = document.createElement('i');
        iconEl.className = icon;
        iconWrap.appendChild(iconEl);

        const headerText = document.createElement('div');
        const nameEl = document.createElement('h3');
        nameEl.className = 'lib-name';
        nameEl.textContent = item.name;
        if (item.visibility === 'shared') {
            const sharedBadge = document.createElement('span');
            sharedBadge.className = 'shared-badge';
            sharedBadge.textContent = '共享';
            sharedBadge.style.cssText = 'display:inline-block;margin-left:8px;padding:1px 8px;font-size:0.7rem;background:#e8f5e9;color:#2e7d32;border-radius:10px;vertical-align:middle;font-weight:normal;';
            nameEl.appendChild(sharedBadge);
        }
        const countEl = document.createElement('p');
        countEl.className = 'lib-count';
        countEl.textContent = `${item.file_count || 0} 个文档`;
        headerText.appendChild(nameEl);
        headerText.appendChild(countEl);

        header.appendChild(iconWrap);
        header.appendChild(headerText);

        const body = document.createElement('div');
        body.className = 'card-body';
        const descriptionEl = document.createElement('p');
        descriptionEl.className = 'lib-description';
        descriptionEl.textContent = item.description || '暂无描述';

        const meta = document.createElement('div');
        meta.className = 'lib-meta';
        const createdSpan = document.createElement('span');
        createdSpan.textContent = `创建于 ${createdDate}`;
        const createdIcon = document.createElement('i');
        createdIcon.className = 'far fa-calendar-alt';
        createdSpan.prepend(createdIcon, ' ');

        const updatedSpan = document.createElement('span');
        updatedSpan.textContent = `更新: ${updatedDate}`;
        const updatedIcon = document.createElement('i');
        updatedIcon.className = 'far fa-clock';
        updatedSpan.prepend(updatedIcon, ' ');

        meta.appendChild(createdSpan);
        meta.appendChild(updatedSpan);
        body.appendChild(descriptionEl);
        body.appendChild(meta);

        const footer = document.createElement('div');
        footer.className = 'card-footer';

        const openBtn = document.createElement('button');
        openBtn.className = 'create-btn open-btn';
        openBtn.style.padding = '8px 16px';
        openBtn.style.fontSize = '0.9rem';
        openBtn.addEventListener('click', () => openKnowledgeBase(item.id));
        const openIcon = document.createElement('i');
        openIcon.className = 'fas fa-folder-open';
        openBtn.appendChild(openIcon);
        openBtn.appendChild(document.createTextNode(' 打开'));

        const moreContainer = document.createElement('div');
        moreContainer.className = 'more-container';
        const moreBtn = document.createElement('button');
        moreBtn.className = 'more-btn';
        moreBtn.setAttribute('aria-label', '更多操作');
        const moreIcon = document.createElement('i');
        moreIcon.className = 'fas fa-ellipsis-v';
        moreBtn.appendChild(moreIcon);

        const dropdown = document.createElement('div');
        dropdown.className = 'dropdown-menu';

        const editLink = document.createElement('a');
        editLink.href = '#';
        editLink.className = 'dropdown-item edit';
        editLink.addEventListener('click', (event) => {
            event.preventDefault();
            editKnowledgeBase(item.id);
        });
        const editIcon = document.createElement('i');
        editIcon.className = 'fas fa-edit';
        editLink.appendChild(editIcon);
        editLink.appendChild(document.createTextNode(' 编辑'));

        const deleteLink = document.createElement('a');
        deleteLink.href = '#';
        deleteLink.className = 'dropdown-item delete';
        deleteLink.addEventListener('click', (event) => {
            event.preventDefault();
            deleteLib(item.id);
        });
        const deleteIcon = document.createElement('i');
        deleteIcon.className = 'fas fa-trash-alt';
        deleteLink.appendChild(deleteIcon);
        deleteLink.appendChild(document.createTextNode(' 删除'));

        dropdown.appendChild(editLink);
        dropdown.appendChild(deleteLink);
        moreContainer.appendChild(moreBtn);
        moreContainer.appendChild(dropdown);
        footer.appendChild(openBtn);
        if (isOwner) {
            footer.appendChild(moreContainer);
        }

        card.appendChild(header);
        card.appendChild(body);
        card.appendChild(footer);
        knowledgeList.appendChild(card);
    });
}

// 打开知识库详情
function openKnowledgeBase(kbId) {
    window.location.href = `/knowledge-detail?kb_id=${kbId}`;
}

// 创建知识库
async function createKnowledgeBase(name, description) {
    try {
        const response = await apiFetch(`${API_BASE_URL}/api/knowledge-bases/`, {
            method: 'POST',
            headers: {
                'Content-Type': 'application/json',
            },
            body: JSON.stringify({
                name: name,
                description: description,
                visibility: document.getElementById('libVisibility') ? document.getElementById('libVisibility').value : 'private'
            })
        });
        
        if (!response.ok) {
            const errorData = await response.json();
            throw new Error(errorData.detail || `创建失败，状态码: ${response.status}`);
        }
        
        const newKnowledgeBase = await response.json();
        
        // 添加到本地数据并重新渲染
        knowledgeData.unshift(newKnowledgeBase);
        renderKnowledgeList();
        
        showMessage(`知识库 "${name}" 创建成功！`, 'success');
        return newKnowledgeBase;
        
    } catch (error) {
        console.error('创建知识库失败:', error);
        showMessage(`创建知识库失败: ${error.message}`, 'error');
        throw error;
    }
}

// 删除知识库
async function deleteKnowledgeBase(kbId) {
    try {
        const response = await apiFetch(`${API_BASE_URL}/api/knowledge-bases/${kbId}`, {
            method: 'DELETE'
        });
        
        if (!response.ok) {
            const errorData = await response.json();
            throw new Error(errorData.detail || `删除失败，状态码: ${response.status}`);
        }
        
        // 从本地数据中删除
        const index = knowledgeData.findIndex(item => item.id === kbId);
        const deletedName = knowledgeData[index]?.name || '知识库';
        
        if (index !== -1) {
            knowledgeData.splice(index, 1);
        }
        
        renderKnowledgeList();
        
        showMessage(`知识库 "${deletedName}" 已删除`, 'success');
        return true;
        
    } catch (error) {
        console.error('删除知识库失败:', error);
        showMessage(`删除知识库失败: ${error.message}`, 'error');
        throw error;
    }
}

// 获取知识库详情
async function getKnowledgeBaseDetail(kbId) {
    try {
        const response = await apiFetch(`${API_BASE_URL}/api/knowledge-bases/${kbId}`);
        
        if (!response.ok) {
            const errorData = await response.json();
            throw new Error(errorData.detail || `获取详情失败，状态码: ${response.status}`);
        }
        
        const knowledgeBase = await response.json();
        return knowledgeBase;
        
    } catch (error) {
        console.error('获取知识库详情失败:', error);
        showMessage(`获取知识库详情失败: ${error.message}`, 'error');
        throw error;
    }
}

// 上传文件到知识库
async function uploadFileToKnowledgeBase(kbId, file) {
    try {
        const formData = new FormData();
        formData.append('file', file);
        
        const response = await apiFetch(`${API_BASE_URL}/api/knowledge-bases/${kbId}/upload`, {
            method: 'POST',
            body: formData
        });
        
        if (!response.ok) {
            const errorData = await response.json();
            throw new Error(errorData.detail || `上传失败，状态码: ${response.status}`);
        }
        
        const result = await response.json();
        
        if (result.success) {
            showMessage(`文件 "${file.name}" 上传成功，正在后台处理`, 'success');
            
            // 重新加载知识库列表以更新文件计数
            setTimeout(() => {
                loadKnowledgeBases();
            }, 200);
        } else {
            showMessage(`文件上传失败: ${result.message}`, 'error');
        }
        
        return result;
        
    } catch (error) {
        console.error('上传文件失败:', error);
        showMessage(`上传文件失败: ${error.message}`, 'error');
        throw error;
    }
}

// 获取知识库集合信息（向量存储信息）
async function getCollectionInfo(kbId) {
    try {
        const response = await apiFetch(`${API_BASE_URL}/api/knowledge-bases/${kbId}/collection-info`);
        
        if (!response.ok) {
            const errorData = await response.json();
            throw new Error(errorData.detail || `获取集合信息失败，状态码: ${response.status}`);
        }
        
        const info = await response.json();
        return info;
        
    } catch (error) {
        console.error('获取集合信息失败:', error);
        showMessage(`获取集合信息失败: ${error.message}`, 'error');
        throw error;
    }
}

// 编辑知识库
async function editKnowledgeBase(kbId) {
    try {
        // 获取知识库详情
        const knowledgeBase = await getKnowledgeBaseDetail(kbId);
        if (!knowledgeBase) return;
        
        // 显示编辑模态框
        showEditModal(knowledgeBase);
        
    } catch (error) {
        console.error('获取知识库详情失败:', error);
        showMessage(`获取知识库详情失败: ${error.message}`, 'error');
    }
}


// 显示编辑模态框
function showEditModal(knowledgeBase) {
    const editModal = document.createElement('div');
    editModal.className = 'modal active';
    editModal.id = 'editModal';

    const modalContent = document.createElement('div');
    modalContent.className = 'modal-content';

    const modalHeader = document.createElement('div');
    modalHeader.className = 'modal-header';
    const modalTitle = document.createElement('h3');
    modalTitle.className = 'modal-title';
    modalTitle.textContent = '编辑知识库';
    const closeBtn = document.createElement('button');
    closeBtn.className = 'close-btn';
    closeBtn.id = 'closeEditModalBtn';
    closeBtn.innerHTML = '<i class="fas fa-times"></i>';
    modalHeader.appendChild(modalTitle);
    modalHeader.appendChild(closeBtn);

    const editForm = document.createElement('form');
    editForm.id = 'editForm';

    const hiddenInput = document.createElement('input');
    hiddenInput.type = 'hidden';
    hiddenInput.id = 'editKbId';
    hiddenInput.value = knowledgeBase.id;

    const nameGroup = document.createElement('div');
    nameGroup.className = 'form-group';
    const nameLabel = document.createElement('label');
    nameLabel.htmlFor = 'editLibName';
    nameLabel.textContent = '知识库名称';
    const nameInput = document.createElement('input');
    nameInput.type = 'text';
    nameInput.id = 'editLibName';
    nameInput.placeholder = '请输入知识库名称';
    nameInput.required = true;
    nameInput.value = knowledgeBase.name;
    nameGroup.appendChild(nameLabel);
    nameGroup.appendChild(nameInput);

    const descGroup = document.createElement('div');
    descGroup.className = 'form-group';
    const descLabel = document.createElement('label');
    descLabel.htmlFor = 'editLibDescription';
    descLabel.textContent = '描述';
    const descInput = document.createElement('textarea');
    descInput.id = 'editLibDescription';
    descInput.placeholder = '请描述这个知识库的主要内容和用途';
    descInput.value = knowledgeBase.description || '';
    descGroup.appendChild(descLabel);
    descGroup.appendChild(descInput);

    const visGroup = document.createElement('div');
    visGroup.className = 'form-group';
    const visLabel = document.createElement('label');
    visLabel.htmlFor = 'editLibVisibility';
    visLabel.textContent = '可见性';
    const visSelect = document.createElement('select');
    visSelect.id = 'editLibVisibility';
    visSelect.style.cssText = 'width:100%;padding:8px 12px;border:1px solid #ddd;border-radius:6px;font-size:0.95rem;';
    const optPrivate = document.createElement('option');
    optPrivate.value = 'private';
    optPrivate.textContent = '私有 - 仅自己可见';
    const optShared = document.createElement('option');
    optShared.value = 'shared';
    optShared.textContent = '共享 - 所有人可查看和使用';
    if (knowledgeBase.visibility === 'shared') {
        optShared.selected = true;
    } else {
        optPrivate.selected = true;
    }
    visSelect.appendChild(optPrivate);
    visSelect.appendChild(optShared);
    visGroup.appendChild(visLabel);
    visGroup.appendChild(visSelect);

    const actions = document.createElement('div');
    actions.className = 'modal-actions';
    const cancelBtn = document.createElement('button');
    cancelBtn.type = 'button';
    cancelBtn.className = 'btn-secondary';
    cancelBtn.id = 'cancelEditBtn';
    cancelBtn.textContent = '取消';
    const submitBtn = document.createElement('button');
    submitBtn.type = 'submit';
    submitBtn.className = 'btn-primary';
    submitBtn.textContent = '保存更改';
    actions.appendChild(cancelBtn);
    actions.appendChild(submitBtn);

    editForm.appendChild(hiddenInput);
    editForm.appendChild(nameGroup);
    editForm.appendChild(descGroup);
    editForm.appendChild(visGroup);
    editForm.appendChild(actions);

    modalContent.appendChild(modalHeader);
    modalContent.appendChild(editForm);
    editModal.appendChild(modalContent);
    document.body.appendChild(editModal);
    
    // 添加编辑模态框样式（如果不存在）
    if (!document.querySelector('#editModalStyles')) {
        const style = document.createElement('style');
        style.id = 'editModalStyles';
        style.textContent = `
            #editModal .modal-content {
                max-width: 500px;
            }
        `;
        document.head.appendChild(style);
    }
    
    // 焦点到名称输入框
    setTimeout(() => {
        document.getElementById('editLibName').focus();
    }, 100);
    
    // 事件监听器
    const closeEditModalBtn = document.getElementById('closeEditModalBtn');
    const cancelEditBtn = document.getElementById('cancelEditBtn');
    
    // 关闭编辑模态框
    function closeEditModal() {
        if (editModal.parentNode) {
            editModal.remove();
        }
    }
    
    closeEditModalBtn.addEventListener('click', closeEditModal);
    cancelEditBtn.addEventListener('click', closeEditModal);
    
    // 点击模态框背景关闭
    editModal.addEventListener('click', (e) => {
        if (e.target === editModal) {
            closeEditModal();
        }
    });
    
    // 编辑表单提交
    editForm.addEventListener('submit', async (e) => {
        e.preventDefault();
        
        const kbId = document.getElementById('editKbId').value;
        const name = document.getElementById('editLibName').value;
        const description = document.getElementById('editLibDescription').value;
        
        if (!name.trim()) {
            showMessage('请输入知识库名称', 'warning');
            return;
        }
        
        try {
            // 显示加载状态
            const submitBtn = editForm.querySelector('.btn-primary');
            const originalText = submitBtn.textContent;
            submitBtn.innerHTML = '<i class="fas fa-spinner fa-spin"></i> 保存中...';
            submitBtn.disabled = true;
            
            // 调用更新知识库API
            const response = await apiFetch(`${API_BASE_URL}/api/knowledge-bases/${kbId}`, {
                method: 'PUT',
                headers: {
                    'Content-Type': 'application/json',
                },
                body: JSON.stringify({
                    name: name,
                    description: description,
                    visibility: document.getElementById('editLibVisibility').value
                })
            });

            if (!response.ok) {
                const errorData = await response.json();
                throw new Error(errorData.detail || `更新失败，状态码: ${response.status}`);
            }

            const updatedKnowledgeBase = await response.json();

            // 更新本地数据
            const index = knowledgeData.findIndex(item => item.id === kbId);
            if (index !== -1) {
                knowledgeData[index] = updatedKnowledgeBase;
            }

            renderKnowledgeList();
            closeEditModal();

            showMessage(`知识库 "${name}" 更新成功！`, 'success');

        } catch (error) {
            console.error('更新知识库失败:', error);
            showMessage(`更新知识库失败: ${error.message}`, 'error');
        } finally {
            // 恢复按钮状态
            const submitBtn = editForm.querySelector('.btn-primary');
            submitBtn.textContent = '保存更改';
            submitBtn.disabled = false;
        }
    });
}

// 删除知识库（UI交互）
async function deleteLib(kbId) {
    const lib = knowledgeData.find(item => item.id === kbId);
    if (!lib) return;
    
    if (confirm(`确定要删除知识库 "${lib.name}" 吗？此操作将删除所有关联文件和向量数据，无法撤销。`)) {
        try {
            await deleteKnowledgeBase(kbId);
        } catch (error) {
            // 错误已经在函数中处理
        }
    }
}
// 初始化事件监听器
function initEventListeners() {
    // 移动端菜单切换由 nav.js 统一处理

    // 显示创建模态框
    createLibBtn.addEventListener('click', () => {
        createModal.classList.add('active');
        document.getElementById('libName').focus();
    });

    // 关闭创建模态框
    function closeModal() {
        createModal.classList.remove('active');
        createForm.reset();
    }

    closeModalBtn.addEventListener('click', closeModal);
    cancelBtn.addEventListener('click', closeModal);

    // 创建知识库表单提交
    createForm.addEventListener('submit', async (e) => {
        e.preventDefault();
        
        const name = document.getElementById('libName').value;
        const description = document.getElementById('libDescription').value;
        
        if (!name.trim()) {
            showMessage('请输入知识库名称', 'warning');
            return;
        }
        
        try {
            // 显示加载状态
            const submitBtn = createForm.querySelector('.btn-primary');
            const originalText = submitBtn.textContent;
            submitBtn.innerHTML = '<i class="fas fa-spinner fa-spin"></i> 创建中...';
            submitBtn.disabled = true;
            
            await createKnowledgeBase(name, description);
            closeModal();
            
        } catch (error) {
            // 错误已经在函数中处理
        } finally {
            // 恢复按钮状态
            const submitBtn = createForm.querySelector('.btn-primary');
            submitBtn.textContent = '创建知识库';
            submitBtn.disabled = false;
        }
    });

    // 搜索功能
    searchInput.addEventListener('input', (e) => {
        const searchTerm = e.target.value.toLowerCase().trim();
        
        if (!searchTerm) {
            renderKnowledgeList(knowledgeData);
            return;
        }
        
        const filteredData = knowledgeData.filter(item => 
            item.name.toLowerCase().includes(searchTerm) || 
            (item.description && item.description.toLowerCase().includes(searchTerm))
        );
        
        renderKnowledgeList(filteredData);
    });

    // 窗口大小变化时调整布局
    window.addEventListener('resize', () => {
        if (window.innerWidth > 768) {
            sidebar.classList.remove('active');
        }
    });
}


// 初始化应用
document.addEventListener('DOMContentLoaded', async () => {
    // 初始化事件监听器
    initEventListeners();
    
    // 加载知识库数据
    await loadKnowledgeBases();
    });