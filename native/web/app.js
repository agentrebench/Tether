'use strict';
// Display adapter only: C owns PTYs/activity/game state; Python owns preferences
// and agent behavior. Terminal bytes are never inserted as HTML or JavaScript.
const $ = id => document.getElementById(id);
const send = body => window.webkit.messageHandlers.tether.postMessage(body);
const requests = new Map(), panes = new Map(), tabs = [];
let requestID = 0, tabID = 0, activeTab, activePane, boot, settings, selectedTheme;
let previewPane, previewSettings, orb, noticeTimer;
let appearance = {font: 'Tether Mono', size: 14, theme:'tether'};
try { Object.assign(appearance, JSON.parse(localStorage.getItem('appearance') || '{}')); } catch (_) {}
if (!Number.isFinite(appearance.size) || appearance.size < 11 || appearance.size > 24) appearance.size = 14;
if (typeof appearance.font !== 'string' || appearance.font.length > 120) appearance.font = 'Tether Mono';
if(!Object.hasOwn(appThemes,appearance.theme))appearance.theme='tether';
let colors=applyAppTheme(appearance.theme,panes,send);
function request(type, data = {}) {
  if (requests.size >= 32) return Promise.reject(new Error('Please wait for the current action.'));
  const id = ++requestID;
  return new Promise((resolve, reject) => {
    const timer = setTimeout(() => { requests.delete(id); reject(new Error('The terminal helper took too long.')); }, 20000);
    requests.set(id, {resolve, reject, timer}); send({type, request:id, ...data});
  });
}
function notice(message) { $('notice').textContent = message; clearTimeout(noticeTimer); noticeTimer = setTimeout(() => $('notice').textContent = '', 6000); }
const attempt = task => Promise.resolve().then(task).catch(error => notice(error.message));
// Reflow once after a divider drag, or after native window resizing settles.
// Sending SIGWINCH at every animation frame races the agent's redraw output.
let fitTimer=0;
const layoutDragging=()=>document.body.classList.contains('resizing')||document.body.classList.contains('moving-pane');
function fitAll() {
  clearTimeout(fitTimer);fitTimer=0;if(layoutDragging())return;
  for (const pane of panes.values()) if (!pane.tab.element.hidden && pane.element.getBoundingClientRect().width && pane.element.getBoundingClientRect().height) {
    fitPane(pane);
  }
}
function fitPane(pane) {
  const size=pane.fit.proposeDimensions();
  if(!size||size.cols===pane.term.cols&&size.rows===pane.term.rows||pane.preparingResize)return;
  if(pane.composerReady){
    // Ask the compact prompt to erase itself at the OLD grid size first.
    // Otherwise normal-buffer reflow shifts its origin before it can erase,
    // leaving ghost prompts above the new composer.
    pane.preparingResize=true;
    send({type:'input',id:pane.id,data:'\x1b[24;2~'});
    pane.resizeTimer=setTimeout(()=>finishPaneResize(pane),750);
  }else{try{pane.fit.fit();}catch(_){}}
}
function finishPaneResize(pane){
  clearTimeout(pane.resizeTimer);pane.preparingResize=false;
  if(!pane.closed&&!layoutDragging()&&pane.element.getBoundingClientRect().width&&pane.element.getBoundingClientRect().height){try{pane.fit.fit();}catch(_){}}
}
function scheduleFit(){clearTimeout(fitTimer);if(!layoutDragging())fitTimer=setTimeout(fitAll,120);}
document.addEventListener('layoutdragstart',()=>{clearTimeout(fitTimer);dismissMenu();});
document.addEventListener('layoutdragend',()=>requestAnimationFrame(fitAll));
const observer = new ResizeObserver(scheduleFit); observer.observe($('workspace'));
function focusPane(pane) {
  activePane = pane;
  if (pane) pane.tab.activePane = pane;
  for (const item of panes.values()) item.element.classList.toggle('focused', item === pane);
  $('terminal-status').textContent = pane ? (pane.kind === 'agent' ? 'Tether agent' : pane.kind === 'monitor' ? 'btop' : 'Shell') : '';
  updatePreview();
}
function selectTab(tab) {
  activeTab = tab;
  tabs.forEach(item => { item.element.hidden = item !== tab; item.button.classList.toggle('active', item === tab); item.label.setAttribute('aria-selected', String(item === tab)); });
  const pane = panes.has(tab.activePane?.id) ? tab.activePane : [...panes.values()].find(item => item.tab === tab);
  focusPane(pane); updatePreview(); requestAnimationFrame(() => { fitAll(); pane?.term.focus(); });
}
function updatePreview() {
  $('preview-bar').hidden = !activePane || activePane !== previewPane;
  $('preview-name').textContent = previewSettings ? (settings?.themes.find(t => t.id === previewSettings.theme)?.name || previewSettings.theme) : '';
}
function makeTab(kind) {
  const element = document.createElement('section'); element.className = 'tab-content';
  const button = document.createElement('div'); button.className = 'tab';
  const label = document.createElement('button'); label.className = 'tab-name'; label.setAttribute('role', 'tab');
  label.textContent = 'Session';
  const close = document.createElement('button'); close.textContent = '×'; close.className = 'tab-close'; close.setAttribute('aria-label', 'Close session');
  const tab = {id:++tabID, element, button, label};
  label.onclick = () => selectTab(tab); close.onclick = () => closeTab(tab);
  button.append(label,close); $('tabs').append(button); $('workspace').append(element); tabs.push(tab);
  return tab;
}
function maximizePane(pane, maximize = !pane.element.classList.contains('maximized-pane')) {
  pane.tab.element.classList.toggle('maximized', maximize);
  for (const item of panes.values()) if (item.tab === pane.tab) {
    const selected = maximize && item === pane;
    item.element.classList.toggle('maximized-pane', selected);
    item.maximize.title = selected ? 'Restore pane size' : 'Maximize pane';
    item.maximize.setAttribute('aria-label', item.maximize.title);
    item.maximize.setAttribute('aria-pressed', String(selected));
  }
  focusPane(pane); scheduleFit(); requestAnimationFrame(() => pane.term.focus());
}
function detachPane(element) {
  const parent=element.parentElement;element.remove();
  if(parent?.classList.contains('split')){
    parent.divider?.destroy();
    const survivor=parent.firstElementChild,ancestor=parent.parentElement;
    if(survivor){survivor.style.flex=parent.style.flex;parent.replaceWith(survivor);ancestor?.divider?.replace(parent,survivor);}
  }
  element.style.flex='';
}
function movePane(pane, direction, target=null) {
  if(!panes.has(pane.id)||!['left','right','up','down'].includes(direction))return;
  if(target&&(target===pane||target.tab!==pane.tab||!panes.has(target.id)))return;
  if([...panes.values()].filter(item=>item.tab===pane.tab).length<2)return;
  if(pane.tab.element.classList.contains('maximized'))maximizePane(pane,false);
  detachPane(pane.element);
  const placement=target?.element||pane.tab.element.firstElementChild,parent=placement.parentElement;
  const vertical=['up','down'].includes(direction),before=['left','up'].includes(direction);
  const group=document.createElement('div');group.className='split '+(vertical?'vertical':'horizontal');
  group.style.flex=placement.style.flex;placement.style.flex='';placement.replaceWith(group);
  const first=before?pane.element:placement,second=before?placement:pane.element;
  group.append(first,second);parent.divider?.replace(placement,group);
  group.divider=createDivider({container:group,first,second,vertical,label:'Resize terminal panes',changed:scheduleFit});
  focusPane(pane);scheduleFit();requestAnimationFrame(()=>pane.term.focus());
}
function paneMenu(pane, event, anchor) {
  event?.preventDefault();event?.stopPropagation();focusPane(pane);
  if(anchor?.getAttribute('aria-expanded')==='true'){dismissMenu();return;}
  const alone=[...panes.values()].filter(item=>item.tab===pane.tab).length<2;
  showMenu({label:'Pane actions',anchor,x:event?.clientX,y:event?.clientY,items:[
    ...['left','right','up','down'].map(direction=>({id:'pane-move-'+direction,label:'Move '+direction,disabled:alone,action:()=>movePane(pane,direction)})),
    ...(pane.kind==='shell'?[{label:'Conversation colors…',action:()=>showMenu({label:'Conversation colors',anchor:pane.element.querySelector('.pane-menu'),items:[
      ...[['auto','Automatic (Claude / Codex)'],['claude','Claude Code'],['codex','Codex'],['off','Original CLI colors']].map(([value,label])=>({id:'chat-colors-'+value,label,checked:pane.conversation.mode===value,action:()=>{pane.conversation.setMode(value);pane.term.focus();}}))
    ]})}]:[]),
    {label:pane.element.classList.contains('maximized-pane')?'Restore pane size':'Maximize pane',action:()=>maximizePane(pane)},
    {label:'Close pane',action:()=>closePane(pane)}
  ]});
}
function enablePaneDrag(pane, heading) {
  let drag=null;
  const hint=document.createElement('div');hint.className='pane-drop-hint';hint.setAttribute('aria-hidden','true');
  function move(event){
    if(!drag||event.pointerId!==drag.id)return;
    if(!drag.started&&Math.hypot(event.clientX-drag.x,event.clientY-drag.y)<5)return;
    if(!drag.started){drag.started=true;document.body.classList.add('moving-pane');document.body.append(hint);document.dispatchEvent(new Event('layoutdragstart'));}
    event.preventDefault();
    const element=document.elementFromPoint(event.clientX,event.clientY)?.closest('.pane');
    drag.target=[...panes.values()].find(item=>item.element===element&&item!==pane&&item.tab===pane.tab);
    hint.hidden=!drag.target;if(!drag.target)return;
    const rect=element.getBoundingClientRect(),x=(event.clientX-rect.left)/rect.width,y=(event.clientY-rect.top)/rect.height;
    const distances={left:x,right:1-x,up:y,down:1-y};
    drag.direction=Object.keys(distances).reduce((a,b)=>distances[a]<distances[b]?a:b);
    const vertical=['up','down'].includes(drag.direction);
    Object.assign(hint.style,{left:(rect.left+(drag.direction==='right'?rect.width/2:0))+'px',top:(rect.top+(drag.direction==='down'?rect.height/2:0))+'px',width:(rect.width/(vertical?1:2))+'px',height:(rect.height/(vertical?2:1))+'px'});
  }
  function end(event){
    if(!drag||event?.pointerId!==undefined&&event.pointerId!==drag.id)return;
    const previous=drag;drag=null;hint.remove();document.body.classList.remove('moving-pane');
    document.removeEventListener('pointermove',move,true);document.removeEventListener('pointerup',end,true);document.removeEventListener('pointercancel',end,true);document.removeEventListener('keydown',cancel,true);window.removeEventListener('blur',end);
    try{if(heading.hasPointerCapture(previous.id))heading.releasePointerCapture(previous.id);}catch(_){}
    if(event?.type==='pointerup'&&previous.target)movePane(pane,previous.direction,previous.target);
    else if(event?.type==='pointerup'&&!previous.started)pane.term.focus();
    if(previous.started)document.dispatchEvent(new Event('layoutdragend'));
  }
  function cancel(event){if(event.key==='Escape'){event.preventDefault();event.stopPropagation();end();}}
  heading.addEventListener('pointerdown',event=>{
    if(event.button!==0||event.target.closest('button')||pane.tab.element.classList.contains('maximized')||drag)return;
    event.preventDefault();focusPane(pane);drag={id:event.pointerId,x:event.clientX,y:event.clientY};
    document.addEventListener('pointermove',move,true);document.addEventListener('pointerup',end,true);document.addEventListener('pointercancel',end,true);document.addEventListener('keydown',cancel,true);window.addEventListener('blur',end);
    // Keep receiving events outside the title bar. elementFromPoint still finds
    // the destination beneath the pointer even while this heading has capture.
    try{heading.setPointerCapture(event.pointerId);}catch(_){}
  });
  pane.cancelDrag=()=>end();
}
async function openPane(kind = 'shell', split = 'auto', theme) {
  if (panes.size >= 14) throw new Error('Close a pane before opening another (14 panes maximum).');
  const target = split !== 'tab' && activePane;
  const dockMonitor = target && split === 'auto' && kind === 'monitor';
  let placement = target?.element, initial = .5;
  if (target) {
    if (target.tab.element.classList.contains('maximized')) maximizePane(target, false);
    if (dockMonitor) {
      // btop needs a wide, reasonably tall surface. Dock below the session's
      // existing layout instead of subdividing an already narrow Agent pane.
      placement = target.tab.element.firstElementChild; split = 'down';
      // Reserve btop's 24 rows using the rendered font metrics, including the
      // pane heading and terminal padding. A fixed ratio can leave only 23 rows.
      const surface=target.element.querySelector('.terminal-container');
      const padding=getComputedStyle(surface),screen=target.term.element.querySelector('.xterm-screen');
      const rowHeight=screen?.clientHeight/target.term.rows||Math.ceil(appearance.size*1.5);
      const chrome=target.element.offsetHeight-surface.clientHeight+parseFloat(padding.paddingTop)+parseFloat(padding.paddingBottom);
      const available=Math.max(1,target.tab.element.clientHeight-6);
      const top=Math.min(190,available-Math.ceil(24*rowHeight+chrome+2));
      // When both cannot fit, keep the existing panes usable and let btop
      // show its minimum-size guidance until the user maximizes it.
      initial=Math.min(.5,(top>=Math.ceil(6*rowHeight+chrome+2)?top:190)/available);
    } else if (split === 'auto') {
      const rect = target.element.getBoundingClientRect();
      split = rect.width >= 600 ? 'right' : 'down';
    }
  }
  const result = await request('open', {kind, ...(theme ? {theme} : {})});
  const tab = target ? target.tab : makeTab(kind);
  const element = document.createElement('div'); element.className = 'pane';
  const heading = document.createElement('div'); heading.className = 'pane-heading';
  const title = document.createElement('span'); title.className = 'pane-title';
  title.textContent = kind === 'agent' ? '✦ Agent' : kind === 'monitor' ? '▥ Monitor' : '⌘ Terminal';
  heading.title='Drag to move · right-click for pane actions';
  const menu=document.createElement('button');menu.className='pane-menu';menu.textContent='···';menu.title='Move or manage pane';menu.setAttribute('aria-label',menu.title);menu.setAttribute('aria-haspopup','menu');menu.setAttribute('aria-expanded','false');
  const maximize = document.createElement('button'); maximize.textContent = '□'; maximize.className = 'pane-maximize';
  maximize.title = 'Maximize pane'; maximize.setAttribute('aria-label', maximize.title); maximize.setAttribute('aria-pressed', 'false');
  const close = document.createElement('button'); close.textContent = '×'; close.className = 'pane-close';
  close.title = 'Close pane · ⌘W'; close.setAttribute('aria-label', 'Close '+title.textContent.slice(2)+' pane');
  heading.append(title,menu,maximize,close); element.append(heading);
  const container = document.createElement('div'); container.className = 'terminal-container'; element.append(container);
  if (target && !panes.has(target.id)) { send({type:'close',id:result.id}); throw new Error('The target pane was closed. Try splitting again.'); }
  if (target) {
    if (target.tab.element.classList.contains('maximized')) maximizePane(target,false);
    if (dockMonitor) placement = target.tab.element.firstElementChild;
    const group = document.createElement('div'); group.className = `split ${split === 'down' ? 'vertical' : 'horizontal'}`;
    const parent=placement.parentElement;
    group.style.flex=placement.style.flex; placement.style.flex='';
    placement.replaceWith(group); group.append(placement, element);
    parent.divider?.replace(placement,group);
    group.divider=createDivider({container:group,first:placement,second:element,vertical:split==='down',initial,label:'Resize terminal panes',changed:scheduleFit});
  } else tab.element.append(element);
  const term = new Terminal({theme:colors,fontFamily:`"${appearance.font}", Menlo, monospace`,fontSize:appearance.size,
    lineHeight:1.16,cursorBlink:true,cursorStyle:'bar',scrollback:5000,allowProposedApi:false,macOptionIsMeta:true});
  const fit = new FitAddon.FitAddon(); term.loadAddon(fit); term.open(container);
  const pane = {id:result.id,kind,term,fit,tab,element,title,maximize,closed:false}; panes.set(pane.id,pane);
  pane.conversation=createConversationColors(term,element);
  menu.onclick=event=>paneMenu(pane,event,menu);
  element.addEventListener('contextmenu',event=>paneMenu(pane,event),true);
  enablePaneDrag(pane,heading);
  maximize.onclick = () => maximizePane(pane); close.onclick = () => closePane(pane);
  heading.ondblclick = event => {if (!event.target.closest('button')) maximizePane(pane);};
  observer.observe(element);
  term.registerLinkProvider({provideLinks(lineNumber,callback){
    const line=term.buffer.active.getLine(lineNumber-1);
    if(!line){callback(undefined);return;}
    const columnForIndex=index=>{let offset=0;for(let x=0;x<line.length;x++){const cell=line.getCell(x);if(!cell||cell.getWidth()===0)continue;const size=(cell.getChars()||' ').length;if(offset+size>index)return x+1;offset+=size;}return line.length+1;};
    callback(sourceLinks(line.translateToString(true),boot.cwd,lineNumber,path=>attempt(()=>explorer.preview(path)),columnForIndex));
  }});
  term.onData(data => send({type:'input',id:pane.id,data}));
  term.onResize(({cols,rows}) => send({type:'resize',id:pane.id,cols,rows}));
  term.parser.registerOscHandler(777, data => {
    const input=/^tether;1;input;(ready|prepared|closed)$/.exec(data);
    if(input&&kind==='agent'){
      pane.composerReady=input[1]==='ready';
      if(input[1]==='prepared'||input[1]==='closed'){
        if(pane.preparingResize)requestAnimationFrame(()=>finishPaneResize(pane));
      }else scheduleFit();
      return true;
    }
    const match = /^tether;1;([1-9][0-9]{0,14});(thinking|reasoning|working|waiting|idle)$/.exec(data);
    if (match && kind === 'agent') send({type:'activity',id:pane.id,sequence:Number(match[1]),phase:match[2]});
    return true;
  });
  element.addEventListener('pointerdown', () => focusPane(pane));
  element.addEventListener('focusin', () => focusPane(pane));
  $('empty').hidden = true; selectTab(tab); focusPane(pane);
  requestAnimationFrame(() => { fitAll(); term.focus(); });
  return pane;
}
function closePane(pane, notify = true) {
  if (!pane || pane.closed) return; pane.closed = true;
  pane.cancelDrag?.();dismissMenu();
  clearTimeout(pane.resizeTimer);
  if (pane.element.classList.contains('maximized-pane')) maximizePane(pane,false);
  if (pane === previewPane) { previewPane = null; previewSettings = null; }
  if (notify) send({type:'close',id:pane.id});
  panes.delete(pane.id); observer.unobserve(pane.element); pane.conversation.dispose();pane.term.dispose();detachPane(pane.element);
  if (![...panes.values()].some(p => p.tab === pane.tab)) {
    pane.tab.element.remove(); pane.tab.button.remove(); tabs.splice(tabs.indexOf(pane.tab),1);
  }
  if (!tabs.length) { activeTab = null; focusPane(null); $('empty').hidden = false; updatePreview(); }
  else if (pane.tab === activeTab) selectTab(tabs.includes(pane.tab) ? pane.tab : tabs[tabs.length-1]);
  fitAll();
}
function closeTab(tab) { [...panes.values()].filter(p => p.tab === tab).forEach(p => closePane(p)); }
function renderThemes() {
  const query = $('theme-search').value.toLocaleLowerCase();
  const choices = settings.themes.filter(t => `${t.name} ${t.source}`.toLocaleLowerCase().includes(query));
  $('theme-count').textContent = `${choices.length} available`; $('theme-list').replaceChildren();
  for (const theme of choices) {
    const button = document.createElement('button'); button.className = 'theme-choice'; button.classList.toggle('selected', theme.id === selectedTheme);
    button.setAttribute('role','option'); button.setAttribute('aria-selected',String(theme.id === selectedTheme));
    const name = document.createElement('span'); name.textContent = theme.name;
    const source = document.createElement('small'); source.textContent = theme.source;
    button.append(name,source); button.onclick = () => { selectedTheme = theme.id; renderThemes(); describeTheme(); }; $('theme-list').append(button);
  }
}
function describeTheme() {
  const theme = settings.themes.find(t => t.id === selectedTheme);
  $('selected-theme').textContent = theme?.name || selectedTheme;
  $('theme-source').textContent = theme?.source.includes('Oh My Zsh')
    ? `${theme.source.startsWith('Included') ? 'Included with Tether' : 'From your local installation'} · Oh My Zsh. Preview it in a live shell. Tether Mono includes Powerline glyphs.`
    : 'Applied to new shells. Your running terminals keep their state.';
  $('theme-file').hidden = $('theme-file-label').hidden = selectedTheme !== 'custom';
}
async function showSettings() {
  if (!$('settings-dialog').open) $('settings-dialog').showModal();
  $('settings-status').textContent = 'Loading zsh themes…';
  $('app-theme').value=appearance.theme;
  try {
    settings = settings || await request('settings'); selectedTheme = settings.theme;
    $('theme-search').value = ''; $('glyphs').value = settings.font; $('theme-file').value = settings.theme_file;
    $('shell').value = ['', 'zsh','bash'].includes(settings.shell) ? settings.shell : 'custom';
    $('shell-path').value = settings.shell; $('shell-path').hidden = $('shell').value !== 'custom';
    $('font').replaceChildren();
    for (const family of boot.fonts) { const option = document.createElement('option'); option.value = family; option.textContent = family === 'Tether Mono' ? 'Tether Mono · Nerd Font included' : family; $('font').append(option); }
    $('font').value = appearance.font; $('font-size').value = appearance.size; $('font-size-label').textContent = `${appearance.size} px`;
    $('orb-enabled').checked = boot.orbEnabled; renderThemes(); describeTheme();
    $('settings-status').textContent = 'Preview a theme before making it your default.';
  } catch(error) { $('settings-status').textContent = error.message; }
}
function chosenSettings() {
  let shell = $('shell').value === 'custom' ? $('shell-path').value : $('shell').value;
  if (selectedTheme !== 'inherit' && ['', 'bash'].includes(shell)) shell = 'zsh';
  return {shell,
    theme:selectedTheme,font:$('glyphs').value,theme_file:$('theme-file').value};
}
function saveAppearance() {
  appearance = {...appearance,font:$('font').value || 'Tether Mono',size:Number($('font-size').value)};
  try { localStorage.setItem('appearance',JSON.stringify(appearance)); } catch (_) {}
  boot.orbEnabled = $('orb-enabled').checked; send({type:'orbPreference',enabled:boot.orbEnabled});
  for (const pane of panes.values()) { pane.term.options.fontFamily = `"${appearance.font}", Menlo, monospace`; pane.term.options.fontSize = appearance.size; }
  fitAll();
}
async function previewTheme() {
  if (!settings) return;
  if (selectedTheme === 'custom') { $('settings-status').textContent = 'Save the custom theme file path before previewing it.'; return; }
  try {
    const values = chosenSettings(); if (previewPane) closePane(previewPane);
    const pane = await openPane('shell','auto',selectedTheme); previewPane = pane; previewSettings = values;
    pane.title.textContent = `◈ ${settings.themes.find(t => t.id === selectedTheme)?.name || selectedTheme}`;
    $('settings-dialog').close(); updatePreview(); fitAll();
  } catch(error) { $('settings-status').textContent = error.message; }
}
async function saveSettings(usePreview = false) {
  if (!settings) return;
  try {
    settings = await request('saveSettings',{settings:usePreview ? previewSettings : chosenSettings()});
    if (!usePreview) { saveAppearance(); $('settings-dialog').close(); await openPane('shell'); }
    previewPane = null; previewSettings = null; updatePreview(); notice('Saved. New terminals use your selected theme.');
  } catch(error) { if ($('settings-dialog').open) $('settings-status').textContent = error.message; else notice(error.message); }
}
const trail = [];
function drawOrb(state) {
  orb = state; $('orb-wrap').hidden = !state.active;
  if (!state.active) { trail.length = 0; return; }
  $('orb-phase').textContent = state.phase; $('orb-score').textContent = `${state.score} points${state.combo > 1 ? ' · ×'+state.combo : ''}`;
  const ctx = $('orb').getContext('2d'); ctx.setTransform(2,0,0,2,0,0); ctx.clearRect(0,0,260,180);
  if (!boot.reducedMotion) { trail.push({x:state.x,y:state.y}); if (trail.length > 12) trail.shift(); }
  trail.forEach((point,index) => {ctx.beginPath();ctx.fillStyle=`rgba(116,241,208,${index/trail.length*.12})`;ctx.arc(point.x,point.y,5+index/4,0,Math.PI*2);ctx.fill();});
  const radius = 27 + state.pulse * 14, glow = ctx.createRadialGradient(state.x,state.y,0,state.x,state.y,radius);
  glow.addColorStop(0,'#fff');glow.addColorStop(.14,'#e4fff7');glow.addColorStop(.3,'#91f5d6');glow.addColorStop(.55,'#4bcca780');glow.addColorStop(1,'#4bcca700');
  ctx.fillStyle=glow;ctx.beginPath();ctx.arc(state.x,state.y,radius,0,Math.PI*2);ctx.fill();
  if (state.pulse > .05) {ctx.strokeStyle=`rgba(164,255,225,${state.pulse*.6})`;ctx.lineWidth=1;ctx.beginPath();ctx.arc(state.x,state.y,18+(1-state.pulse)*30,0,Math.PI*2);ctx.stroke();}
}
$('orb').addEventListener('pointerdown',event => {const rect=$('orb').getBoundingClientRect();send({type:'tap',x:(event.clientX-rect.left)*260/rect.width,y:(event.clientY-rect.top)*180/rect.height});});
$('catch-orb').onclick = () => {if(orb?.active)send({type:'tap',x:orb.x,y:orb.y});};
$('hide-orb').onclick = () => {boot.orbEnabled=false;send({type:'orbPreference',enabled:false});};
for(const [id,theme]of Object.entries(appThemes)){const option=document.createElement('option');option.value=id;option.textContent=theme.name;$('app-theme').append(option);}
$('app-theme').onchange=()=>{appearance.theme=$('app-theme').value;colors=applyAppTheme(appearance.theme,panes,send);try{localStorage.setItem('appearance',JSON.stringify(appearance));}catch(_){};};
let layoutPrefs={};try{layoutPrefs=JSON.parse(localStorage.getItem('layout')||'{}')||{};}catch(_){}
if(typeof layoutPrefs!=='object'||Array.isArray(layoutPrefs))layoutPrefs={};
const persistLayout=(key,value)=>{layoutPrefs[key]=value;try{localStorage.setItem('layout',JSON.stringify(layoutPrefs));}catch(_){}};
const explorerDivider=createDivider({container:document.querySelector('.workbench'),first:$('explorer'),second:document.querySelector('.workbench-main'),label:'Resize Explorer',pixels:true,initial:Number.isFinite(layoutPrefs.sidebar)?layoutPrefs.sidebar:230,changed:scheduleFit,persist:value=>persistLayout('sidebar',value)});
let previewDivider;
function dockPreview(position, save=true) {
  if(!['top','right','bottom','left'].includes(position))position='top';
  previewDivider?.destroy();
  const container=document.querySelector('.workbench-main'), preview=$('file-viewer'), terminal=$('workspace');
  const vertical=['top','bottom'].includes(position), reverse=['right','bottom'].includes(position);
  container.style.flexDirection=vertical?'column':'row';
  preview.style.order=reverse?'2':'0';terminal.style.order=reverse?'0':'2';
  previewDivider=createDivider({container,first:preview,second:terminal,vertical,reverse,label:'Resize file preview',initial:Number.isFinite(layoutPrefs.preview)?layoutPrefs.preview:.48,changed:scheduleFit,persist:value=>persistLayout('preview',value)});
  previewDivider.bar.style.order='1';
  $('file-position').value=position;
  if(save)persistLayout('previewPosition',position);
  scheduleFit();
}
dockPreview(layoutPrefs.previewPosition,false);
$('file-position').onchange=()=>dockPreview($('file-position').value);

const explorer = createExplorer({request, chooseFolder:()=>attempt(chooseFolder), focusTerminal:()=>activePane?.term.focus()});
async function chooseFolder() {
  const path = await request('folder');
  if (path === boot.cwd) return;
  boot.cwd = path; $('cwd').textContent = path; await explorer.setRoot(path);
  notice('New terminals will start in this folder.');
}
const actions = {files:()=>explorer.toggle(),new:()=>openPane(),agent:()=>openPane('agent'),monitor:()=>openPane('monitor'),split:()=>openPane('shell','right'),close:()=>closePane(activePane),settings:showSettings};
for (const id of ['new','agent','monitor','settings','files']) $(id).onclick = () => attempt(actions[id]);
$('empty-new').onclick = () => attempt(actions.new);
for (const direction of ['right','down']) $('split-'+direction).onclick = () => {document.querySelector('.split-menu').open=false;attempt(()=>openPane('shell',direction));};
$('folder').onclick = () => attempt(chooseFolder);
for(const id of ['dismiss-settings','cancel-settings']) $(id).onclick=()=>$('settings-dialog').close();
$('settings-dialog').addEventListener('close',()=>activePane?.term.focus());
$('theme-search').oninput=()=>{if(settings)renderThemes();};
$('shell').onchange=()=>$('shell-path').hidden=$('shell').value!=='custom';
$('font-size').oninput=()=>$('font-size-label').textContent=`${$('font-size').value} px`;
$('preview-theme').onclick=previewTheme;$('save-settings').onclick=()=>saveSettings();$('use-theme').onclick=()=>saveSettings(true);$('browse-themes').onclick=showSettings;
window.tetherReceive = event => {
  switch(event.type) {
    case 'program': panes.get(event.id)?.conversation.setProgram(event.program);break;
    case 'reply': {const pending=requests.get(event.request);if(pending){clearTimeout(pending.timer);requests.delete(event.request);event.ok?pending.resolve(event.result):pending.reject(new Error(event.error));}break;}
    case 'bootstrap': boot=event;$('cwd').textContent=event.cwd;void explorer.setRoot(event.cwd);document.fonts.ready.then(()=>openPane()).then(pane=>{if(boot.smoke)runSmoke(pane);}).catch(error=>notice(error.message));break;
    case 'output': {
      const bytes=Uint8Array.from(atob(event.data),c=>c.charCodeAt(0)),pane=panes.get(event.id);
      const ack=()=>send({type:'ack',id:event.id,bytes:bytes.length});if(pane)pane.term.write(bytes,ack);else ack();break;
    }
    case 'exit': {const pane=panes.get(event.id);if(pane){if(event.code!==0)notice(`Terminal exited (${event.code}).`);closePane(pane,false);}break;}
    case 'orb': drawOrb(event);break;
    case 'notice': notice(event.message);break;
    case 'menu': if(actions[event.action])attempt(actions[event.action]);break;
  }
};
// Integration harness is enabled only by an explicit native-host test flag.
// It exercises this UI and actual bundled processes; simulated activity is
// clearly recorded and never sends a model request.
async function runSmoke(first) {
  if (window.tetherUISmoke) return window.tetherUISmoke(first);
  const checks=[], pause=ms=>new Promise(resolve=>setTimeout(resolve,ms));
  const screen=p=>Array.from({length:p.term.buffer.active.length},(_,i)=>p.term.buffer.active.getLine(i)?.translateToString(true)||'').join('\n');
  async function until(check,label,timeout=15000){const start=Date.now();while(!check()){if(Date.now()-start>timeout)throw new Error(label+' timed out');await pause(80);}checks.push(label);}
  try {
    await pause(1300);send({type:'input',id:first.id,data:"printf 'TETHER_%s\\n' SHELL_OK\r"});await until(()=>screen(first).includes('TETHER_SHELL_OK'),'real zsh input/output');
    send({type:'input',id:first.id,data:'sleep 30\r'});await pause(300);send({type:'input',id:first.id,data:'\u0003'});await pause(150);send({type:'input',id:first.id,data:"printf 'CTRL_%s\\n' C_OK\r"});await until(()=>screen(first).includes('CTRL_C_OK'),'foreground Ctrl+C');
    const second=await openPane('shell','right');await until(()=>first.term.cols<100,'split resize');
    await showSettings();if(settings.themes.length<5)throw new Error('Missing theme catalog');checks.push(`settings catalog: ${settings.themes.length} themes`);
    selectedTheme=settings.themes.find(t=>t.id==='omz:agnoster')?.id||settings.themes[1].id;renderThemes();await previewTheme();await pause(900);checks.push('live theme preview');
    await saveSettings(true);const saved=await request('settings');if(saved.theme!==selectedTheme)throw new Error('Theme persistence failed');checks.push('settings saved and reloaded');
    const monitor=await openPane('monitor');await until(()=>/\d[.,]\d+\s*[GM]iB/.test(screen(monitor)),'bundled btop live metrics');
    // Let a full sampler/renderer cycle complete before capturing its view.
    await pause(2500);
    await request('snapshot',{name:'btop'});
    closePane(monitor);selectTab(first.tab);closePane(second);
    send({type:'input',id:first.id,data:"vim -Nu NONE -n\r"});await until(()=>first.term.buffer.active.type==='alternate','vim alternate screen');send({type:'input',id:first.id,data:':q!\r'});await until(()=>first.term.buffer.active.type==='normal','vim restores shell');
    await request('snapshot',{name:'terminal'});
    const agent=await openPane('agent');await pause(1200);await until(()=>screen(agent).length>30,'bundled agent picker');
    await new Promise(resolve=>agent.term.write('\x1b]777;tether;1;900000;reasoning\x07',resolve));await until(()=>orb?.active,'simulated reasoning activates light');
    const before=orb.score;$('catch-orb').click();await until(()=>orb.score>before,'interactive light scores');
    selectTab(first.tab);await request('snapshot',{name:'light'});
    await new Promise(resolve=>agent.term.write('\x1b]777;tether;1;900001;waiting\x07',resolve));await until(()=>!orb.active,'approval/waiting hides light');
    await new Promise(resolve=>agent.term.write('\x1b]777;tether;1;900002;thinking\x07',resolve));await until(()=>orb.active,'thinking reactivates light');closePane(agent);await until(()=>!orb.active,'closing agent clears light');
    selectTab(first.tab);await showSettings();await pause(400);
    send({type:'smoke',result:{ok:true,checks,modelRequests:0,activity:'simulated OSC; Python event mapping tested separately'}});
  } catch(error) {send({type:'smoke',result:{ok:false,checks,error:error.message}});}
}
send({type:'ready'});
