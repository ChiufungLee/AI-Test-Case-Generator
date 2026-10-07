class LoginManager {
    constructor() {
        this.baseURL = '/';
        this.initialize();
    }

    initialize() {
        this.bindEvents();
    }

    async parseResponse(response) {
        const contentType = response.headers.get('content-type') || '';
        if (contentType.includes('application/json')) {
            try {
                return await response.json();
            } catch {
                return null;
            }
        }

        try {
            const text = await response.text();
            return text ? { detail: text } : null;
        } catch {
            return null;
        }
    }
    
    bindEvents() {
        const loginForm = document.getElementById('loginForm');
        if (loginForm) {
            loginForm.addEventListener('submit', async (e) => {
                e.preventDefault();
                await this.handleLogin();
            });
        }
    }
    
    async handleLogin() {
        const usernameInput = document.getElementById('username');
        const passwordInput = document.getElementById('password');
        
        if (!usernameInput || !passwordInput) return;
        
        const username = usernameInput.value.trim();
        const password = passwordInput.value;
        
        // 验证输入
        if (!username || !password) {
            this.showError('请输入用户名和密码');
            return;
        }
        
        // 显示加载状态
        this.setLoading(true);
        
        try {
            // 调用登录API
            const formData = new URLSearchParams();
            formData.append('username', username);
            formData.append('password', password);

            const response = await fetch('/login', {
                method: 'POST',
                headers: {
                    'Content-Type': 'application/x-www-form-urlencoded',
                },
                body: formData.toString()
            });

            // 检查是否被重定向
            if (response.redirected) {
                // 重定向到目标页面，不尝试解析JSON
                window.location.href = response.url;
                return;
            }

            const data = await this.parseResponse(response);

            if (response.status === 401) {
                this.setLoading(false);
                this.showError(data?.detail || '用户名或密码错误');
                return;
            }

            if (!response.ok) {
                this.setLoading(false);
                this.showError(data?.detail || '登录失败');
                return;
            }
            
            this.showSuccess('登录成功，正在跳转...');
            
            // 延迟跳转到主页面
            setTimeout(() => {
                window.location.href = '/chat';
            }, 500);
            
        } catch (error) {
            console.error('登录请求失败:', error);
            this.setLoading(false);
            this.showError('登录请求失败，请检查网络连接');
        }
    }
    
    setLoading(isLoading) {
        const button = document.getElementById('loginButton');
        if (button) {
            if (isLoading) {
                button.disabled = true;
                button.textContent = '登录中...';
            } else {
                button.disabled = false;
                button.textContent = '登录';
            }
        }
    }
    
    showError(message) {
        const errorDiv = document.getElementById('errorMessage');
        if (errorDiv) {
            errorDiv.textContent = message;
            errorDiv.style.display = 'block';
            
            // 隐藏成功消息
            const successDiv = document.getElementById('successMessage');
            if (successDiv) successDiv.style.display = 'none';
            
            // 5秒后自动隐藏错误消息
            setTimeout(() => {
                errorDiv.style.display = 'none';
            }, 5000);
        }
    }
    
    showSuccess(message) {
        const successDiv = document.getElementById('successMessage');
        if (successDiv) {
            successDiv.textContent = message;
            successDiv.style.display = 'block';
            
            // 隐藏错误消息
            const errorDiv = document.getElementById('errorMessage');
            if (errorDiv) errorDiv.style.display = 'none';
        }
    }
}

// 页面加载完成后初始化
document.addEventListener('DOMContentLoaded', () => {
    new LoginManager();
});