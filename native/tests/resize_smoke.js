'use strict';
window.tetherUISmoke=async first=>{
  const checks=[],events=[],pause=ms=>new Promise(resolve=>setTimeout(resolve,ms));
  const check=(value,label)=>{if(!value)throw Error(label);checks.push(label);};
  for(const type of ['pointerdown','pointermove','pointerup','gotpointercapture','lostpointercapture','mousedown','mousemove','mouseup'])
    document.addEventListener(type,event=>{if(events.length<160)events.push({type,trusted:event.isTrusted,target:event.target.className,x:event.clientX,y:event.clientY,buttons:event.buttons});},true);
  const until=async(test,label)=>{const start=Date.now();while(!test()){if(Date.now()-start>10000)throw Error(label+' timed out');await pause(30);}checks.push(label);};
  const drag=async(bar,firstPane,delta,label,loseCapture=false)=>{
    const rect=bar.getBoundingClientRect(),vertical=bar.getAttribute('aria-orientation')==='horizontal';
    const before=firstPane.getBoundingClientRect()[vertical?'height':'width'];
    const x=rect.left+rect.width/2+(vertical?0:6),y=rect.top+rect.height/2+(vertical?6:0);
    check(document.elementFromPoint(x,y)===bar,label+' hit target');
    if(loseCapture)bar.addEventListener('gotpointercapture',event=>bar.releasePointerCapture(event.pointerId),{once:true});
    await request('testDrag',{x,y,endX:x+(vertical?0:delta),endY:y+(vertical?delta:0)});
    const after=firstPane.getBoundingClientRect()[vertical?'height':'width'];
    check(Math.abs(after-before)>15,label+' native mouse drag changes size ('+before+' → '+after+')');
    check(!document.body.classList.contains('resizing'),label+' releases drag');
  };
  try{
    const initialFilesHidden=$('explorer').hidden;
    check(initialFilesHidden&&$('file-viewer').hidden&&$('files').getAttribute('aria-pressed')==='false','file browser and preview start closed');
    check(document.querySelectorAll('.file-row').length===0,'closed browser does not load a directory listing');
    const second=await openPane('shell','right');await pause(150);
    await drag(second.element.parentElement.divider.bar,first.element,45,'horizontal terminal divider');
    const third=await openPane('shell','down');await pause(150);
    await drag(third.element.parentElement.divider.bar,second.element,-35,'nested vertical terminal divider with dropped capture',true);
    if($('explorer').hidden)$('files').click();await pause(100);
    await drag(explorerDivider.bar,$('explorer'),45,'Explorer divider');
    await explorer.preview('src/main.py');await pause(150);
    await drag(previewDivider.bar,$('file-viewer'),-35,'file preview divider');
    dockPreview('right');await pause(120);
    await drag(previewDivider.bar,$('file-viewer'),35,'right-docked file preview divider');
    check(events.some(e=>e.type==='pointermove'&&e.trusted),'trusted WebKit pointer movement received');
    check(events.some(e=>e.type==='gotpointercapture'&&e.trusted),'native WebKit pointer capture exercised');
    await request('snapshot',{name:'explorer'});
    $('files').click();await pause(80);
    check($('explorer').hidden&&$('file-viewer').hidden&&explorerDivider.bar.hidden&&previewDivider.bar.hidden,'Files button closes browser, preview and dividers');
    $('files').click();await until(()=>document.querySelectorAll('.file-row').length>0,'Files button reopens browser on demand');
    check(!$('explorer').hidden&&$('file-viewer').hidden,'preview stays closed until a file is selected');
    send({type:'smoke',result:{ok:true,checks,events,initialFilesHidden,viewport:{width:innerWidth,height:innerHeight}}});
  }catch(error){send({type:'smoke',result:{ok:false,checks,events,error:error.message,stack:error.stack}});}
};
