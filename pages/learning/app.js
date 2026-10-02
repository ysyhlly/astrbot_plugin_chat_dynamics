import { apiGet, apiPost, readyBridge, friendlyError } from './api.js';
import { renderNav } from './shell.js';
const el = id => document.getElementById(id);
let refreshing = false, exporting = false, stopped = false, cursor = '0';
async function refresh() {
  if (refreshing || stopped) return;
  refreshing = true;
  el('refresh').disabled = true;
  try {
    const data = await apiGet('decision/status');
    if (stopped) return;
    const service = data.service || {};
    el('mode').textContent = data.enabled ? 'Jev 直接决策' : data.decision_mode !== 'persona_model' ? '回复桥接不可用' : '关闭或仅观察';
    el('model').textContent = service.model || data.provider_id || '未选择';
    el('calls').textContent = String(service.calls ?? 0);
    el('failures').textContent = String(service.failures ?? 0);
    el('latency').textContent = service.last_latency_ms ? `${service.last_latency_ms} ms` : '—';
    el('serviceState').textContent = service.available ? '最近请求成功' : service.configured ? '已配置，等待请求或已降级' : '模型连接未就绪';
    el('detail').textContent = `诊断：${service.detail || '未调用'}。次数从本次插件运行起累计；调用成功不代表决策通过或回复已发送。`;
    el('status').textContent = `已连接 · ${new Date().toLocaleTimeString()} 更新 · 每 10 秒刷新`;
    el('status').dataset.error = 'false';
  } catch (error) {
    el('status').textContent = friendlyError(error, '读取失败，请重试。');
    el('status').dataset.error = 'true';
    for (const id of ['mode', 'model', 'calls', 'failures', 'latency', 'serviceState']) el(id).textContent = '—';
  } finally { refreshing = false; el('refresh').disabled = false; }
}
async function exportHistory() {
  if (exporting) return;
  exporting = true; el('export').disabled = true;
  el('historyStatus').textContent = '正在读取历史样本…';
  try {
    const data = await apiPost('decision/history', {cursor, limit: 500});
    if (!data.records.length) {
      el('historyStatus').textContent = cursor === '0' ? '没有历史样本。' : '历史样本已导出完毕。';
      return;
    }
    const url = URL.createObjectURL(new Blob([data.records.map(row => JSON.stringify(row)).join('\n') + '\n'], {type: 'application/x-ndjson;charset=utf-8'}));
    const link = document.createElement('a'); link.href = url;
    link.download = `decision-history-${cursor}.jsonl`;
    document.body.appendChild(link); link.click(); link.remove();
    window.setTimeout(() => URL.revokeObjectURL(url), 1000);
    cursor = data.next_cursor;
    el('historyStatus').textContent = `已导出 ${data.records.length} 条${data.content_hidden ? '，聊天内容按隐私设置隐藏' : ''}。${cursor ? '可继续导出下一批。' : '已到最后一批。'}`;
    el('export').textContent = cursor ? '导出下一批（最多 500 条）' : '导出完毕';
  } catch (error) { el('historyStatus').textContent = friendlyError(error, '历史导出失败，请重试。'); }
  finally { exporting = false; el('export').disabled = cursor === null; }
}
el('refresh').addEventListener('click', () => void refresh());
el('export').addEventListener('click', () => void exportHistory());
window.addEventListener('pagehide', () => { stopped = true; });
await readyBridge().catch(() => {}); renderNav('learning'); await refresh();
const timer = window.setInterval(() => void refresh(), 10000);
window.addEventListener('pagehide', () => window.clearInterval(timer));
