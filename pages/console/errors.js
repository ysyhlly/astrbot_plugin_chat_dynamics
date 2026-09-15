/**
 * Canonical error copy for every plugin page.
 *
 * Backend/host errors arrive as English developer strings (see core/web_api.py
 * _json_err and main.py ValueError templates). The panel is a Chinese admin UI,
 * so map the known contracts to actionable Chinese here, once, at the display
 * boundary. Unknown text is passed through so nothing is silently swallowed.
 */

const EXACT = {
  // Host / bridge (thrown locally by api.js and the page helpers)
  "Plugin Page bridge 不可用": "无法连接插件页面，请刷新后重试。",
  "请求超时": "请求超时，请稍后重试。",
  "空响应": "插件没有返回内容，请刷新后重试。",
  // Session scope
  "unknown session": "这个会话已经不在了，请刷新后重试。",
  "missing session_key": "请先选择一个会话。",
  "session_key required": "请先选择一个会话。",
  "session_key is required": "请先选择一个会话。",
  "umo required": "请先选择一个会话。",
  "session_key is too long": "会话标识过长，请重新选择会话。",
  "session_key too long": "会话标识过长，请重新选择会话。",
  "umo too long": "会话标识过长，请重新选择会话。",
  // Throttle / lifecycle
  "request rate limit exceeded": "操作太频繁了，请等 1 分钟后再试。",
  "plugin is shutting down": "插件正在重启，请稍后再试。",
  // Panel reads
  "runtime state unavailable": "运行时状态暂时读不到，请稍后重试。",
  "config unavailable": "配置暂时读不到，请稍后重试。",
  "config could not be saved": "配置没有保存成功，请稍后重试。",
  "config could not be applied": "配置没能应用到运行时，请稍后重试。",
  "config must be an object": "配置格式不对，请刷新页面后重试。",
  "providers unavailable": "模型列表暂时读不到，已保存的模型选择不受影响。",
  "preset catalog unavailable": "预设列表暂时读不到，请稍后重试。",
  "unknown preset": "这个预设不存在，请重新选择。",
  "preset could not be applied": "预设没能应用，请稍后重试。",
  "name must be a string": "预设取值不对，请重新选择。",
  "preset name is too long": "预设名称过长。",
  "confirm must be true": "这次操作需要二次确认。",
  "read air unavailable": "读空气暂时不可用，请稍后刷新。",
  "replay unavailable": "回放暂时不可用，请稍后刷新。",
  "annotations unavailable": "标注暂时读不到，请稍后重试。",
  "annotation write failed": "标注没有保存成功，请稍后重试。",
  "annotation draft failed": "生成草稿失败，请稍后重试。",
  "annotation drafts unavailable": "草稿列表暂时读不到，请稍后重试。",
  "annotation drafts apply failed": "草稿处理失败，请稍后重试。",
  "notebook unavailable": "记忆小本暂时读不到，请稍后重试。",
  "notebook write failed": "记忆小本没有写入成功，请稍后重试。",
  "cool operation unavailable": "冷却操作暂时不可用，请稍后重试。",
  "reset operation unavailable": "重置操作暂时不可用，请稍后重试。",
  "minutes must be between 1 and 180": "冷却分钟必须在 1 到 180 之间。",
  // Navigation
  "unsupported page": "这个页面不存在。",
  "page service unavailable": "页面跳转暂时不可用，请稍后重试。",
  "page entry unavailable": "页面跳转暂时不可用，请稍后重试。",
  "content_path missing": "页面跳转暂时不可用，请稍后重试。",
  "plugin request unavailable": "页面跳转暂时不可用，请稍后重试。",
  "underlying request unavailable": "页面跳转暂时不可用，请稍后重试。",
  "unauthorized": "登录状态已失效，请重新登录后台。",
  "UI preference unavailable": "界面偏好暂时读不到。",
  "UI preference save failed": "界面偏好没有保存成功。",
  "ui must be day or night": "界面主题取值不对。",
  // Notebook / annotation domain codes
  "sensitive_blocked": "这条短语包含敏感词，没有保存。",
  "unapproved_slang": "这条梗还没确认可用，请先勾选「我确认这是群里已允许的梗」。",
  "title required": "请填写标题。",
  "text required": "请填写内容。",
  "phrase required": "请填写短语。",
  "invalid date": "日期不对，请检查月份和日期。",
  "invalid annotation fields": "标注内容不合法，请检查后重试。",
  "invalid annotation value": "标注取值不合法，请检查后重试。",
  "invalid recipient boolean": "收件人选项不合法，请检查后重试。",
  "invalid recipient identities": "收件人 ID 不合法，请检查后重试。",
  "invalid recipient error type": "收件人错误类型不合法，请检查后重试。",
  "invalid error type": "错误类型不合法，请检查后重试。",
  "message no longer available; refresh replay": "这条消息已经不在保留窗口里了，请刷新回放。",
  "unknown target topic": "目标话题已不存在，请刷新后重试。",
  "correct label must match prediction": "选「判断正确」时，话题必须与系统判断一致。",
  "invalid body": "提交的数据不完整，请刷新页面后重试。",
  "invalid expected_topic": "话题取值不对，请重新选择。",
  "msg_ids is required": "请先勾选要处理的草稿。",
  "unknown action": "这个操作不支持，请刷新页面后重试。",
  "body too large": "一次提交的内容太多了，请减少数量后重试。",
  "invalid JSON body": "提交的数据格式不对，请刷新页面后重试。",
  "JSON body must be an object": "提交的数据格式不对，请刷新页面后重试。",
};

const RULES = [
  [/^unknown fields: (.+)$/, (m) => `有无法识别的参数：${m[1]}`],
  [/^([a-zA-Z0-9_]+) must be a number$/, (m) => `参数「${m[1]}」必须是数字。`],
  [/^([a-zA-Z0-9_]+) must be an integer$/, (m) => `参数「${m[1]}」必须是整数。`],
  [/^([a-zA-Z0-9_]+) must be a finite number$/, (m) => `参数「${m[1]}」必须是有限数字。`],
  [/^([a-zA-Z0-9_]+) must be a boolean$/, (m) => `参数「${m[1]}」必须是开关值。`],
  [/^([a-zA-Z0-9_]+) must be <= ([0-9.]+)$/, (m) => `参数「${m[1]}」不能大于 ${m[2]}。`],
  [/^([a-zA-Z0-9_]+) must be >= ([0-9.]+)$/, (m) => `参数「${m[1]}」不能小于 ${m[2]}。`],
  [/^([a-zA-Z0-9_]+) required$/, (m) => `请填写「${m[1]}」。`],
  [/^unknown notebook action: (.+)$/, () => "这个操作不支持，请刷新页面后重试。"],
  [/^模型不可用（[^）]*）[。.]?$/, () => "所选模型暂时不可用，请检查 Provider 配置或稍后重试。"],
  [/^调用模型失败（[^）]*）[。.]?$/, () => "调用模型失败，请检查 Provider 配置或网络后重试。"],
];

/**
 * Return user-facing copy for an error raised by a bridge call or thrown locally.
 * @param {unknown} error
 * @param {string} fallback copy used when the error carries no message
 */
export function friendlyError(error, fallback = "操作失败，请稍后重试。") {
  const raw = typeof error === "string" ? error : (error && error.message) || "";
  const text = String(raw).trim();
  if (!text) return fallback;
  if (Object.hasOwn(EXACT, text)) return EXACT[text];
  for (const [pattern, build] of RULES) {
    const match = pattern.exec(text);
    if (match) return build(match);
  }
  // Anything the table does not know is shown verbatim: the panel is an admin
  // tool, and swallowing an unfamiliar message would hide the only clue there
  // is. Every message this plugin itself emits is covered above.
  return text;
}

export default { friendlyError };
