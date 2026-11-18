import {msg,tr,setText,feedback,languagePicker,confirmAction,getLanguage,getSharedScope,setSharedScope,formatScopeLabel} from './series-ui.js';
const root=document.getElementById('dynamics-series-panel');
const states={missing:'未安装',disabled:'已停用',unloaded:'未加载',conflict:'同名冲突',enabled:'已启用',ready:'已就绪',inactive:'未接入',not_ready:'未就绪',error:'状态异常'};
const names={
 astrbot_plugin_chat_dynamics:'对话主控',
 astrbot_plugin_dynamics_memory:'群间记忆',
 astrbot_plugin_dynamics_world:'群间世界',
 astrbot_plugin_dynamics_learning:'群间学习',
 astrbot_plugin_dynamics_games:'群间游戏',
 astrbot_plugin_dynamics_horizons:'群间见闻',
 astrbot_plugin_visual_media:'视觉工坊',
 astrbot_plugin_group_chronicle:'群文化沿革',
};
const WORLD_ID='astrbot_plugin_dynamics_world';
const SVG_NS='http://www.w3.org/2000/svg';
const node=(tag,text='',cls='')=>{const n=document.createElement(tag);if(cls)n.className=cls;if(text!=='')setText(n,text);return n;};
function svgIcon(paths){
 const svg=document.createElementNS(SVG_NS,'svg');
 svg.setAttribute('viewBox','0 0 16 16');svg.setAttribute('width','14');svg.setAttribute('height','14');
 svg.setAttribute('fill','none');svg.setAttribute('stroke','currentColor');svg.setAttribute('stroke-width','1.6');
 svg.setAttribute('stroke-linecap','round');svg.setAttribute('stroke-linejoin','round');svg.setAttribute('aria-hidden','true');
 for(const d of paths){const p=document.createElementNS(SVG_NS,'path');p.setAttribute('d',d);svg.append(p);}
 return svg;
}
let loading=false,navigating=false,edited=false,cached=null,refreshRequest,scopePill;
const getBridge=()=>window.AstrBotPluginPage||window.AstrBotPluginView;
for(const type of ['input','change'])document.addEventListener(type,event=>{
 const target=event.target;
 // Searches and filters are navigation state, not unsaved edits.
 if(event.isTrusted&&!root?.contains(target)&&!target.closest?.('dialog,[id*=filter],.toolbar,.console-controls,.review-controls,.creative-toolbar,.persona-bar,.blocklist-bar')&&target.matches?.('textarea,[contenteditable=true],form input:not([type=search])'))edited=true;
},true);
async function timed(promise){let timer;try{return await Promise.race([promise,new Promise((_,reject)=>{timer=setTimeout(()=>reject(Error(tr('请求超时，请刷新重试'))),8000);})]);}finally{clearTimeout(timer);}}
async function beforeNavigate(destination){
 if(window.ChatDynamicsTheme?.flush)await timed(window.ChatDynamicsTheme.flush());
 const handler=window.DynamicsBeforeNavigate||window.ChatDynamicsBeforeNavigate;
 if(typeof handler==='function')return (await handler(destination))!==false;
 return !edited||confirmAction(msg('此页有编辑操作。请确认已保存，再打开其他面板。'));
}
window.DynamicsSharedBeforeNavigate=beforeNavigate;
function paintScope(){
 if(!scopePill)return;
 const activeUmo=getSharedScope();
 scopePill.hidden=!activeUmo;
 setText(scopePill,msg('当前会话：{0}',activeUmo),'title');
 setText(scopePill,msg('当前会话：{0}',activeUmo),'aria-label');
 setText(scopePill.querySelector('.sp-scope-text'),formatScopeLabel(activeUmo));
}
function render(data){
 if(!root)return;
 const focused=root.contains(document.activeElement)?document.activeElement?.dataset.spFocus:null;
 const extraOpen=root.querySelector('.sp-extra')?.open===true;
 const linkScroll=root.querySelector('.sp-links')?.scrollLeft||0;
 const isWorld=data?.current?data.current===WORLD_ID:location.pathname.includes('/world/');
 root.dataset.spHost=isWorld?'world':'companion';
 root.replaceChildren();
 const bar=node('div','','sp-bar');
 const links=node('nav','','sp-links');setText(links,msg('群间系列面板互跳'),'aria-label');
 const note=node('p','','sp-feedback');note.setAttribute('role','status');note.setAttribute('aria-live','polite');
 const pagePlugins=(data?.plugins||[]).filter(p=>p.page&&(!p.association||p.current));
 const ordered=isWorld?pagePlugins:[...pagePlugins.filter(p=>p.id===WORLD_ID),...pagePlugins.filter(p=>p.id!==WORLD_ID)];
 if(!data)links.append(node('span',msg('互通插件'),'sp-title'));
 function pluginButton(plugin){
  const isHubReturn=!isWorld&&plugin.id===WORLD_ID;
  const label=isHubReturn?msg('返回群间小屋'):(names[plugin.id]?msg(names[plugin.id]):plugin.label);
  const button=node('button','',`sp-link sp-${plugin.state}${isHubReturn?' sp-home-hub':''}`);
  button.type='button';button.dataset.spFocus=plugin.id;
  if(isHubReturn){
   button.append(svgIcon(['M2.2 7.4L8 2.6l5.8 4.8','M4 6.6v6.2h8V6.6','M6.6 12.8V9.4h2.8v3.4']));
  }else{
   const dot=node('span','','sp-dot');dot.setAttribute('aria-hidden','true');button.append(dot);
  }
  button.append(node('span',label,'sp-name'),node('span',msg(states[plugin.state]||'未知'),'sp-state'));
  button.disabled=!!plugin.current||!plugin.can_open;
  setText(button,plugin.current?msg('当前面板'):!plugin.can_open?msg('此插件当前不可打开'):label,'title');
  if(plugin.current)button.setAttribute('aria-current','page');
  button.seriesNavigate=async(transition)=>{if(navigating||plugin.current||!plugin.can_open)return false;button.disabled=true;navigating=true;try{
   const bridge=getBridge();
   if(typeof bridge?.navigateToPlugin!=='function')throw Error(tr('当前宿主未提供跨插件导航。请先整页刷新；仍不可用时可从左侧插件菜单打开。'));
   const hostGuard=bridge.getContext?.()?.supportsNavigationGuard===true;
   if(!hostGuard&&!await beforeNavigate({pluginName:plugin.id,pageName:plugin.page})){
    feedback(note,msg('已取消跳转，保留当前编辑。'));
    return false;
   }
   feedback(note,msg('正在打开{0}…',names[plugin.id]?msg(names[plugin.id]):plugin.label),'loading');
   if(transition)await transition();
   const operation=bridge.navigateToPlugin(plugin.id,plugin.page,{
    ui:document.documentElement.dataset.uiTheme,
    locale:getLanguage(),
    scope:getSharedScope()||undefined,
   });
   const result=hostGuard?await operation:await timed(operation);
   if(result?.navigating===false&&!result?.updated){
    feedback(note,msg('已取消跳转，保留当前编辑。'));
    return false;
   }
   return true;
  }catch(error){feedback(note,String(error.message||'').includes('Unsupported plugin bridge action')?msg('宿主导航尚未更新，请整页刷新 AstrBot（Ctrl+F5）后重试。'):error.message||msg('跳转失败，请刷新后重试'),'error');if(transition)throw error;return false;}
  finally{button.disabled=!!plugin.current||!plugin.can_open;navigating=false;}};
  button.addEventListener('click',()=>button.seriesNavigate());
  return button;
 }
 for(const plugin of ordered)links.append(pluginButton(plugin));
 bar.append(links);
 const tools=node('div','','sp-tools');
 if(!isWorld){
  scopePill=node('span','','sp-scope-pill');
  scopePill.append(node('span','','sp-scope-text'));
  const clearBtn=node('button','×','sp-scope-clear');clearBtn.type='button';
  setText(clearBtn,msg('清除跨面板会话筛选'),'aria-label');setText(clearBtn,msg('清除跨面板会话筛选'),'title');
  clearBtn.addEventListener('click',()=>{setSharedScope('',{syncUrl:true,emit:true});});
  scopePill.append(clearBtn);
  paintScope();
  tools.append(scopePill);
 }
 if(data){
  const extra=node('details','',`sp-extra${data.legacy_conflict?' sp-extra-alert':''}`);extra.open=extraOpen;
  extra.append(node('summary',msg(data.legacy_conflict?'存在旧插件冲突':'辅助与兼容状态')));
  const pop=node('div','','sp-extra-body');
  for(const plugin of data.plugins.filter(p=>!p.page||p.association&&!p.current)){
   if(plugin.page){pop.append(pluginButton(plugin));continue;}
   const row=node('p');row.append(node('span',plugin.label+' · '),node('span',msg(states[plugin.state]||'未知')));pop.append(row);
  }
  if(data.legacy_conflict){
   pop.append(node('p',msg('旧插件仍在运行，请检查接管归属，避免重复处理。'),'sp-warning'));
   const conflictNames=(data.legacy_conflicts||[]).map(conflict=>conflict.label);
   if(conflictNames.length)pop.append(node('p',conflictNames.join('、'),'sp-warning'));
  }
  for(const plugin of data.plugins.filter(p=>p.version&&names[p.id])){
   const version=node('p');
   version.append(node('span',msg(names[plugin.id])+' · '),node('code',
    [plugin.version,plugin.source_commit].filter(Boolean).join(' · ')));
   pop.append(version);
  }
  extra.append(pop);
  tools.append(extra);
 }
 tools.append(languagePicker());
 const refreshButton=node('button','','sp-refresh');refreshButton.dataset.spFocus='refresh';refreshButton.type='button';
 setText(refreshButton,msg('刷新状态'),'title');setText(refreshButton,msg('刷新状态'),'aria-label');
 refreshButton.append(svgIcon(['M13.5 8a5.5 5.5 0 1 1-1.6-3.9','M13.5 2.5v2.8h-2.8']),node('span',msg('刷新状态'),'sp-refresh-label'));
 refreshButton.addEventListener('click',()=>refresh());
 tools.append(refreshButton);
 bar.append(tools);
 root.append(bar,note);
 links.scrollLeft=linkScroll;
 if(focused)for(const control of root.querySelectorAll('[data-sp-focus]'))if(control.dataset.spFocus===focused)control.focus({preventScroll:true});
}
function refresh(automatic=false){
 if(!root||navigating)return Promise.resolve(false);
 if(loading)return refreshRequest;
 // Background polling must not replace a focused select or navigation button.
 if(automatic&&root.contains(document.activeElement))return Promise.resolve(false);
 loading=true;root.setAttribute('aria-busy','true');
 const refreshButton=root.querySelector('.sp-refresh');if(refreshButton)refreshButton.disabled=true;
 if(!automatic)feedback(root.querySelector('.sp-feedback'),msg('正在读取互通状态…'),'loading');
 refreshRequest=(async()=>{
  try{
   const bridge=getBridge();if(!bridge)throw Error(tr('请从 AstrBot 控制台打开'));
   if(bridge.ready)await timed(bridge.ready());const raw=await timed(bridge.apiGet('series_presence',{}));
   if(raw?.status==='error'||raw?.ok===false)throw Error(raw.message||tr('读取失败'));
   const data=raw?.data??raw;if(!Array.isArray(data?.plugins))throw Error(tr('读取失败'));
   cached=data;render(data);return true;
  }catch(error){feedback(root.querySelector('.sp-feedback'),error.message||msg('互通状态读取失败'),'error');return false;}
  finally{loading=false;root.setAttribute('aria-busy','false');const button=root.querySelector('.sp-refresh');if(button)button.disabled=false;}
 })();
 return refreshRequest;
}
if(root){
 render(null);refresh();
 setInterval(()=>{if(!document.hidden)refresh(true);},30000);
 window.addEventListener('dynamics-navigation-contextchange',paintScope);
 window.addEventListener('dynamics-scopechange',paintScope);
 window.addEventListener('series-languagechange',paintScope);
 document.addEventListener('click',e=>{const ex=root.querySelector('.sp-extra[open]');if(ex&&!ex.contains(e.target))ex.open=false;});
}
// Cabin objects reuse exactly the same availability, dirty-edit and host-navigation checks.
window.DynamicsSeriesNavigation={async open(id,transition){
 if(await refresh()!==true)throw Error(tr('互通状态读取失败'));
 const target=cached?.plugins.find(plugin=>plugin.id===id);
 if(!target?.can_open||target.current)throw Error(tr('此插件当前不可打开'));
 const button=[...root.querySelectorAll('[data-sp-focus]')].find(n=>n.dataset.spFocus===id);
 if(!button||button.disabled||!button.seriesNavigate)throw Error(tr('此插件当前不可打开'));
 return button.seriesNavigate(transition);
}};
