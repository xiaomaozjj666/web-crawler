    /* ========== JS 逆向 Agent 逻辑 ========== */
    var reverseForm = document.getElementById('reverse-form');
    var reverseStopBtn = document.getElementById('reverse-stop');
    var reverseClearLogBtn = document.getElementById('reverse-clear-log');
    var currentReverseJobId = null;
    var reverseTimer = null;
    /* 自适应轮询间隔：running=800ms，done/error/cancelled=停止 */
    var REVERSE_POLL_RUNNING = 800;
    var REVERSE_POLL_IDLE = 3000;
    /* 轮询失败重试计数：连续 3 次失败停止轮询 */
    var REVERSE_POLL_FAIL = 0;
    var REVERSE_POLL_FAIL_MAX = 3;
    /* 隐藏的下载 iframe：避免 window.location.href 中断 SSE 连接 */
    var downloadFrame = document.createElement('iframe');
    downloadFrame.style.display = 'none';
    downloadFrame.setAttribute('aria-hidden', 'true');
    downloadFrame.title = 'download-frame';
    document.body.appendChild(downloadFrame);
    function downloadViaFrame(url) {
      downloadFrame.src = url;
    }

    function escapeHtml(s) {
      return String(s == null ? '' : s).replace(/[<>&"]/g, function(c) {
        return {'<':'&lt;','>':'&gt;','&':'&amp;','"':'&quot;'}[c];
      });
    }

    function renderStep(s) {
      /* 步骤类型调色板：全局唯一一份，第二个 script 块也复用它（原来两份内容完全相同） */
    var STEP_COLORS = { navigate: '#3b82f6', inject_hook: '#a855f7', analyze_js: '#f97316',
                       wait: '#6b7280', extract: '#22c55e', solve_captcha: '#eab308', done: '#16a34a',
                       click: '#06b6d4', type: '#8b5cf6', scroll: '#64748b',
                       press: '#f59e0b', hover: '#ec4899', select_option: '#14b8a6' };
      var color = STEP_COLORS[s.action_type] || '#6b7280';
      var confColor = '#6b7280';
      if (s.confidence != null) {
        confColor = s.confidence >= 0.7 ? '#22c55e' : s.confidence >= 0.4 ? '#eab308' : '#ef4444';
      }
      var confHtml = (s.confidence != null) ? '<span class="step-conf" style="color:' + confColor + '">conf ' + Number(s.confidence).toFixed(2) + '</span>' : '';
      return '<div class="trace-step" style="border-left-color:' + color + '">' +
        '<div class="step-header">' +
        '<span class="step-num">Step ' + s.step + '</span>' +
        '<span class="action-badge" style="background:' + color + '">' + escapeHtml(s.action_type || '--') + '</span>' +
        '<span class="step-meta">' + (s.duration_ms || 0) + 'ms</span>' +
        confHtml +
        '</div>' +
        '<div class="step-reasoning">' + escapeHtml(s.reasoning || '') + '</div>' +
        '</div>';
    }

    /* 更新统计卡片 */
    function updateStats(data) {
      var setText = function(id, val) {
        var el = document.getElementById(id);
        if (el) { el.textContent = val; }
      };
      setText('rev-stat-steps', data.current_step || 0);
      setText('rev-stat-avg-ms', (data.avg_step_ms || 0) + ' ms');
      setText('rev-stat-hooks', data.hook_count || 0);
      setText('rev-stat-net', data.network_count != null ? data.network_count : (data.network_requests || []).length);
      var tp = data.target_params || [];
      var tpFound = data.target_params_found || {};
      var foundCount = tp.filter(function(p) { return tpFound[p]; }).length;
      setText('rev-stat-params', foundCount + '/' + tp.length);
    }

    /* 渲染截图卡片 */
    function renderScreenshots(screenshots, errorScreenshot, jobId) {
      var wrap = document.getElementById('rev-screenshots');
      var countEl = document.getElementById('rev-screenshot-count');
      if (!screenshots || !screenshots.length) {
        wrap.innerHTML = '<div class="screenshot-empty">等待截图数据...</div>';
        countEl.textContent = '0';
        return;
      }
      countEl.textContent = screenshots.length;
      var html = screenshots.map(function(s) {
        var isError = s.error || (errorScreenshot && s.path === errorScreenshot);
        var imgUrl = '/reverse/screenshot?id=' + encodeURIComponent(jobId) + '&step=' + s.step + (isError ? '&error=1' : '');
        var label = 'Step ' + s.step + (isError ? ' (错误)' : '');
        return '<div class="screenshot-card' + (isError ? ' error' : '') + '" data-src="' + imgUrl + '" role="button" tabindex="0" aria-label="放大 ' + escapeHtml(label) + '">' +
          '<img src="' + imgUrl + '" alt="' + escapeHtml(label) + '" loading="lazy">' +
          '<div class="screenshot-label">' + escapeHtml(label) + '</div>' +
          '</div>';
      }).join('');
      wrap.innerHTML = html;
      /* 点击放大（含键盘 Enter/Space 触发） */
      wrap.querySelectorAll('.screenshot-card').forEach(function(card) {
        var open = function() {
          var src = card.getAttribute('data-src');
          openScreenshotModal(src);
        };
        card.onclick = open;
        card.onkeydown = function(e) {
          if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); open(); }
        };
      });
    }

    /* 截图放大弹窗（Esc 关闭，焦点归还触发元素） */
    var screenshotModal = document.getElementById('rev-screenshot-modal');
    var screenshotModalImg = document.getElementById('rev-screenshot-modal-img');
    var screenshotModalOpener = null;
    function openScreenshotModal(src) {
      screenshotModalImg.src = src;
      screenshotModal.classList.add('show');
      screenshotModalOpener = document.activeElement;
    }
    function closeScreenshotModal() {
      screenshotModal.classList.remove('show');
      screenshotModalImg.src = '';
      if (screenshotModalOpener) { try { screenshotModalOpener.focus(); } catch(_) {} screenshotModalOpener = null; }
    }
    screenshotModal.onclick = closeScreenshotModal;
    /* 全局 Esc：关闭截图/结果弹窗 */
    document.addEventListener('keydown', function(e) {
      if (e.key !== 'Escape') return;
      if (screenshotModal && screenshotModal.classList.contains('show')) { closeScreenshotModal(); return; }
      var resultsModal = document.getElementById('results-modal');
      if (resultsModal && resultsModal.classList.contains('show')) { closeResults(); }
    });

    async function pollReverse() {
      if (!currentReverseJobId) return;
      try {
        var response = await fetch('/reverse/status?id=' + encodeURIComponent(currentReverseJobId));
        if (!response.ok) { throw new Error('HTTP ' + response.status); }
        var data = await response.json();
        REVERSE_POLL_FAIL = 0;
      } catch(e) {
        REVERSE_POLL_FAIL++;
        var st = document.getElementById('rev-status');
        if (REVERSE_POLL_FAIL >= REVERSE_POLL_FAIL_MAX) {
          if (st) { st.textContent = '查询连续失败 ' + REVERSE_POLL_FAIL + ' 次'; }
          if (reverseTimer) { clearTimeout(reverseTimer); reverseTimer = null; }
          if (reverseSSE) { reverseSSE.close(); reverseSSE = null; }
          var submitBtn = reverseForm.querySelector('button[type=submit]');
          if (submitBtn) { submitBtn.disabled = false; }
          /* 任务仍在服务器侧运行,保留停止按钮可用,用户可刷新恢复 */
        } else {
          if (st) { st.textContent = '查询失败（第 ' + REVERSE_POLL_FAIL + ' 次，将重试）'; }
        }
        return;
      }
      if (!data) return;
      /* 统一走 updateReverseUI：避免与 SSE snapshot 路径的渲染逻辑分叉 */
      updateReverseUI(data);
      if (!reverseLogCleared) {
        renderScreenshots(data.screenshots, data.error_screenshot, currentReverseJobId);
      }
    }

    function showReverseResult(data) {
      var section = document.getElementById('rev-result-section');
      section.style.display = 'block';
      document.getElementById('rev-result-status').textContent = STATUS_LABELS[data.status] || data.status || '--';
      if (data.error) { document.getElementById('rev-result-status').textContent += ' (' + data.error + ')'; }
      var judge = data.judge_result || {};
      var judgeText = '--';
      if (judge.verified !== undefined) {
        judgeText = judge.verified ? '通过' : '未通过';
        if (judge.missing && judge.missing.length) { judgeText += ' (缺失: ' + judge.missing.join(', ') + ')'; }
      }
      document.getElementById('rev-judge').textContent = judgeText;
      document.getElementById('rev-analysis').textContent = data.analysis || '--';

      /* 同步更新底部统计卡片（避免重复 DOM ID 导致永不刷新） */
      var steps = (data.steps || []).length;
      var stepDurations = data.step_durations || [];
      var avgMs = stepDurations.length ? (stepDurations.reduce(function(a, b) { return a + b; }, 0) / stepDurations.length) : 0;
      var hookCount = data.hook_count || 0;
      var tpFound = data.target_params_found || {};
      var targetFound = Object.keys(tpFound).length;
      var targetTotal = (data.target_params || []).length;
      var setText = function(id, val) {
        var el = document.getElementById(id);
        if (el) { el.textContent = val; }
      };
      setText('rev-result-stat-steps', steps);
      setText('rev-result-stat-avg-ms', Math.round(avgMs) + ' ms');
      setText('rev-result-stat-hooks', hookCount);
      setText('rev-result-stat-params', targetFound + '/' + targetTotal);

      var dlBtn = document.getElementById('rev-download-script');
      dlBtn.disabled = !data.compiled_script;
      dlBtn.onclick = function() {
        if (!data.compiled_script) { return; }
        /* 走后端 /reverse/script 接口下载（含 Content-Disposition），
           用隐藏 iframe 触发，避免 window.location.href 中断 SSE 连接 */
        downloadViaFrame('/reverse/script?id=' + encodeURIComponent(currentReverseJobId));
      };
    }

    /* 自适应轮询：根据状态动态调整间隔（仅作 SSE 不可用时的降级方案） */
    function startReversePolling() {
      if (reverseTimer) { clearTimeout(reverseTimer); reverseTimer = null; }
      /* 优先尝试 SSE 实时推送 */
      if (typeof EventSource !== 'undefined' && currentReverseJobId) {
        startReverseSSE();
        return;
      }
      /* 降级：HTTP 轮询（用 setTimeout 递归避免并发重叠） */
      var schedulePoll = function() {
        var statusEl = document.getElementById('rev-status');
        var currentStatus = statusEl ? statusEl.textContent : '';
        var interval = (currentStatus === 'running') ? REVERSE_POLL_RUNNING : REVERSE_POLL_IDLE;
        reverseTimer = setTimeout(function() {
          pollReverse().then(function() {
            if (reverseTimer) { schedulePoll(); }
          }).catch(function() {
            if (reverseTimer) { schedulePoll(); }
          });
        }, interval);
      };
      schedulePoll();
    }

    /* SSE 实时推送 */
    var reverseSSE = null;
    function startReverseSSE() {
      if (reverseSSE) { reverseSSE.close(); reverseSSE = null; }
      if (reverseTimer) { clearTimeout(reverseTimer); reverseTimer = null; }
      reverseSSE = new EventSource('/reverse/stream?id=' + encodeURIComponent(currentReverseJobId));
      /* 快照事件：更新整个 UI */
      reverseSSE.addEventListener('snapshot', function(e) {
        try {
          var data = JSON.parse(e.data);
          updateReverseUI(data);
        } catch(err) {}
      });
      /* 单步事件：增量更新轨迹 */
      reverseSSE.addEventListener('step', function(e) {
        try {
          var evt = JSON.parse(e.data);
          appendReverseEvent(evt);
        } catch(err) {}
      });
      /* 终态事件：关闭 SSE */
      reverseSSE.addEventListener('final', function(e) {
        try {
          var data = JSON.parse(e.data);
          updateReverseUI(data);
          /* 终态快照含截图列表：SSE 主路径也需渲染（此前只有轮询降级路径能出图） */
          if (!reverseLogCleared) {
            renderScreenshots(data.screenshots, data.error_screenshot, currentReverseJobId);
          }
        } catch(err) {}
        if (reverseSSE) { reverseSSE.close(); reverseSSE = null; }
      });
      reverseSSE.onerror = function() {
        /* SSE 断开，降级为轮询 */
        if (reverseSSE) { reverseSSE.close(); reverseSSE = null; }
        if (!reverseTimer && currentReverseJobId) {
          /* 用 setTimeout 递归避免并发重叠 */
          var scheduleFallback = function() {
            reverseTimer = setTimeout(function() {
              pollReverse().then(function() {
                if (reverseTimer) { scheduleFallback(); }
              }).catch(function() {
                if (reverseTimer) { scheduleFallback(); }
              });
            }, REVERSE_POLL_RUNNING);
          };
          scheduleFallback();
        }
      };
    }

    /* SSE 增量事件：追加到轨迹（不重画整个 UI） */
    function appendReverseEvent(evt) {
      var traceEl = document.getElementById('rev-trace');
      if (!traceEl) return;
      if (evt.type === 'step.end' || evt.type === 'action' || evt.type === 'browser.action') {
        var s = evt.payload || {};
        var step = evt.step || s.step || 0;
        var actionType = s.action_type || s.action || 'step';
        var reasoning = s.reasoning || '';
        var conf = s.confidence;
        var card = document.createElement('div');
        card.className = 'trace-step';
        var color = STEP_COLORS[actionType] || '#6b7280';
        card.style.borderLeftColor = color;
        var html = '<div class="step-header"><span class="step-num">Step ' + step + '</span>' +
          '<span class="action-badge" style="background:' + color + '">' + escapeHtml(actionType) + '</span>';
        if (s.duration_ms) html += '<span class="step-meta">' + s.duration_ms + 'ms</span>';
        if (conf !== undefined) {
          var cc = conf >= 0.7 ? '#22c55e' : conf >= 0.4 ? '#eab308' : '#ef4444';
          html += '<span class="step-conf" style="color:' + cc + '">conf ' + Number(conf).toFixed(2) + '</span>';
        }
        html += '</div><div class="step-reasoning">' + escapeHtml(reasoning) + '</div>';
        card.innerHTML = html;
        /* 移除空状态提示 */
        var empty = traceEl.querySelector('.trace-empty');
        if (empty) empty.remove();
        var nearBottom = traceEl.scrollHeight - traceEl.scrollTop - traceEl.clientHeight < 60;
        traceEl.appendChild(card);
        if (nearBottom) { traceEl.scrollTop = traceEl.scrollHeight; }
      }
    }

    /* 抽取 UI 更新逻辑供 SSE 复用 */
    /* lastRenderedStepCount 用于避免 SSE snapshot 全量重绘 trace：
       增量 step 事件已由 appendReverseEvent 处理，snapshot 仅在
       步数减少（切换任务/清空）或首次绘制时做全量重写 */
    var lastRenderedStepCount = -1;
    function updateReverseUI(data) {
      if (!data) return;
      document.getElementById('rev-current-step').textContent = data.current_step || 0;
      document.getElementById('rev-max-steps-display').textContent = data.max_steps || 20;
      var statusBadge = document.getElementById('rev-status');
      statusBadge.textContent = STATUS_LABELS[data.status] || data.status || '--';
      statusBadge.className = 'status-badge ' + (data.status || '');
      /* 运行中确保停止可用：页面刷新后按钮初始为 disabled,轮询恢复时重新启用 */
      if (data.status === 'running' && reverseStopBtn) { reverseStopBtn.disabled = false; }
      document.getElementById('rev-stat-steps').textContent = data.current_step || 0;
      document.getElementById('rev-stat-avg-ms').textContent = Math.round(data.avg_step_ms || 0) + ' ms';
      /* 总耗时：终态用 finished_at 固定值，running 才显示动态墙钟值 */
      var elapsed;
      if (['done','error','cancelled','missing','stopped'].indexOf(data.status) >= 0 && data.finished_at) {
        elapsed = Math.max(0, Math.floor(data.finished_at - (data.created_at || 0)));
      } else {
        elapsed = Math.max(0, Math.floor((Date.now() / 1000) - (data.created_at || 0)));
      }
      document.getElementById('rev-stat-elapsed').textContent = elapsed + 's';
      document.getElementById('rev-stat-hooks').textContent = data.hook_count || 0;
      document.getElementById('rev-stat-net').textContent = data.network_count != null ? data.network_count : (data.network_requests || []).length;
      if (data.last_confidence && data.last_confidence.score !== undefined) {
        var score = data.last_confidence.score;
        var confColor = score >= 0.7 ? '#22c55e' : score >= 0.4 ? '#eab308' : '#ef4444';
        document.getElementById('rev-confidence-meter').style.background =
          'conic-gradient(' + confColor + ' ' + (score * 360) + 'deg, var(--bar-bg) ' + (score * 360) + 'deg)';
        document.getElementById('rev-confidence-value').textContent = Number(score).toFixed(2);
        document.getElementById('rev-confidence-value').style.color = confColor;
        var reasonsHtml = (data.last_confidence.reasons || []).map(function(r) {
          return '<div class="reason-item">' + escapeHtml(r) + '</div>';
        }).join('');
        document.getElementById('rev-confidence-reasons').innerHTML = reasonsHtml || '无';
      }
      var guardHtml = (data.guard_blocks || []).map(function(g) {
        return '<div class="guard-item">' + escapeHtml(g.rule) + ': ' + escapeHtml(g.detail) + '</div>';
      }).join('');
      document.getElementById('rev-guard-blocks').innerHTML = guardHtml || '无';
      var cpHtml = (data.checkpoints || []).map(function(c) {
        return '<div class="checkpoint-item">Step ' + c.step + ' - ' + escapeHtml(String(c.url || c.path || '').slice(0, 50)) + '</div>';
      }).join('');
      document.getElementById('rev-checkpoints').innerHTML = cpHtml || '无';
      var tpHtml = (data.target_params || []).map(function(p) {
        var found = data.target_params_found && data.target_params_found[p];
        return '<div class="param-item ' + (found ? 'found' : '') + '">' + (found ? '[OK] ' : '[..] ') + escapeHtml(p) + (found ? ': ' + escapeHtml(found) : '') + '</div>';
      }).join('');
      document.getElementById('rev-target-params-found').innerHTML = tpHtml || '--';
      document.getElementById('rev-hook-count').textContent = data.hook_count || 0;
      var hookHtml = (data.hook_records || []).slice(-10).map(function(h) {
        return '<div class="hook-item">' + escapeHtml(JSON.stringify(h)) + '</div>';
      }).join('');
      document.getElementById('rev-hooks').innerHTML = hookHtml || '无数据';
      /* Trace 全量重绘只在步数减少（切任务/清空）或首次绘制时触发；
         否则依赖增量 step 事件，避免 innerHTML 重写导致用户滚动位置丢失 */
      var traceEl = document.getElementById('rev-trace');
      var stepCount = (data.steps || []).length;
      if (!reverseLogCleared && (stepCount < lastRenderedStepCount || lastRenderedStepCount < 0)) {
        var nearBottom = traceEl.scrollHeight - traceEl.scrollTop - traceEl.clientHeight < 60;
        var stepsHtml = stepCount ? (data.steps || []).map(function(s) { return renderStep(s); }).join('') : '';
        traceEl.innerHTML = stepsHtml || '<div class="trace-empty">等待数据...</div>';
        if (nearBottom) { traceEl.scrollTop = traceEl.scrollHeight; }
        lastRenderedStepCount = stepCount;
      } else if (!reverseLogCleared) {
        /* 步数未减少：同步 lastRenderedStepCount 以便后续切换任务时正确判断 */
        lastRenderedStepCount = Math.max(lastRenderedStepCount, stepCount);
      }
      updateStats(data);
      if (['done', 'error', 'cancelled'].includes(data.status)) {
        if (reverseSSE) { reverseSSE.close(); reverseSSE = null; }
        if (reverseTimer) { clearTimeout(reverseTimer); reverseTimer = null; }
        reverseLogCleared = false;
        showReverseResult(data);
        /* 恢复提交按钮状态，让用户能启动新任务 */
        var form = document.getElementById('reverse-form');
        if (form) {
          var submitBtn = form.querySelector('button[type=submit]');
          if (submitBtn) { submitBtn.disabled = false; }
        }
        var stopBtn = document.getElementById('reverse-stop');
        if (stopBtn) { stopBtn.disabled = true; }
        loadReverseHistory();
      }
    }

    /* ========== 历史任务加载 ========== */
    async function loadReverseHistory() {
      var list = document.getElementById('rev-history-list');
      var countEl = document.getElementById('rev-history-count');
      try {
        var response = await fetch('/reverse/jobs');
        var data = await response.json();
      } catch(e) {
        if (list) { list.innerHTML = '<div class="history-empty">加载失败</div>'; }
        if (countEl) { countEl.textContent = '0'; }
        return;
      }
      var jobs = data.jobs || [];
      countEl.textContent = jobs.length;
      if (!jobs.length) {
        list.innerHTML = '<div class="history-empty">暂无历史任务</div>';
        return;
      }
      var html = jobs.map(function(j) {
        var statusClass = j.status === 'done' ? 'done' : j.status === 'error' ? 'error' : 'running';
        var isActive = j.id === currentReverseJobId ? ' active' : '';
        var ts = new Date((j.created_at || 0) * 1000);
        var tsStr = ts.toLocaleTimeString('zh-CN', { hour: '2-digit', minute: '2-digit', second: '2-digit' });
        var successMark = j.success ? ' [OK]' : '';
        return '<div class="history-item' + isActive + '" data-job-id="' + escapeHtml(j.id) + '">' +
          '<span class="history-status status-badge ' + statusClass + '">' + escapeHtml(STATUS_LABELS[j.status] || j.status) + '</span>' +
          '<span class="history-url" title="' + escapeHtml(j.url) + '">' + escapeHtml(j.url) + escapeHtml(successMark) + '</span>' +
          '<span class="history-step">' + tsStr + ' S' + (j.current_step || 0) + '/' + (j.max_steps || 0) + '</span>' +
          '</div>';
      }).join('');
      list.innerHTML = html;
      /* 点击历史任务：加载该任务状态 */
      list.querySelectorAll('.history-item').forEach(function(item) {
        item.onclick = function() {
          var jobId = item.getAttribute('data-job-id');
          if (jobId) { selectHistoryJob(jobId); }
        };
      });
    }

    /* 选中历史任务：加载其状态到面板 */
    async function selectHistoryJob(jobId) {
      /* 先清理旧的 SSE/轮询，避免旧任务事件覆盖新选中任务的 UI */
      if (reverseSSE) { reverseSSE.close(); reverseSSE = null; }
      if (reverseTimer) { clearTimeout(reverseTimer); reverseTimer = null; }
      currentReverseJobId = jobId;
      var myToken = jobId;
      /* 切换任务时重置 trace 重绘计数器，强制下一次 snapshot 全量绘制 */
      lastRenderedStepCount = -1;
      reverseLogCleared = false;
      REVERSE_POLL_FAIL = 0;
      try {
        await pollReverse();
      } catch(e) { /* pollReverse 内部已处理 */ }
      /* 防竞态：若用户在等待期间又切到另一个任务，则不继续 */
      if (myToken !== currentReverseJobId) return;
      /* 若任务仍在运行，恢复实时轮询/SSE */
      var statusEl = document.getElementById('rev-status');
      var status = statusEl ? statusEl.textContent : '';
      if (status === 'running') { startReversePolling(); }
      /* 高亮当前选中的历史项 */
      document.querySelectorAll('.history-item').forEach(function(el) {
        el.classList.toggle('active', el.getAttribute('data-job-id') === jobId);
      });
    }

    /* ========== 任务模板 ========== */
    var TEMPLATES = {
      anti_content: { task: '提取 Anti-Content 加密参数', target_params: 'anti_content' },
      x_bogus: { task: '提取 X-Bogus 加密参数', target_params: 'X-Bogus' },
      signature: { task: '提取 _signature 加密参数', target_params: '_signature' },
      generic: { task: '提取页面所有加密参数', target_params: 'anti_content, sign, X-Bogus' }
    };
    document.querySelectorAll('.template-btn').forEach(function(btn) {
      btn.onclick = function() {
        var key = btn.getAttribute('data-template');
        var tpl = TEMPLATES[key];
        if (!tpl) return;
        reverseForm.querySelector('[name=task]').value = tpl.task;
        reverseForm.querySelector('[name=target_params]').value = tpl.target_params;
      };
    });

    /* ========== 配置导入导出 ========== */
    document.getElementById('rev-config-export').onclick = function() {
      var formData = new FormData(reverseForm);
      fetch('/reverse/config/export', { method: 'POST', body: formData })
        .then(function(r) { return r.json(); })
        .then(function(data) {
          if (data.error) { alert(data.error); return; }
          var blob = new Blob([JSON.stringify(data, null, 2)], { type: 'application/json' });
          var a = document.createElement('a');
          a.href = URL.createObjectURL(blob);
          a.download = 'reverse_config.json';
          a.click();
          URL.revokeObjectURL(a.href);
        })
        .catch(function(e) { alert('导出失败: ' + e.message); });
    };

    document.getElementById('rev-config-import-btn').onclick = function() {
      document.getElementById('rev-config-file').click();
    };
    document.getElementById('rev-config-file').onchange = function(e) {
      var file = e.target.files[0];
      if (!file) return;
      var reader = new FileReader();
      reader.onload = function(ev) {
        try {
          var config = JSON.parse(ev.target.result);
        } catch(err) {
          alert('配置文件格式错误: ' + err.message);
          return;
        }
        fetch('/reverse/config/import', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify(config)
        })
          .then(function(r) { return r.json(); })
          .then(function(data) {
            if (data.error) { alert(data.error); return; }
            applyConfigToForm(data.config || {});
            alert('配置已导入');
          })
          .catch(function(err) { alert('导入失败: ' + err.message); });
      };
      reader.readAsText(file);
      /* 重置 input 以便再次选择同一文件 */
      e.target.value = '';
    };

    /* 把配置 dict 回填到表单 */
    function applyConfigToForm(cfg) {
      var fields = {
        max_steps: 'max_steps', target_params: 'target_params', proxy: 'proxy',
        os_name: 'os_name',
        min_confidence: 'min_confidence', allowed_domains: 'allowed_domains',
        checkpoint_interval: 'checkpoint_interval', checkpoint_keep: 'checkpoint_keep',
        dom_prune_max_chars: 'dom_prune_max_chars'
      };
      Object.keys(fields).forEach(function(key) {
        var el = reverseForm.querySelector('[name="' + fields[key] + '"]');
        if (el && cfg[key] !== undefined && cfg[key] !== null) {
          el.value = Array.isArray(cfg[key]) ? cfg[key].join(', ') : String(cfg[key]);
        }
      });
      /* 复选框 */
      var checkboxes = {
        enable_checkpoint: 'enable_checkpoint', enable_guard: 'enable_guard',
        enable_screenshot: 'enable_screenshot', dom_prune: 'dom_prune'
      };
      Object.keys(checkboxes).forEach(function(key) {
        var el = reverseForm.querySelector('[name="' + checkboxes[key] + '"]');
        if (el) { el.checked = !!cfg[key]; }
      });
      /* headless 下拉 */
      var headlessEl = reverseForm.querySelector('[name=headless]');
      if (headlessEl && cfg.headless !== undefined) {
        headlessEl.value = cfg.headless ? 'true' : 'false';
      }
    }

    /* ========== 清空日志 ========== */
    var reverseLogCleared = false;
    reverseClearLogBtn.onclick = async function() {
      if (!currentReverseJobId) {
        document.getElementById('rev-trace').innerHTML = '<div class="trace-empty">已清空</div>';
        return;
      }
      try {
        await fetch('/reverse/clear?id=' + encodeURIComponent(currentReverseJobId), { method: 'POST' });
        reverseLogCleared = true;
        document.getElementById('rev-trace').innerHTML = '<div class="trace-empty">已清空运行时数据</div>';
        document.getElementById('rev-screenshots').innerHTML = '<div class="screenshot-empty">已清空</div>';
        document.getElementById('rev-screenshot-count').textContent = '0';
      } catch(e) { /* 忽略 */ }
    };

    reverseForm.addEventListener('submit', async function(event) {
      event.preventDefault();
      var submitBtn = reverseForm.querySelector('button[type=submit]');
      /* 客户端校验：URL 必填且合规 */
      var urlInput = reverseForm.querySelector('[name=url]');
      var urlErr = validateUrl(urlInput.value.trim());
      if (urlErr) {
        urlInput.focus();
        var st = document.getElementById('rev-status');
        if (st) { st.textContent = urlErr; }
        return;
      }
      /* 最大步数校验：>=1 */
      var maxStepsInput = reverseForm.querySelector('[name=max_steps]');
      var maxStepsVal = parseInt(maxStepsInput.value, 10);
      if (isNaN(maxStepsVal) || maxStepsVal < 1) {
        maxStepsInput.focus();
        var st2 = document.getElementById('rev-status');
        if (st2) { st2.textContent = '最大步数必须为不小于 1 的整数'; }
        return;
      }
      /* 若当前任务仍在运行，需用户确认才能启动新任务 */
      if (currentReverseJobId && !submitBtn.disabled) {
        var statusEl = document.getElementById('rev-status');
        var curStatus = statusEl ? statusEl.textContent : '';
        if (curStatus === 'running' && !confirm('当前有任务正在运行，是否强制启动新任务？')) {
          return;
        }
      }
      submitBtn.disabled = true;
      reverseStopBtn.disabled = false;
      reverseLogCleared = false;
      REVERSE_POLL_FAIL = 0;
      lastRenderedStepCount = -1;  /* 重置，强制首次全量绘制 */
      document.getElementById('rev-result-section').style.display = 'none';
      document.getElementById('rev-trace').innerHTML = '<div class="trace-empty">正在启动 Agent…（首次使用会自动下载浏览器组件，可能需要几分钟）</div>';
      try {
        var result = await fetch('/reverse/run', { method: 'POST', body: new FormData(reverseForm) }).then(function(r) { return r.json(); });
        if (result.id) {
          currentReverseJobId = result.id;
          startReversePolling();
          await pollReverse();
          /* 启动后刷新历史列表 */
          loadReverseHistory();
        } else {
          submitBtn.disabled = false;
          reverseStopBtn.disabled = true;
          alert(result.error || '启动失败');
        }
      } catch(e) {
        submitBtn.disabled = false;
        reverseStopBtn.disabled = true;
        alert('请求失败: ' + e.message);
      }
    });

    reverseStopBtn.onclick = async function() {
      if (!currentReverseJobId) return;
      reverseStopBtn.disabled = true;
      try {
        await fetch('/reverse/stop?id=' + encodeURIComponent(currentReverseJobId), { method: 'POST' });
        var st = document.getElementById('rev-status');
        if (st) { st.textContent = '已请求停止:Agent 将在当前动作完成后立即中断'; }
        await pollReverse();
      } catch(e) {
        reverseStopBtn.disabled = false;
        var st = document.getElementById('rev-status');
        if (st) { st.textContent = '停止失败：' + e.message; }
      }
    };

    /* 页面加载时拉取历史任务列表 */
    loadReverseHistory();