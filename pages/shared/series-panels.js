/* Canonical series navigation shell v2.0 — single-line bar, 8-panel live matrix & Ctrl+K command palette. */
import {
 msg,tr,setText,feedback,languagePicker,confirmAction,
 getLanguage,getSharedScope,setSharedScope,getSharedPersona,formatScopeLabel,icon,
} from './series-ui.js';

const root=document.getElementById('dynamics-series-panel');
const WORLD_ID='astrbot_plugin_dynamics_world';
const HUB_ID='astrbot_plugin_chat_dynamics';

const states={
 missing:'未安装',disabled:'已停用',unloaded:'未加载',conflict:'同名冲突',
 enabled:'已启用',ready:'已就绪',inactive:'未接入',not_ready:'未就绪',error:'状态异常',
};

const PLUGIN_ORDER=[
 'astrbot_plugin_dynamics_world',
 'astrbot_plugin_chat_dynamics',
 'astrbot_plugin_dynamics_learning',
 'astrbot_plugin_dynamics_memory',
 'astrbot_plugin_dynamics_horizons',
 'astrbot_plugin_dynamics_games',
 'astrbot_plugin_visual_media',
 'astrbot_plugin_group_chronicle',
];

const PLUGIN_META={
 astrbot_plugin_dynamics_world:{
  mark:'DW',name:'群间世界',icon:'world',page:'world',
  desc:'像素小屋、日程、天气与世界地图',
 },
 astrbot_plugin_chat_dynamics:{
  mark:'CD',name:'对话主控',icon:'chat',page:'app',
  desc:'回复节奏、冷却观测与群聊总控矩阵',
 },
 astrbot_plugin_dynamics_learning:{
  mark:'DL',name:'群间学习',icon:'learning',page:'review',
  desc:'提案审查、群友关系图与好感度',
 },
 astrbot_plugin_dynamics_memory:{
  mark:'DM',name:'群间记忆',icon:'memory',page:'dashboard',
  desc:'知识图谱、事实记忆与召回测试',
 },
 astrbot_plugin_dynamics_horizons:{
  mark:'DH',name:'群间见闻',icon:'horizons',page:'horizons',
  desc:'外部书架、暂存兴趣与阅读手账',
 },
 astrbot_plugin_dynamics_games:{
  mark:'DG',name:'群间游戏',icon:'games',page:'games',
  desc:'共读、约局、接龙与成果归档',
 },
 astrbot_plugin_visual_media:{
  mark:'VM',name:'视觉工坊',icon:'visual',page:'workshop',
  desc:'角色参考图、表情图库与图片创作',
 },
 astrbot_plugin_group_chronicle:{
  mark:'GC',name:'群文化沿革',icon:'chronicle',page:'culture',
  desc:'本群词库、梗起源时间线与纠偏',
 },
};

const VALID_TONES=new Set(['accent','warm','info','danger','muted']);
const node=(tag,text='',cls='')=>{
 const n=document.createElement(tag);
 if(cls)n.className=cls;
 if(text!=='')setText(n,text);
 return n;
};

let loading=false,navigating=false,edited=false,cached=null,refreshRequest=null;
let scopePill=null,matrixOpen=false,lastSyncedAt=0;
const commandProviders=new Set();

const getBridge=()=>(window.DynamicsPanelBridge||window.AstrBotPluginPage||window.AstrBotPluginView);

for(const type of ['input','change']){
 document.addEventListener(type,event=>{
  const target=event.target;
  // Searches, filters, and command palette inputs are navigation state, not unsaved edits.
  if(event.isTrusted&&!root?.contains(target)&&!target?.closest?.('#dynamics-cmdk-modal,dialog,[id*=filter],.toolbar,.console-controls,.review-controls,.creative-toolbar,.persona-bar,.blocklist-bar')&&target?.matches?.('textarea,[contenteditable=true],form input:not([type=search])')){
   edited=true;
  }
 },true);
}

const FEDERATED_SEARCH_TIMEOUT_MS=1500;
const RESERVED_NAV_KEYS=new Set([
 'ui','locale','scope','persona','deep_link','series_entry','series_deeplink','lang','theme','umo','plugin',
]);
const TARGET_QUERY_KEYS=new Set([
 'tab','section','view','page','subpage','anchor','id','memory_id','node_id','entry_id',
 'entry_uid','place_id','interest_id','log_id','review_id','source_id','job_id',
 'character','key','hash','query','identity',
]);
const SAFE_DEEPLINK_KEY=/^[a-zA-Z][a-zA-Z0-9_]{0,31}$/;
const PENDING_DEEPLINK_KEY='dynamics_series_pending_deeplink';

function sanitizeDeepLink(raw){
 if(typeof raw==='string'){
  const clean=raw.trim().slice(0,256);
  return clean||null;
 }
 if(!raw||typeof raw!=='object'||Array.isArray(raw))return null;
 const out={};
 for(const [k,v] of Object.entries(raw)){
  if(!SAFE_DEEPLINK_KEY.test(k)||v==null||v==='')continue;
  if(typeof v!=='string'&&typeof v!=='number'&&typeof v!=='boolean')continue;
  const text=String(v).trim().slice(0,256);
  if(text)out[k]=text;
 }
 return Object.keys(out).length?out:null;
}

async function timed(promise,ms=8000,signal=null){
 if(signal?.aborted)throw Error(tr('请求已取消'));
 let timer,onAbort;
 try{
  const racers=[
   promise,
   new Promise((_,reject)=>{timer=setTimeout(()=>reject(Error(tr('请求超时，请刷新重试'))),ms);}),
  ];
  if(signal&&typeof signal.addEventListener==='function'){
   racers.push(new Promise((_,reject)=>{
    onAbort=()=>reject(Error(tr('请求已取消')));
    signal.addEventListener('abort',onAbort,{once:true});
   }));
  }
  return await Promise.race(racers);
 }finally{
  clearTimeout(timer);
  if(signal&&onAbort&&typeof signal.removeEventListener==='function'){
   signal.removeEventListener('abort',onAbort);
  }
 }
}

async function beforeNavigate(destination){
 if(window.ChatDynamicsTheme?.flush)await timed(window.ChatDynamicsTheme.flush());
 const handler=window.DynamicsBeforeNavigate||window.ChatDynamicsBeforeNavigate;
 if(typeof handler==='function')return (await handler(destination))!==false;
 return !edited||confirmAction(msg('此页有编辑操作。请确认已保存，再打开其他面板。'));
}
window.DynamicsSharedBeforeNavigate=beforeNavigate;

function pluginLabel(plugin){
 const meta=PLUGIN_META[plugin?.id];
 return meta?msg(meta.name):(plugin?.label||plugin?.id||'');
}

function guessCurrentId(data){
 if(data?.current)return data.current;
 const cur=data?.plugins?.find?.(p=>p.current);
 if(cur?.id)return cur.id;
 try{
  const ctxId=getBridge()?.getContext?.()?.pluginName;
  if(ctxId&&PLUGIN_META[ctxId])return ctxId;
 }catch{}
 const p=String(location.pathname||'').toLowerCase();
 if(p.includes('/world/'))return WORLD_ID;
 if(p.includes('/review/'))return 'astrbot_plugin_dynamics_learning';
 if(p.includes('/dashboard/'))return 'astrbot_plugin_dynamics_memory';
 if(p.includes('/horizons/'))return 'astrbot_plugin_dynamics_horizons';
 if(p.includes('/games/'))return 'astrbot_plugin_dynamics_games';
 if(p.includes('/workshop/'))return 'astrbot_plugin_visual_media';
 if(p.includes('/culture/'))return 'astrbot_plugin_group_chronicle';
 if(p.includes('/app/'))return HUB_ID;
 return '';
}

function orderedPagePlugins(data){
 const pagePlugins=(data?.plugins||[]).filter(p=>p.page&&(!p.association||p.current));
 const rank=id=>{const idx=PLUGIN_ORDER.indexOf(id);return idx===-1?999:idx;};
 return [...pagePlugins].sort((a,b)=>rank(a.id)-rank(b.id));
}

function feedbackNode(){
 return root?.querySelector('.sp-feedback')||null;
}

const escAttr=s=>(typeof CSS!=='undefined'&&typeof CSS.escape==='function')?CSS.escape(s):String(s).replace(/["\\]/g,'\\$&');
async function navigatePlugin(plugin,transition,extraOptions={}){
 if(!plugin||navigating||plugin.current||!plugin.can_open)return false;
 const note=feedbackNode();
 navigating=true;
 const buttons=root?[...root.querySelectorAll(`[data-sp-plugin="${escAttr(plugin.id)}"], [data-sp-action="go-home"]`)]:[];
 for(const b of buttons)b.disabled=true;
 try{
  const bridge=getBridge();
  const nativeNavigation=typeof bridge?.navigateToPlugin==='function';
  if(!nativeNavigation&&!window.DynamicsWorkspace)throw Error(tr('请更新入口和目标群间插件后重试。'));
  const hostGuard=nativeNavigation&&bridge.getContext?.()?.supportsNavigationGuard===true;
  if(!hostGuard&&!await beforeNavigate({pluginName:plugin.id,pageName:plugin.page})){
   feedback(note,msg('已取消跳转，保留当前编辑。'));
   return false;
  }
  setMatrixOpen(false);
  feedback(note,msg('正在打开{0}…',pluginLabel(plugin)),'loading');
  const deepLink=sanitizeDeepLink(extraOptions.deepLink??extraOptions.deep_link);
  const options={
   ui:document.documentElement.dataset.uiTheme,
   locale:getLanguage(),
   scope:extraOptions.scope??(getSharedScope()||undefined),
   persona:extraOptions.persona??(getSharedPersona()||undefined),
  };
  if(deepLink){
   options.deep_link=deepLink;
   if(typeof deepLink==='object'){
    for(const [k,v] of Object.entries(deepLink)){
     if(!RESERVED_NAV_KEYS.has(k))options[k]=v;
    }
   }
   try{
    sessionStorage.setItem(PENDING_DEEPLINK_KEY,JSON.stringify({
     plugin_id:plugin.id,
     page:plugin.page,
     title:extraOptions.entry?.title?String(extraOptions.entry.title).slice(0,160):'',
     kind:extraOptions.entry?.kind?String(extraOptions.entry.kind).slice(0,64):'',
     deep_link:deepLink,
     ts:Date.now(),
    }));
   }catch{}
  }
  if(!nativeNavigation){
   await window.DynamicsWorkspace.open(plugin.id,plugin.page,options,transition);
   return true;
  }
  if(transition)await transition();
  const operation=bridge.navigateToPlugin(plugin.id,plugin.page,options);
  const result=hostGuard?await operation:await timed(operation);
  if(result?.navigating===false&&!result?.updated){
   if(deepLink){try{sessionStorage.removeItem(PENDING_DEEPLINK_KEY);}catch{}}
   feedback(note,msg('已取消跳转，保留当前编辑。'));
   return false;
  }
  return true;
 }catch(error){
  if(extraOptions.deepLink||extraOptions.deep_link){try{sessionStorage.removeItem(PENDING_DEEPLINK_KEY);}catch{}}
  feedback(
   note,
   String(error?.message||'').includes('Unsupported plugin bridge action')
    ?msg('宿主导航尚未更新，请整页刷新 AstrBot（Ctrl+F5）后重试。')
    :error?.message||msg('跳转失败，请刷新后重试'),
   'error',
  );
  if(transition)throw error;
  return false;
 }finally{
  navigating=false;
  for(const b of buttons){
   const isHomeBtn=b.dataset.spAction==='go-home';
   const target=isHomeBtn?(cached?.plugins?.find(p=>p.id===WORLD_ID)||plugin):plugin;
   b.disabled=!!target.current||!target.can_open;
  }
 }
}

function paintScope(){
 if(!scopePill)return;
 const activeUmo=getSharedScope();
 scopePill.hidden=!activeUmo;
 setText(scopePill,msg('当前会话：{0}',activeUmo),'title');
 setText(scopePill,msg('当前会话：{0}',activeUmo),'aria-label');
 setText(scopePill.querySelector('.sp-scope-text'),formatScopeLabel(activeUmo));
}

function setMatrixOpen(next,{restoreFocus=false}={}){
 matrixOpen=Boolean(next);
 const pop=root?.querySelector('#sp-matrix-popover');
 const trigger=root?.querySelector('.sp-matrix-trigger');
 if(pop)pop.hidden=!matrixOpen;
 if(trigger)trigger.setAttribute('aria-expanded',String(matrixOpen));
 if(!matrixOpen&&restoreFocus)trigger?.focus?.({preventScroll:true});
}

function buildPluginCard(plugin){
 const meta=PLUGIN_META[plugin.id]||{mark:'SP',icon:'sparkle',desc:''};
 const label=pluginLabel(plugin);
 const button=node('button','',`sp-link sp-matrix-card sp-${plugin.state||'ready'}`);
 button.type='button';
 button.dataset.spFocus=plugin.id;
 button.dataset.spPlugin=plugin.id;

 const iconWrap=node('span','','sp-card-icon');
 iconWrap.append(icon(meta.icon,16));

 const body=node('div','','sp-card-body');
 const top=node('div','','sp-card-top');
 const nameWrap=node('span','','sp-card-name-wrap');
 const dot=node('span','','sp-dot');dot.setAttribute('aria-hidden','true');
 nameWrap.append(dot,node('span',label,'sp-name'));
 top.append(nameWrap);

 const badge=plugin.badge&&typeof plugin.badge==='object'?plugin.badge:null;
 const isHealthy=plugin.state==='ready'||plugin.state==='enabled';
 if(badge&&badge.label){
  const tone=VALID_TONES.has(badge.tone)?badge.tone:'accent';
  top.append(node('span',String(badge.label),`sp-badge is-${tone}`));
 }else if(!isHealthy){
  top.append(node('span',msg(states[plugin.state]||'未知'),'sp-state'));
 }else if(plugin.current){
  top.append(node('span',msg('当前面板'),'sp-badge is-accent'));
 }

 const descText=plugin.pulse?.headline?String(plugin.pulse.headline):(meta.desc?msg(meta.desc):'');
 body.append(top);
 if(descText)body.append(node('div',descText,'sp-card-desc'));

 button.append(iconWrap,body);
 button.disabled=!!plugin.current||!plugin.can_open;
 setText(button,plugin.current?msg('当前面板'):!plugin.can_open?msg('此插件当前不可打开'):label,'title');
 if(plugin.current)button.setAttribute('aria-current','page');

 button.seriesNavigate=transition=>navigatePlugin(plugin,transition);
 button.addEventListener('click',()=>button.seriesNavigate());
 return button;
}

function render(data){
 if(!root)return;
 const focused=root.contains(document.activeElement)?document.activeElement?.dataset.spFocus:null;
 const extraOpen=root.querySelector('.sp-extra')?.open===true;
 const currentId=guessCurrentId(data);
 const isWorld=currentId?currentId===WORLD_ID:location.pathname.includes('/world/');
 root.dataset.spHost=isWorld?'world':'companion';
 root.replaceChildren();

 const bar=node('div','','sp-bar');
 const left=node('div','','sp-bar-left');
 const note=node('p','','sp-feedback');note.setAttribute('role','status');note.setAttribute('aria-live','polite');

 const pagePlugins=orderedPagePlugins(data);
 const worldPlugin=pagePlugins.find(p=>p.id===WORLD_ID)||data?.plugins?.find(p=>p.id===WORLD_ID)||null;
 const currentPlugin=pagePlugins.find(p=>p.current||p.id===currentId)||null;
 const currentMeta=PLUGIN_META[currentPlugin?.id||currentId]||{mark:'SP',name:currentPlugin?.label||'群间系列',icon:'sparkle'};

 // 1. Companion back-to-cabin button
 if(!isWorld){
  const homeBtn=node('button','','sp-link sp-home-hub');
  homeBtn.type='button';
  homeBtn.dataset.spFocus='home';
  homeBtn.dataset.spAction='go-home';
  homeBtn.append(icon('home',15),node('span',msg('返回群间小屋'),'sp-home-label'));
  setText(homeBtn,msg('返回群间小屋'),'title');
  setText(homeBtn,msg('返回群间小屋'),'aria-label');
  const canOpenWorld=Boolean(worldPlugin?.can_open&&!worldPlugin?.current);
  homeBtn.disabled=data?!canOpenWorld:true;
  homeBtn.addEventListener('click',()=>{if(worldPlugin)navigatePlugin(worldPlugin);});
  left.append(homeBtn,node('span','/','sp-sep'));
 }

 // 2. Current Panel Matrix Trigger + 2x4 Popover
 const anchor=node('div','','sp-matrix-anchor');
 const trigger=node('button','','sp-matrix-trigger');
 trigger.type='button';
 trigger.dataset.spFocus='matrix';
 trigger.dataset.spAction='matrix-toggle';
 trigger.setAttribute('aria-haspopup','true');
 trigger.setAttribute('aria-expanded',String(matrixOpen));
 trigger.setAttribute('aria-controls','sp-matrix-popover');
 setText(trigger,msg('展开或收起 8 面板状态矩阵'),'aria-label');
 setText(trigger,msg('切换面板'),'title');

 trigger.append(node('span',currentMeta.mark,'sp-mark'));
 trigger.append(node('span',PLUGIN_META[currentPlugin?.id||currentId]?msg(currentMeta.name):currentMeta.name,'sp-current-name'));

 const hasDanger=Boolean(
  data?.legacy_conflict
  ||pagePlugins.some(p=>p.state==='error'||p.state==='conflict'||p.badge?.tone==='danger'),
 );
 const hasWarmBadge=!hasDanger&&pagePlugins.some(p=>Number(p.badge?.count||0)>0||p.badge?.tone==='warm');
 if(hasDanger||hasWarmBadge){
  const alertDot=node('span','',`sp-alert-dot${hasDanger?' is-danger':''}`);
  alertDot.setAttribute('aria-hidden','true');
  trigger.append(alertDot);
 }
 trigger.append(icon('chevron',13,'sp-chevron'));
 trigger.addEventListener('click',()=>setMatrixOpen(!matrixOpen));

 const popover=node('div','','sp-matrix-popover');
 popover.id='sp-matrix-popover';
 popover.dataset.spMatrix='';
 popover.hidden=!matrixOpen;

 const popHead=node('div','','sp-matrix-head');
 const popTitleWrap=node('div');
 popTitleWrap.append(
  node('div',msg('群间系列 · 8 面板实时状态'),'sp-matrix-title'),
  node('div',msg('实时角标聚合 · 保留当前作用域切换'),'sp-matrix-sub'),
 );
 const popRefresh=node('button',msg('刷新状态'),'sp-matrix-refresh');
 popRefresh.type='button';
 popRefresh.addEventListener('click',()=>refresh(false));
 popHead.append(popTitleWrap,popRefresh);
 popover.append(popHead);

 const grid=node('nav','','sp-matrix-grid sp-links');
 setText(grid,msg('群间系列面板互跳'),'aria-label');
 if(pagePlugins.length){
  for(const plugin of pagePlugins)grid.append(buildPluginCard(plugin));
 }else{
  grid.append(node('span',msg('互通插件'),'sp-title'));
 }
 popover.append(grid);

 // Matrix footer: health, legacy conflicts & versions
 if(data){
  const readyCount=pagePlugins.filter(p=>p.state==='ready'||p.state==='enabled').length;
  const footer=node('div','','sp-matrix-footer');
  const health=node('div','',`sp-matrix-health${data.legacy_conflict?' is-conflict':''}`);
  health.append(
   node('span',msg(data.legacy_conflict?'存在旧插件冲突':'辅助与兼容：无旧插件冲突')),
   node('span',msg('{0}/{1} 已就绪',readyCount,pagePlugins.length)),
  );
  footer.append(health);

  const auxPlugins=(data.plugins||[]).filter(p=>!p.page||(p.association&&!p.current));
  const versionPlugins=(data.plugins||[]).filter(p=>p.version&&PLUGIN_META[p.id]);
  const overlapPlugins=Array.isArray(data.overlap_plugins)?data.overlap_plugins:[];
  if(data.legacy_conflict||overlapPlugins.length||auxPlugins.length||versionPlugins.length){
   const extra=node('details','',`sp-extra${data.legacy_conflict?' sp-extra-alert':''}`);
   extra.open=extraOpen||Boolean(data.legacy_conflict);
   extra.append(node('summary',msg(data.legacy_conflict?'存在旧插件冲突':'系列版本与兼容详情')));
   const body=node('div','','sp-extra-body');
   if(data.legacy_conflict){
    body.append(node('p',msg('旧插件仍在运行，请检查接管归属，避免重复处理。'),'sp-warning'));
    const conflictNames=(data.legacy_conflicts||[]).map(c=>c.label).filter(Boolean);
    if(conflictNames.length)body.append(node('p',conflictNames.join('、'),'sp-warning'));
   }
   if(overlapPlugins.length){
    const names=overlapPlugins.map(item=>item.label||item.id).filter(Boolean).join('、');
    body.append(node('p',msg('温和提醒：检测到功能有部分重叠的生态插件（{0}），可和平共存，不阻断运行。',names),'sp-note'));
   }
   for(const plugin of auxPlugins){
    if(plugin.page){body.append(buildPluginCard(plugin));continue;}
    const row=node('p');
    row.append(node('span',`${plugin.label} · `),node('span',msg(states[plugin.state]||'未知')));
    body.append(row);
   }
   for(const plugin of versionPlugins){
    const row=node('p');
    row.append(
     node('span',msg(PLUGIN_META[plugin.id].name)),
     node('span',' · '),
     node('code',[plugin.version,plugin.source_commit].filter(Boolean).join(' · ')),
    );
    body.append(row);
   }
   extra.append(body);
   footer.append(extra);
  }
  popover.append(footer);
 }

 anchor.append(trigger,popover);
 left.append(anchor);

 // 3. Ctrl+K Quick Palette Trigger
 const cmdkBtn=node('button','','sp-cmdk-trigger');
 cmdkBtn.type='button';
 cmdkBtn.dataset.spFocus='cmdk';
 cmdkBtn.dataset.spAction='cmdk-open';
 setText(cmdkBtn,msg('打开快捷命令面板 (Ctrl+K)'),'title');
 setText(cmdkBtn,msg('打开快捷命令面板 (Ctrl+K)'),'aria-label');
 const isMac=/mac/i.test(typeof navigator!=='undefined'?(navigator.platform||''):'');
 cmdkBtn.append(
  icon('search',14),
  node('span',msg('搜索面板、群聊…'),'sp-cmdk-label'),
  node('kbd',isMac?'⌘K':'Ctrl+K','sp-kbd'),
 );
 cmdkBtn.addEventListener('click',()=>openCmdK());
 left.append(cmdkBtn);

 bar.append(left);

 // 4. Right side tools: Scope pill + Language picker + Sync status button
 const tools=node('div','','sp-tools');
 if(!isWorld){
  scopePill=node('span','','sp-scope-pill');
  scopePill.append(node('span','','sp-scope-text'));
  const clearBtn=node('button','×','sp-scope-clear');
  clearBtn.type='button';
  setText(clearBtn,msg('清除跨面板会话筛选'),'aria-label');
  setText(clearBtn,msg('清除跨面板会话筛选'),'title');
  clearBtn.addEventListener('click',()=>{setSharedScope('',{syncUrl:true,emit:true});});
  scopePill.append(clearBtn);
  paintScope();
  tools.append(scopePill);
 }
 tools.append(languagePicker());

 const refreshButton=node('button','','sp-refresh');
 refreshButton.dataset.spFocus='refresh';
 refreshButton.type='button';
 setText(refreshButton,msg('刷新状态'),'title');
 setText(refreshButton,msg('刷新状态'),'aria-label');
 const healthDot=node('span','',`sp-health-dot${hasDanger?' is-danger':hasWarmBadge?' is-warn':''}`);
 healthDot.setAttribute('aria-hidden','true');
 const syncLabel=lastSyncedAt?msg('同步于刚刚'):msg('刷新状态');
 refreshButton.append(healthDot,icon('refresh',14),node('span',syncLabel,'sp-refresh-label'));
 refreshButton.addEventListener('click',()=>refresh(false));
 tools.append(refreshButton);

 bar.append(tools);
 root.append(bar,note);

 if(focused){
  for(const control of root.querySelectorAll('[data-sp-focus]')){
   if(control.dataset.spFocus===focused)control.focus({preventScroll:true});
  }
 }
}

let deepLinkDelivered=false;
function consumeIncomingDeepLink(currentId){
 if(deepLinkDelivered)return null;
 let payload=null;
 try{
  const params=new URLSearchParams(location.search||'');
  const rawParam=params.get('series_deeplink');
  if(rawParam){
   let parsed=null;
   try{parsed=JSON.parse(rawParam);}catch{parsed=rawParam;}
   const clean=sanitizeDeepLink(parsed);
   if(clean)payload={plugin_id:currentId||'',deep_link:clean};
   try{
    const nextUrl=new URL(location.href);
    nextUrl.searchParams.delete('series_deeplink');
    history.replaceState(history.state,'',nextUrl);
   }catch{}
  }
 }catch{}
 if(!payload){
  try{
   const ctx=getBridge()?.getContext?.();
   const clean=sanitizeDeepLink(ctx?.deep_link);
   if(clean)payload={plugin_id:currentId||'',deep_link:clean};
  }catch{}
 }
 if(currentId){
  try{
   const raw=sessionStorage.getItem(PENDING_DEEPLINK_KEY);
   if(raw){
    const stored=JSON.parse(raw);
    const fresh=stored&&Date.now()-Number(stored.ts||0)<30000;
    if(fresh&&stored.plugin_id===currentId){
     if(!payload){
      const clean=sanitizeDeepLink(stored.deep_link);
      if(clean){
       payload={
        plugin_id:currentId,
        page:stored.page||'',
        title:stored.title||'',
        kind:stored.kind||'',
        deep_link:clean,
       };
      }
     }
     sessionStorage.removeItem(PENDING_DEEPLINK_KEY);
    }else if(!fresh){
     sessionStorage.removeItem(PENDING_DEEPLINK_KEY);
    }
   }
  }catch{}
 }
 if(!payload){
  try{
   const params=new URLSearchParams(location.search||'');
   const reconstructed={};
   for(const [k,v] of params.entries()){
    if(TARGET_QUERY_KEYS.has(k)&&v.trim())reconstructed[k]=v.trim().slice(0,256);
   }
   if(Object.keys(reconstructed).length){
    const umo=(params.get('umo')||params.get('scope')||'').trim();
    if(umo){reconstructed.umo=umo;reconstructed.scope=umo;}
    payload={plugin_id:currentId||'',deep_link:reconstructed};
   }
  }catch{}
 }
 if(payload){
  deepLinkDelivered=true;
  window.dispatchEvent(new CustomEvent('dynamics-cmdk-deeplink',{detail:payload}));
 }
 return payload;
}

function refresh(automatic=false){
 if(!root||navigating)return Promise.resolve(false);
 if(loading)return refreshRequest;
 // Background polling must not replace a focused control or close an open matrix popover.
 if(automatic&&(root.contains(document.activeElement)||matrixOpen))return Promise.resolve(false);
 loading=true;
 root.setAttribute('aria-busy','true');
 const refreshButton=root.querySelector('.sp-refresh');
 if(refreshButton)refreshButton.disabled=true;
 if(!automatic)feedback(feedbackNode(),msg('正在读取互通状态…'),'loading');
 refreshRequest=(async()=>{
  try{
   const bridge=getBridge();
   if(!bridge)throw Error(tr('请从 AstrBot 控制台打开'));
   if(bridge.ready)await timed(bridge.ready());
   const raw=await timed(bridge.apiGet('series_presence',{}));
   if(raw?.status==='error'||raw?.ok===false)throw Error(raw.message||tr('读取失败'));
   const data=raw?.data??raw;
   if(!Array.isArray(data?.plugins))throw Error(tr('读取失败'));
   cached=data;
   lastSyncedAt=Date.now();
   render(data);
   window.dispatchEvent(new CustomEvent('dynamics-presence-updated',{detail:data}));
   consumeIncomingDeepLink(guessCurrentId(data));
   return true;
  }catch(error){
   feedback(feedbackNode(),error?.message||msg('互通状态读取失败'),'error');
   consumeIncomingDeepLink(guessCurrentId(cached));
   return false;
  }finally{
   loading=false;
   root.setAttribute('aria-busy','false');
   const button=root.querySelector('.sp-refresh');
   if(button)button.disabled=false;
  }
 })();
 return refreshRequest;
}

/* =========================================================================
 * GLOBAL CTRL+K COMMAND PALETTE
 * ========================================================================= */
let cmdkModal=null,cmdkInput=null,cmdkList=null,cmdkReturnFocus=null;
let cmdkItems=[],cmdkSelected=0,cmdkTimer=null,cmdkSeq=0,cmdkAbort=null;

function ensureCmdK(){
 if(cmdkModal)return cmdkModal;
 const backdrop=node('div','','sp-cmdk-backdrop');
 backdrop.id='dynamics-cmdk-modal';
 backdrop.hidden=true;
 backdrop.addEventListener('click',event=>{if(event.target===backdrop)closeCmdK();});

 const dialog=node('div','','sp-cmdk-dialog');
 dialog.setAttribute('role','dialog');
 dialog.setAttribute('aria-modal','true');
 setText(dialog,msg('全局快捷跳转与搜索'),'aria-label');

 const head=node('div','','sp-cmdk-head');
 head.append(icon('search',16));
 const input=document.createElement('input');
 input.type='search';
 input.className='sp-cmdk-input';
 input.autocomplete='off';
 input.spellcheck=false;
 setText(input,msg('搜索面板、群聊、记忆或指令…'),'placeholder');
 setText(input,msg('全局快捷跳转与搜索'),'aria-label');
 input.addEventListener('input',()=>handleCmdKQuery(input.value));
 input.addEventListener('keydown',handleCmdKKeydown);
 head.append(input,node('kbd','ESC','sp-cmdk-kbd'));

 const list=node('div','','sp-cmdk-list');
 list.setAttribute('role','listbox');

 const foot=node('div','','sp-cmdk-foot');
 foot.append(
  node('span',msg('↑↓ 选择 · Enter 打开 · Esc 关闭')),
  node('span',msg('输入关键词可跨插件检索群聊、记忆与提案')),
 );

 dialog.append(head,list,foot);
 backdrop.append(dialog);
 document.body.append(backdrop);

 cmdkModal=backdrop;
 cmdkInput=input;
 cmdkList=list;
 return backdrop;
}

function collectLocalCommands(){
 const items=[];
 const currentId=guessCurrentId(cached);
 const plugins=orderedPagePlugins(cached);

 // 1. Panels (from live presence, or static metadata fallback in standalone mode)
 if(plugins.length){
  for(const plugin of plugins){
   const meta=PLUGIN_META[plugin.id]||{icon:'sparkle',desc:''};
   const badgeText=plugin.badge?.label?`${plugin.badge.label} · `:'';
   const sub=plugin.pulse?.headline||(meta.desc?tr(meta.desc):'');
   items.push({
    group:tr('面板'),
    kind:tr('面板'),
    icon:meta.icon,
    title:String(pluginLabel(plugin)),
    subtitle:`${badgeText}${sub}`.trim(),
    disabled:Boolean(plugin.current||!plugin.can_open),
    action:()=>navigatePlugin(plugin),
   });
  }
 }else{
  for(const id of PLUGIN_ORDER){
   const meta=PLUGIN_META[id];
   items.push({
    group:tr('面板'),
    kind:tr('面板'),
    icon:meta.icon,
    title:tr(meta.name),
    subtitle:tr(meta.desc),
    disabled:id===currentId,
    action:()=>window.DynamicsSeriesNavigation.open(id).catch(()=>{}),
   });
  }
 }

 // 2. Current page tabs
 const seenTabs=new Set();
 const tabSelector='.series-tabs > button, .series-tabs > a, .ds-more-menu-panel > button, .ds-more-menu-panel > a, [role="tab"]';
 for(const el of document.querySelectorAll(tabSelector)){
  if(root?.contains(el)||el.disabled||el.hidden)continue;
  const label=(el.textContent||'').replace(/\s+/g,' ').trim();
  if(!label||seenTabs.has(label))continue;
  seenTabs.add(label);
  items.push({
   group:tr('当前页签'),
   kind:tr('当前页签'),
   icon:'tab',
   title:label,
   subtitle:PLUGIN_META[currentId]?tr(PLUGIN_META[currentId].name):document.title,
   action:()=>{
    const parentDetails=el.closest('details');
    if(parentDetails)parentDetails.open=false;
    el.click();
    el.focus?.({preventScroll:true});
   },
  });
 }

 // 3. Page-registered custom commands
 for(const provider of commandProviders){
  try{
   const provided=provider();
   if(Array.isArray(provided)){
    for(const entry of provided){
     if(!entry||!entry.title||typeof entry.action!=='function')continue;
     items.push({
      group:entry.group||tr('快捷动作'),
      kind:entry.kind||tr('快捷动作'),
      icon:entry.icon||'sparkle',
      title:String(entry.title),
      subtitle:String(entry.subtitle||''),
      action:entry.action,
     });
    }
   }
  }catch{}
 }

 // 4. Built-in quick actions
 items.push({
  group:tr('快捷动作'),
  kind:tr('快捷动作'),
  icon:document.documentElement.dataset.uiTheme==='night'?'sun':'moon',
  title:tr('切换日间 / 夜间主题'),
  subtitle:tr(document.documentElement.dataset.uiTheme==='night'?'切换到日间模式':'切换到夜间模式'),
  action:()=>{
   const btn=document.querySelector('#theme-toggle, .series-theme, .plugin-theme-toggle, .dw-theme');
   if(btn){btn.click();return;}
   const next=document.documentElement.dataset.uiTheme==='night'?'day':'night';
   document.documentElement.dataset.uiTheme=next;
   try{localStorage.setItem('chat_dynamics_ui',next);}catch{}
  },
 });

 if(getSharedScope()){
  items.push({
   group:tr('快捷动作'),
   kind:tr('快捷动作'),
   icon:'close',
   title:tr('清除当前跨面板会话筛选'),
   subtitle:formatScopeLabel(getSharedScope()),
   action:()=>setSharedScope('',{syncUrl:true,emit:true}),
  });
 }

 items.push({
  group:tr('快捷动作'),
  kind:tr('快捷动作'),
  icon:'refresh',
  title:tr('刷新全系列插件状态'),
  subtitle:tr('群间系列 · 8 面板实时状态'),
  action:()=>{refresh(false);},
 });

 return items;
}

function appendTimedOutNotice(timedOutPlugins){
 if(!cmdkList||!Array.isArray(timedOutPlugins)||!timedOutPlugins.length)return;
 const names=timedOutPlugins.map(id=>{
  const meta=PLUGIN_META[id];
  return meta?tr(meta.name):String(id);
 }).filter(Boolean);
 if(!names.length)return;
 cmdkList.append(node('div',msg('部分插件检索超时，已显示已返回结果：{0}',names.join('、')),'sp-cmdk-status sp-cmdk-partial'));
}

function renderCmdKList(items,{queryText='',searching=false,timedOutPlugins=[]}={}){
 if(!cmdkList)return;
 cmdkItems=items;
 if(cmdkSelected>=items.length)cmdkSelected=Math.max(0,items.length-1);
 cmdkList.replaceChildren();

 if(!items.length&&!searching){
  cmdkList.append(node('div',msg('没有找到与「{0}」匹配的结果',queryText),'sp-cmdk-empty'));
  appendTimedOutNotice(timedOutPlugins);
  return;
 }

 let lastGroup='';
 items.forEach((item,index)=>{
  if(item.group&&item.group!==lastGroup){
   lastGroup=item.group;
   cmdkList.append(node('div',item.group,'sp-cmdk-group'));
  }
  const btn=node('button','',`sp-cmdk-item${index===cmdkSelected?' is-selected':''}`);
  btn.type='button';
  btn.setAttribute('role','option');
  btn.setAttribute('aria-selected',String(index===cmdkSelected));
  btn.dataset.cmdkIdx=String(index);
  if(item.disabled)btn.disabled=true;

  const main=node('div','','sp-cmdk-item-main');
  const ico=node('span','','sp-cmdk-item-icon');
  ico.append(icon(item.icon||'sparkle',15));
  const text=node('div','','sp-cmdk-item-text');
  text.append(node('div',item.title,'sp-cmdk-item-title'));
  if(item.subtitle)text.append(node('div',item.subtitle,'sp-cmdk-item-sub'));
  main.append(ico,text);
  btn.append(main);
  if(item.kind)btn.append(node('span',item.kind,'sp-cmdk-kind'));

  btn.addEventListener('mouseenter',()=>{
   cmdkSelected=index;
   paintCmdKSelection();
  });
  btn.addEventListener('click',()=>activateCmdKItem(index));
  cmdkList.append(btn);
 });

 if(searching){
  cmdkList.append(node('div',msg('正在联合检索兄弟插件…'),'sp-cmdk-status'));
 }else{
  appendTimedOutNotice(timedOutPlugins);
 }
}

function paintCmdKSelection(){
 if(!cmdkList)return;
 for(const el of cmdkList.querySelectorAll('.sp-cmdk-item')){
  const active=Number(el.dataset.cmdkIdx)===cmdkSelected;
  el.classList.toggle('is-selected',active);
  el.setAttribute('aria-selected',String(active));
  if(active)el.scrollIntoView?.({block:'nearest'});
 }
}

function activateCmdKItem(index){
 const item=cmdkItems[index];
 if(!item||item.disabled)return;
 closeCmdK();
 Promise.resolve().then(()=>item.action()).catch(()=>{});
}

function handleCmdKKeydown(event){
 if(event.key==='Escape'){event.preventDefault();closeCmdK();return;}
 if(event.key==='ArrowDown'){
  event.preventDefault();
  if(cmdkItems.length){cmdkSelected=(cmdkSelected+1)%cmdkItems.length;paintCmdKSelection();}
  return;
 }
 if(event.key==='ArrowUp'){
  event.preventDefault();
  if(cmdkItems.length){cmdkSelected=(cmdkSelected-1+cmdkItems.length)%cmdkItems.length;paintCmdKSelection();}
  return;
 }
 if(event.key==='Enter'){
  event.preventDefault();
  activateCmdKItem(cmdkSelected);
 }
}

async function fetchFederatedSearch(q,signal=null){
 const empty={items:[],timedOutPlugins:[]};
 const bridge=getBridge();
 if(!bridge?.apiGet||signal?.aborted)return empty;
 const currentId=guessCurrentId(cached);
 const hub=cached?.plugins?.find?.(p=>p.id===HUB_ID);
 // Avoid pointless remote calls if chat_dynamics is explicitly known missing/disabled.
 if(currentId!==HUB_ID&&hub&&(hub.state==='missing'||hub.state==='disabled'||hub.state==='unloaded'))return empty;
 const endpoint=currentId===HUB_ID
  ?'series_search'
  :`series_workspace/panel/${HUB_ID}/series_search`;
 try{
  const raw=await timed(
   bridge.apiGet(endpoint,{q,scope:getSharedScope()||'',limit:12}),
   FEDERATED_SEARCH_TIMEOUT_MS,
   signal,
  );
  if(signal?.aborted||raw?.status==='error'||raw?.ok===false)return empty;
  const data=raw?.data??raw;
  const list=Array.isArray(data?.items)?data.items:[];
  const timedOutPlugins=Array.isArray(data?.timed_out_plugins)
   ?data.timed_out_plugins.map(v=>String(v||'').trim()).filter(Boolean)
   :[];
  const items=list.map(entry=>{
   const pluginId=String(entry.plugin_id||'');
   const meta=PLUGIN_META[pluginId]||{icon:'search',page:''};
   return {
    group:tr('跨插件搜索'),
    kind:String(entry.kind||tr('跨插件搜索')),
    icon:meta.icon,
    title:String(entry.title||''),
    subtitle:String(entry.subtitle||''),
    action:()=>{
     if(pluginId&&pluginId===currentId){
      window.dispatchEvent(new CustomEvent('dynamics-cmdk-deeplink',{detail:entry}));
      return;
     }
     const target=cached?.plugins?.find?.(p=>p.id===pluginId)||{
      id:pluginId,
      page:entry.page||meta.page,
      can_open:Boolean(pluginId&&(entry.page||meta.page)),
      current:false,
     };
     const deepLink=sanitizeDeepLink(entry.deep_link);
     const scope=(deepLink&&typeof deepLink==='object'&&(deepLink.scope||deepLink.umo))
      ?String(deepLink.scope||deepLink.umo)
      :undefined;
     return navigatePlugin(target,undefined,{scope,deepLink,entry});
    },
   };
  }).filter(item=>item.title);
  return {items,timedOutPlugins};
 }catch{
  return empty;
 }
}

function handleCmdKQuery(rawQuery){
 clearTimeout(cmdkTimer);
 const seq=++cmdkSeq;
 if(cmdkAbort){cmdkAbort.abort();cmdkAbort=null;}
 const q=String(rawQuery||'').trim();
 const term=q.toLowerCase();
 const allLocal=collectLocalCommands();
 const matched=term
  ?allLocal.filter(item=>`${item.title} ${item.subtitle} ${item.kind}`.toLowerCase().includes(term))
  :allLocal;
 cmdkSelected=0;
 if(!q){
  renderCmdKList(matched,{queryText:q,searching:false});
  return;
 }
 const controller=typeof AbortController==='function'?new AbortController():null;
 cmdkAbort=controller;
 renderCmdKList(matched,{queryText:q,searching:Boolean(getBridge()?.apiGet)});
 cmdkTimer=setTimeout(async()=>{
  if(seq!==cmdkSeq||controller?.signal?.aborted||!cmdkModal||cmdkModal.hidden)return;
  const {items:remote,timedOutPlugins}=await fetchFederatedSearch(q,controller?.signal);
  if(seq!==cmdkSeq||controller?.signal?.aborted||!cmdkModal||cmdkModal.hidden||String(cmdkInput?.value||'').trim()!==q)return;
  renderCmdKList([...matched,...remote],{queryText:q,searching:false,timedOutPlugins});
 },140);
}

export function openCmdK(){
 setMatrixOpen(false);
 const modal=ensureCmdK();
 cmdkReturnFocus=document.activeElement;
 modal.hidden=false;
 cmdkInput.value='';
 handleCmdKQuery('');
 setTimeout(()=>cmdkInput?.focus?.({preventScroll:true}),10);
}

export function closeCmdK(){
 clearTimeout(cmdkTimer);
 cmdkSeq++;
 if(cmdkAbort){cmdkAbort.abort();cmdkAbort=null;}
 if(cmdkModal)cmdkModal.hidden=true;
 cmdkReturnFocus?.focus?.({preventScroll:true});
 cmdkReturnFocus=null;
}

/* =========================================================================
 * BOOTSTRAP & BACKWARD-COMPATIBLE GLOBAL CONTRACT
 * ========================================================================= */
if(root){
 render(null);
 refresh(false);
 setInterval(()=>{if(!document.hidden)refresh(true);},30000);
 window.addEventListener('dynamics-navigation-contextchange',paintScope);
 window.addEventListener('dynamics-scopechange',paintScope);
 window.addEventListener('series-languagechange',paintScope);
 document.addEventListener('click',event=>{
  if(matrixOpen&&!root.querySelector('.sp-matrix-anchor')?.contains(event.target)){
   setMatrixOpen(false);
  }
  const ex=root.querySelector('.sp-extra[open]');
  if(ex&&!ex.contains(event.target)&&!root.querySelector('.sp-matrix-popover')?.contains(event.target)){
   ex.open=false;
  }
 });
}

document.addEventListener('keydown',event=>{
 if((event.ctrlKey||event.metaKey)&&!event.altKey&&String(event.key||'').toLowerCase()==='k'){
  event.preventDefault();
  if(cmdkModal&&!cmdkModal.hidden)closeCmdK();
  else openCmdK();
  return;
 }
 if(event.key==='Escape'){
  if(cmdkModal&&!cmdkModal.hidden){event.preventDefault();closeCmdK();return;}
  if(matrixOpen){event.preventDefault();setMatrixOpen(false,{restoreFocus:true});}
 }
});

// Cabin objects and sibling panels reuse the exact same availability, dirty-edit and host-navigation checks.
window.DynamicsSeriesNavigation={
 async open(id,transition){
  if(await refresh(false)!==true)throw Error(tr('互通状态读取失败'));
  const target=cached?.plugins?.find(plugin=>plugin.id===id);
  if(!target?.can_open||target.current)throw Error(tr('此插件当前不可打开'));
  const button=root?[...root.querySelectorAll('[data-sp-focus]')].find(n=>n.dataset.spFocus===id):null;
  if(button&&!button.disabled&&button.seriesNavigate)return button.seriesNavigate(transition);
  return navigatePlugin(target,transition);
 },
 openCmdK,
 closeCmdK,
 toggleMatrix(force){setMatrixOpen(force===undefined?!matrixOpen:Boolean(force));},
 refresh,
 getPresence(){return cached;},
 registerCommands(providerFn){
  if(typeof providerFn!=='function')return ()=>{};
  commandProviders.add(providerFn);
  return ()=>commandProviders.delete(providerFn);
 },
};
