'use strict';
// Installed only in the temporary development app by test_native_explorer.py.
window.tetherUISmoke = async first => {
  const checks = [], pause = ms => new Promise(resolve => setTimeout(resolve, ms));
  function check(value, label) { if (!value) throw new Error(label); checks.push(label); }
  async function until(test, label) {
    const start = Date.now();
    while (!test()) { if (Date.now() - start > 10000) throw new Error(label + ' timed out'); await pause(40); }
    checks.push(label);
  }
  const screen = () => Array.from({length:first.term.buffer.active.length}, (_, i) => first.term.buffer.active.getLine(i)?.translateToString(true) || '').join('\n');
  const row = path => [...document.querySelectorAll('.file-row')].find(item => item.title === path);
  try {
    check($('explorer').hidden && $('file-viewer').hidden, 'file views start closed');
    $('files').click();
    await until(() => row('src') && row('README.md'), 'root tree loaded');
    check(!row('.hidden'), 'hidden files start hidden');
    row('src').click();
    await until(() => row('src/main.py'), 'lazy folder expansion');
    row('src/main.py').click();
    await until(() => $('file-content').textContent.includes('Hello from the explorer'), 'native text preview');
    check($('file-title').textContent === 'src/main.py', 'relative preview path');
    check($('file-content').querySelectorAll('li').length >= 2, 'line numbers');
    await explorer.preview('unsafe.html');
    check(!$('file-content').querySelector('img') && $('file-content').textContent.includes('<img'), 'file HTML stays inert');
    await explorer.preview('data.bin');
    check($('file-content').textContent.includes('Binary'), 'binary explanation');
    await explorer.preview('large.txt');
    check($('file-meta').textContent.includes('truncated') && $('file-content').querySelectorAll('li').length === 2000, 'bounded large-file preview');
    await explorer.preview('outside/secret.txt');
    check($('file-content').textContent.includes('Cannot open'), 'symlink outside root refused');
    $('files-menu').click();$('files-hidden').click();
    await until(() => row('.hidden'), 'hidden-file toggle');
    row('src').focus();
    $('file-tree').dispatchEvent(new KeyboardEvent('keydown', {key:'ArrowRight', bubbles:true}));
    await until(() => row('src/main.py'), 'keyboard expand');
    $('file-tree').dispatchEvent(new KeyboardEvent('keydown', {key:'ArrowRight', bubbles:true}));
    check(document.activeElement.title === 'src/main.py', 'keyboard enters child');
    $('file-tree').dispatchEvent(new KeyboardEvent('keydown', {key:'ArrowLeft', bubbles:true}));
    check(document.activeElement.title === 'src', 'keyboard returns to parent');
    await explorer.preview('src/main.py');
    await pause(1200);
    send({type:'input', id:first.id, data:"printf 'EXPLORER_%s\\n' SHELL_ALIVE\r"});
    await until(() => screen().includes('EXPLORER_SHELL_ALIVE'), 'real shell stays alive beside preview');
    await pause(250);
    check($('workspace').getBoundingClientRect().height > 100 && $('file-tree').getBoundingClientRect().width >= 180, 'tree preview and terminal share visible space');
    await request('snapshot', {name:'explorer'});
    $('file-close').click();
    check($('file-viewer').hidden, 'close preview');
    $('files').click(); await until(() => $('explorer').hidden, 'hide explorer');
    $('files').click(); await until(() => !$('explorer').hidden, 'show explorer');

    // Deterministically delay replies to exercise file-selection/root-change races.
    const pending = [];
    const test = createExplorer({request:(type, data) => new Promise((resolve, reject) => pending.push({data, resolve, reject})), chooseFolder:()=>{}, focusTerminal:()=>{}});
    let job = test.setRoot('/one');
    pending.shift().resolve({kind:'directory', entries:[{name:'old.txt', directory:false}]}); await job;
    const old = test.preview('old.txt'), newest = test.preview('new.txt');
    const a = pending.shift(), b = pending.shift();
    b.resolve({kind:'file', content:'NEW', size:3}); await newest;
    a.resolve({kind:'file', content:'OLD', size:3}); await old;
    check($('file-content').textContent === 'NEW', 'late preview cannot replace newer selection');
    const closing = test.preview('old.txt'); $('file-close').click();
    pending.shift().resolve({kind:'file', content:'CLOSED', size:6}); await closing;
    check($('file-viewer').hidden && !$('file-content').textContent, 'late response cannot reopen a closed preview');
    const oldRoot = test.setRoot('/old-root'), newRoot = test.setRoot('/new-root');
    const c = pending.shift(), d = pending.shift();
    d.resolve({kind:'directory', entries:[{name:'new-root.txt', directory:false}]}); await newRoot;
    c.resolve({kind:'directory', entries:[{name:'old-root.txt', directory:false}]}); await oldRoot;
    check(row('new-root.txt') && !row('old-root.txt'), 'late folder response cannot replace new root');
    const failed = test.preview('gone.txt'); pending.shift().reject(new Error('File removed')); await failed;
    check($('file-content').textContent === 'File removed', 'deleted file error displayed');
    send({type:'smoke', result:{ok:true, checks, modelRequests:0}});
  } catch (error) { send({type:'smoke', result:{ok:false, checks, error:error.message, stack:error.stack}}); }
};
