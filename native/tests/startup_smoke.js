'use strict';
window.tetherUISmoke=async first=>{
  const samples=[],pause=ms=>new Promise(resolve=>setTimeout(resolve,ms));
  const text=p=>Array.from({length:p.term.rows},(_,i)=>p.term.buffer.active.getLine(p.term.buffer.active.viewportY+i)?.translateToString(true)||'').join('\n');
  const until=async(test,label)=>{const start=performance.now();while(!test()){if(performance.now()-start>12000)throw Error(label+' timed out');await pause(10);}};
  try{
    await until(()=>text(first).includes('EXPLORER>'),'initial shell ready');
    send({type:'input',id:first.id,data:"printf 'INITIAL_%s\\n' READY\r"});
    await until(()=>text(first).includes('INITIAL_READY'),'initial shell accepts command');
    for(let trial=0;trial<5;trial++){
      for(const kind of ['shell','agent']){
        const start=performance.now(),pane=await openPane(kind),created=performance.now();
        await until(()=>text(pane).includes(kind==='shell'?'EXPLORER>':'Select your agent'),kind+' ready');
        const ready=performance.now();
        // A visible prompt must also accept input. No model calls.
        if(kind==='shell'){
          send({type:'input',id:pane.id,data:"printf 'READY_%s\\n' OK\r"});
          await until(()=>text(pane).includes('READY_OK'),'shell accepts command');
        }
        samples.push({kind,trial,createMs:created-start,readyMs:ready-start});
        if(kind==='agent')send({type:'input',id:pane.id,data:'\u001b'});
        else send({type:'input',id:pane.id,data:'exit\r'});
        await until(()=>!panes.has(pane.id),'pane closes');
      }
    }
    const stats={};for(const kind of ['shell','agent']){
      const values=samples.filter(s=>s.kind===kind).map(s=>s.readyMs).sort((a,b)=>a-b);
      stats[kind]={medianMs:values[2],maxMs:values[4]};
    }
    send({type:'smoke',result:{ok:true,samples,stats,modelRequests:0,conditions:'Five sequential launches per pane kind; isolated zsh startup; real native PTYs and xterm screen; 10ms polling'}});
  }catch(error){send({type:'smoke',result:{ok:false,error:error.message,samples}});}
};
