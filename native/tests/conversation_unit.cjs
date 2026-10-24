'use strict';
const test=require('node:test'),assert=require('node:assert/strict'),vm=require('node:vm'),fs=require('node:fs');
const context=vm.createContext({});vm.runInContext(fs.readFileSync('native/web/conversation.js','utf8'),context);
function roles(lines,program='claude'){
  context.lines=lines.map(value=>typeof value==='string'?{text:value}:value);context.program=program;
  return Array.from(vm.runInContext('conversationRoles(lines,program)',context));
}
test('Claude conversation, tool output, wrapped rows and prompt stay distinct',()=>{
  assert.deepEqual(roles(['❯ My question','⏺ My answer','  continuing answer','', '  another paragraph','⏺ Bash(ls)', '  tool output','⎿ result','❯ next']),['user','assistant','assistant','','assistant','','','','user']);
  assert.deepEqual(roles(['⏺ response',{text:'continues',wrapped:true}]),['assistant','assistant']);
});
test('Codex prose, errors, diffs and code retain their roles',()=>{
  assert.deepEqual(roles(['› question','• Answer','    const x = 1;','  explanation','Error: failed','• Ran ls','  output','• Next answer','```js','console.log(1)','```','  prose'],'codex'),['user','assistant','','assistant','','','','assistant','','','','assistant']);
});
test('No color inference for ordinary shell output or unknown providers',()=>{
  assert.deepEqual(roles(['› question','• output','  more'],'bash'),['','','']);
  assert.deepEqual(roles(['⏺ Answer','unrecognized status','        ● high · /effort','  footer']),['assistant','','','']);
});
test('ANSI and true-color semantic colors are preserved',()=>{
  const check=(classList,color)=>{context.span={classList,style:{color}};return vm.runInContext('neutralTerminalSpan(span)',context);};
  assert.equal(check([],''),true);assert.equal(check(['xterm-fg-15'],''),true);
  assert.equal(check(['xterm-fg-1'],''),false);assert.equal(check(['xterm-fg-2'],''),false);
  assert.equal(check([],'rgb(211, 211, 211)'),true);
  assert.equal(check([],'rgb(220, 30, 30)'),false);
});
