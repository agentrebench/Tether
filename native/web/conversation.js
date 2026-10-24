'use strict';
// Presentation adapter for the pinned xterm DOM renderer. The PTY stream and
// terminal buffer remain byte-for-byte untouched. Ambiguous rows stay native.
function conversationRoles(lines, program) {
  let role='',fenced=false;
  return lines.map(({text,wrapped=false})=>{
    if(!['claude','codex'].includes(program))return '';
    if(!wrapped){
      if(/^\s*```/.test(text)){fenced=!fenced;return '';}
      if(fenced)return '';
      if(/^ {0,3}[❯›>]\s?/.test(text)||/^ {0,3}You:\s/.test(text))role='user';
      else if(/^ {0,3}(?:[⏺●•]\s+|Claude:\s|Codex:\s)/.test(text)){
        const body=text.replace(/^\s*(?:[⏺●•]\s+|Claude:\s|Codex:\s)/,'');
        role=/^(?:Bash|Read|Write|Edit|Update|Glob|Grep|Search|WebFetch|WebSearch|Task|Agent|Tool|Skill|NotebookEdit)\s*\(|^(?:Ran|Explored|Running|Searching|Searched|Updated plan|Working|Thinking|Error|Warning|Permission|Waiting|Interrupted)\b/.test(body)?'':'assistant';
      }else if(/^\s*[╭╰╮╯│─━┌└┐┘┼⎿]|^\s*(?:Error:|Warning:|diff --git|@@|[+-]{3}\s)|^\s*(?:esc to|Esc to|ctrl\+|shift\+|\? for|Context left|\d+% context)/i.test(text))role='';
      // Indented code and diff rows retain their original foreground colors.
      else if(/^ {4,}\S|^\s*[+-]\s/.test(text))return '';
      else if(text.trim()&&!/^\s/.test(text))role='';
    }
    return text.trim()?role:'';
  });
}

function neutralTerminalSpan(span) {
  // ANSI error/diff/syntax colors take precedence. Only default/white/gray
  // text receives a conversation color, including Claude's true-color white.
  const ansi=[...span.classList].find(name=>name.startsWith('xterm-fg-'));
  if(ansi&&!['xterm-fg-7','xterm-fg-15'].includes(ansi))return false;
  const color=span.style.color;
  if(!color)return true;
  const channels=color.match(/^rgb\((\d+),\s*(\d+),\s*(\d+)\)$/);
  if(!channels)return false;
  const rgb=channels.slice(1).map(Number);
  return Math.max(...rgb)-Math.min(...rgb)<=12;
}

function createConversationColors(term, element) {
  let mode='auto',program='',painted=false;
  const selected=()=>mode==='auto'?program:mode==='off'?'':mode;
  function paint(){
    const provider=selected(),rows=element.querySelector('.xterm-rows');
    if(!rows||!provider&&!painted)return;
    const buffer=term.buffer.active,start=Math.max(0,buffer.viewportY-240),end=Math.min(buffer.length,buffer.viewportY+term.rows);
    // Bounded look-behind recovers the speaker when scrolling into a response.
    const lines=[];
    if(provider)for(let i=start;i<end;i++){
      const line=buffer.getLine(i);lines.push({text:line?.translateToString(true,0,512)||'',wrapped:line?.isWrapped});
    }
    const roles=conversationRoles(lines,provider);
    for(let y=0;y<rows.children.length;y++){
      const row=rows.children[y],role=roles[buffer.viewportY+y-start]||'';
      if(role)row.dataset.chatRole=role;else delete row.dataset.chatRole;
      for(const span of row.children)span.classList.toggle('tether-chat-text',!!role&&neutralTerminalSpan(span));
    }
    painted=!!provider;
  }
  const subscriptions=[term.onRender(paint),term.onScroll(paint),term.onResize(paint)];
  return {
    get mode(){return mode;},get program(){return program;},
    setMode(value){if(['auto','off','claude','codex'].includes(value)){mode=value;paint();}},
    setProgram(value){program=['claude','codex'].includes(value)?value:'';paint();},
    dispose(){subscriptions.forEach(subscription=>subscription.dispose());}
  };
}
