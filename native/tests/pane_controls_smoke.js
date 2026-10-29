'use strict';
window.tetherUISmoke=async first=>{
  const checks=[],pause=ms=>new Promise(resolve=>setTimeout(resolve,ms));
  const check=(value,label)=>{if(!value)throw Error(label);checks.push(label);};
  const lines=p=>Array.from({length:p.term.rows},(_,i)=>p.term.buffer.active.getLine(p.term.buffer.active.viewportY+i)?.translateToString(true)||'');
  const screen=p=>lines(p).join('\n');
  const until=async(test,label)=>{const start=Date.now();while(!test()){if(Date.now()-start>10000)throw Error(label+' timed out');await pause(35);}checks.push(label);};
  const row=path=>[...document.querySelectorAll('.file-row')].find(item=>item.title===path);
  let agent,monitor,resizeCount=0,resizeDuringDrag=0;
  const dragEvents=[];
  for(const type of ['pointerdown','pointermove','pointerup'])document.addEventListener(type,event=>{
    if(dragEvents.length===60)dragEvents.shift();
    dragEvents.push({type,target:event.target.className,x:event.clientX,y:event.clientY,moving:document.body.classList.contains('moving-pane')});
  },true);
  try{
    await until(()=>screen(first).includes('EXPLORER>'),'shell ready');
    agent=await openPane('agent');maximizePane(agent,true);
    await until(()=>screen(agent).includes('Select your agent'),'original picker ready');
    send({type:'input',id:agent.id,data:'\r'});
    await until(()=>screen(agent).includes('Ask Tether to build'),'real compact Agent composer ready');
    send({type:'input',id:agent.id,data:'resize_draft_42'});
    await until(()=>screen(agent).includes('resize_draft_42'),'draft entered');
    maximizePane(agent,false);await pause(250);
    monitor=await openPane('monitor');await pause(350);
    const ids=[...panes.keys()].join(',');
    // Real Cocoa right-click, not a DOM contextmenu dispatch.
    const content=agent.element.querySelector('.terminal-container').getBoundingClientRect();
    await request('testRightClick',{x:content.left+25,y:content.top+14});
    check(!!$('pane-move-left'),'native right-click inside terminal opens pane move menu');
    await request('snapshot',{name:'terminal'});
    $('pane-move-left').click();await pause(250);
    for(const direction of ['left','right','up','down']){
      if(direction!=='left'){agent.element.querySelector('.pane-menu').click();$('pane-move-'+direction).click();await pause(250);}
      const root=first.tab.element.firstElementChild,rect=agent.element.getBoundingClientRect(),bounds=first.tab.element.getBoundingClientRect();
      check(root.classList.contains(['up','down'].includes(direction)?'vertical':'horizontal'),'move '+direction+' sets split direction');
      check(['left','up'].includes(direction)?root.firstElementChild===agent.element:root.lastElementChild===agent.element,'move '+direction+' places existing Agent on requested edge');
      check(['left','right'].includes(direction)?Math.abs(rect.height-bounds.height)<2:Math.abs(rect.width-bounds.width)<2,'move '+direction+' spans session edge');
      check([...panes.keys()].join(',')===ids&&tabs.length===1,'move '+direction+' preserves all process IDs and session');
    }
    for(const pane of [first,monitor]){
      pane.element.querySelector('.pane-menu').click();$('pane-move-right').click();await pause(200);
      check(first.tab.element.firstElementChild.lastElementChild===pane.element,pane.kind+' moves with its own menu');
    }
    // Drag a title bar onto a sibling's left edge using native mouse input.
    const source=monitor.element.querySelector('.pane-heading').getBoundingClientRect(),target=agent.element.getBoundingClientRect();
    await request('testDrag',{x:source.left+25,y:source.top+14,endX:target.left+12,endY:target.top+target.height/2});await pause(180);
    check(monitor.element.parentElement===agent.element.parentElement&&monitor.element.parentElement.firstElementChild===monitor.element,'native title drag docks Monitor left of Agent');
    check(!document.body.classList.contains('moving-pane')&&!document.querySelector('.pane-drop-hint'),'title drag cleans up');
    const subscription=agent.term.onResize(()=>{resizeCount++;if(layoutDragging())resizeDuringDrag++;});
    // Put Agent above siblings, then repeatedly drag the width divider.
    movePane(agent,'left');await pause(250);resizeCount=0;
    for(const delta of [65,-90,45]){
      const bar=agent.element.parentElement.divider.bar,rect=bar.getBoundingClientRect();
      const count=resizeCount;
      await request('testDrag',{x:rect.left+rect.width/2,y:rect.top+rect.height/2,endX:rect.left+rect.width/2+delta,endY:rect.top+rect.height/2});await pause(180);
      check(resizeCount===count+1,'one Agent grid resize per completed divider drag');
    }
    check(resizeDuringDrag===0,'Agent grid stays stable while divider moves');
    subscription.dispose();
    maximizePane(agent,true);await pause(350);
    check(screen(agent).includes('resize_draft_42'),'Agent draft survives moves and repeated resizing');
    check(lines(agent).filter(line=>line.includes('resize_draft_42')).length===1,'resized Agent has one draft without ghost copies');
    const surface=agent.element.querySelector('.terminal-container').getBoundingClientRect(),terminal=agent.element.querySelector('.xterm-screen').getBoundingClientRect();
    check(terminal.bottom<=surface.bottom+1&&terminal.right<=surface.right+1,'terminal grid fits inside content box without clipped rows');
    send({type:'input',id:agent.id,data:'\u0015/plan on\r'});
    await until(()=>screen(agent).includes('Plan mode ON')&&screen(agent).includes('Ask Tether to build'),'resized Agent still accepts commands and redraws its composer');
    await request('snapshot',{name:'explorer'});
    maximizePane(agent,false);await pause(200);
    $('files').click();await until(()=>row('src'),'Files opens on demand');
    $('files-menu').click();
    check(document.querySelector('[role=menu][aria-label="Explorer actions"]')&&$('files-hidden'),'three dots opens visible Explorer menu');
    await request('snapshot',{name:'themes'});
    $('files-hidden').click();await until(()=>row('.hidden'),'menu shows hidden files');
    row('src').click();await until(()=>row('src/main.py'),'folder expands');
    $('files-menu').click();$('files-collapse').click();check(!row('src/main.py'),'menu collapses folders');
    $('files-menu').click();check($('files-hidden').getAttribute('aria-checked')==='true','menu reflects hidden-file state');
    document.activeElement.dispatchEvent(new KeyboardEvent('keydown',{key:'Escape',bubbles:true}));
    check(!document.querySelector('.context-menu')&&document.activeElement===$('files-menu'),'Escape dismisses menu and restores button focus');
    $('files-menu').click();$('files-menu').click();check(!document.querySelector('.context-menu'),'three dots toggles menu closed');
    $('files-menu').click();$('files-menu-refresh').click();await until(()=>row('src'),'menu refreshes folder list');
    await explorer.preview('src/main.py');$('files-menu').click();$('files-menu-close').click();
    check($('explorer').hidden&&$('file-viewer').hidden,'menu closes Files and preview');
    closePane(monitor);closePane(agent);await pause(200);
    check(panes.size===1&&!first.tab.element.querySelector('.divider'),'moved nested panes close cleanly');
    send({type:'input',id:first.id,data:"printf 'MOVE_%s\\n' SURVIVED\r"});
    await until(()=>screen(first).includes('MOVE_SURVIVED'),'original shell survives all moves');
    send({type:'smoke',result:{ok:true,checks,resizeCount,resizeDuringDrag,modelRequests:0,viewport:{width:innerWidth,height:innerHeight}}});
  }catch(error){send({type:'smoke',result:{ok:false,checks,error:error.message,stack:error.stack,agentScreen:agent&&panes.has(agent.id)?screen(agent):'',resizeCount,resizeDuringDrag,dragEvents}});}
};
