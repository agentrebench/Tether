'use strict';
window.tetherUISmoke=async first=>{
  const checks=[],pause=ms=>new Promise(resolve=>setTimeout(resolve,ms));
  const check=(value,label)=>{if(!value)throw Error(label);checks.push(label);};
  const lines=p=>Array.from({length:p.term.rows},(_,i)=>p.term.buffer.active.getLine(p.term.buffer.active.viewportY+i)?.translateToString(true)||'');
  const screen=p=>lines(p).join('\n');
  const until=async(test,label)=>{const start=Date.now();while(!test()){if(Date.now()-start>12000)throw Error(label+' timed out');await pause(50);}checks.push(label);};
  const palette=id=>{appearance.theme=id;colors=applyAppTheme(id,panes,send);};
  let agent;
  const input=async text=>{send({type:'input',id:agent.id,data:text});await pause(150);};
  const submit=async text=>{await input(text);await input('\r');};
  try{
    if(!$('explorer').hidden)explorer.toggle();
    palette('dark');
    agent=await openPane('agent');
    maximizePane(agent,true);
    await until(()=>screen(agent).includes('Select your agent'),'original provider picker opens');
    check(screen(agent).includes('TETHER'),'original logo restored');
    if (agent.term.rows >= 20) check(screen(agent).includes('Who are we building'),'original greeting retained when space allows');
    const orb=()=>lines(agent).map((line,row)=>[row,line.indexOf('●')]).find(x=>x[1]>=0)?.join(':');
    const initial=orb();check(!!initial,'original glowing ball visible');
    await until(()=>orb()&&orb()!==initial,'original ball animates');
    await request('snapshot',{name:'terminal'});
    // Fixture configuration selects Codex; Enter takes the real native launch path.
    await input('\r');
    await until(()=>screen(agent).includes('Ask Tether to build'),'Codex selection opens compact conversation');
    check(screen(agent).includes('tether / Codex'),'selected provider in session heading');
    check(!screen(agent).includes('██')&&!screen(agent).includes('using Codex CLI provider'),'oversized banner and duplicate startup text removed');
    const geometry=()=>{
      const text=lines(agent),prompt=text.findIndex(x=>x.includes('›'));
      const status=text.findIndex(x=>x.includes('% context'));
      return {prompt,status,gap:status-prompt};
    };
    const initialGeometry=geometry();
    check(initialGeometry.prompt>=0&&initialGeometry.gap===2,'idle composer occupies one input row');
    check(initialGeometry.status<agent.term.rows-2,'composer does not fill the terminal');
    await request('snapshot',{name:'explorer'});
    await input('/pla');
    await until(()=>screen(agent).includes('Toggle plan mode'),'slash completion preserved');
    await input('\u001b');await input('\u0015');
    await input('\u001b[200~/plan on\u001b[201~');
    await input('\u001b\r');
    check(geometry().gap===3,'multiline input grows by one row');
    await input('\r');
    await until(()=>screen(agent).includes('PLAN')&&screen(agent).includes('Ask Tether to build'),'pasted multiline command submits and prompt resets');
    check(geometry().status>=0,'model status remains visible after command');
    await submit('/plan off');
    await until(()=>screen(agent).includes('Plan mode OFF'),'second command uses real REPL loop');
    palette('light');await pause(150);
    const active=agent.term.buffer.active;
    check(active.getLine(active.viewportY+active.cursorY)?.getCell(0).isBgDefault(),'composer uses terminal default background');
    await request('snapshot',{name:'light'});
    await submit('/quit');
    await until(()=>!panes.has(agent.id),'quit exits agent cleanly');
    check(panes.has(first.id),'original shell remains running');
    send({type:'smoke',result:{ok:true,checks,modelRequests:0,route:'native agent -> original picker -> Codex -> compact REPL; isolated authentication fixture',initialGeometry,viewport:{width:innerWidth,height:innerHeight}}});
  }catch(error){send({type:'smoke',result:{ok:false,checks,error:error.message,screen:agent?screen(agent):'',stack:error.stack}});}
};
