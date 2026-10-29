'use strict';
// One accessible pop-up at a time, shared by pane and Explorer controls.
let dismissMenu = () => {};
function showMenu({items, x, y, anchor, label}) {
  dismissMenu();
  const menu=document.createElement('div'), previous=anchor||document.activeElement;
  menu.className='context-menu';menu.setAttribute('role','menu');menu.setAttribute('aria-label',label);
  if(anchor){const rect=anchor.getBoundingClientRect();x=rect.left;y=rect.bottom;anchor.setAttribute('aria-expanded','true');}
  function close(restore=false){
    menu.remove();document.removeEventListener('pointerdown',outside,true);document.removeEventListener('keydown',key,true);
    window.removeEventListener('blur',close);window.removeEventListener('resize',close);
    anchor?.setAttribute('aria-expanded','false');dismissMenu=()=>{};
    if(restore&&previous?.isConnected)previous.focus();
  }
  const buttons=items.map(item=>{
    const button=document.createElement('button');button.textContent=(item.checked===undefined?'':item.checked?'✓ ':'   ')+item.label;
    button.setAttribute('role',item.checked===undefined?'menuitem':'menuitemcheckbox');
    if(item.checked!==undefined)button.setAttribute('aria-checked',String(item.checked));
    if(item.id)button.id=item.id;
    button.disabled=!!item.disabled;button.tabIndex=-1;
    button.onclick=()=>{close();item.action();};menu.append(button);return button;
  });
  function outside(event){if(!menu.contains(event.target)&&!anchor?.contains(event.target))close();}
  function key(event){
    const enabled=buttons.filter(button=>!button.disabled),index=enabled.indexOf(document.activeElement);
    if(['ArrowDown','ArrowUp','Home','End'].includes(event.key)){
      event.preventDefault();event.stopPropagation();
      enabled[event.key==='Home'?0:event.key==='End'?enabled.length-1:(index+(event.key==='ArrowUp'?-1:1)+enabled.length)%enabled.length]?.focus();
    }else if(event.key==='Escape'||event.key==='Tab'){event.preventDefault();event.stopPropagation();close(true);}
  }
  document.body.append(menu);
  menu.style.left=Math.max(4,Math.min(x,innerWidth-menu.offsetWidth-4))+'px';
  menu.style.top=Math.max(4,Math.min(y,innerHeight-menu.offsetHeight-4))+'px';
  document.addEventListener('pointerdown',outside,true);document.addEventListener('keydown',key,true);
  window.addEventListener('blur',close);window.addEventListener('resize',close);
  buttons.find(button=>!button.disabled)?.focus();dismissMenu=close;
}
// Shared pointer/keyboard splitters. Only pane geometry changes; PTYs survive.
function createDivider({container, first, second, vertical=false, reverse=false, label, initial=.5, changed=()=>{}, persist=()=>{}, pixels=false}) {
  const bar=document.createElement('div');bar.className='divider '+(vertical?'row-divider':'column-divider');
  bar.tabIndex=0;bar.setAttribute('role','separator');bar.setAttribute('aria-label',label);
  bar.setAttribute('aria-orientation',vertical?'horizontal':'vertical');
  bar.title='Drag to resize · double-click to reset';
  let value=initial, drag=null, frame=0, pending;
  const extent=()=>vertical?container.clientHeight:container.clientWidth;
  function apply(next,save=false) {
    const total=extent()-6;if(total<=0)return;
    const minimum=vertical?Math.min(90,total*.25):Math.min(140,total*.25);
    const size=Math.max(minimum,Math.min(total-minimum,pixels?next:next*total));
    value=pixels?size:size/total;
    first.style.flex=`0 0 ${pixels?size+'px':`calc(${value*100}% - ${6*value}px)`}`;second.style.flex='1 1 0';
    bar.setAttribute('aria-valuenow',String(Math.round(size/total*100)));bar.setAttribute('aria-valuemin',String(Math.ceil(minimum/total*100)));bar.setAttribute('aria-valuemax',String(Math.floor((total-minimum)/total*100)));
    changed();if(save)persist(value);
  }
  function flush(){cancelAnimationFrame(frame);frame=0;if(pending!==undefined){apply(pending);pending=undefined;}}
  bar.addEventListener('pointerdown',event=>{
    if(event.button!==0||drag)return;event.preventDefault();event.stopPropagation();
    const rect=first.getBoundingClientRect();drag={id:event.pointerId,start:vertical?event.clientY:event.clientX,size:vertical?rect.height:rect.width,cursor:document.body.style.cursor};
    document.body.classList.add('resizing');document.body.style.cursor=vertical?'row-resize':'col-resize';bar.classList.add('dragging');
    document.dispatchEvent(new Event('layoutdragstart'));
    // Track at document level as well as requesting capture. A dropped or
    // unavailable capture must not strand the drag when it leaves this bar.
    document.addEventListener('pointermove',move,true);
    document.addEventListener('pointerup',finish,true);
    document.addEventListener('pointercancel',finish,true);
    document.addEventListener('keydown',cancel,true);
    window.addEventListener('blur',end);
    try{bar.setPointerCapture(event.pointerId);}catch(_){}
  });
  function move(event){
    if(!drag||event.pointerId!==drag.id)return;
    event.preventDefault();
    const size=drag.size+((vertical?event.clientY:event.clientX)-drag.start)*(reverse?-1:1);
    pending=pixels?size:size/Math.max(1,extent()-6);
    if(!frame)frame=requestAnimationFrame(flush);
  }
  function finish(event){if(drag&&event.pointerId===drag.id)end();}
  function cancel(event){if(event.key==='Escape'){event.preventDefault();event.stopPropagation();end();}}
  function end(){
    if(!drag)return;flush();const previous=drag;drag=null;
    document.removeEventListener('pointermove',move,true);
    document.removeEventListener('pointerup',finish,true);
    document.removeEventListener('pointercancel',finish,true);
    document.removeEventListener('keydown',cancel,true);window.removeEventListener('blur',end);
    try{if(bar.hasPointerCapture(previous.id))bar.releasePointerCapture(previous.id);}catch(_){}
    document.body.classList.remove('resizing');document.body.style.cursor=previous.cursor;bar.classList.remove('dragging');persist(value);changed();
    document.dispatchEvent(new Event('layoutdragend'));
  }
  bar.addEventListener('keydown',event=>{
    const negative=vertical?'ArrowUp':'ArrowLeft',positive=vertical?'ArrowDown':'ArrowRight';
    if(![negative,positive,'Home','End'].includes(event.key))return;event.preventDefault();
    const delta=pixels?20:.03;apply(event.key==='Home'?0:event.key==='End'?(pixels?extent():1):value+(event.key===negative?-delta:delta)*(reverse?-1:1),true);
  });
  bar.addEventListener('dblclick',()=>apply(initial,true));
  first.after(bar);
  const observer=new ResizeObserver(()=>{bar.hidden=first.hidden||second.hidden;if(!bar.hidden)apply(value);});observer.observe(container);
  const visibility=new MutationObserver(()=>{bar.hidden=first.hidden||second.hidden;if(!bar.hidden)apply(value);});
  visibility.observe(first,{attributes:true,attributeFilter:['hidden']});visibility.observe(second,{attributes:true,attributeFilter:['hidden']});
  bar.hidden=first.hidden||second.hidden;apply(initial);
  return {bar,apply,replace(oldPane,newPane){
    visibility.disconnect();
    if(first===oldPane)first=newPane;if(second===oldPane)second=newPane;
    visibility.observe(first,{attributes:true,attributeFilter:['hidden']});visibility.observe(second,{attributes:true,attributeFilter:['hidden']});
    apply(value);
  },destroy(){observer.disconnect();visibility.disconnect();cancelAnimationFrame(frame);end();bar.remove();}};
}
