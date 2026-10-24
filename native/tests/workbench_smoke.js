'use strict';
window.tetherUISmoke = async first => {
  const checks=[], metrics={}, pause=ms=>new Promise(resolve=>setTimeout(resolve,ms));
  const check=(value,label)=>{if(!value)throw Error(label);checks.push(label);};
  const until=async(test,label)=>{const start=Date.now();while(!test()){if(Date.now()-start>10000)throw Error(label);await pause(30);}checks.push(label);};
  try {
    check($('explorer').hidden && $('file-viewer').hidden, 'file views start closed');
    $('files').click();
    await until(()=>document.querySelectorAll('.file-row').length>300,'large fixture tree loaded');
    document.querySelector('.file-row').focus();
    const samples=[];
    for(let round=0;round<5;round++){
      const start=performance.now();
      for(let i=0;i<80;i++)$('file-tree').dispatchEvent(new KeyboardEvent('keydown',{key:i%2?'ArrowUp':'ArrowDown',bubbles:true}));
      samples.push(performance.now()-start); await pause(20);
    }
    metrics.treeNavigation80=samples;
    await explorer.preview('src/main.py');
    check($('file-meta').textContent.includes('Python'),'Python language recognition');
    check($('file-content').querySelector('.hljs-keyword') && $('file-content').querySelector('.hljs-string'),'Python syntax tokens');
    for(const [path,label]of [['src/main.c','C'],['src/main.js','JavaScript'],['src/tool','Python']]){
      await explorer.preview(path);check($('file-meta').textContent.includes(label)&&$('file-content').querySelector('span'),'highlight '+label+' '+path);
    }
    await explorer.preview('src/empty.py');check($('file-content').textContent==='Empty file.','empty source file explanation');
    await explorer.preview('unsafe.html');check(!$('file-content').querySelector('img')&&!window.INJECTED&&$('file-content').textContent.includes('<img'),'highlighted markup stays inert');
    await explorer.preview('src/main.py');
    const firstRow=document.querySelector('.file-row');
    firstRow.focus();$('file-tree').dispatchEvent(new KeyboardEvent('keydown',{key:'ArrowDown',bubbles:true}));
    check(firstRow.isConnected,'tree navigation preserves DOM nodes');
    const sidebar=$('explorer'), side=explorerDivider.bar;
    const before=sidebar.getBoundingClientRect().width;
    side.dispatchEvent(new KeyboardEvent('keydown',{key:'ArrowRight',bubbles:true}));await pause(60);
    check(sidebar.getBoundingClientRect().width>before,`Explorer keyboard resize (${before} to ${sidebar.getBoundingClientRect().width})`);
    check(JSON.parse(localStorage.getItem('layout')).sidebar>before,'Explorer size persisted');
    // Pointer event geometry with capture stubbed (synthetic pointers are not OS pointers).
    const capture=side.setPointerCapture;side.setPointerCapture=()=>{};
    const start=sidebar.getBoundingClientRect().width;
    side.dispatchEvent(new PointerEvent('pointerdown',{pointerId:77,clientX:start,button:0}));
    side.dispatchEvent(new PointerEvent('pointermove',{pointerId:77,clientX:start-35}));
    side.dispatchEvent(new PointerEvent('pointerup',{pointerId:77,clientX:start-35}));side.setPointerCapture=capture;await pause(60);
    check(sidebar.getBoundingClientRect().width<start,'Explorer pointer resize');
    const height=$('file-viewer').getBoundingClientRect().height;
    previewDivider.bar.dispatchEvent(new KeyboardEvent('keydown',{key:'ArrowUp',bubbles:true}));await pause(60);
    check($('file-viewer').getBoundingClientRect().height<height,'preview keyboard resize');
    const second=await openPane('shell','right');await pause(100);
    const group=second.element.parentElement, oldWidth=first.element.getBoundingClientRect().width;
    group.divider.bar.dispatchEvent(new KeyboardEvent('keydown',{key:'ArrowRight',bubbles:true}));await pause(100);
    check(first.element.getBoundingClientRect().width>oldWidth,'terminal horizontal split resize');
    const third=await openPane('shell','down');await pause(80);
    const nested=third.element.parentElement,oldHeight=second.element.getBoundingClientRect().height;
    nested.divider.bar.dispatchEvent(new KeyboardEvent('keydown',{key:'ArrowDown',bubbles:true}));await pause(80);
    check(second.element.getBoundingClientRect().height>oldHeight,'nested vertical split resize');
    const nestedWidth=nested.getBoundingClientRect().width;
    group.divider.bar.dispatchEvent(new KeyboardEvent('keydown',{key:'ArrowLeft',bubbles:true}));await pause(80);
    check(nested.getBoundingClientRect().width>nestedWidth,'outer divider resizes nested group');
    closePane(third);
    const survivorWidth=second.element.getBoundingClientRect().width;
    group.divider.bar.dispatchEvent(new KeyboardEvent('keydown',{key:'ArrowRight',bubbles:true}));await pause(80);
    check(second.element.getBoundingClientRect().width<survivorWidth,'outer divider resizes surviving pane');
    closePane(second);await pause(80);
    check(first.element.isConnected&&!first.tab.element.querySelector('.divider'),'closing nested panes removes orphan dividers');
    await showSettings();
    const id=first.id;
    for(const theme of ['light','gruvbox','dracula','dark','gruvbox-light','solarized']){
      $('app-theme').value=theme;$('app-theme').dispatchEvent(new Event('change'));await pause(40);
      check(document.documentElement.dataset.theme===theme&&first.term.options.theme.background===appThemes[theme].p[0],'app and terminal palette '+theme);
    }
    $('app-theme').value='light';$('app-theme').dispatchEvent(new Event('change'));await pause(60);
    check(JSON.parse(localStorage.getItem('appearance')).theme==='light','app theme persisted');
    check(panes.get(id)===first,'appearance changes preserve running shell');
    await request('snapshot',{name:'themes'});
    $('settings-dialog').close();await explorer.preview('src/main.py');
    await request('snapshot',{name:'explorer'});
    const links=sourceLinks('src/main.py:2:4 /outside/private.c '+boot.cwd+'/src/main.c:1',boot.cwd,1,path=>explorer.preview(path));
    check(links.length===2,'source links exclude paths outside root');
    const suffixLinks=sourceLinks('main.cpp view.jsx worker.tsx wrong.pyx ../escape.py',boot.cwd,1,()=>{});
    check(suffixLinks.map(link=>link.text).join(',')==='main.cpp,view.jsx,worker.tsx','source link extension boundaries');
    await links[0].activate();check($('file-title').textContent==='src/main.py','terminal source link opens preview');
    const screen=()=>Array.from({length:first.term.buffer.active.length},(_,i)=>first.term.buffer.active.getLine(i)?.translateToString(true)||'').join('\n');
    send({type:'input',id:first.id,data:"printf 'WORKBENCH_%s\\n' ALIVE\r"});
    await until(()=>screen().includes('WORKBENCH_ALIVE'),'real PTY survives appearance and resizing');
    for(const position of ['right','left','bottom','top']){
      $('file-position').value=position;$('file-position').dispatchEvent(new Event('change'));await pause(80);
      const previewRect=$('file-viewer').getBoundingClientRect(),terminalRect=$('workspace').getBoundingClientRect();
      const correct=position==='right'?previewRect.left>=terminalRect.right:position==='left'?previewRect.right<=terminalRect.left:position==='bottom'?previewRect.top>=terminalRect.bottom:previewRect.bottom<=terminalRect.top;
      check(correct,'preview docks '+position);
      check(JSON.parse(localStorage.getItem('layout')).previewPosition===position,'dock position persisted '+position);
      check(panes.get(first.id)===first && $('file-content').querySelector('.hljs-keyword'),'docking preserves shell and highlighted file '+position);
      const vertical=['top','bottom'].includes(position),reverse=['right','bottom'].includes(position);
      const size=vertical?previewRect.height:previewRect.width;
      previewDivider.bar.dispatchEvent(new KeyboardEvent('keydown',{key:vertical?'ArrowDown':'ArrowRight',bubbles:true}));await pause(60);
      const rect=$('file-viewer').getBoundingClientRect(),next=vertical?rect.height:rect.width;
      check(reverse?next<size:next>size,'docked divider resizes '+position);
    }
    $('file-position').value='right';$('file-position').dispatchEvent(new Event('change'));await pause(60);
    await request('snapshot',{name:'explorer'});
    $('file-close').click();await pause(30);check(previewDivider.bar.hidden,'preview divider hides on close');
    explorer.toggle();await pause(30);check(explorerDivider.bar.hidden,'Explorer divider hides with sidebar');
    check($('workspace').getBoundingClientRect().height>100,'small-window terminal remains usable');
    send({type:'smoke',result:{ok:true,checks,metrics,viewport:{width:innerWidth,height:innerHeight}}});
  }catch(error){send({type:'smoke',result:{ok:false,checks,metrics,error:error.message,workerError:window.syntaxWorkerError,stack:error.stack}});}
};
