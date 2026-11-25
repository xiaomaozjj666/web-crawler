  // ===== 验证码识别 tab =====
  (function() {
    var capJobTimer = null;

    function capPoll(id) {
      if (capJobTimer) clearInterval(capJobTimer);
      capJobTimer = setInterval(function() {
        var xhr = new XMLHttpRequest();
        xhr.open('GET', '/captcha/status?id=' + encodeURIComponent(id));
        xhr.onload = function() {
          var st;
          try { st = JSON.parse(xhr.responseText); } catch (e) { return; }
          document.getElementById('cap-page-status').textContent =
            '状态: ' + st.status + (st.error ? (' — ' + st.error) : '');
          if (st.status === 'done' || st.status === 'error') {
            clearInterval(capJobTimer);
            capJobTimer = null;
            var out = document.getElementById('cap-page-result');
            out.style.display = 'block';
            out.textContent = JSON.stringify(st.result || { error: st.error || st.status }, null, 2);
            capRefreshJobs();
          }
        };
        xhr.send();
      }, 2500);
    }

    function capRefreshJobs() {
      var xhr = new XMLHttpRequest();
      xhr.open('GET', '/captcha/jobs');
      xhr.onload = function() {
        var data;
        try { data = JSON.parse(xhr.responseText); } catch (e) { return; }
        var tbody = document.querySelector('#cap-jobs tbody');
        if (!tbody) return;
        tbody.innerHTML = '';
        (data.jobs || []).forEach(function(j) {
          var tr = document.createElement('tr');
          var t = new Date((j.created_at || 0) * 1000).toLocaleTimeString();
          tr.innerHTML = '<td>' + t + '</td><td>' + (j.url || '') +
            '</td><td>' + (j.status || '') + '</td>';
          tbody.appendChild(tr);
        });
      };
      xhr.send();
    }

    document.getElementById('cap-page-btn').addEventListener('click', function() {
      var url = document.getElementById('cap-page-url').value.trim();
      if (!url) { capSetStatus('请输入目标页面 URL'); return; }
      document.getElementById('cap-page-result').style.display = 'none';
      capSetStatus('已提交，Camoufox 启动并求解中（约 1-3 分钟，请勿关闭页面）…');
      var body = 'url=' + encodeURIComponent(url) +
        '&headless=' + (document.getElementById('cap-page-headless').checked ? '1' : '0');
      var xhr = new XMLHttpRequest();
      xhr.open('POST', '/captcha/run');
      xhr.setRequestHeader('Content-Type', 'application/x-www-form-urlencoded');
      xhr.onload = function() {
        var data;
        try { data = JSON.parse(xhr.responseText); } catch (e) { capSetStatus('提交失败'); return; }
        if (data.id) { capPoll(data.id); } else { capSetStatus('提交失败: ' + (data.error || '')); }
      };
      xhr.onerror = function() { capSetStatus('网络错误'); };
      xhr.send(body);
    });

    document.getElementById('cap-img-btn').addEventListener('click', function() {
      var fileInput = document.getElementById('cap-img-file');
      var out = document.getElementById('cap-img-result');
      if (!fileInput.files.length) { out.textContent = '请先选择图片文件'; out.style.color = 'crimson'; return; }
      var reader = new FileReader();
      reader.onload = function() {
        out.textContent = '识别中…'; out.style.color = 'inherit';
        var body = 'image=' + encodeURIComponent(reader.result) +
          '&math=' + (document.getElementById('cap-img-math').checked ? '1' : '0');
        var xhr = new XMLHttpRequest();
        xhr.open('POST', '/captcha/image');
        xhr.setRequestHeader('Content-Type', 'application/x-www-form-urlencoded');
        xhr.onload = function() {
          var data;
          try { data = JSON.parse(xhr.responseText); } catch (e) { out.textContent = '解析失败'; return; }
          if (data.ok) {
            out.textContent = '识别结果: ' + data.text + (data.math ? '（算式答案）' : '');
            out.style.color = 'green';
          } else {
            out.textContent = '识别失败: ' + (data.error || '');
            out.style.color = 'crimson';
          }
        };
        xhr.send(body);
      };
      reader.readAsDataURL(fileInput.files[0]);
    });

    capRefreshJobs();
    setInterval(capRefreshJobs, 15000);
  })();