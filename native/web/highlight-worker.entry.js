// Bundled at packaging time with the existing locked highlight.js dependency.
import hljs from '../../desktop/node_modules/highlight.js/lib/common.js';
import c from '../../desktop/node_modules/highlight.js/lib/languages/c.js';
import rust from '../../desktop/node_modules/highlight.js/lib/languages/rust.js';
import swift from '../../desktop/node_modules/highlight.js/lib/languages/swift.js';
import objectivec from '../../desktop/node_modules/highlight.js/lib/languages/objectivec.js';
import cmake from '../../desktop/node_modules/highlight.js/lib/languages/cmake.js';
import dockerfile from '../../desktop/node_modules/highlight.js/lib/languages/dockerfile.js';
for(const [name,grammar] of Object.entries({c,rust,swift,objectivec,cmake,dockerfile}))hljs.registerLanguage(name,grammar);
self.onmessage=({data})=>{
  if(typeof data.code!=='string'||data.code.length>64000||!hljs.getLanguage(data.language)){self.postMessage(null);return;}
  try{self.postMessage(hljs.highlight(data.code,{language:data.language,ignoreIllegals:true}).value);}catch(_){self.postMessage(null);}
};
