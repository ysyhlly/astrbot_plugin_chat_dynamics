import { apiGet, apiPost, readyBridge, friendlyError } from './api.js';
import { renderNav } from './shell.js';
import {msg,setText,getLanguage} from '../shared/series-ui.js';
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
    setText(el('mode'),msg(data.enabled ? (data.backend === 'llm' ? '普通 LLM 参与决策' : 'Jev 直接决策') : data.decision_mode !== 'persona_model' ? '回复桥接不可用' : '关闭或仅观察'));
    setText(el('model'),service.model || data.provider_id || msg('未选择'));
    el('calls').textContent = String(service.calls ?? 0);
    el('failures').textContent = String(service.failures ?? 0);
    el('latency').textContent = service.last_latency_ms ? `${service.last_latency_ms} ms` : '—';
    setText(el('serviceState'),msg(service.available ? '最近请求成功' : service.configured ? '已配置，等待请求或已降级' : '模型连接未就绪'));
    setText(el('detail'),msg('诊断：{0}。次数从本次插件运行起累计；调用成功不代表决策通过或回复已发送。',service.detail || msg('未调用')));
    setText(el('status'),msg('已连接 · {0} 更新 · 每 10 秒刷新',new Date().toLocaleTimeString(getLanguage())));
    el('status').dataset.error = 'false';
  } catch (error) {
    setText(el('status'),friendlyError(error,msg('读取失败，请重试。')));
    el('status').dataset.error = 'true';
    for (const id of ['mode', 'model', 'calls', 'failures', 'latency', 'serviceState']) setText(el(id),'—');
  } finally { refreshing = false; el('refresh').disabled = false; }
}
async function exportHistory() {
  if (exporting) return;
  exporting = true; el('export').disabled = true;
  setText(el('historyStatus'),msg('正在读取历史样本…'));
  try {
    const data = await apiPost('decision/history', {cursor, limit: 500});
    if (!data.records.length) {
      setText(el('historyStatus'),msg(cursor === '0' ? '没有历史样本。' : '历史样本已导出完毕。'));
      return;
    }
    const url = URL.createObjectURL(new Blob([data.records.map(row => JSON.stringify(row)).join('\n') + '\n'], {type: 'application/x-ndjson;charset=utf-8'}));
    const link = document.createElement('a'); link.href = url;
    link.download = `decision-history-${cursor}.jsonl`;
    document.body.appendChild(link); link.click(); link.remove();
    window.setTimeout(() => URL.revokeObjectURL(url), 1000);
    cursor = data.next_cursor;
    setText(el('historyStatus'),msg(data.content_hidden ? '已导出 {0} 条，聊天内容按隐私设置隐藏。{1}' : '已导出 {0} 条。{1}',data.records.length,msg(cursor ? '可继续导出下一批。' : '已到最后一批。')));
    setText(el('export'),msg(cursor ? '导出下一批（最多 500 条）' : '导出完毕'));
  } catch (error) { setText(el('historyStatus'),friendlyError(error,msg('历史导出失败，请重试。'))); }
  finally { exporting = false; el('export').disabled = cursor === null; }
}
el('refresh').addEventListener('click', () => void refresh());
el('export').addEventListener('click', () => void exportHistory());
window.addEventListener('pagehide', () => { stopped = true; });
await readyBridge().catch(() => {}); renderNav('learning'); await refresh();
const timer = window.setInterval(() => void refresh(), 10000);
window.addEventListener('pagehide', () => window.clearInterval(timer));
