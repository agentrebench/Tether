'use strict';
const test=require('node:test'),assert=require('node:assert/strict'),vm=require('node:vm'),fs=require('node:fs'),path=require('node:path'),cp=require('node:child_process');
const root=path.resolve(__dirname,'../..');
function context(extra={}){const ctx=vm.createContext({setTimeout,clearTimeout,...extra});vm.runInContext(fs.readFileSync(path.join(root,'native/web/source.js'),'utf8'),ctx);return ctx;}
test('source type extensions, special filenames and interpreter lines',()=>{
  const ctx=context();
  for(const [name,code,expected]of [['main.PY','','python'],['test.cpp','','cpp'],['foo.jsx','','javascript'],['foo.tsx','','typescript'],['Dockerfile','','dockerfile'],['CMakeLists.txt','','cmake'],['tool','#!/usr/bin/env python3\n','python'],['tool','#!/bin/zsh\n','bash'],['x.unknown','',''],['x.constructor','','']]){
    ctx.name=name;ctx.code=code;assert.equal(vm.runInContext('sourceType(name,code)[0]',ctx),expected);
  }
});
test('source links use exact extensions, scoped paths and cell columns',()=>{
  const ctx=context();
  ctx.result=[];ctx.opened=[];
  vm.runInContext(`result=sourceLinks('🐍 src/main.cpp:20:4 /work/app.py ../no.py /other/private.c wrong.pyx','/work',2,path=>opened.push(path),index=>index+2)`,ctx);
  assert.deepEqual(Array.from(ctx.result,l=>l.text),['src/main.cpp:20:4','/work/app.py']);
  assert.equal(ctx.result[0].range.start.x,5);ctx.result[0].activate();assert.deepEqual(Array.from(ctx.opened),['src/main.cpp']);
});
test('all advertised syntax grammars are bundled',()=>{
  const code=cp.execFileSync(path.join(root,'desktop/node_modules/.bin/esbuild'),[path.join(root,'native/web/highlight-worker.entry.js'),'--bundle','--format=iife','--target=safari17'],{encoding:'utf8',maxBuffer:2*1024*1024});
  const messages=[],self={postMessage:message=>messages.push(message)};
  vm.runInNewContext(code,{self});
  const ctx=context(),languages=vm.runInContext('[...new Set(Object.values(sourceTypes).map(value=>value[0]))]',ctx);
  for(const language of languages){self.onmessage({data:{language,code:'example = 42'}});assert.equal(typeof messages.pop(),'string',language);}
  self.onmessage({data:{language:'python',code:'x'.repeat(64001)}});assert.equal(messages.pop(),null);
});
test('highlighter cancels stale work and releases worker and URL',async()=>{
  const workers=[],revoked=[];
  class Worker {constructor(){workers.push(this);}postMessage(){}terminate(){this.terminated=true;}}
  const ctx=context({Worker,Blob,URL:{createObjectURL:()=>`blob:${workers.length}`,revokeObjectURL:url=>revoked.push(url)},window:{tetherSyntaxWorkerSource:'/* fixture */'}});
  const highlighter=vm.runInContext('createSourceHighlighter()',ctx);
  const old=highlighter.highlight('print(1)','python'),next=highlighter.highlight('print(2)','python');
  assert.equal(await old,null);assert.equal(workers[0].terminated,true);
  workers[1].onmessage({data:'<span>safe</span>'});assert.equal(await next,'<span>safe</span>');assert.equal(workers[1].terminated,true);assert.equal(revoked.length,2);
  assert.equal(await highlighter.highlight('x'.repeat(64001),'python'),null);assert.equal(workers.length,2);
});
test('highlighter timeout leaves plain text and terminates worker',async()=>{
  let timeout,terminated=false;
  class Worker {postMessage(){}terminate(){terminated=true;}}
  const ctx=context({Worker,Blob,setTimeout:callback=>{timeout=callback;return 1;},clearTimeout:()=>{},URL:{createObjectURL:()=>'',revokeObjectURL:()=>{}},window:{tetherSyntaxWorkerSource:''}});
  const pending=vm.runInContext('createSourceHighlighter().highlight("hello","python")',ctx);timeout();assert.equal(await pending,null);assert.equal(terminated,true);
});
