/* Shared UI contract. Translates declared interface copy only, never record bodies. */
import {messages} from './series-messages.js';
const languages=['zh','en','ja','ru'],bindings=new Map();
const LANG_KEY='dynamics_series_lang',LEGACY_LANG_KEY='lmem_lang';
const normalize=value=>String(value||'').toLowerCase().split(/[-_]/)[0];
const stored=()=>{try{return localStorage.getItem(LANG_KEY)||localStorage.getItem(LEGACY_LANG_KEY);}catch{return null;}};
const query=new URLSearchParams(location.search);
const queryLanguage=normalize(query.get('lang'));
const bridge=()=>(window.DynamicsPanelBridge || window.AstrBotPluginPage||window.AstrBotPluginView);
const nativeLanguage=typeof window.getLanguage==='function';
let context={};try{context=bridge()?.getContext?.()||{};}catch{}
let language=[queryLanguage,normalize(context.localeOverride),normalize(context.locale),stored(),normalize(document.documentElement.lang),normalize(navigator.language),'zh'].find(v=>languages.includes(v));
let localOverride=languages.includes(queryLanguage);
const originalSet=window.setLanguage;
if(window.registerTranslations)window.registerTranslations(Object.fromEntries(Object.entries(messages).map(([key,value])=>['series.'+key,value])));
export const getLanguage=()=>nativeLanguage?window.getLanguage():language;
export function tr(key,...args){
 const values=messages[key],locale={zh:'zh-CN',en:'en-US',ja:'ja-JP',ru:'ru-RU'}[getLanguage()];let result=context.i18n?.[locale]?.series_ui?.[key]||values?.[getLanguage()]||values?.zh||key;
 return result.replace(/\{(\d+)\}/g,(_,i)=>String(args[Number(i)]??''));
}
export function msg(key,...args){if(key?.seriesMessage)return key;return {seriesMessage:true,key,args,toString(){return tr(key,...args);}};}
export function setText(node,value,attribute=null){
 if(!node)return;
 let fields=bindings.get(node);if(!fields&&value?.seriesMessage){fields=new Map();bindings.set(node,fields);}
 if(value?.seriesMessage)fields.set(attribute,value);else fields?.delete(attribute);
 const text=String(value??'');
 if(attribute){if(node.getAttribute(attribute)!==text)node.setAttribute(attribute,text);}
 else if(node.textContent!==text)node.textContent=text;
}
export function apply(root=document){
 for(const name of ['text','aria-label','title','placeholder']){
  const binding='data-series-'+name,selector='['+binding+']';
  const nodes=[...root.querySelectorAll(selector)];
  if(root.matches?.(selector))nodes.unshift(root);
  for(const node of nodes)setText(node,msg(node.getAttribute(binding)),name==='text'?null:name);
 }
}
function repaint(){
 document.documentElement.lang={zh:'zh-CN',en:'en-US',ja:'ja-JP',ru:'ru-RU'}[getLanguage()]||'zh-CN';
 for(const [node,fields] of bindings){if(!node.isConnected){bindings.delete(node);continue;}for(const [attr,value] of fields)setText(node,value,attr);}
 for(const select of document.querySelectorAll('[data-series-language]'))select.value=getLanguage();
 themeLabels();
 window.dispatchEvent(new CustomEvent('series-languagechange',{detail:{lang:getLanguage()}}));
}
export function setLanguage(value,{persist=true}={}){
 value=normalize(value);if(!languages.includes(value))return;
 if(persist){localOverride=true;updateHostContext({locale:value});}
 if(nativeLanguage&&getLanguage()!==value){originalSet(value,{persist});return;}
 if(!nativeLanguage)language=value;
 if(persist&&!bridge()?.setNavigationContext)try{localStorage.setItem(LANG_KEY,value);}catch{}
 repaint();
}
if(!nativeLanguage){window.getLanguage=getLanguage;window.setLanguage=setLanguage;}
window.addEventListener('languagechange',event=>{
 if(!nativeLanguage)return;
 if(event.detail?.source==='user'){
  localOverride=true;
  updateHostContext({locale:getLanguage()});
 }
 repaint();
});
window.addEventListener('storage',event=>{if((event.key===LANG_KEY||event.key===LEGACY_LANG_KEY)&&languages.includes(event.newValue))setLanguage(event.newValue,{persist:false});});
function start(){
 if(nativeLanguage&&getLanguage()!==language)originalSet(language,{persist:false});
 apply();repaint();
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
  ...root.querySelectorAll(selector),
 ];
 for(const node of matching('.dw-theme-label,#theme-label,.ui-theme-label'))setText(node,msg(night?'夜间模式':'日间模式'));
 for(const node of matching('.dw-theme,#theme-toggle,.plugin-theme-toggle,.series-theme[aria-pressed]')){
  setText(node,msg(night?'切换到日间模式':'切换到夜间模式'),'aria-label');
 }
}
export function languagePicker(){
 const label=document.createElement('label');label.className='sp-language';const text=document.createElement('span');setText(text,msg('语言'));label.append(text);
 const select=document.createElement('select');select.dataset.seriesLanguage='';setText(select,msg('语言'),'aria-label');
 for(const [value,name] of [['zh','中文'],['en','English'],['ja','日本語'],['ru','Русский']]){const option=document.createElement('option');option.value=value;option.textContent=name;select.append(option);}
 select.value=getLanguage();select.addEventListener('change',()=>setLanguage(select.value));label.append(select);return label;
}
export function feedback(node,message,state='info'){if(!node)return;setText(node,message);node.dataset.uiState=state;node.setAttribute('role',state==='error'?'alert':'status');node.setAttribute('aria-live',state==='error'?'assertive':'polite');}
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
 if(syncUrl){try{const u=new URL(location.href);u.searchParams.set('umo',clean);history.replaceState(history.state,'',u);}catch{}}
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
window.DynamicsUI={tr,msg,setText,apply,getLanguage,setLanguage,feedback,getSharedScope,setSharedScope,formatScopeLabel,confirm:confirmAction,prompt:promptAction};

if(document.readyState==='loading')document.addEventListener('DOMContentLoaded',start,{once:true});else start();
setInterval(()=>{for(const [node] of bindings)if(!node.isConnected)bindings.delete(node);},30000);
