'use strict';
// Display adapter only: C owns PTYs/activity/game state; Python owns preferences
// and agent behavior. Terminal bytes are never inserted as HTML or JavaScript.
const $ = id => document.getElementById(id);
const send = body => window.webkit.messageHandlers.tether.postMessage(body);
const requests = new Map(), panes = new Map(), tabs = [];
let requestID = 0, tabID = 0, activeTab, activePane, boot, settings, selectedTheme;
let previewTab, previewSettings, orb, noticeTimer;
let appearance = {font: 'Tether Mono', size: 14};
try { Object.assign(appearance, JSON.parse(localStorage.getItem('appearance') || '{}')); } catch (_) {}
if (!Number.isFinite(appearance.size) || appearance.size < 11 || appearance.size > 24) appearance.size = 14;
if (typeof appearance.font !== 'string' || appearance.font.length > 120) appearance.font = 'Tether Mono';
const colors = {background:'#0c1014',foreground:'#dce7ed',cursor:'#83e1c2',selectionBackground:'#294b48',
  black:'#17232b',red:'#f38d91',green:'#91d6ad',yellow:'#e8c991',blue:'#8bbbef',magenta:'#c9a3e8',cyan:'#83d4d6',white:'#dce7ed',
  brightBlack:'#708793',brightRed:'#ffacaf',brightGreen:'#b2efca',brightYellow:'#f7dfad',brightBlue:'#add5ff',brightMagenta:'#e0c1f8',brightCyan:'#acf0ed',brightWhite:'#ffffff'};
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
function fitAll() { for (const pane of panes.values()) if (!pane.tab.element.hidden) { try { pane.fit.fit(); } catch (_) {} } }
const observer = new ResizeObserver(() => requestAnimationFrame(fitAll)); observer.observe($('workspace'));
function focusPane(pane) {
  activePane = pane;
  for (const item of panes.values()) item.element.classList.toggle('focused', item === pane);
  $('terminal-status').textContent = pane ? (pane.kind === 'agent' ? 'Tether agent' : pane.kind === 'monitor' ? 'btop' : 'Shell') : '';
}
function selectTab(tab) {
  activeTab = tab;
  tabs.forEach(item => { item.element.hidden = item !== tab; item.button.classList.toggle('active', item === tab); item.label.setAttribute('aria-selected', String(item === tab)); });
  const pane = [...panes.values()].find(item => item.tab === tab);
  focusPane(pane); updatePreview(); requestAnimationFrame(() => { fitAll(); pane?.term.focus(); });
}
function updatePreview() {
  $('preview-bar').hidden = !activeTab || activeTab !== previewTab;
  $('preview-name').textContent = previewSettings ? (settings?.themes.find(t => t.id === previewSettings.theme)?.name || previewSettings.theme) : '';
}
function makeTab(kind) {
  const element = document.createElement('section'); element.className = 'tab-content';
  const button = document.createElement('div'); button.className = 'tab';
  const label = document.createElement('button'); label.className = 'tab-name'; label.setAttribute('role', 'tab');
  label.textContent = kind === 'agent' ? '✦ Agent' : kind === 'monitor' ? '▥ Monitor' : '⌘ Terminal';
  const close = document.createElement('button'); close.textContent = '×'; close.className = 'tab-close'; close.setAttribute('aria-label', 'Close tab');
  const tab = {id:++tabID, element, button, label};
  label.onclick = () => selectTab(tab); close.onclick = () => closeTab(tab);
  button.append(label,close); $('tabs').append(button); $('workspace').append(element); tabs.push(tab);
  return tab;
}
async function openPane(kind = 'shell', split, theme) {
  if (panes.size >= 14) throw new Error('Close a pane before opening another (14 panes maximum).');
  const target = split && activePane;
  const result = await request('open', {kind, ...(theme ? {theme} : {})});
  const tab = target ? target.tab : makeTab(kind);
  const element = document.createElement('div'); element.className = 'pane';
  const container = document.createElement('div'); container.className = 'terminal-container'; element.append(container);
  if (target) {
    const group = document.createElement('div'); group.className = `split ${split === 'down' ? 'vertical' : 'horizontal'}`;
    target.element.replaceWith(group); group.append(target.element, element);
  } else tab.element.append(element);
  const term = new Terminal({theme:colors,fontFamily:`"${appearance.font}", Menlo, monospace`,fontSize:appearance.size,
    lineHeight:1.16,cursorBlink:true,cursorStyle:'bar',scrollback:5000,allowProposedApi:false,macOptionIsMeta:true});
  const fit = new FitAddon.FitAddon(); term.loadAddon(fit); term.open(container);
  const pane = {id:result.id,kind,term,fit,tab,element,closed:false}; panes.set(pane.id,pane);
  term.onData(data => send({type:'input',id:pane.id,data}));
  term.onResize(({cols,rows}) => send({type:'resize',id:pane.id,cols,rows}));
  term.parser.registerOscHandler(777, data => {
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
  if (notify) send({type:'close',id:pane.id});
  panes.delete(pane.id); pane.term.dispose(); const parent = pane.element.parentElement; pane.element.remove();
  if (parent?.classList.contains('split') && parent.children.length === 1) parent.replaceWith(parent.firstElementChild);
  if (![...panes.values()].some(p => p.tab === pane.tab)) {
    pane.tab.element.remove(); pane.tab.button.remove(); tabs.splice(tabs.indexOf(pane.tab),1);
    if (previewTab === pane.tab) { previewTab = null; previewSettings = null; }
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
  $('theme-source').textContent = theme?.source.includes('Oh My Zsh') ? 'Installed Oh My Zsh theme. Preview it in a live shell.' : 'Applied to new shells. Your running terminals keep their state.';
  $('theme-file').hidden = $('theme-file-label').hidden = selectedTheme !== 'custom';
}
async function showSettings() {
  if (!$('settings-dialog').open) $('settings-dialog').showModal();
  $('settings-status').textContent = 'Loading your installed themes…';
  try {
    settings = await request('settings'); selectedTheme = settings.theme;
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
  return {shell:$('shell').value === 'custom' ? $('shell-path').value : $('shell').value,
    theme:selectedTheme,font:$('glyphs').value,theme_file:$('theme-file').value};
}
function saveAppearance() {
  appearance = {font:$('font').value || 'Tether Mono',size:Number($('font-size').value)};
  try { localStorage.setItem('appearance',JSON.stringify(appearance)); } catch (_) {}
  boot.orbEnabled = $('orb-enabled').checked; send({type:'orbPreference',enabled:boot.orbEnabled});
  for (const pane of panes.values()) { pane.term.options.fontFamily = `"${appearance.font}", Menlo, monospace`; pane.term.options.fontSize = appearance.size; }
  fitAll();
}
async function previewTheme() {
  if (!settings) return;
  if (selectedTheme === 'custom') { $('settings-status').textContent = 'Save the custom theme file path before previewing it.'; return; }
  try {
    const values = chosenSettings(); if (previewTab) closeTab(previewTab);
    const pane = await openPane('shell',null,selectedTheme); previewTab = pane.tab; previewSettings = values;
    pane.tab.label.textContent = `◈ ${settings.themes.find(t => t.id === selectedTheme)?.name || selectedTheme}`;
    $('settings-dialog').close(); updatePreview(); fitAll();
  } catch(error) { $('settings-status').textContent = error.message; }
}
async function saveSettings(usePreview = false) {
  if (!settings) return;
  try {
    settings = await request('saveSettings',{settings:usePreview ? previewSettings : chosenSettings()});
    if (!usePreview) { saveAppearance(); $('settings-dialog').close(); await openPane('shell'); }
    previewTab = null; previewSettings = null; updatePreview(); notice('Saved. New terminals use your selected theme.');
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
const actions = {new:()=>openPane(),agent:()=>openPane('agent'),monitor:()=>openPane('monitor'),split:()=>openPane('shell','right'),close:()=>closePane(activePane),settings:showSettings};
for (const id of ['new','agent','monitor','settings']) $(id).onclick = () => attempt(actions[id]);
$('empty-new').onclick = () => attempt(actions.new);
for (const direction of ['right','down']) $('split-'+direction).onclick = () => {document.querySelector('.split-menu').open=false;attempt(()=>openPane('shell',direction));};
$('folder').onclick = () => attempt(async () => {boot.cwd=await request('folder');$('cwd').textContent=boot.cwd;notice('New terminals will start in this folder.');});
for(const id of ['dismiss-settings','cancel-settings']) $(id).onclick=()=>$('settings-dialog').close();
$('settings-dialog').addEventListener('close',()=>activePane?.term.focus());
$('theme-search').oninput=()=>{if(settings)renderThemes();};
$('shell').onchange=()=>$('shell-path').hidden=$('shell').value!=='custom';
$('font-size').oninput=()=>$('font-size-label').textContent=`${$('font-size').value} px`;
$('preview-theme').onclick=previewTheme;$('save-settings').onclick=()=>saveSettings();$('use-theme').onclick=()=>saveSettings(true);$('browse-themes').onclick=showSettings;
window.tetherReceive = event => {
  switch(event.type) {
    case 'reply': {const pending=requests.get(event.request);if(pending){clearTimeout(pending.timer);requests.delete(event.request);event.ok?pending.resolve(event.result):pending.reject(new Error(event.error));}break;}
    case 'bootstrap': boot=event;$('cwd').textContent=event.cwd;document.fonts.ready.then(()=>openPane()).then(pane=>{if(boot.smoke)runSmoke(pane);}).catch(error=>notice(error.message));break;
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
