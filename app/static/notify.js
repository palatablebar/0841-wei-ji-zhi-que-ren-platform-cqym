(function () {
  function $(id) { return document.getElementById(id); }
  function esc(value) {
    return String(value === null || value === undefined ? '' : value).replace(/[&<>"']/g, function (ch) {
      return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[ch];
    });
  }
  async function api(url, options) {
    var resp = await fetch(url, Object.assign({ headers: { 'Content-Type': 'application/json' } }, options || {}));
    var data = await resp.json().catch(function () { return {}; });
    if (!resp.ok) throw new Error(data.error || data.code || ('HTTP ' + resp.status));
    return data;
  }
  function notice(text, bad) {
    var el = $('notify-notice');
    if (!el) return;
    el.textContent = text || '';
    el.style.color = bad ? '#b00020' : '';
  }
  function runId() {
    return window.workbench && window.workbench.state && window.workbench.state.run
      ? window.workbench.state.run.run_id : null;
  }
  function fmt(t) { return t ? esc(String(t).replace('T', ' ').replace('+00:00', ' UTC')) : '—'; }

  function statusCell(n) {
    if (n.closed) return '<span class="nv-badge closed">已回执·闭环</span>';
    if (n.overdue) return '<span class="nv-badge overdue">待回执·已逾期</span>';
    return '<span class="nv-badge pending">待回执</span>';
  }

  function trail(n) {
    var html = '';
    if (n.escalations && n.escalations.length) {
      html += '<ul class="nv-trail esc">' + n.escalations.map(function (e) {
        return '<li>升级 · ' + fmt(e.at) + ' · ' + esc(e.reason || e.kind || 'OVERDUE') +
          (e.to_owner ? ' → ' + esc(e.to_owner) : '') + '</li>';
      }).join('') + '</ul>';
    }
    if (n.handovers && n.handovers.length) {
      html += '<ul class="nv-trail hov">' + n.handovers.map(function (h) {
        return '<li>交班 · ' + fmt(h.at) + ' · ' + esc(h.from_owner || '原责任人') +
          ' → ' + esc(h.to_owner) + (h.escalated ? '（逾期补升级）' : '（仅换人）') + '</li>';
      }).join('') + '</ul>';
    }
    if (!html) html = '<span class="hint">暂无升级/交班</span>';
    return html;
  }

  function receiptCell(n) {
    if (n.closed && n.receipt) {
      return '<div class="nv-readback">“' + esc(n.readback) + '”</div>' +
        '<small>回执医师：' + esc(n.receipt.by || '—') + ' · ' + fmt(n.receipt.at) + '</small>';
    }
    return '<input type="text" class="rb-input" data-id="' + esc(n.id) +
      '" placeholder="填写临床读回内容（必填）">' +
      '<button type="button" data-action="receipt" data-id="' + esc(n.id) + '">提交回执并闭环</button>';
  }

  function row(n) {
    var crit = '<strong>' + esc(n.specimen_id || n.critical_key) + '</strong>' +
      '<br><small>' + esc(n.analyte || '') + ' ' + esc(n.raw_value != null ? n.raw_value : '') +
      ' ' + esc(n.unit || '') + '</small>';
    var actions = n.closed ? '<span class="hint">已闭环</span>' :
      '<input type="text" class="hov-input" placeholder="交班医师">' +
      '<button type="button" class="ghost" data-action="handover-one" data-id="' + esc(n.id) + '">交班</button>';
    return '<tr data-id="' + esc(n.id) + '">' +
      '<td>' + crit + '</td>' +
      '<td>' + esc(n.receiver || '—') + '</td>' +
      '<td>' + esc(n.owner || '—') + '</td>' +
      '<td class="num">' + fmt(n.first_notified_at) + '</td>' +
      '<td class="num">' + fmt(n.due_at || n.deadline) + '</td>' +
      '<td>' + statusCell(n) + '</td>' +
      '<td>' + receiptCell(n) + '</td>' +
      '<td>' + trail(n) + '</td>' +
      '<td class="nv-actions">' + actions + '</td>' +
      '</tr>';
  }

  async function load() {
    try {
      var data = await api('/api/notifications');
      var items = data.notifications || data.items || data.tasks || [];
      $('notify-count').textContent = items.length
        ? (data.pending_count + ' 待回执 / ' + data.closed_count + ' 已闭环 / 共 ' + items.length + ' 条')
        : '暂无通知';
      $('notify-body').innerHTML = items.length
        ? items.map(row).join('')
        : '<tr><td class="empty" colspan="9">执行批次后点击“为每条危急值建通知”</td></tr>';
    } catch (e) { notice(e.message, true); }
  }

  async function createNotifications() {
    var doctor = $('notify-doctor').value.trim();
    var minutes = $('notify-sla').value;
    var payload = { doctor: doctor, receiver: doctor };
    if (minutes !== '') payload.deadline_minutes = Number(minutes);
    try {
      notice('正在建立通知…');
      var id = runId();
      if (id) {
        await api('/api/runs/' + encodeURIComponent(id) + '/notifications', { method: 'POST', body: JSON.stringify(payload) });
      } else {
        var profile = $('profile') ? $('profile').value : 'dirty';
        payload.profile = profile;
        await api('/api/notifications/dispatch', { method: 'POST', body: JSON.stringify(payload) });
      }
      notice('已按危急值逐条登记（重复通知保持幂等）');
      await load();
    } catch (e) { notice(e.message, true); }
  }

  async function submitReceipt(id, input) {
    var readback = input.value.trim();
    if (!readback) { notice('必须填写临床读回内容才能闭环', true); input.focus(); return; }
    try {
      await api('/api/notifications/' + encodeURIComponent(id) + '/receipt', {
        method: 'POST', body: JSON.stringify({ readback: readback, content: readback })
      });
      notice('回执已提交，闭环完成');
      await load();
    } catch (e) { notice(e.message, true); }
  }

  async function handoverOne(id, tr) {
    var input = tr.querySelector('.hov-input');
    var doctor = input.value.trim();
    if (!doctor) { notice('请填写交班医师', true); return; }
    var payload = { doctor: doctor, on_duty_doctor: doctor };
    var at = $('handover-at').value;
    if (at) payload.at = at;
    try {
      await api('/api/notifications/' + encodeURIComponent(id) + '/handover', { method: 'POST', body: JSON.stringify(payload) });
      notice('已交班：未回执任务更换责任人，逾期者补一次升级');
      await load();
    } catch (e) { notice(e.message, true); }
  }

  async function handoverAll() {
    var doctor = $('handover-doctor').value.trim();
    if (!doctor) { notice('请填写当班医师', true); return; }
    var payload = { doctor: doctor, on_duty_doctor: doctor };
    var at = $('handover-at').value;
    if (at) payload.at = at;
    try {
      var res = await api('/api/handover', { method: 'POST', body: JSON.stringify(payload) });
      notice('交班完成：换人 ' + (res.handed_over || 0) + ' 条，逾期补升级 ' + (res.escalated || 0) + ' 条');
      await load();
    } catch (e) { notice(e.message, true); }
  }

  function boot() {
    $('notify-create').addEventListener('click', createNotifications);
    $('notify-refresh').addEventListener('click', load);
    $('handover-all').addEventListener('click', handoverAll);
    $('notify-body').addEventListener('click', function (ev) {
      var btn = ev.target.closest('button[data-action]');
      if (!btn) return;
      var id = btn.getAttribute('data-id');
      if (btn.getAttribute('data-action') === 'receipt') {
        var input = document.querySelector('.rb-input[data-id="' + (window.CSS && CSS.escape ? CSS.escape(id) : id) + '"]');
        if (input) submitReceipt(id, input);
      } else if (btn.getAttribute('data-action') === 'handover-one') {
        handoverOne(id, btn.closest('tr'));
      }
    });
    window.addEventListener('wb:run', load);
    load();
  }

  document.addEventListener('DOMContentLoaded', boot);
})();
