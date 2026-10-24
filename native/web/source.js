'use strict';
// Explicit language selection avoids expensive, ambiguous auto-detection.
const sourceTypes = {
  py:['python','Python','Py'], pyw:['python','Python','Py'], c:['c','C','C'], h:['c','C header','C'],
  cc:['cpp','C++','C++'], cpp:['cpp','C++','C++'], cxx:['cpp','C++','C++'], hpp:['cpp','C++ header','C++'],
  js:['javascript','JavaScript','JS'], mjs:['javascript','JavaScript','JS'], cjs:['javascript','JavaScript','JS'], jsx:['javascript','JSX','JS'],
  ts:['typescript','TypeScript','TS'], tsx:['typescript','TSX','TS'], json:['json','JSON','{}'],
  html:['xml','HTML','<>'], htm:['xml','HTML','<>'], xml:['xml','XML','<>'], svg:['xml','SVG','<>'],
  css:['css','CSS','#'], sh:['bash','Shell','$'], zsh:['bash','Zsh','$'], bash:['bash','Bash','$'],
  md:['markdown','Markdown','M↓'], yaml:['yaml','YAML','Y'], yml:['yaml','YAML','Y'],
  rs:['rust','Rust','Rs'], go:['go','Go','Go'], java:['java','Java','J'], rb:['ruby','Ruby','Rb'],
  swift:['swift','Swift','Sw'], m:['objectivec','Objective-C','C'], mm:['objectivec','Objective-C++','C'],
  sql:['sql','SQL','DB'], toml:['ini','TOML','≡'], ini:['ini','INI','≡'], makefile:['makefile','Makefile','≡'],
  dockerfile:['dockerfile','Dockerfile','≡'], cmake:['cmake','CMake','≡']
};
function sourceType(path, content='') {
  const name=path.split('/').pop().toLowerCase();
  const extension=name==='cmakelists.txt'?'cmake':name.split('.').pop();
  let type=Object.hasOwn(sourceTypes,extension)?sourceTypes[extension]:undefined;
  if(!type && content.startsWith('#!')) {
    const first=content.split('\n',1)[0];
    type=/\bpython[\d.]*\b/.test(first)?sourceTypes.py:/\b(node|nodejs)\b/.test(first)?sourceTypes.js:/\b(bash|zsh|sh)\b/.test(first)?sourceTypes.sh:undefined;
  }
  return type || ['', 'Plain text','≡'];
}
// A fresh bounded worker per preview can be cancelled on navigation or timeout.
function createSourceHighlighter() {
  let worker, timer, finish, url;
  function cancel() { if(worker)worker.terminate(); worker=null; if(url)URL.revokeObjectURL(url);url=null;clearTimeout(timer); if(finish)finish(null); finish=null; }
  function highlight(code,language) {
    cancel();
    if(!language || code.length>64000)return Promise.resolve(null);
    return new Promise(resolve=>{
      finish=resolve;
      try {
        url=URL.createObjectURL(new Blob([window.tetherSyntaxWorkerSource],{type:'text/javascript'}));
        worker=new Worker(url);
        worker.onmessage=event=>{const html=event.data;finish=null;cancel();resolve(typeof html==='string'&&html.length<=1048576?html:null);};
        worker.onerror=()=>cancel(); timer=setTimeout(cancel,2000);
        worker.postMessage({code,language});
      }catch(_){cancel();}
    });
  }
  return {highlight,cancel};
}
function sourceLines(code,html) {
  const list=document.createElement('ol');list.className='file-lines';
  let line=document.createElement('li');list.append(line);
  // Only text and generated span classes cross into the live document. Even
  // unexpected markup from a grammar cannot become executable DOM.
  function text(value,classes=[]) {
    const parts=value.split('\n');
    for(let i=0;i<parts.length;i++) {
      if(i){line=document.createElement('li');list.append(line);}
      if(parts[i]){let node=document.createTextNode(parts[i]);for(const cls of [...classes].reverse()){const span=document.createElement('span');span.className=cls;span.append(node);node=span;}line.append(node);}
    }
  }
  function walk(node,classes=[]) {
    if(node.nodeType===Node.TEXT_NODE){text(node.textContent,classes);return;}
    const next=node.nodeName==='SPAN'?[...classes,...[...node.classList].filter(cls=>/^[a-zA-Z_][a-zA-Z0-9_-]*$/.test(cls))]:classes;
    for(const child of node.childNodes)walk(child,next);
  }
  if(html!==null){const parsed=new DOMParser().parseFromString(html,'text/html');walk(parsed.body);}
  else text(code);
  return list;
}
// Paths printed by compilers and test runners are opened as read-only files.
// Resolution stays scoped to the Explorer root; no shell commands are injected.
function sourceLinks(line,root,lineNumber,activate,columnForIndex) {
  const links=[], pattern=/(?:\/?[\w.@+~()-]+\/)*[\w.@+~-]+\.(?:pyw?|[ch]|cc|cpp|cxx|hpp|[cm]?js|jsx|tsx?|json|html?|xml|svg|css|sh|zsh|bash|md|ya?ml|rs|go|java|rb|swift|mm?|sql|toml|ini)(?::\d+(?::\d+)?)?(?![\w.])/g;
  for(const match of line.matchAll(pattern)){
    let path=match[0].replace(/:\d+(?::\d+)?$/,'');
    if(path.startsWith('/')){if(!path.startsWith(root.replace(/\/$/,'')+'/'))continue;path=path.slice(root.replace(/\/$/,'').length+1);}
    path=path.replace(/^\.\//,'');if(path.split('/').includes('..'))continue;
    const column=columnForIndex || (index=>index+1);
    links.push({text:match[0],range:{start:{x:column(match.index),y:lineNumber},end:{x:column(match.index+match[0].length)-1,y:lineNumber}},activate:()=>activate(path)});
  }
  return links;
}
