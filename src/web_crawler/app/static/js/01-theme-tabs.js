    /* ========== 主题切换 ========== */
    (function() {
      var theme = localStorage.getItem('crawler-theme');
      if (theme === 'dark') document.documentElement.setAttribute('data-theme', 'dark');
    })();
    function toggleTheme() {
      var html = document.documentElement;
      var isDark = html.getAttribute('data-theme') === 'dark';
      if (isDark) { html.removeAttribute('data-theme'); localStorage.setItem('crawler-theme', 'light'); }
      else { html.setAttribute('data-theme', 'dark'); localStorage.setItem('crawler-theme', 'dark'); }
    }

    /* ========== Tab 切换 ========== */
    function activateTab(tab) {
      document.querySelectorAll('.tab').forEach(function(t) {
        var on = (t === tab);
        t.classList.toggle('active', on);
        t.setAttribute('aria-selected', on ? 'true' : 'false');
        t.tabIndex = on ? 0 : -1;
      });
      document.querySelectorAll('.tab-content').forEach(function(c) { c.classList.remove('active'); });
      document.getElementById('tab-' + tab.dataset.tab).classList.add('active');
      if (tab.dataset.tab === 'history') loadHistory(1);
    }
    document.querySelectorAll('.tab').forEach(function(tab) {
      tab.tabIndex = tab.classList.contains('active') ? 0 : -1;
      tab.onclick = function() { activateTab(tab); };
    });
    var activityButtons = document.querySelectorAll('.activity-item');
    activityButtons.forEach(function(button) {
      button.onclick = function() {
        var tab = document.getElementById('tabbtn-' + button.dataset.targetTab);
        activateTab(tab);
        tab.focus();
      };
    });
    async function refreshActivity() {
      try {
        if (document.hidden) return;
        var response = await fetch('/activity', { signal: AbortSignal.timeout(5000) });
        if (!response.ok) throw new Error('HTTP ' + response.status);
        var counts = (await response.json()).active;
        activityButtons.forEach(function(button) {
          var count = counts[button.dataset.targetTab] || 0;
          button.dataset.active = count > 0 ? 'true' : 'false';
          button.querySelector('span').textContent = count ? count + ' 个运行中' : '空闲';
        });
      } catch (_) {
        activityButtons.forEach(function(button) {
          button.dataset.active = 'false';
          button.querySelector('span').textContent = '状态不可用';
        });
      } finally {
        setTimeout(refreshActivity, 5000);
      }
    }
    refreshActivity();
    /* 页签键盘操作：左右方向键切换（role=tab 的无障碍惯例） */
    document.querySelector('.tabs').addEventListener('keydown', function(e) {
      var step = e.key === 'ArrowRight' ? 1 : (e.key === 'ArrowLeft' ? -1 : 0);
      if (!step) { return; }
      var tabs = Array.prototype.slice.call(document.querySelectorAll('.tab'));
      var i = tabs.indexOf(document.activeElement);
      if (i < 0) { return; }
      e.preventDefault();
      var next = tabs[(i + step + tabs.length) % tabs.length];
      next.focus();
      activateTab(next);
    });
