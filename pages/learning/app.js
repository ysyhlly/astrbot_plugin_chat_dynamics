import { apiGet, apiPost, readyBridge, escapeHtml } from './api.js';
import { renderNav } from './shell.js';

const el = id => document.getElementById(id);
let busy = false;
let stopped = false;
const labels = { off: '关闭', collect: '教师采集', shadow: '旁路比较', active: '学生接管' };
const percent = value => typeof value === 'number' && Number.isFinite(value) ? `${(value * 100).toFixed(1)}%` : '—';
function status(message, error = false) { el('status').textContent = message; el('status').dataset.error = String(error); }
function render(data) {
  const stats = data.stats || {};
  const total = (stats.laya || 0) + (stats.teacher || 0) + (stats.jev || 0);
  data.takeover_rate ??= total ? (stats.laya || 0) / total : null;
  data.teacher_fallback_rate ??= null;
  const recent = data.recent || [];
  const latencies = recent.map(item => item.latency_ms).filter(Number.isFinite).sort((a, b) => a - b);
  data.p95_ms ??= latencies.length ? latencies[Math.min(latencies.length - 1, Math.ceil(latencies.length * .95) - 1)] : null;
  data.model_version ||= recent.find(item => item.model_version)?.model_version;
  el('mode').textContent = labels[data.mode] || data.mode || '关闭';
  el('model').textContent = data.model_id || data.model_version || '尚未晋升';
  el('takeover').textContent = percent(data.takeover_rate);
  el('fallback').textContent = percent(data.teacher_fallback_rate);
  el('latency').textContent = typeof data.p95_ms === 'number' ? `${data.p95_ms.toFixed(0)} ms` : '—';
  const tasks = Array.isArray(data.tasks) ? data.tasks.map(task => typeof task === 'string' ? { task_id: task, ...(data.dataset?.tasks?.[task] || {}) } : task) : Object.entries(data.tasks || {}).map(([task_id, item]) => ({ task_id, ...(typeof item === 'object' ? item : { samples: item }) }));
  const taskRows = tasks.map(task => ({ ...(data.dataset?.tasks?.[task.task_id] || {}), ...task }));
  const emptyTasks = taskRows.filter(task => (task.samples ?? task.count ?? 0) === 0);
  el('collectionGaps').textContent = `共 ${taskRows.length} 个任务，${emptyTasks.length} 个尚无采集样本。下列缺口仅用于安排采集，不代表已满足测试集门槛。`;
  const taskStatus = { off: '关闭', pending: '待验收', approved: '已验收', collecting: '采集中' };
  el('tasks').innerHTML = taskRows.map(task => {
    const classes = Object.entries(task.class_counts || {});
    const covered = classes.filter(([, count]) => count > 0).length;
    const gap = task.collection_gap || {};
    const missing = Object.entries(gap.classes || {}).filter(([, count]) => count > 0);
    const distribution = classes.map(([label, count]) => `${label}: ${count}`).join(' · ') || '尚无类别统计';
    const gapText = [gap.samples > 0 ? `教师采集缺 ${gap.samples} 条` : '', ...missing.map(([label, count]) => `${label} 类缺 ${count} 条`)].filter(Boolean).join('；');
    return `<tr data-task-id="${escapeHtml(task.task_id)}"><td>${escapeHtml(task.label || task.task_id)}</td><td>${escapeHtml(task.samples ?? task.count ?? 0)}</td><td>${escapeHtml(task.teacher_labels ?? '—')}</td><td>${escapeHtml(task.valid_pairs ?? '—')}</td><td>${classes.length ? `${covered} / ${classes.length}` : '—'}</td><td class="task-classes">${escapeHtml(distribution)}${task.dynamic ? '<span class="gap-note">动态候选，仅展示观测分布</span>' : ''}${gapText ? `<span class="gap-note">${escapeHtml(gapText)}</span>` : ''}</td><td>${escapeHtml(taskStatus[task.status] || task.status || '待验收')}</td><td>${escapeHtml(task.quality ?? '查看模型评估报告')}</td></tr>`;
  }).join('') || '<tr><td colspan="8">暂无任务目录，请刷新后重试。</td></tr>';
  const qualityLabels = { disagreement: '师生分歧', low_confidence: '置信度偏低', invalid_label: '标签格式待核验', needs_review: '需人工复核', join_action_conflict: '参与意愿与动作矛盾', affirmative_without_target: '肯定回应无目标', critical_teacher_student_disagreement: '关键决策师生分歧', insufficient_evidence: '证据不足，需人工复核' };
  const qualityFlags = data.dataset?.quality_flags;
  const flagEntries = qualityFlags && typeof qualityFlags === 'object' && !Array.isArray(qualityFlags) ? Object.entries(qualityFlags) : [];
  el('qualityFlags').innerHTML = flagEntries.map(([key, count]) => `<div><dt>${escapeHtml(qualityLabels[key] || key)}</dt><dd>${escapeHtml(count)}</dd></div>`).join('') || `<div><dt>质量标记</dt><dd>${qualityFlags ? '暂无标记' : '尚无统计'}</dd></div>`;
  const priority = data.dataset?.queue_priority;
  const priorityEntries = priority && typeof priority === 'object' && !Array.isArray(priority) ? Object.entries(priority) : [];
  el('queuePrioritySection').hidden = !priorityEntries.length;
  el('queuePriority').innerHTML = priorityEntries.map(([key, count]) => `<div><dt>${escapeHtml(key)}</dt><dd>${escapeHtml(count)}</dd></div>`).join('');
  const versions = data.dataset?.teacher_prompt_versions;
  const versionEntries = versions && typeof versions === 'object' && !Array.isArray(versions) ? Object.entries(versions) : [];
  el('teacherPromptVersions').innerHTML = versionEntries.map(([key, count]) => `<div><dt>${escapeHtml(key)}</dt><dd>${escapeHtml(count)}</dd></div>`).join('') || '<div><dt>教师提示词版本</dt><dd>尚无统计</dd></div>';
  const studentLabels = {
    student_attempts: '学生请求', student_requests: '学生请求', student_answered: '学生已回答',
    student_success: '成功返回', student_timeout: '学生超时', student_invalid: '无效回答',
    student_unavailable: '服务不可用', student_not_prepared: '输入未准备',
    student_not_requested: '未请求学生', student_unavailable_or_invalid: '不可用或无效',
    student_partial_answers: '部分回答', student_invalid_answers: '回答无效', student_version_mismatch: '版本不匹配',
    student_teacher_finished_first: '教师先完成', student_not_configured: '未配置服务',
    student_response_too_large: '响应过大', student_invalid_json: '响应 JSON 无效',
    student_stale_or_closed: '结果过期或已关闭', student_transport_or_json_error: '传输或解析失败',
  };
  const counters = Object.entries(stats).filter(([key]) => key.startsWith('student_'));
  el('studentCounters').innerHTML = counters.map(([key, count]) => `<div><dt>${escapeHtml(studentLabels[key] || key)}</dt><dd>${escapeHtml(count)}</dd></div>`).join('') || '<div><dt>学生诊断</dt><dd>暂无记录</dd></div>';
  const studentStatuses = Object.fromEntries(Object.entries(studentLabels).map(([key, label]) => [key.slice('student_'.length), label]));
  el('studentRecent').innerHTML = [...recent].reverse().slice(0, 10).map(item => `<li><strong>${escapeHtml(studentStatuses[item.student_status] || item.student_status || '状态未记录')}</strong> · ${escapeHtml((item.tasks || []).join('、'))}${Number.isFinite(item.latency_ms) ? ` · ${item.latency_ms.toFixed(0)} ms` : ''}</li>`).join('') || '<li>暂无请求记录</li>';
  el('jobs').innerHTML = (data.jobs || []).map(job => `<li><strong>${escapeHtml(job.job_id || job.id)}</strong> · ${escapeHtml(job.status)} ${escapeHtml(job.progress ?? '')}${['queued', 'running'].includes(job.status) ? `<button type="button" data-cancel="${escapeHtml(job.job_id || job.id)}">取消作业</button>` : ''}</li>`).join('') || `<li>${escapeHtml(data.job_error || '尚无训练作业')}</li>`;
  el('disagreements').innerHTML = (data.disagreements || []).map(item => `<li><strong>${escapeHtml(item.task_id)}</strong> · 教师：${escapeHtml(JSON.stringify(item.teacher))} · 学生：${escapeHtml(JSON.stringify(item.student))}</li>`).join('') || '<li>暂无分歧记录</li>';
}
async function refresh() {
  if (busy || stopped) return;
  try {
    const data = await apiGet('learning/stats');
    try { const result = await apiPost('learning/jobs/status', {}, { timeoutMs: 35000 }); data.jobs = (result.jobs || []).map(job => ({ ...job, status: job.state, progress: typeof job.progress === 'object' ? JSON.stringify(job.progress) : job.progress })); }
    catch (error) { data.jobs = []; data.job_error = `训练服务不可用：${error.message}`; }
    render(data); status('已连接 · 每 10 秒更新');
  }
  catch (error) { status(error.message || '读取失败，请重试', true); }
}
async function act(action, body = {}, timeoutMs = 35000) {
  if (busy) return null;
  busy = true;
  document.querySelectorAll('button').forEach(button => { button.disabled = true; });
  status('正在提交…');
  try {
    const result = await apiPost(`learning/${action}`, body, { timeoutMs });
    el('result').textContent = JSON.stringify(result, null, 2);
    status('操作完成');
    return result;
  } catch (error) { status(error.message || '操作失败，请重试', true); return null; }
  finally { busy = false; document.querySelectorAll('button').forEach(button => { button.disabled = false; }); }
}
el('refresh').addEventListener('click', refresh);
el('trainForm').addEventListener('submit', async event => { event.preventDefault(); await act('jobs/create', { model_id: el('trainModel').value.trim(), epochs: Number(el('epochs').value), seed: Number(el('seed').value) }); });
el('jobs').addEventListener('click', event => { const button = event.target.closest('[data-cancel]'); if (button) void act('jobs/cancel', { job_id: button.dataset.cancel }); });
el('modelForm').addEventListener('submit', event => { event.preventDefault(); void act('models/evaluate', { model_id: el('candidate').value.trim() }); });
el('promote').addEventListener('click', () => { if (el('modelForm').reportValidity() && window.confirm('将已通过验收的候选模型设为线上版本？')) void act('models/promote', { model_id: el('candidate').value.trim() }); });
el('rollback').addEventListener('click', () => { if (window.confirm('回滚至上一稳定模型？')) void act('models/rollback'); });
el('rollout').addEventListener('click', () => { if (window.confirm('检查本阶段观察时长和有效比较数量，通过后扩大接管比例？')) void act('models/rollout'); });
el('compareJev').addEventListener('click', () => { void act('models/compare_jev'); });
el('compareTeacher').addEventListener('click', () => { void act('models/compare_teacher', { limit: 6 }, 120000); });
el('sampleForm').addEventListener('submit', async event => {
  event.preventDefault();
  if (busy) return;
  const session = el('session').value.trim();
  busy = true;
  document.querySelectorAll('button').forEach(button => { button.disabled = true; });
  const records = [];
  const seen = new Set();
  let cursor = null;
  try {
    do {
      status(`正在导出…已读取 ${records.length} 条`);
      const data = await apiPost('learning/samples/export', {
        ...(session ? { session_key: session } : {}), limit: 1000,
        ...(cursor ? { cursor } : {}),
      }, { timeoutMs: 35000 });
      if (stopped) return;
      if (!Array.isArray(data.records)) throw new Error('样本响应无效，未生成下载文件');
      records.push(...data.records);
      cursor = data.next_cursor ?? null;
      if (cursor !== null && (typeof cursor !== 'string' || !cursor || seen.has(cursor))) {
        throw new Error('样本分页游标无效，未生成下载文件');
      }
      if (cursor) seen.add(cursor);
    } while (cursor);
    const url = URL.createObjectURL(new Blob([JSON.stringify({ records }, null, 2)], { type: 'application/json' }));
    const link = document.createElement('a'); link.href = url; link.download = 'decision-samples.json';
    link.click(); setTimeout(() => URL.revokeObjectURL(url), 1000);
    status(`已完整导出 ${records.length} 条样本`);
  } catch (error) { status(error.message || '导出失败，未生成下载文件', true); }
  finally { busy = false; document.querySelectorAll('button').forEach(button => { button.disabled = false; }); }
});
el('deleteSamples').addEventListener('click', () => {
  const session = el('session').value.trim();
  if (!session) { status('请填写要删除的会话标识', true); el('session').focus(); return; }
  if (window.confirm('删除此会话的采集样本及关联数据集？此操作无法撤销。')) void act('samples/delete', { session_key: session });
});
renderNav('learning');
await readyBridge().catch(error => status(error.message, true));
await refresh();
const timer = setInterval(() => { if (!document.hidden) void refresh(); }, 10000);
window.addEventListener('pagehide', () => { stopped = true; clearInterval(timer); }, { once: true });
