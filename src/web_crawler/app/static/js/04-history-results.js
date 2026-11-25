    var historyPage = 1;
    var resultsTaskId = null;
    var resultsPage = 1;
    var resultsPaneTouched = false;
    var historyVersion = 0;
    var resultsVersion = 0;
    var detailsVersion = 0;
    var pagesVersion = 0;
    var extractionVersion = 0;
    var sourceUrl = '';
    var selectedPageUrl = '';
    var sampleAvailable = false;
    var ruleRequestVersion = 0;
    var verifiedRules = null;
    var auditedRules = null;
    var taskHasSamples = false;
    var auditController = null;
    var auditReport = null;
    var rulesEditor = document.getElementById('rules-editor');
    var rulesFeedback = document.getElementById('rules-status');
    var rulesOutput = document.getElementById('rules-result');
    var rulesFile = document.getElementById('rules-import-file');
    var validateRules = document.getElementById('validate-rules');
    var exportRules = document.getElementById('export-rules');
    var useRules = document.getElementById('use-rules');
    var auditRules = document.getElementById('audit-rules');
    var auditStop = document.getElementById('stop-rule-audit');
    var auditFeedback = document.getElementById('rule-audit-status');
    var auditResults = document.getElementById('rule-audit-results');

    function fmtSize(bytes) {
      if (bytes < 1024) return bytes + ' B';
      if (bytes < 1048576) return (bytes/1024).toFixed(1) + ' KB';
      if (bytes < 1073741824) return (bytes/1048576).toFixed(1) + ' MB';
      return (bytes/1073741824).toFixed(1) + ' GB';
    }
    function fmtTime(ts) {
      if (!ts) return '-';
      var d = new Date(ts * 1000);
      return d.toLocaleString();
    }
    /* 状态圆点用的高饱和色（只是装饰条，不承载文字对比度） */
    function statusColor(s) {
      return {done:'#16a34a',running:'#3b82f6',error:'#ef4444',cancelled:'#6b7280',paused:'#eab308',partial:'#eab308',cancelling:'#3b82f6'}[s] || '#6b7280';
    }
    function historyStatusClass(s) {
      return ['running','done','partial','skipped','error','cancelled','paused','cancelling'].indexOf(s) >= 0 ? s : 'cancelled';
    }
    function goToCrawlerTab() {
      var tab = document.querySelector('.tab[data-tab="crawler"]');
      if (tab) { tab.click(); tab.focus(); }
    }
    function resetHistoryFilter() {
      document.getElementById('history-filter').value = 'all';
      loadHistory(1);
    }
    function historyEmptyHtml(filter) {
      if (filter !== 'all') {
        return '<div class="empty-state">' +
          '<p class="empty-title">当前筛选下没有任务</p>' +
          '<p class="empty-text">把「状态筛选」切回「全部状态」，可以看到所有历史任务。</p>' +
          '<button type="button" class="btn-primary" onclick="resetHistoryFilter()">查看全部任务</button>' +
        '</div>';
      }
      return '<div class="empty-state">' +
        '<p class="empty-title">还没有采集任务</p>' +
        '<p class="empty-text">在「网页采集」页填写要采集的网页 URL，点「开始采集」。任务结束后，这里会列出每次任务的状态、资源数量和采集结果。</p>' +
        '<button type="button" class="btn-primary" onclick="goToCrawlerTab()">去「网页采集」新建任务</button>' +
      '</div>';
    }

    function renderPagination(container, page, totalPages, navigate) {
      container.replaceChildren();
      if (totalPages <= 1) return;
      var previous = 0;
      var pages = new Set([1, page - 1, page, page + 1, totalPages]);
      Array.from(pages).filter(function(p) { return p >= 1 && p <= totalPages; })
        .sort(function(a, b) { return a - b; })
        .forEach(function(p) {
          if (p - previous > 1) {
            var gap = document.createElement('span');
            gap.textContent = '…';
            container.appendChild(gap);
          }
          var btn = document.createElement('button');
          btn.type = 'button';
          btn.textContent = p;
          if (p === page) { btn.className = 'current'; btn.setAttribute('aria-current', 'page'); }
          btn.onclick = function() { navigate(p); };
          container.appendChild(btn);
          previous = p;
        });
    }

    function loadHistory(page) {
      if (page) historyPage = page;
      var version = ++historyVersion;
      var filter = document.getElementById('history-filter').value;
      var list = document.getElementById('history-list');
      var pg = document.getElementById('history-pagination');
      pg.replaceChildren();
      list.innerHTML = '<div class="empty-state"><p class="empty-text">正在加载任务列表…</p></div>';
      fetch('/jobs?page=' + historyPage + '&page_size=15&status=' + encodeURIComponent(filter))
        .then(function(r) {
          if (!r.ok) { throw new Error('HTTP ' + r.status); }
          return r.json();
        })
        .then(function(data) {
          if (version !== historyVersion) return;
          if (!data.tasks.length) {
            if (data.total > 0) {
              /* 翻过了最后一页 / 刚删完本页：不要说成「还没有任务」 */
              list.innerHTML = '<div class="empty-state">' +
                '<p class="empty-title">这一页没有任务</p>' +
                '<p class="empty-text">任务总数为 ' + data.total + ' 个，可能是刚删除完本页或翻过了最后一页。</p>' +
                '<button type="button" class="btn-primary" onclick="loadHistory(1)">回到第 1 页</button>' +
              '</div>';
            } else {
              list.innerHTML = historyEmptyHtml(filter);
            }
          } else {
            list.innerHTML = '';
          }
          data.tasks.forEach(function(t) {
            var active = isActiveCrawl(t.status);
            var id = escapeHtml(t.id);
            var card = document.createElement('div');
            card.className = 'task-card';
            card.innerHTML =
              '<div style="width:8px;height:36px;border-radius:4px;background:' + statusColor(t.status) + ';flex-shrink:0"></div>' +
              '<div class="task-main">' +
                '<div class="task-url" title="' + escapeHtml(t.url || '') + '">' + escapeHtml(t.url || '-') + '</div>' +
                '<div class="task-meta">' +
                  '<span>' + fmtTime(t.created_at) + '</span>' +
                  '<span>' + (t.processed_resources || 0) + '/' + (t.total_resources || 0) + ' 资源</span>' +
                  '<span>' + (t.pages_scanned || 0) + ' 页</span>' +
                '</div>' +
                ((t.status === 'error' && t.error_message) ? '<div class="task-error" title="失败原因">' + escapeHtml(t.error_message) + '</div>' : '') +
              '</div>' +
              '<span class="status-badge ' + historyStatusClass(t.status) + '">' + escapeHtml(STATUS_LABELS[t.status] || t.status || '未知') + '</span>' +
              '<div class="task-actions">' +
                (active ? '<button type="button" data-watch-id="' + id + '" class="btn-results">查看进度</button>' :
                  '<button type="button" data-results-id="' + id + '" data-results-url="' + escapeHtml(t.url || '') + '" class="btn-results">查看详情</button>') +
                ((t.status === 'error' || t.status === 'partial' || t.status === 'cancelled') ? '<button type="button" data-retry-id="' + id + '" class="btn-secondary">重试</button>' : '') +
                (active ? '' : '<button type="button" data-delete-id="' + id + '" class="btn-delete">删除</button>') +
              '</div>';
            list.appendChild(card);
          });
          var totalPages = Math.ceil(data.total / data.page_size);
          renderPagination(pg, historyPage, totalPages, loadHistory);
          document.getElementById('history-total').textContent = '共 ' + data.total + ' 个任务';
        })
        .catch(function(err) {
          if (version !== historyVersion) return;
          list.innerHTML = '<div class="empty-state">' +
            '<p class="empty-title">任务列表加载失败</p>' +
            '<p class="empty-text">' + escapeHtml(err.message) + '。请确认服务仍在运行，然后点下方按钮重试。</p>' +
            '<button type="button" class="btn-primary" onclick="loadHistory()">重试</button>' +
          '</div>';
        });
    }

    function deleteTask(id) {
      if (!confirm('删除这条任务记录和资源清单？已下载的文件会保留。')) return;
      fetch('/jobs/' + encodeURIComponent(id), { method: 'DELETE' })
        .then(function(r) { return r.json(); })
        .then(function(data) {
          if (data.ok) loadHistory(); else alert(data.error || '删除失败');
        })
        .catch(function(err) { alert('删除失败：' + err.message); });
    }

    // 事件委托：点击查看结果/删除按钮
    document.getElementById('history-list').addEventListener('click', function(e) {
      var btn = e.target.closest('button');
      if (!btn) return;
      if (btn.dataset.watchId) {
        goToCrawlerTab();
        watchCrawl(btn.dataset.watchId);
      } else if (btn.dataset.resultsId) {
        showResults(btn.dataset.resultsId, btn.dataset.resultsUrl);
      } else if (btn.classList.contains('btn-delete')) {
        deleteTask(btn.dataset.deleteId);
      } else if (btn.dataset.retryId) {
        retryTask(btn.dataset.retryId, btn);
      }
    });

    /* 用库里的任务记录重跑：后端只读入库白名单（不含 Cookie），前端只负责发起与反馈 */
    function retryTask(taskId, btn) {
      if (btn) { btn.disabled = true; btn.textContent = '重试中…'; }
      post('/retry?id=' + encodeURIComponent(taskId))
        .then(function(d) {
          if (!d.id) { throw new Error(d.error || '重试失败'); }
          if (btn) { btn.disabled = false; btn.textContent = '重试'; }
          goToCrawlerTab();
          watchCrawl(d.id);
        })
        .catch(function(err) {
          if (btn) { btn.disabled = false; btn.textContent = '重试'; }
          window.alert('重试失败：' + err.message);
        });
    }

    var lastResultsTrigger = null;
    function updateRuleActions() {
      exportRules.disabled = useRules.disabled = !(verifiedRules || auditedRules);
      auditRules.disabled = Boolean(auditController) || !(taskHasSamples || sampleAvailable);
      auditStop.hidden = !auditController;
    }
    function clearAudit() {
      if (auditController) auditController.abort();
      auditController = null;
      auditReport = null;
      auditedRules = null;
      auditResults.hidden = true;
      auditFeedback.textContent = '';
      updateRuleActions();
    }
    function resetRulePreview() {
      ruleRequestVersion++;
      verifiedRules = null;
      rulesOutput.hidden = true;
      validateRules.disabled = !sampleAvailable;
      updateRuleActions();
      rulesFeedback.textContent = sampleAvailable ? '' : (selectedPageUrl ?
        '此页没有验证样本。' : '选择一个页面后可验证单页。');
    }
    rulesEditor.oninput = function() { clearAudit(); resetRulePreview(); };
    document.getElementById('import-rules').onclick = function() { rulesFile.click(); };
    rulesFile.onchange = async function() {
      if (!this.files.length) return;
      var file = this.files[0];
      this.value = '';
      var version = ruleRequestVersion;
      try {
        var data = await localJson(file, 131072);
        if (version !== ruleRequestVersion) return;
        rulesEditor.value = JSON.stringify(data, null, 2);
        clearAudit();
        resetRulePreview();
      } catch(e) {
        if (version === ruleRequestVersion) rulesFeedback.textContent = '规则文件读取失败：' + e.message;
      }
    };
    validateRules.onclick = async function() {
      if (!sampleAvailable) return;
      resetRulePreview();
      var version = ruleRequestVersion;
      validateRules.disabled = true;
      rulesFeedback.textContent = '正在验证已保存样本…';
      try {
        var path = '/jobs/' + encodeURIComponent(resultsTaskId) + '/validate-rules?url=' + encodeURIComponent(selectedPageUrl);
        var response = await postJson(path, JSON.parse(rulesEditor.value));
        if (version !== ruleRequestVersion) return;
        verifiedRules = response.rules;
        var missing = response.result.missing_required;
        rulesFeedback.textContent = missing.length ? '缺少必填字段：' + missing.join('、') : '当前样本验证通过';
        rulesOutput.textContent = JSON.stringify(response.result, null, 2);
        rulesOutput.hidden = false;
        updateRuleActions();
      } catch(e) {
        if (version === ruleRequestVersion) rulesFeedback.textContent = '验证失败：' + e.message;
      } finally {
        if (version === ruleRequestVersion) validateRules.disabled = !sampleAvailable;
      }
    };
    exportRules.onclick = function() {
      var rules = verifiedRules || auditedRules;
      if (rules) downloadJson('field-rules.json', rules);
    };
    useRules.onclick = function() {
      var rules = verifiedRules || auditedRules;
      if (!rules) return;
      crawlRules.value = JSON.stringify(rules, null, 2);
      closeResults();
      goToCrawlerTab();
      document.getElementById('crawler-rule-settings').open = true;
      crawlRules.focus();
      configFeedback.textContent = '字段规则已填入采集表单';
    };
    function mergeRuleCoverage(total, batch) {
      if (!total) return batch;
      ['pages_checked', 'pages_extracted', 'pages_missing_required', 'pages_error'].forEach(function(name) {
        total[name] += batch[name];
      });
      batch.fields.forEach(function(field, index) {
        total.fields[index].matched += field.matched;
        total.fields[index].missing += field.missing;
      });
      total.examples = total.examples.concat(batch.examples).slice(0, 30);
      return total;
    }
    function renderRuleCoverage(report) {
      var fields = document.getElementById('rule-audit-fields');
      var examples = document.getElementById('rule-audit-examples');
      fields.replaceChildren();
      examples.replaceChildren();
      report.fields.forEach(function(field) {
        var row = fields.insertRow();
        [field.name, field.required ? '必填' : '可选', field.matched, field.missing].forEach(function(value) {
          row.insertCell().textContent = value;
        });
        if (field.required && field.missing) row.lastElementChild.className = 'results-diagnostic';
      });
      if (report.examples.length) {
        var label = document.createElement('p');
        label.className = 'hint';
        label.textContent = '问题样本（最多显示 30 条）';
        examples.appendChild(label);
      }
      report.examples.forEach(function(example) {
        var button = document.createElement('button');
        var description = document.createElement('small');
        button.type = 'button';
        button.textContent = example.page_url;
        description.className = 'hint';
        description.textContent = example.error || '缺少 ' + example.missing_required.join('、');
        button.append(document.createElement('br'), description);
        button.onclick = function() { loadExtraction(example.page_url, true); };
        examples.appendChild(button);
      });
      auditResults.hidden = false;
    }
    auditStop.onclick = function() { if (auditController) auditController.abort(); };
    auditRules.onclick = async function() {
      clearAudit();
      var controller = new AbortController();
      auditController = controller;
      updateRuleActions();
      var after = '';
      var total = null;
      var expected = null;
      auditFeedback.textContent = '正在验证任务样本…';
      try {
        var rules = JSON.parse(rulesEditor.value);
        var url = '/jobs/' + encodeURIComponent(resultsTaskId) + '/audit-rules';
        do {
          var response = await postJson(url + '?after=' + encodeURIComponent(after), rules, controller.signal);
          if (auditController !== controller) return;
          if (response.total_samples !== null) expected = response.total_samples;
          total = mergeRuleCoverage(total, response.coverage);
          after = response.next_after;
          auditFeedback.textContent = '已检查 ' + total.pages_checked + ' / ' + expected + ' 页…';
        } while (after);
        if (total.pages_checked !== expected) throw new Error('采集样本已发生变化，请重新打开任务后重试');
        if (!total.pages_checked) {
          auditFeedback.textContent = '此任务没有保留验证样本。';
          return;
        }
        auditReport = total;
        auditedRules = response.rules;
        auditFeedback.textContent = '检查 ' + total.pages_checked + ' 页 · 缺少必填字段 ' +
          total.pages_missing_required + ' 页 · 未完成提取 ' + total.pages_error + ' 页';
        renderRuleCoverage(total);
      } catch(e) {
        if (auditController === controller) {
          var count = total ? total.pages_checked : 0;
          auditFeedback.textContent = e.name === 'AbortError' ? '已停止，已检查 ' + count + ' 页' :
            '验证中断（已检查 ' + count + ' 页）：' + e.message;
        }
      } finally {
        if (auditController === controller) {
          auditController = null;
          updateRuleActions();
        }
      }
    };
    document.getElementById('export-rule-audit').onclick = function() {
      if (auditReport) downloadJson('rule-coverage.json', auditReport);
    };
    function showResultsPane(pane) {
      var resources = pane === 'resources';
      document.getElementById('resource-pane').hidden = !resources;
      document.getElementById('page-pane').hidden = resources;
      document.getElementById('show-resources').setAttribute('aria-pressed', String(resources));
      document.getElementById('show-pages').setAttribute('aria-pressed', String(!resources));
    }

    function setResultsSource(url) {
      sourceUrl = url;
      document.getElementById('results-source').hidden = !url;
      document.getElementById('results-source-url').textContent = url ? '来源页面 · ' + url : '';
    }

    function taskPagesUrl() {
      return '/jobs/' + encodeURIComponent(resultsTaskId) + '/pages';
    }

    async function loadPageOptions(page) {
      var version = ++pagesVersion;
      var options = document.getElementById('page-options');
      var feedback = document.getElementById('pages-state');
      var pager = document.getElementById('page-pagination');
      options.replaceChildren();
      pager.replaceChildren();
      feedback.textContent = '正在读取已提取页面…';
      try {
        var response = await fetch(taskPagesUrl() + '?page=' + page);
        if (!response.ok) throw new Error('HTTP ' + response.status);
        var data = await response.json();
        if (version !== pagesVersion) return;
        feedback.textContent = data.total ? '可查看 ' + data.total + ' 个页面' : '本次任务没有提取结果或验证样本。可在采集表单开启页面提取或样本保留。';
        data.pages.forEach(function(item) {
          var button = document.createElement('button');
          button.type = 'button';
          button.dataset.url = item.page_url;
          button.title = item.page_url;
          button.textContent = item.title || item.page_url;
          button.setAttribute('aria-pressed', String(selectedPageUrl === item.page_url));
          button.onclick = function() { loadExtraction(item.page_url); };
          options.appendChild(button);
        });
        renderPagination(pager, page, Math.ceil(data.total / data.page_size), loadPageOptions);
      } catch(e) {
        if (version === pagesVersion) feedback.textContent = '页面列表读取失败：' + e.message;
      }
    }

    async function loadExtraction(url, focusTitle) {
      var version = ++extractionVersion;
      sampleAvailable = false;
      resetRulePreview();
      var panel = document.getElementById('page-extraction');
      var feedback = document.getElementById('pages-state');
      var options = document.querySelectorAll('#page-options button');
      options.forEach(function(button) {
        button.setAttribute('aria-pressed', String(button.dataset.url === url));
      });
      panel.hidden = true;
      feedback.textContent = '正在读取页面内容…';
      try {
        var response = await fetch(taskPagesUrl() + '?url=' + encodeURIComponent(url));
        if (!response.ok) throw new Error('HTTP ' + response.status);
        var item = await response.json();
        if (version !== extractionVersion) return;
        selectedPageUrl = item.page_url;
        document.getElementById('extraction-title').textContent = item.title || '页面提取';
        document.getElementById('extraction-url').textContent = item.page_url;
        document.getElementById('extraction-text').textContent = item.body || '';
        document.getElementById('extraction-text-details').hidden = !item.body;
        document.getElementById('extraction-data').textContent = item.data ? JSON.stringify(item.data, null, 2) : '';
        document.getElementById('extraction-data-details').hidden = !item.data;
        if (!item.body && item.data) document.getElementById('extraction-data-details').open = true;
        document.getElementById('extraction-rule-result').hidden = !item.rule_result;
        document.getElementById('extraction-rule-output').textContent = item.rule_result ? JSON.stringify(item.rule_result, null, 2) : '';
        sampleAvailable = item.has_sample;
        resetRulePreview();
        feedback.textContent = '';
        panel.hidden = false;
        if (focusTitle) document.getElementById('extraction-title').focus();
      } catch(e) {
        if (version === extractionVersion) feedback.textContent = '页面内容读取失败：' + e.message;
      }
    }

    document.getElementById('show-resources').onclick = function() {
      resultsPaneTouched = true;
      showResultsPane('resources');
    };
    document.getElementById('show-pages').onclick = function() {
      resultsPaneTouched = true;
      showResultsPane('pages');
      loadPageOptions(1);
    };
    document.getElementById('extraction-resources').onclick = function() {
      resultsPaneTouched = true;
      setResultsSource(selectedPageUrl);
      document.getElementById('results-search').value = '';
      showResultsPane('resources');
      document.getElementById('show-resources').focus();
      loadResults(1);
    };
    document.getElementById('clear-results-source').onclick = function() {
      setResultsSource('');
      document.getElementById('results-search').focus();
      loadResults(1);
    };

    function taskIdFromLocation() {
      var id = new URLSearchParams(location.hash.slice(1)).get('task');
      return id && /^[a-zA-Z0-9_-]{1,128}$/.test(id) ? id : null;
    }

    function syncResultsLocation() {
      var taskId = taskIdFromLocation();
      if (!taskId) {
        closeResults(true);
      } else if (taskId !== resultsTaskId) {
        var tab = document.getElementById('tabbtn-history');
        if (!tab.classList.contains('active')) activateTab(tab);
        tab.focus();
        showResults(taskId);
      }
    }

    function showResults(taskId, url) {
      if (taskId !== taskIdFromLocation()) {
        history.pushState(null, '', '#task=' + encodeURIComponent(taskId));
      }
      resultsTaskId = taskId;
      pagesVersion++;
      extractionVersion++;
      resultsPage = 1;
      resultsPaneTouched = false;
      lastResultsTrigger = document.activeElement;
      document.getElementById('results-search').value = '';
      selectedPageUrl = '';
      sampleAvailable = false;
      taskHasSamples = false;
      clearAudit();
      rulesEditor.value = crawlRules.value || JSON.stringify({version: 1, fields: {title: {css: 'title', required: true}}}, null, 2);
      document.getElementById('rules-workbench').open = false;
      resetRulePreview();
      setResultsSource('');
      showResultsPane('resources');
      document.getElementById('page-extraction').hidden = true;
      document.getElementById('page-options').replaceChildren();
      document.getElementById('show-pages').textContent = '页面提取';
      document.getElementById('results-title').textContent = '采集详情 · ' + (url || taskId);
      var summary = document.getElementById('results-summary');
      var diagnostic = document.getElementById('results-diagnostic');
      var offline = document.getElementById('results-offline');
      var offlineCheck = document.getElementById('results-offline-check');
      var reuseConfig = document.getElementById('reuse-task-config');
      var logDetails = document.getElementById('results-log-details');
      summary.textContent = '正在读取任务记录…';
      diagnostic.hidden = true;
      offline.hidden = true;
      offlineCheck.hidden = true;
      reuseConfig.hidden = true;
      logDetails.hidden = true;
      logDetails.open = false;
      var version = ++detailsVersion;
      fetch('/jobs/' + encodeURIComponent(taskId))
        .then(function(r) {
          if (!r.ok) throw new Error(r.status === 404 ? '任务记录已不存在' : 'HTTP ' + r.status);
          return r.json();
        })
        .then(function(task) {
          if (version !== detailsVersion) return;
          document.getElementById('results-title').textContent = '采集详情 · ' + (task.url || url || taskId);
          summary.textContent = formatStatus(task) +
            (task.output_dir ? ' · 输出目录：' + task.output_dir : '');
          var run = task.summary || {};
          if (run.offline_pages && run.offline_pages.count && run.offline_pages.index) {
            offline.textContent = '离线页面（' + run.offline_pages.count + ' 页） · 在输出目录打开 ' + run.offline_pages.index;
            offline.hidden = false;
          }
          if (run.offline_check) {
            var check = run.offline_check;
            var readiness = {passed: '静态引用通过', failed: '静态引用有缺失或越界', limited: '静态引用部分可验证'};
            offlineCheck.textContent = '生成时离线验收 · ' + (readiness[check.status] || '静态引用未验证') + ' · 检查 ' + check.documents_checked + ' 份文档' +
              ' · 缺失 ' + check.missing + ' 项 · 越界 ' + check.outside + ' 项 · 外部资源 ' + check.remote_resources + ' 项' +
              ' · 未检查或无法解析 ' + check.unverified + ' 项';
            offlineCheck.hidden = false;
          }
          taskHasSamples = Boolean(task.page_sample_count);
          updateRuleActions();
          if (!taskHasSamples && !auditController && !auditReport) {
            auditFeedback.textContent = '此任务未保留整批样本。采集时可开启「保留页面样本用于验证」。';
          }
          if (!resultsPaneTouched && !task.processed_resources && (run.extracted_pages || taskHasSamples)) {
            showResultsPane('pages');
            loadPageOptions(1);
          }
          var scan = run.page_scan || {};
          var message = scan.first_error || task.error_message || '';
          if (run.extraction_save_failures) {
            message += (message ? '\n' : '') + run.extraction_save_failures + ' 个页面的提取结果未写入任务历史。';
          }
          if (run.page_samples_skipped) {
            message += (message ? '\n' : '') + run.page_samples_skipped + ' 个页面未保存验证样本。';
          }
          diagnostic.textContent = message;
          diagnostic.hidden = !message;
          if (run.extracted_pages) document.getElementById('show-pages').textContent = '页面提取 (' + run.extracted_pages + ')';
          reuseConfig.hidden = !task.config || !task.config.url;
          reuseConfig.onclick = async function() {
            try {
              var response = await postJson('/crawl/config/import', task.config);
              if (version !== detailsVersion) return;
              applyCrawlConfig(response.config);
              closeResults();
              goToCrawlerTab();
              configFeedback.textContent = '历史任务配置已载入';
            } catch(e) {
              if (version === detailsVersion) summary.textContent = '配置载入失败：' + e.message;
            }
          };
          document.getElementById('results-log').textContent = task.log || '';
          logDetails.hidden = !task.log;
        })
        .catch(function(err) {
          if (version === detailsVersion) summary.textContent = '任务记录读取失败：' + err.message;
        });
      var modal = document.getElementById('results-modal');
      modal.classList.add('show');
      modal.setAttribute('aria-hidden', 'false');
      document.body.style.overflow = 'hidden';
      var closeBtn = modal.querySelector('.btn-close');
      if (closeBtn) { closeBtn.focus(); }
      loadResults(1);
    }

    /* 后端结果状态是英文枚举/自由文本，这里翻译成中文并保留原文作悬停说明 */
    function resultStatusInfo(s) {
      var raw = String(s || '');
      if (!raw) { return { text: '未知', cls: 'status-idle', title: '' }; }
      if (raw.indexOf('ok') === 0) { return { text: '成功', cls: 'status-ok', title: raw }; }
      if (raw.indexOf('listed only') === 0) { return { text: '仅清单（未下载）', cls: 'status-idle', title: raw }; }
      if (raw.indexOf('skipped by robots') === 0) { return { text: 'robots 协议禁止', cls: 'status-idle', title: raw }; }
      if (raw.indexOf('error') === 0) { return { text: '失败', cls: 'status-bad', title: raw }; }
      return { text: raw, cls: '', title: raw };
    }

    function loadResults(page) {
      if (page) resultsPage = page;
      var version = ++resultsVersion;
      var search = document.getElementById('results-search').value;
      var body = document.getElementById('results-body');
      var pg = document.getElementById('results-pagination');
      body.innerHTML = '<tr><td colspan="7">正在读取资源清单…</td></tr>';
      pg.innerHTML = '';
      var url = '/jobs/' + encodeURIComponent(resultsTaskId) + '/results?page=' + resultsPage + '&page_size=50';
      if (search) url += '&q=' + encodeURIComponent(search);
      if (sourceUrl) url += '&source=' + encodeURIComponent(sourceUrl);
      fetch(url)
        .then(function(r) {
          if (!r.ok) { throw new Error('HTTP ' + r.status); }
          return r.json();
        })
        .then(function(data) {
          if (version !== resultsVersion) return;
          body.innerHTML = '';
          if (!data.results.length) {
            var hint = search
              ? '没有匹配「' + escapeHtml(search) + '」的结果，换个关键词试试。'
              : (sourceUrl ? '这个页面没有记录到资源。' : '本次任务没有记录到资源。');
            body.innerHTML = '<tr><td colspan="7" style="padding:28px;text-align:center;color:var(--text-muted)">' + hint + '</td></tr>';
          }
          data.results.forEach(function(r) {
            var tr = document.createElement('tr');
            /* 结果字段来自被抓站点（content_type/status/url 等），必须转义防存储型 XSS */
            var st = resultStatusInfo(r.status);
            tr.innerHTML =
              '<td class="cell-url" title="' + escapeHtml(r.url || '') + '">' + escapeHtml(r.url || '-') + '</td>' +
              '<td class="cell-path" title="' + escapeHtml(r.saved_path || '') + '">' + escapeHtml(r.saved_path || '—') + '</td>' +
              '<td class="cell-page" title="' + escapeHtml(sourceUrl || r.page_url || '') + '">' + escapeHtml(sourceUrl || r.page_url || '—') + '</td>' +
              '<td>' + escapeHtml(r.content_type || '-') + '</td>' +
              /* 清单模式只登记不下载：bytes=0 且没有 saved_path，显示 0 B 会被误读成"空文件" */
              '<td class="cell-size">' +
                (r.saved_path || r.bytes ? fmtSize(r.bytes) : '<span class="muted">—</span>') + '</td>' +
              '<td>' + escapeHtml(r.category || '-') + '</td>' +
              '<td class="cell-status ' + st.cls + '"' + (st.title ? ' title="' + escapeHtml(st.title) + '"' : '') + '>' + escapeHtml(st.text) +
                (r.diagnostic ? '<div class="hint">' + escapeHtml(r.diagnostic) + '</div>' : '') + '</td>';
            body.appendChild(tr);
          });
          var totalPages = Math.ceil(data.total / data.page_size);
          renderPagination(pg, resultsPage, totalPages, loadResults);
        })
        .catch(function(err) {
          if (version !== resultsVersion) return;
          body.innerHTML = '<tr><td colspan="7" style="padding:24px;text-align:center;color:var(--danger-text)">加载失败：' + escapeHtml(err.message) + '。关闭弹窗后重试，或确认服务仍在运行。</td></tr>';
        });
    }

    function closeResults(fromLocation) {
      var modal = document.getElementById('results-modal');
      if (!modal.classList.contains('show')) return;
      if (!fromLocation && taskIdFromLocation()) {
        history.replaceState(null, '', location.pathname + location.search);
      }
      resultsTaskId = null;
      detailsVersion++;
      resultsVersion++;
      pagesVersion++;
      extractionVersion++;
      ruleRequestVersion++;
      clearAudit();
      modal.classList.remove('show');
      modal.setAttribute('aria-hidden', 'true');
      document.body.style.overflow = '';
      if (lastResultsTrigger && lastResultsTrigger.focus) { lastResultsTrigger.focus(); }
    }
    /* 点遮罩空白处关闭（移动端按钮不在视野内时的兜底） */
    document.getElementById('results-modal').addEventListener('click', function(e) {
      if (e.target === this) { closeResults(); }
    });
    /* 焦点陷阱：弹窗打开时 Tab 只在弹窗内循环 —— 否则键盘用户会被带到背后页面上，
       屏幕阅读器也会读错上下文。只在弹窗可见时生效，不引入额外的绑定/解绑簿记。 */
    function trapResultsFocus(modal, e) {
      if (e.key !== 'Tab') { return; }
      var nodes = modal.querySelectorAll(
        'a[href], button:not([disabled]), input:not([disabled]), select:not([disabled]), textarea:not([disabled]), summary, [tabindex]:not([tabindex="-1"])'
      );
      var list = Array.prototype.filter.call(nodes, function(el) {
        return el.offsetWidth > 0 || el.offsetHeight > 0 || el === document.activeElement;
      });
      if (!list.length) { return; }
      var first = list[0];
      var last = list[list.length - 1];
      var inside = modal.contains(document.activeElement);
      if (!inside) { e.preventDefault(); first.focus(); return; }
      if (e.shiftKey && document.activeElement === first) { e.preventDefault(); last.focus(); }
      else if (!e.shiftKey && document.activeElement === last) { e.preventDefault(); first.focus(); }
    }
    /* Esc 关闭 + Tab 焦点循环 */
    document.addEventListener('keydown', function(e) {
      var modal = document.getElementById('results-modal');
      if (!modal.classList.contains('show')) { return; }
      if (e.key === 'Escape') { closeResults(); return; }
      trapResultsFocus(modal, e);
    });
    window.addEventListener('popstate', syncResultsLocation);
    window.addEventListener('hashchange', syncResultsLocation);
    syncResultsLocation();

    fetch('/status?active=1')
      .then(function(response) { return response.ok ? response.json() : null; })
      .then(function(active) {
        if (active && active.id && !jobId && !runButton.disabled) {
          jobId = active.id;
          renderCrawl(active);
          if (isActiveCrawl(active.status)) timer = setTimeout(poll, 1000);
        }
      })
      .catch(function() { /* 手动启动时，服务端仍会返回活动任务。 */ });