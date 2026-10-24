'use strict';
window.tetherUISmoke = async first => {
  const checks = [], pause = ms => new Promise(resolve => setTimeout(resolve, ms));
  function check(value, label) { if (!value) throw new Error(label); checks.push(label); }
  async function until(test, label) {
    const start = Date.now();
    while (!test()) { if (Date.now() - start > 10000) throw new Error(label + ' timed out'); await pause(50); }
    checks.push(label);
  }
  const screen = pane => Array.from({length:pane.term.buffer.active.length}, (_, i) => pane.term.buffer.active.getLine(i)?.translateToString(true) || '').join('\n');
  try {
    await showSettings();
    check(settings.themes.filter(t => t.source === 'Included · Oh My Zsh').length === 142, '142 official themes included on a fresh home');
    for (const name of ['agnoster', 'robbyrussell', 'af-magic', 'bira', 'avit']) {
      check(settings.themes.some(t => t.id === 'omz:' + name), name + ' available');
    }
    $('theme-search').value = 'agnoster'; $('theme-search').dispatchEvent(new Event('input'));
    check($('theme-list').children.length === 1, 'search narrows the theme list');
    $('theme-list').firstElementChild.click();
    check($('theme-source').textContent.includes('Included with Tether'), 'included theme attribution');
    $('shell').value = 'bash';
    check(chosenSettings().shell === 'zsh', 'selecting a zsh theme chooses zsh');
    await previewTheme();
    let preview = activePane;
    check(preview !== first && panes.has(first.id), 'preview preserves original shell');
    check(preview.tab === first.tab && tabs.length === 1, 'theme preview stays in current session');
    await until(() => screen(preview).includes('\ue0b0'), 'agnoster Powerline prompt renders');
    send({type:'input', id:preview.id, data:"printf 'ZSH_THEME_%s\\n' READY\r"});
    await until(() => screen(preview).includes('ZSH_THEME_READY'), 'themed shell accepts commands');
    const previous = preview;
    await previewTheme(); preview = activePane;
    check(!panes.has(previous.id) && panes.has(first.id) && tabs.length === 1, 'replacing preview closes only the preview pane');
    check((await request('settings')).theme === 'inherit', 'preview leaves saved theme unchanged');
    await saveSettings(true);
    check((await request('settings')).theme === 'omz:agnoster', 'selected theme saved and reloaded');
    const fresh = await openPane();
    await until(() => screen(fresh).includes('\ue0b0'), 'new shell uses saved theme');
    selectTab(first.tab);
    send({type:'input', id:first.id, data:"printf 'ORIGINAL_THEME_%s\\n' ALIVE\r"});
    await until(() => screen(first).includes('ORIGINAL_THEME_ALIVE'), 'existing shell stays alive');
    selectTab(preview.tab); await request('snapshot', {name:'terminal'});
    await showSettings();
    check($('theme-list').children.length === 146, 'settings retain full collection');
    await pause(200);
    await request('snapshot', {name:'themes'});
    send({type:'smoke', result:{ok:true, checks, modelRequests:0, viewport:{width:innerWidth, height:innerHeight}}});
  } catch (error) { send({type:'smoke', result:{ok:false, checks, error:error.message, stack:error.stack}}); }
};
