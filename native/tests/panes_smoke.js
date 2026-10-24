'use strict';
window.tetherUISmoke=async first=>{
  const checks=[],pause=ms=>new Promise(resolve=>setTimeout(resolve,ms));
  const check=(value,label)=>{if(!value)throw Error(label);checks.push(label);};
  const screen=p=>Array.from({length:p.term.buffer.active.length},(_,i)=>p.term.buffer.active.getLine(i)?.translateToString(true)||'').join('\n');
  const until=async(test,label)=>{const start=Date.now();while(!test()){if(Date.now()-start>10000)throw Error(label+' timed out');await pause(30);}checks.push(label);};
  const open=async(action,kind)=>{
    const ids=new Set(panes.keys());action();
    await until(()=>[...panes.values()].some(p=>!ids.has(p.id)),kind+' pane created');
    const pane=[...panes.values()].find(p=>!ids.has(p.id));
    check(pane.kind===kind&&pane.tab===first.tab&&tabs.length===1,kind+' stays in the same session');
    return pane;
  };
  try{
    if(!$('explorer').hidden)explorer.toggle();
    await until(()=>screen(first).includes('EXPLORER>'),'initial shell ready');
    const agent=await open(()=>$('agent').click(),'agent');
    agent.maximize.click();
    await until(()=>screen(agent).includes('Select your agent'),'original picker runs in Agent pane');
    check(agent.term.rows>15,'maximized Agent has room for picker');
    agent.maximize.click();await pause(80);
    const monitor=await open(()=>$('monitor').click(),'monitor');
    if (innerWidth >= 800 && innerHeight >= 700)
      await until(()=>/\d[.,]\d+\s*[GM]iB/.test(screen(monitor)),'Monitor dashboard fits its initial dock');
    monitor.maximize.click();
    if (innerWidth >= 800) {
      await until(()=>/\d[.,]\d+\s*[GM]iB/.test(screen(monitor)),'bundled Monitor has live metrics');
    } else {
      await until(()=>/\d[.,]\d+\s*[GM]iB/.test(screen(monitor))||screen(monitor).includes('Terminal size too small'),'Monitor renders metrics or its minimum-size guidance');
    }
    monitor.maximize.click();await pause(80);
    check(!screen(agent).includes('Window too small'),'Agent picker adapts to short split pane');
    check(first.element.getBoundingClientRect().width>100&&agent.element.getBoundingClientRect().height>60&&monitor.element.getBoundingClientRect().height>60,'all three windows visible together');
    const parent=monitor.element.parentElement;
    const vertical=parent.classList.contains('vertical'),rect=agent.element.getBoundingClientRect(),before=vertical?rect.height:rect.width;
    parent.divider.bar.dispatchEvent(new KeyboardEvent('keydown',{key:vertical?'ArrowDown':'ArrowRight',bubbles:true}));await pause(80);
    const after=agent.element.getBoundingClientRect();
    check((vertical?after.height:after.width)>before,'Agent and Monitor divider resizes panes');
    // Synthetic pointer capture is stubbed, as in the existing workbench test.
    const bar=parent.divider.bar,capture=bar.setPointerCapture;bar.setPointerCapture=()=>{};
    const start=vertical?after.bottom:after.right;
    bar.dispatchEvent(new PointerEvent('pointerdown',{button:0,pointerId:91,clientX:start,clientY:start}));
    bar.dispatchEvent(new PointerEvent('pointermove',{pointerId:91,clientX:start-25,clientY:start-25}));
    bar.dispatchEvent(new PointerEvent('pointerup',{pointerId:91,clientX:start-25,clientY:start-25}));bar.setPointerCapture=capture;await pause(80);
    const dragged=agent.element.getBoundingClientRect();
    check((vertical?dragged.height:dragged.width)<(vertical?after.height:after.width),'pointer drag resizes windows');
    await request('snapshot',{name:'terminal'});
    first.element.querySelector('.pane-heading').dispatchEvent(new PointerEvent('pointerdown',{bubbles:true}));
    const second=await open(()=>$('new').click(),'shell');
    await until(()=>screen(second).includes('EXPLORER>'),'new Terminal accepts input in session');
    send({type:'input',id:second.id,data:"printf 'PANE_%s\\n' READY\r"});
    await until(()=>screen(second).includes('PANE_READY'),'new Terminal runs commands');
    second.element.querySelector('.pane-close').click();
    check(!panes.has(second.id)&&panes.has(first.id)&&panes.has(agent.id)&&panes.has(monitor.id),'pane close leaves siblings running');
    monitor.maximize.click();
    const extra=await open(()=>window.tetherReceive({type:'menu',action:'new'}),'shell');
    check(!first.tab.element.classList.contains('maximized'),'opening from native menu restores split layout');
    extra.element.querySelector('.pane-close').click();
    monitor.maximize.click();monitor.element.querySelector('.pane-close').click();await pause(80);
    check(!first.tab.element.classList.contains('maximized')&&panes.has(agent.id)&&panes.has(first.id),'closing maximized pane restores siblings');
    agent.element.querySelector('.pane-close').click();await pause(80);
    check(tabs.length===1&&panes.size===1&&!first.tab.element.querySelector('.divider'),'closing nested panes removes dividers');
    send({type:'input',id:first.id,data:"printf 'SESSION_%s\\n' ALIVE\r"});
    await until(()=>screen(first).includes('SESSION_ALIVE'),'original shell survives pane lifecycle');
    first.element.querySelector('.pane-close').click();
    check(tabs.length===0&&!$('empty').hidden,'last pane returns to empty workspace');
    $('empty-new').click();await until(()=>panes.size===1&&tabs.length===1,'new session opens after last pane closes');
    const reopened=[...panes.values()][0];
    await until(()=>screen(reopened).includes('EXPLORER>'),'reopened terminal reaches a shell prompt');
    send({type:'input',id:reopened.id,data:"printf 'REOPEN_%s\\n' READY\r"});
    await until(()=>screen(reopened).includes('REOPEN_READY'),'reopened terminal runs commands without returning to New terminal');
    send({type:'input',id:reopened.id,data:'exit\r'});
    await until(()=>panes.size===0&&tabs.length===0&&!$('empty').hidden,'reopened shell exits cleanly');
    send({type:'smoke',result:{ok:true,checks,modelRequests:0,viewport:{width:innerWidth,height:innerHeight}}});
  }catch(error){send({type:'smoke',result:{ok:false,checks,error:error.message,stack:error.stack}});}
};
