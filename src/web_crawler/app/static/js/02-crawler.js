    /* ========== 采集器逻辑（原有） ========== */
    var form = document.getElementById('crawler-form');
    var log = document.getElementById('log');
    var statusEl = document.getElementById('status');
    var bar = document.getElementById('progress-bar');
    var runButton = document.getElementById('run-button');
    var pauseButton = document.getElementById('pause-button');
    var resumeButton = document.getElementById('resume-button');
    var cancelButton = document.getElementById('cancel-button');
    var openButton = document.getElementById('open-button');
    var currentResults = document.getElementById('current-results');
    var crawlTarget = document.getElementById('crawl-target');
    var jobId = null;
    var timer = null;
    var pollVersion = 0;
    var pollFailures = 0;
    var currentJob = null;

    async function post(path, data) {
      var response = await fetch(path, { method: 'POST', body: data || new URLSearchParams() });
      if (!response.ok) {
        var msg = 'HTTP ' + response.status;
        try { var errBody = await response.json(); if (errBody && errBody.error) { msg = errBody.error; } } catch(_) {}
        throw new Error(msg);
      }
      return await response.json();
    }
    async function postJson(path, data, signal) {
      var response = await fetch(path, {
        method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(data), signal: signal
      });
      var result = await response.json();
      if (!response.ok) throw new Error(result.error || 'HTTP ' + response.status);
      return result;
    }
    function downloadJson(name, value) {
      var blob = new Blob([JSON.stringify(value, null, 2) + '\n'], {type: 'application/json'});
      var url = URL.createObjectURL(blob);
      var link = document.createElement('a');
      link.href = url;
      link.download = name;
      document.body.appendChild(link);
      link.click();
      link.remove();
      setTimeout(function() { URL.revokeObjectURL(url); }, 1000);
    }
    async function localJson(file, limit) {
      if (file.size > limit) throw new Error('文件超出大小限制');
      return JSON.parse((await file.text()).replace(/^\uFEFF/, ''));
    }
    function applyCrawlConfig(config) {
      ['cookie', 'referer', 'headers'].forEach(function(name) { form.elements.namedItem(name).value = ''; });
      Object.keys(config).forEach(function(name) {
        var fieldName = name === 'block_keyword' ? 'block_keywords' : name;
        var field = form.elements.namedItem(fieldName);
        if (!field) return;
        if (field.type === 'checkbox') field.checked = config[name];
        else if (name === 'block_keyword') field.value = config[name].join('\n');
        else if (name === 'field_rules') field.value = config[name] ? JSON.stringify(config[name], null, 2) : '';
        else field.value = config[name] == null ? '' : String(config[name]);
      });
      if (config.field_rules) document.getElementById('crawler-rule-settings').open = true;
      if (window.__crawlPresetSync) window.__crawlPresetSync();
      // 导入的配置带了请求设置时自动展开，避免用户看不到被填进折叠区的值
      if (['referer', 'headers'].some(function(n) { return config[n]; }) || (config.block_keyword && config.block_keyword.length)) {
        document.getElementById('crawler-request-settings').open = true;
      }
    }
    var configFeedback = document.getElementById('crawl-config-status');
    var configFile = document.getElementById('crawl-config-file');
    var crawlRulesFile = document.getElementById('crawl-rules-file');
    var crawlRules = document.getElementById('crawler-field-rules');
    document.getElementById('export-crawl-config').onclick = async function() {
      try {
        var data = await post('/crawl/config/export', new URLSearchParams(new FormData(form)));
        downloadJson('crawl-config.json', data.config);
        configFeedback.textContent = '采集配置已导出';
      } catch(e) { configFeedback.textContent = '导出失败：' + e.message; }
    };
    document.getElementById('import-crawl-config').onclick = function() { configFile.click(); };
    configFile.onchange = async function() {
      if (!this.files.length) return;
      var file = this.files[0];
      this.value = '';
      try {
        var data = await postJson('/crawl/config/import', await localJson(file, 262144));
        applyCrawlConfig(data.config);
        configFeedback.textContent = '采集配置已载入';
      } catch(e) { configFeedback.textContent = '导入失败：' + e.message; }
    };
    document.getElementById('load-crawl-rules').onclick = function() { crawlRulesFile.click(); };
    crawlRulesFile.onchange = async function() {
      if (!this.files.length) return;
      var file = this.files[0];
      this.value = '';
      try {
        crawlRules.value = JSON.stringify(await localJson(file, 131072), null, 2);
      } catch(e) { configFeedback.textContent = '规则文件读取失败：' + e.message; }
    };
    function isActiveCrawl(status) {
      return ['running', 'paused', 'cancelling', 'finishing'].indexOf(status) >= 0;
    }
    function setCrawlControls(status) {
      runButton.disabled = status === 'starting' || isActiveCrawl(status);
      pauseButton.disabled = status !== 'running';
      resumeButton.disabled = status !== 'paused';
      cancelButton.disabled = status !== 'running' && status !== 'paused';
      currentResults.hidden = ['done', 'partial', 'skipped', 'error', 'cancelled'].indexOf(status) < 0;
    }
    /* URL 校验：必须含 http(s):// 前缀
       （注意：正则字面量中斜杠必须转义 \/，此前写 \\/ 会终止正则导致整段脚本 SyntaxError） */
    function validateUrl(value) {
      if (!value) { return 'URL 不能为空'; }
      if (!/^https?:\/\/[^\s]+$/i.test(value)) { return 'URL 必须以 http:// 或 https:// 开头'; }
      return null;
    }
    /* 后端状态值 → 中文（后端仍返回英文枚举，这里只做展示层翻译） */
    var STATUS_LABELS = { running: '运行中', paused: '已暂停', cancelling: '正在取消', finishing: '正在保存结果', done: '已完成', partial: '完成（有失败项）', skipped: '页面已跳过', error: '失败', cancelled: '已取消', missing: '任务已不存在', stopped: '已停止' };
    var STATUS_HINTS = {
      running: '正在采集，可「暂停」或「取消」',
      paused: '已暂停，点「继续」接着跑',
      cancelling: '等待当前操作结束',
      error: '原因见下方日志，修改参数后可重试',
      cancelled: '任务已取消，修改参数后可重新开始',
      missing: '可在「任务历史」查看已保存的记录'
    };
    function formatStatus(data) {
      var st = data.status || '';
      var label = STATUS_LABELS[st] || st || '等待开始';
      var total = data.total_resources || 0;
      var processed = data.processed_resources || 0;
      var parts = [label];
      if (total > 0) { parts.push('资源 ' + processed + '/' + total); }
      else if (processed > 0) { parts.push('资源 ' + processed); }
      parts.push('已扫描页面 ' + (data.pages_scanned || 0));
      var summary = data.summary || {};
      var scan = summary.page_scan || {};
      if (scan.failed) parts.push('页面失败 ' + scan.failed);
      if (scan.skipped) parts.push('页面跳过 ' + scan.skipped);
      if (summary.failed) parts.push('资源失败 ' + summary.failed);
      if (summary.extracted_pages) parts.push('提取 ' + summary.extracted_pages + ' 页');
      if (summary.page_samples) parts.push('保留 ' + summary.page_samples + ' 个样本');
      if (summary.rule_results && summary.rule_results.pages_missing_required) {
        parts.push('缺必填字段 ' + summary.rule_results.pages_missing_required + ' 页');
      }
      var text = parts.join(' · ');
      if (data.current_url) { text += ' · 当前：' + data.current_url; }
      if (data.error || STATUS_HINTS[st]) { text += '（' + (data.error || STATUS_HINTS[st]) + '）'; }
      return text;
    }
    function renderCrawl(result) {
      currentJob = result;
      crawlTarget.textContent = result.url || '';
      crawlTarget.hidden = !result.url;
      var logText = result.log || '';
      if (log.textContent !== logText) {
        var atBottom = log.scrollHeight - log.clientHeight - log.scrollTop < 24;
        log.textContent = logText || '等待日志…';
        if (atBottom) log.scrollTop = log.scrollHeight;
      }
      log.classList.toggle('log-empty', !logText);
      var pct = result.percent || 0;
      bar.style.width = pct + '%';
      bar.setAttribute('aria-valuenow', String(pct));
      var statusText = formatStatus(result);
      if (statusEl.textContent !== statusText) statusEl.textContent = statusText;
      setCrawlControls(result.status);
    }
    async function poll() {
      if (!jobId) return;
      clearTimeout(timer);
      var version = ++pollVersion;
      var controller = new AbortController();
      var deadline = setTimeout(function() { controller.abort(); }, 8000);
      var result;
      try {
        var response = await fetch('/status?id=' + encodeURIComponent(jobId), { signal: controller.signal });
        if (!response.ok) { throw new Error('HTTP ' + response.status); }
        result = await response.json();
      } catch(e) {
        if (version !== pollVersion) return;
        pollFailures++;
        statusEl.textContent = '暂时无法读取任务状态，正在重新连接：' + e.message;
        timer = setTimeout(poll, Math.min(1000 * 2 ** Math.min(pollFailures, 4), 10000));
        return;
      } finally {
        clearTimeout(deadline);
      }
      if (version !== pollVersion) return;
      pollFailures = 0;
      var wasActive = !currentJob || currentJob.status === 'starting' || isActiveCrawl(currentJob.status);
      renderCrawl(result);
      if (isActiveCrawl(result.status)) {
        timer = setTimeout(poll, 1000);
      } else if (wasActive && document.getElementById('tab-history').classList.contains('active')) {
        loadHistory();
      }
    }
    function watchCrawl(id) {
      jobId = id;
      pollFailures = 0;
      renderCrawl({ id: id, status: 'starting' });
      statusEl.textContent = '正在读取采集任务…';
      poll();
    }
    form.addEventListener('submit', async function(event) {
      event.preventDefault();
      /* 客户端校验 */
      var urlErr = validateUrl(form.querySelector('[name=url]').value);
      if (urlErr) {
        statusEl.textContent = urlErr;
        form.querySelector('[name=url]').focus();
        return;
      }
      setCrawlControls('starting');
      log.textContent = '正在启动采集进程，日志马上出现…';
      log.classList.add('log-empty');
      statusEl.textContent = '正在启动采集…';
      bar.style.width = '0%';
      bar.setAttribute('aria-valuenow', '0');
      try {
        var result = await post('/run', new URLSearchParams(new FormData(form)));
        var id = result.id || result.active_id;
        if (!id) { throw new Error(result.error || '启动失败'); }
        watchCrawl(id);
      } catch(e) {
        setCrawlControls(currentJob ? currentJob.status : '');
        statusEl.textContent = '启动失败：' + e.message + '（检查网页 URL 与参数后重试）';
      }
    });
    async function ctrl(path) {
      if (!jobId) return;
      try {
        var result = await post(path + '?id=' + encodeURIComponent(jobId));
        if (!result.ok) throw new Error(result.message || '操作未生效');
        await poll();
      }
      catch(e) {
        /* 写入状态行 + 日志区：状态行会被下次 poll 覆盖，日志区持久可见 */
        statusEl.textContent = '操作失败：' + e.message;
        if (log.textContent) { log.textContent += '\\n操作失败：' + e.message; }
        else { log.textContent = '操作失败：' + e.message; }
      }
    }
    pauseButton.onclick = function() { ctrl('/pause'); };
    resumeButton.onclick = function() { ctrl('/resume'); };
    cancelButton.onclick = function() { ctrl('/cancel'); };
    openButton.onclick = async function() {
      var data = new URLSearchParams();
      data.set('out', (currentJob && currentJob.output_dir) || document.getElementById('out').value);
      try { var result = await post('/open-output', data); statusEl.textContent = result.message; }
      catch(e) { statusEl.textContent = '打开失败：' + e.message; }
    };
    currentResults.onclick = function() { showResults(jobId, currentJob.url); };
