/* Shared UI contract v2.0. Translates declared interface copy only, never record bodies. */
import {messages,loadLocaleMessages,isLocaleLoaded} from './series-messages.js';
export {loadLocaleMessages,isLocaleLoaded};

const languages=['zh','en','ja','ru'],bindings=new Map(),pendingLocaleSync=new Map();
const LANG_KEY='dynamics_series_lang',LEGACY_LANG_KEY='lmem_lang';
const SVG_NS='http://www.w3.org/2000/svg';
const normalize=value=>String(value||'').toLowerCase().split(/[-_]/)[0];
const stored=()=>{try{return localStorage.getItem(LANG_KEY)||localStorage.getItem(LEGACY_LANG_KEY);}catch{return null;}};
const query=new URLSearchParams(location.search);
const queryLanguage=normalize(query.get('lang'));
const bridge=()=>(window.DynamicsPanelBridge||window.AstrBotPluginPage||window.AstrBotPluginView);
const nativeLanguage=typeof window.getLanguage==='function';
let context={};try{context=bridge()?.getContext?.()||{};}catch{}
let language=[queryLanguage,normalize(context.localeOverride),normalize(context.locale),stored(),normalize(document.documentElement.lang),normalize(navigator.language),'zh'].find(v=>languages.includes(v));
let localOverride=languages.includes(queryLanguage),started=false;
const originalSet=window.setLanguage;

const LOCALE_TAGS={zh:'zh-CN',en:'en-US',ja:'ja-JP',ru:'ru-RU'};

function syncLocaleMeta(){
 if(typeof document==='undefined')return;
 const cur=getLanguage();
 document.documentElement.lang=LOCALE_TAGS[cur]||'zh-CN';
 for(const select of document.querySelectorAll?.('[data-series-language]')||[])select.value=cur;
}

function syncExternalTranslations(){
 if(typeof window==='undefined'||typeof window.registerTranslations!=='function')return;
 const cur=getLanguage(),entries={};
 for(const [key,value] of Object.entries(messages)){
  entries['series.'+key]=typeof value==='string'?{zh:key,[cur]:value}:{zh:key,...value};
 }
 if(Object.keys(entries).length)window.registerTranslations(entries);
}

function ensureLocale(lang){
 const code=normalize(lang);
 if(!languages.includes(code)||isLocaleLoaded(code)){
  syncExternalTranslations();
  return Promise.resolve(messages);
 }
 if(pendingLocaleSync.has(code))return pendingLocaleSync.get(code);
 const task=loadLocaleMessages(code).then(()=>{
  if(typeof window==='undefined'||typeof document==='undefined')return messages;
  syncExternalTranslations();
  if(started&&getLanguage()===code)repaint();
  return messages;
 }).finally(()=>{pendingLocaleSync.delete(code);});
 pendingLocaleSync.set(code,task);
 return task;
}

if(Object.keys(messages).length)syncExternalTranslations();
if(languages.includes(language)&&!isLocaleLoaded(language))ensureLocale(language);

export const SERIES_ICONS={
 home:['M3 10.5L12 3l9 7.5','M5.5 9.5V20h13V9.5','M9.5 20v-5.5h5V20'],
 world:['M3 10.5L12 3l9 7.5','M5.5 9.5V20h13V9.5','M9.5 20v-5.5h5V20'],
 chat:['M21 11.5a8.5 8.5 0 0 1-12.4 7.6L3 20.5l1.4-5.2A8.5 8.5 0 1 1 21 11.5Z','M8.5 11.5h.01','M12 11.5h.01','M15.5 11.5h.01'],
 learning:['M2.5 5h6a3.5 3.5 0 0 1 3.5 3.5V20a2.5 2.5 0 0 0-2.5-2.5h-7Z','M21.5 5h-6A3.5 3.5 0 0 0 12 8.5V20a2.5 2.5 0 0 1 2.5-2.5h7Z'],
 memory:['M4.5 19.5A2.5 2.5 0 0 1 7 17h12.5','M7 3h12.5v18H7a2.5 2.5 0 0 1-2.5-2.5v-13A2.5 2.5 0 0 1 7 3Z','M9 8h6','M9 12h4'],
 horizons:['M12 2.5a9.5 9.5 0 1 0 0 19a9.5 9.5 0 0 0 0-19Z','M12 2.5a14 14 0 0 1 0 19','M12 2.5a14 14 0 0 0 0 19','M3 12h18'],
 games:['M6.5 7h11a4 4 0 0 1 3.9 4.8l-.8 4A3 3 0 0 1 17.7 18a2.5 2.5 0 0 1-2-1l-1.2-1.5h-5L8.3 17a2.5 2.5 0 0 1-2 1a3 3 0 0 1-2.9-2.2l-.8-4A4 4 0 0 1 6.5 7Z','M8 11.5h3','M9.5 10v3','M15.5 10.5h.01','M17.5 12.5h.01'],
 visual:['M3.5 5.5A2 2 0 0 1 5.5 3.5h13a2 2 0 0 1 2 2v13a2 2 0 0 1-2 2h-13a2 2 0 0 1-2-2Z','M8.5 9.5a1 1 0 1 0 0-2a1 1 0 0 0 0 2Z','M20.5 15.5l-4.5-4.5L6.5 20.5'],
 chronicle:['M5 4h11a3 3 0 0 1 3 3v13l-4-2.5L11 20l-4-2.5L5 19V4Z','M8.5 8.5h7','M8.5 12h5'],
 search:['M11 4.5a6.5 6.5 0 1 0 0 13a6.5 6.5 0 0 0 0-13Z','M16 16l4.5 4.5'],
 refresh:['M20 12a8 8 0 1 1-2.3-5.6','M20 4v4.5h-4.5'],
 chevron:['M6 9l6 6l6-6'],
 info:['M12 3a9 9 0 1 0 0 18a9 9 0 0 0 0-18Z','M12 11v5','M12 8h.01'],
 more:['M6 12h.01','M12 12h.01','M18 12h.01'],
 sun:['M12 7.5a4.5 4.5 0 1 0 0 9a4.5 4.5 0 0 0 0-9Z','M12 2.5v2','M12 19.5v2','M4.9 4.9l1.4 1.4','M17.7 17.7l1.4 1.4','M2.5 12h2','M19.5 12h2','M4.9 19.1l1.4-1.4','M17.7 6.3l1.4-1.4'],
 moon:['M19.5 14.5A7.5 7.5 0 0 1 9.5 4.5a7.5 7.5 0 1 0 10 10Z'],
 check:['M5 12.5l4.5 4.5L19 7.5'],
 close:['M6 6l12 12','M18 6L6 18'],
 alert:['M12 3.5L2.5 19.5h19L12 3.5Z','M12 10v4','M12 16.8h.01'],
 sparkle:['M12 3l2 6l6 2l-6 2l-2 6l-2-6l-6-2l6-2Z'],
 sliders:['M4 7h16','M4 17h16','M9 4.5v5','M15 14.5v5'],
 camera:['M4 8.5A2 2 0 0 1 6 6.5h2.2l1.6-2h4.4l1.6 2H18a2 2 0 0 1 2 2v9a2 2 0 0 1-2 2H6a2 2 0 0 1-2-2Z','M12 10a3.5 3.5 0 1 0 0 7a3.5 3.5 0 0 0 0-7Z'],
 clock:['M12 3a9 9 0 1 0 0 18a9 9 0 0 0 0-18Z','M12 7.5V12l3 2'],
 tab:['M4 6a2 2 0 0 1 2-2h12a2 2 0 0 1 2 2v12a2 2 0 0 1-2 2H6a2 2 0 0 1-2-2Z','M4 9.5h16'],
};

export function icon(name,size=16,cls=''){
 const paths=SERIES_ICONS[name]||SERIES_ICONS.sparkle;
 if(typeof document.createElementNS!=='function'){
  const fallback=document.createElement('span');
  if(cls)fallback.className=cls;
  fallback.setAttribute('aria-hidden','true');
  return fallback;
 }
 const svg=document.createElementNS(SVG_NS,'svg');
 svg.setAttribute('viewBox','0 0 24 24');
 svg.setAttribute('width',String(size));
 svg.setAttribute('height',String(size));
 svg.setAttribute('fill','none');
 svg.setAttribute('stroke','currentColor');
 svg.setAttribute('stroke-width','1.75');
 svg.setAttribute('stroke-linecap','round');
 svg.setAttribute('stroke-linejoin','round');
 svg.setAttribute('aria-hidden','true');
 if(cls)svg.setAttribute('class',cls);
 for(const d of paths){
  const p=document.createElementNS(SVG_NS,'path');
  p.setAttribute('d',d);
  svg.append(p);
 }
 return svg;
}

export function getLanguage(){return nativeLanguage?window.getLanguage():language;}

export function tr(key,...args){
 const lang=getLanguage(),values=messages[key],locale={zh:'zh-CN',en:'en-US',ja:'ja-JP',ru:'ru-RU'}[lang];
 const picked=typeof values==='string'?(lang==='zh'?key:values):(values?.[lang]||values?.zh);
 const result=context.i18n?.[locale]?.series_ui?.[key]||picked||key;
 return result.replace(/\{(\d+)\}/g,(_,i)=>String(args[Number(i)]??''));
}

export function msg(key,...args){
 if(key?.seriesMessage)return key;
 return {seriesMessage:true,key,args,toString(){return tr(key,...args);}};
}

export function setText(node,value,attribute=null){
 if(!node)return;
 let fields=bindings.get(node);
 if(!fields&&value?.seriesMessage){fields=new Map();bindings.set(node,fields);}
 if(value?.seriesMessage)fields.set(attribute,value);else fields?.delete(attribute);
 const text=String(value??'');
 if(attribute){if(node.getAttribute(attribute)!==text)node.setAttribute(attribute,text);}
 else if(node.textContent!==text)node.textContent=text;
}

export function apply(root=document){
 for(const name of ['text','aria-label','title','placeholder']){
  const binding='data-series-'+name,selector='['+binding+']';
  const nodes=[...(root.querySelectorAll?.(selector)||[])];
  if(root.matches?.(selector))nodes.unshift(root);
  for(const node of nodes)setText(node,msg(node.getAttribute(binding)),name==='text'?null:name);
 }
 const i18nSelector='[data-series-i18n]:not([data-series-text])';
 const legacyNodes=[...(root.querySelectorAll?.(i18nSelector)||[])];
 if(root.matches?.(i18nSelector))legacyNodes.unshift(root);
 for(const node of legacyNodes)setText(node,msg(node.getAttribute('data-series-i18n')));
}

function repaint(){
 document.documentElement.lang={zh:'zh-CN',en:'en-US',ja:'ja-JP',ru:'ru-RU'}[getLanguage()]||'zh-CN';
 for(const [node,fields] of bindings){
  if(!node.isConnected){bindings.delete(node);continue;}
  for(const [attr,value] of fields)setText(node,value,attr);
 }
 for(const select of document.querySelectorAll('[data-series-language]'))select.value=getLanguage();
 themeLabels();
 window.dispatchEvent(new CustomEvent('series-languagechange',{detail:{lang:getLanguage()}}));
}

export function setLanguage(value,{persist=true}={}){
 value=normalize(value);if(!languages.includes(value))return Promise.resolve(messages);
 if(persist){localOverride=true;updateHostContext({locale:value});}
 if(nativeLanguage&&getLanguage()!==value){
  originalSet(value,{persist});
  syncLocaleMeta();
  return ensureLocale(value);
 }
 if(!nativeLanguage)language=value;
 if(persist&&!bridge()?.setNavigationContext)try{localStorage.setItem(LANG_KEY,value);}catch{}
 syncLocaleMeta();
 if(isLocaleLoaded(value)){
  syncExternalTranslations();
  repaint();
  return Promise.resolve(messages);
 }
 return ensureLocale(value);
}

if(!nativeLanguage){window.getLanguage=getLanguage;window.setLanguage=setLanguage;}

window.addEventListener('languagechange',event=>{
 if(!nativeLanguage)return;
 if(event.detail?.source==='user'){
  localOverride=true;
  updateHostContext({locale:getLanguage()});
 }
 const cur=getLanguage();
 syncLocaleMeta();
 if(isLocaleLoaded(cur)){syncExternalTranslations();repaint();}
 else ensureLocale(cur);
});

window.addEventListener('storage',event=>{
 if((event.key===LANG_KEY||event.key===LEGACY_LANG_KEY)&&languages.includes(event.newValue))setLanguage(event.newValue,{persist:false});
});

let shellMenusBound=false;
export function initShellMenus(root=document){
 if(shellMenusBound||typeof document.addEventListener!=='function')return;
 shellMenusBound=true;
 document.addEventListener('click',event=>{
  const target=event.target;
  for(const menu of document.querySelectorAll?.('.ds-info-pop[open], .ds-more-menu[open]')||[]){
   if(!menu.contains?.(target))menu.open=false;
   else if(menu.classList?.contains('ds-more-menu')&&target?.closest?.('.ds-more-menu-panel button, .ds-more-menu-panel a')){
    menu.open=false;
   }
  }
 });
 document.addEventListener('keydown',event=>{
  if(event.key!=='Escape')return;
  for(const menu of document.querySelectorAll?.('.ds-info-pop[open], .ds-more-menu[open]')||[])menu.open=false;
 });
 if(root!==document)apply(root);
}

export function createInfoPopover({label=msg('边界说明'),title='',body='',align='right'}={}){
 const details=document.createElement('details');
 details.className='ds-info-pop';
 const summary=document.createElement('summary');
 const dot=document.createElement('span');dot.className='ds-info-pop-dot';dot.textContent='i';dot.setAttribute('aria-hidden','true');
 const text=document.createElement('span');setText(text,label);
 summary.append(dot,text);
 const panel=document.createElement('div');
 panel.className='ds-info-pop-body';
 if(align==='left')panel.dataset.align='left';
 if(title){const heading=document.createElement('div');heading.className='ds-info-pop-title';setText(heading,title);panel.append(heading);}
 const paragraphs=Array.isArray(body)?body:[body];
 for(const item of paragraphs){
  if(!item)continue;
  const p=document.createElement('p');
  setText(p,item);
  panel.append(p);
 }
 details.append(summary,panel);
 return details;
}

export function createOverflowMenu({label=msg('更多与运维'),items=[]}={}){
 const details=document.createElement('details');
 details.className='ds-more-menu';
 const summary=document.createElement('summary');
 const labelEl=document.createElement('span');setText(labelEl,label);
 summary.append(labelEl,icon('chevron',13));
 const panel=document.createElement('div');
 panel.className='ds-more-menu-panel';
 panel.setAttribute('role','menu');
 for(const item of items){
  const btn=document.createElement('button');
  btn.type='button';
  btn.setAttribute('role','menuitem');
  if(item.tab)btn.dataset.tab=item.tab;
  if(item.id)btn.id=item.id;
  if(item.className)btn.className=item.className;
  const caption=document.createElement('span');setText(caption,item.label||'');
  btn.append(caption);
  if(item.badge){
   const badge=document.createElement('span');
   badge.className='ds-badge ds-badge-muted';
   setText(badge,item.badge);
   btn.append(badge);
  }
  if(typeof item.onClick==='function')btn.addEventListener('click',event=>{details.open=false;item.onClick(event);});
  panel.append(btn);
 }
 details.append(summary,panel);
 return details;
}

function start(){
 started=true;
 if(nativeLanguage&&getLanguage()!==language)originalSet(language,{persist:false});
 apply();initShellMenus();
 const cur=getLanguage();
 syncLocaleMeta();
 if(isLocaleLoaded(cur)){syncExternalTranslations();repaint();}
 else ensureLocale(cur);
 let uiTheme=document.documentElement.dataset.uiTheme;
 new MutationObserver(records=>{
  for(const record of records){
   if(record.type!=='childList')continue;
   for(const node of record.addedNodes){
    if(node.nodeType!==1)continue;
    apply(node);
    themeLabels(node);
   }
  }
 }).observe(document.body,{childList:true,subtree:true});
 new MutationObserver(()=>{
  const next=document.documentElement.dataset.uiTheme;
  if(next!==uiTheme&&['day','night'].includes(next)){
   uiTheme=next;
   updateHostContext({ui:next});
  }
  themeLabels();
 }).observe(document.documentElement,{attributes:true,attributeFilter:['data-ui-theme']});
 try{bridge()?.onContext?.(ctx=>{
  context={...context,...ctx};
  const nextLanguage=normalize(ctx?.localeOverride||ctx?.locale);
  if((ctx?.localeOverride||!localOverride)&&languages.includes(nextLanguage)&&nextLanguage!==getLanguage()){
   setLanguage(nextLanguage,{persist:false});
  }else repaint();
  if(typeof ctx?.scope==='string'&&ctx.scope!==sharedScope){
   sharedScope=ctx.scope;
   emitScope();
  }
 });}catch{}
}

function themeLabels(root=document){
 const night=document.documentElement.dataset.uiTheme==='night';
 const matching=selector=>[
  ...(root.matches?.(selector)?[root]:[]),
  ...(root.querySelectorAll?.(selector)||[]),
 ];
 for(const node of matching('.dw-theme-label,#theme-label,.ui-theme-label'))setText(node,msg(night?'夜间模式':'日间模式'));
 for(const node of matching('.dw-theme,#theme-toggle,.plugin-theme-toggle,.series-theme[aria-pressed]')){
  setText(node,msg(night?'切换到日间模式':'切换到夜间模式'),'aria-label');
 }
}

export function languagePicker(){
 const label=document.createElement('label');label.className='sp-language';
 const text=document.createElement('span');setText(text,msg('语言'));label.append(text);
 const select=document.createElement('select');select.dataset.seriesLanguage='';setText(select,msg('语言'),'aria-label');
 for(const [value,name] of [['zh','中文'],['en','English'],['ja','日本語'],['ru','Русский']]){
  const option=document.createElement('option');option.value=value;option.textContent=name;select.append(option);
 }
 select.value=getLanguage();select.addEventListener('change',()=>setLanguage(select.value));label.append(select);
 return label;
}

export function feedback(node,message,state='info'){
 if(!node)return;
 setText(node,message);
 node.dataset.uiState=state;
 node.setAttribute('role',state==='error'?'alert':'status');
 node.setAttribute('aria-live',state==='error'?'assertive':'polite');
}

let sharedScope=(query.get('umo')??context.scope??'').trim();
const emitScope=()=>window.dispatchEvent(new CustomEvent('dynamics-scopechange',{detail:{umo:sharedScope}}));
const emitSharedContext=()=>window.dispatchEvent(new CustomEvent('dynamics-navigation-contextchange'));
function updateHostContext(options){
 context={...context,...options};
 try{bridge()?.setNavigationContext?.(options)?.catch(()=>{});}catch{}
}

export const getSharedScope=()=>sharedScope;
export function setSharedScope(umo,{syncUrl=false,emit=true}={}){
 const clean=String(umo??'').trim();
 sharedScope=clean;
 updateHostContext({scope:clean});
 if(syncUrl){try{const u=new URL(location.href);if(clean)u.searchParams.set('umo',clean);else u.searchParams.delete('umo');history.replaceState(history.state,'',u);}catch{}}
 if(emit)emitScope();
 emitSharedContext();
 return clean;
}

export function formatScopeLabel(umo){
 const clean=String(umo??'').trim();if(!clean)return '';
 const parts=clean.split(':');
 if(parts.length>=3){
  const kind=parts[1].toLowerCase().includes('group')?tr('群聊'):parts[1].toLowerCase().includes('friend')||parts[1].toLowerCase().includes('private')?tr('私聊'):parts[0];
  return `${kind} · ${parts.slice(2).join(':')}`;
 }
 return clean;
}

let confirmation;
function showDialog(message,{confirmLabel=msg('继续'),cancelLabel=msg('取消'),danger=false,initialValue='',placeholder=''}={},isPrompt=false){
 if(confirmation)return Promise.resolve(isPrompt?null:false);
 const focus=document.activeElement,dialog=document.createElement('dialog');dialog.className='series-confirm';
 dialog.setAttribute('aria-labelledby','series-dialog-title');
 dialog.setAttribute('aria-describedby','series-dialog-message');
 const title=document.createElement('h2');title.id='series-dialog-title';setText(title,msg('确认操作'));
 const text=document.createElement(isPrompt?'label':'p');text.id='series-dialog-message';setText(text,message);
 const form=document.createElement('form'),footer=document.createElement('footer');
 const cancel=document.createElement('button'),accept=document.createElement('button');
 const input=isPrompt?document.createElement('input'):null;
 if(input){input.id='series-dialog-input';input.value=initialValue;setText(input,placeholder,'placeholder');text.htmlFor=input.id;}
 cancel.type='button';accept.type='submit';
 cancel.className='wb-button';accept.className=`wb-button primary${danger?' danger':''}`;
 setText(cancel,cancelLabel);setText(accept,confirmLabel);
 cancel.addEventListener('click',()=>dialog.close('cancel'));
 form.addEventListener('submit',event=>{event.preventDefault();dialog.close('continue');});
 footer.append(cancel,accept);form.append(text);if(input)form.append(input);form.append(footer);
 dialog.append(title,form);document.body.append(dialog);
 confirmation=new Promise(resolve=>{
  dialog.addEventListener('close',()=>{
   const yes=dialog.returnValue==='continue';
   dialog.remove();confirmation=null;focus?.focus?.({preventScroll:true});
   resolve(isPrompt?(yes?input.value:null):yes);
  },{once:true});
 });
 dialog.showModal();
 (input||cancel).focus();input?.select();
 return confirmation;
}

export const confirmAction=(message,options)=>showDialog(message,options);
export const promptAction=(message,options)=>showDialog(message,options,true);

window.DynamicsUI={
 tr,msg,setText,apply,getLanguage,setLanguage,loadLocaleMessages,feedback,
 getSharedScope,setSharedScope,formatScopeLabel,
 confirm:confirmAction,prompt:promptAction,
 icon,icons:SERIES_ICONS,createInfoPopover,createOverflowMenu,initShellMenus,
};

if(document.readyState==='loading')document.addEventListener('DOMContentLoaded',start,{once:true});else start();
setInterval(()=>{for(const [node] of bindings)if(!node.isConnected)bindings.delete(node);},30000);
