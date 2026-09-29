// 公共侧栏行为：用户菜单、退出登录、移动端抽屉开关（配合 templates/partials/sidebar.html 使用）

document.addEventListener('DOMContentLoaded', () => {
    const userInfo = document.getElementById('userInfo');
    const dropdownContent = document.getElementById('dropdownContent');
    const mobileMenuBtn = document.getElementById('mobileMenuBtn');
    const sidebar = document.querySelector('.sidebar');

    // 版权年份（主内容区页脚）
    const year = String(new Date().getFullYear());
    document.querySelectorAll('.js-nav-year').forEach((el) => {
        el.textContent = year;
    });

    // 侧栏收缩/展开（移动端抽屉模式忽略收缩态）
    const sidebarToggle = document.getElementById('sidebarToggle');
    const COLLAPSE_KEY = 'sidebarCollapsed';
    if (sidebarToggle && sidebar) {
        const applyCollapseState = () => {
            const collapsed = localStorage.getItem(COLLAPSE_KEY) === '1';
            sidebar.classList.toggle('collapsed', collapsed);
            sidebarToggle.setAttribute('aria-expanded', String(!collapsed));
            sidebarToggle.setAttribute('aria-label', collapsed ? '展开菜单' : '收起菜单');
            const icon = sidebarToggle.querySelector('i');
            if (icon) {
                icon.className = collapsed ? 'fas fa-chevron-right' : 'fas fa-chevron-left';
            }
        };

        applyCollapseState();
        sidebarToggle.addEventListener('click', (event) => {
            event.stopPropagation();
            const collapsed = localStorage.getItem(COLLAPSE_KEY) !== '1';
            localStorage.setItem(COLLAPSE_KEY, collapsed ? '1' : '0');
            applyCollapseState();
        });
    }

    if (userInfo && dropdownContent) {
        userInfo.addEventListener('click', (event) => {
            event.stopPropagation();
            dropdownContent.classList.toggle('show');
        });
        dropdownContent.addEventListener('click', (event) => event.stopPropagation());
    }

    document.addEventListener('click', () => {
        if (dropdownContent) {
            dropdownContent.classList.remove('show');
        }
    });

    if (mobileMenuBtn && sidebar) {
        mobileMenuBtn.addEventListener('click', (event) => {
            event.stopPropagation();
            sidebar.classList.toggle('active');
        });

        document.addEventListener('click', (event) => {
            if (window.innerWidth <= 768
                && sidebar.classList.contains('active')
                && !sidebar.contains(event.target)
                && !mobileMenuBtn.contains(event.target)) {
                sidebar.classList.remove('active');
            }
        });
    }
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
