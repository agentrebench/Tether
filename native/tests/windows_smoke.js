"use strict";
// Second window, pasteboard round trip, copy-on-select and paste. No model calls.
window.tetherUISmoke=async first=>{
  const checks=[],pause=ms=>new Promise(resolve=>setTimeout(resolve,ms));
  const text=p=>Array.from({length:p.term.rows},(_,i)=>p.term.buffer.active.getLine(p.term.buffer.active.viewportY+i)?.translateToString(true)||'').join('\n');
  const until=async(test,label,timeout=12000)=>{const start=performance.now();while(!test()){if(performance.now()-start>timeout)throw Error(label+' timed out');await pause(20);}checks.push(label);};
  try{
    await until(()=>text(first).includes('EXPLORER>'),'initial shell ready');
    // Pasteboard write and read through the host.
    const token='TETHER_CLIP_'+Date.now().toString(36);
    send({type:'clipboard',text:token});await pause(150);
    if(await request('clipboardText')!==token)throw Error('pasteboard round trip');checks.push('pasteboard round trip');
    // Highlighting copies the selected cells.
    send({type:'input',id:first.id,data:"printf 'SELECT_%s\\n' ME_PLEASE\r"});
    await until(()=>text(first).includes('SELECT_ME_PLEASE'),'selection fixture printed');
    const line=first.term.buffer.active;let row=-1;
    for(let i=0;i<line.length;i++)if(line.getLine(i)?.translateToString(true).startsWith('SELECT_ME_PLEASE'))row=i;
    if(row<0)throw Error('fixture row');
    first.term.select(0,row,'SELECT_ME_PLEASE'.length);await pause(400);
    if(await request('clipboardText')!=='SELECT_ME_PLEASE')throw Error('copy on select');checks.push('copy on select');
    first.term.clearSelection();
    // The pane menu's Paste: host pasteboard read, then xterm paste into the PTY.
    // zsh's bracketed paste keeps pasted text literal, so Enter follows the paste.
    send({type:'clipboard',text:"printf 'PASTE_%s\\n' OK"});await pause(150);
    first.term.paste(await request('clipboardText'));await pause(100);send({type:'input',id:first.id,data:'\r'});
    await until(()=>text(first).includes('PASTE_OK'),'pasteboard paste reaches shell');
    // Edit ▸ Paste and ⌘V arrive as DOM paste events on whatever has focus.
    const transfer=new DataTransfer();transfer.setData('text/plain',"printf 'DOM_%s\\n' OK");
    const event=new ClipboardEvent('paste',{clipboardData:transfer,bubbles:true,cancelable:true});
    document.dispatchEvent(event);
    if(event.clipboardData){await pause(100);send({type:'input',id:first.id,data:'\r'});await until(()=>text(first).includes('DOM_OK'),'document paste reaches shell');}
    else checks.push('synthetic paste carries no clipboardData in WebKit; listener tolerated it');
    // A second window bootstraps on the shared runtime; this one keeps working.
    send({type:'newWindow'});await until(()=>windowCount===2,'second window opened');
    await pause(1500);
    send({type:'input',id:first.id,data:"printf 'STILL_%s\\n' ALIVE\r"});
    await until(()=>text(first).includes('STILL_ALIVE'),'first window shell still responds');
    send({type:'smoke',result:{ok:true,checks,modelRequests:0,windows:windowCount}});
  }catch(error){send({type:'smoke',result:{ok:false,checks,error:error.message}});}
};
