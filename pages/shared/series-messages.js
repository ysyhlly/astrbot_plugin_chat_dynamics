/* Shared interface messages loader for the eight Dynamics workspaces (v2.0 SSOT). */
export const messages = {};

const PACK_LOCALES = new Set(['en', 'ja', 'ru']);
const loadedLocales = new Set(['zh']);
const pendingLoads = new Map();

const normalizeLocale = (value) => String(value || '').toLowerCase().split(/[-_]/)[0];

export function isLocaleLoaded(lang) {
  const code = normalizeLocale(lang);
  return !PACK_LOCALES.has(code) || loadedLocales.has(code);
}

function mergeLocalePack(lang, pack) {
  if (!pack || typeof pack !== 'object') return;
  for (const [key, value] of Object.entries(pack)) {
    if (typeof value !== 'string') continue;
    const existing = messages[key];
    if (existing && typeof existing === 'object') {
      if (!(lang in existing)) existing[lang] = value;
    } else if (typeof existing === 'string') {
      messages[key] = { zh: key, [lang]: value };
    } else {
      messages[key] = { [lang]: value };
    }
  }
}

export function loadLocaleMessages(lang) {
  const code = normalizeLocale(lang);
  if (!PACK_LOCALES.has(code) || loadedLocales.has(code)) {
    return Promise.resolve(messages);
  }
  if (pendingLoads.has(code)) {
    return pendingLoads.get(code);
  }
  const base = new URL(import.meta.url);
  const url = new URL(`./series-messages-${code}.js${base.search}`, base).href;
  const task = import(url)
    .then((mod) => {
      mergeLocalePack(code, mod?.default || mod?.messages || mod);
      loadedLocales.add(code);
      return messages;
    })
    .catch(() => messages)
    .finally(() => {
      pendingLoads.delete(code);
    });
  pendingLoads.set(code, task);
  return task;
}
