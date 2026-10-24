'use strict';

// Local display only. Paths and file contents are text, never markup or code.
function createExplorer({request, chooseFolder, focusTerminal}) {
  const get = id => document.getElementById(id);
  const tree = get('file-tree'), panel = get('file-viewer');
  const highlighter = createSourceHighlighter();
  const cache = new Map(), expanded = new Set(['']), loading = new Set();
  let root = '', selected = '', generation = 0, previewRequest = 0, previewPath = null, rows = [];
  let hidden = false, visible = !get('explorer').hidden;
  const clean = value => value.replace(/[\x00-\x1f\x7f-\x9f]/g, '?');
  const join = (parent, name) => parent ? `${parent}/${name}` : name;
  const parent = path => path.includes('/') ? path.slice(0, path.lastIndexOf('/')) : '';
  const report = message => { get('files-status').textContent = message; };

  function render(focus = false) {
    const scroll = tree.scrollTop;
    const hadFocus = tree.contains(document.activeElement);
    rows = [];
    function walk(path, depth) {
      const data = cache.get(path);
      if (!data) return;
      for (const entry of data.entries || []) {
        if (rows.length >= 2000) return;
        const row = {...entry, path:join(path, entry.name), depth};
        rows.push(row);
        if (entry.directory && expanded.has(row.path) && depth < 30) walk(row.path, depth + 1);
      }
    }
    walk('', 0);
    if (!rows.some(row => row.path === selected)) selected = rows[0]?.path || '';
    const fragment = document.createDocumentFragment();
    for (const row of rows) {
      const button = document.createElement('button');
      button.className = 'file-row'; button.style.paddingLeft = `${10 + row.depth * 16}px`;
      button.setAttribute('role', 'treeitem'); button.setAttribute('aria-level', String(row.depth + 1));
      button.setAttribute('aria-selected', String(row.path === selected));
      button.tabIndex = row.path === selected ? 0 : -1;
      if (row.directory) button.setAttribute('aria-expanded', String(expanded.has(row.path)));
      const chevron = document.createElement('span'); chevron.className = 'file-chevron'; chevron.setAttribute('aria-hidden', 'true');
      chevron.textContent = row.directory ? (expanded.has(row.path) ? '⌄' : '›') : '';
      const icon = document.createElement('span'); icon.className = row.directory ? 'folder-icon' : 'file-icon';
      icon.setAttribute('aria-hidden', 'true'); icon.textContent = row.directory ? '▱' : row.symlink ? '↗' : sourceType(row.name)[2];
      if(!row.directory)icon.title=sourceType(row.name)[1];
      const name = document.createElement('span'); name.className = 'file-name'; name.textContent = clean(row.name);
      button.title = clean(row.path); button.append(chevron, icon, name);
      button.onclick = () => { selected = row.path; void activate(row); };
      button.onfocus = () => {
        selected = row.path;
        selectButton(button);
      };
      fragment.append(button);
    }
    tree.replaceChildren(fragment);
    tree.tabIndex = rows.length ? -1 : 0;
    if (!rows.length) {
      const empty = document.createElement('p'); empty.className = 'files-empty';
      empty.textContent = loading.has('') ? 'Loading folder…' : cache.get('')?.error || 'This folder is empty.';
      tree.append(empty);
    }
    tree.scrollTop = scroll;
    if (focus || hadFocus) focusSelected();
    if (rows.length >= 2000) report('Showing 2,000 rows. Collapse folders to see more.');
  }

  function selectButton(button) {
    const old=tree.querySelector('[aria-selected="true"]');
    if(old && old!==button){old.setAttribute('aria-selected','false');old.tabIndex=-1;}
    if(button){button.setAttribute('aria-selected','true');button.tabIndex=0;}
  }
  function focusSelected() {
    const index = rows.findIndex(row => row.path === selected);
    const item = index >= 0 ? tree.children[index] : tree;
    if(index>=0)selectButton(item);
    item?.focus({preventScroll:true});
    if (index >= 0) item.scrollIntoView({block:'nearest'});
  }

  async function load(path) {
    if (loading.has(path)) return;
    if (loading.size >= 4) { expanded.delete(path); report('Loading folders. Try again in a moment.'); render(); return; }
    if (!cache.has(path) && cache.size + loading.size >= 128) {
      expanded.delete(path); report('Folder limit reached. Refresh to browse more folders.'); render(); return;
    }
    const version = generation;
    loading.add(path); render();
    try {
      const data = await request('files', {root, path, hidden});
      if (version !== generation) return;
      if (data.kind !== 'directory') throw new Error('This folder is no longer available.');
      cache.set(path, data);
      report(data.truncated ? 'Showing up to 500 entries per folder.' : '');
    } catch (error) {
      if (version !== generation) return;
      cache.set(path, {entries:[], error:error.message}); report(error.message);
    } finally {
      if (version === generation) { loading.delete(path); render(); }
    }
  }

  async function activate(row, expandOnly = false) {
    if (row.directory) {
      if (expanded.has(row.path)) {
        if (!expandOnly) expanded.delete(row.path);
        else {
          const index = rows.indexOf(row);
          if (rows[index + 1]?.depth > row.depth) selected = rows[index + 1].path;
        }
        render(true);
      } else {
        if (row.depth >= 30) { report('Folder nesting limit reached. Open this folder as the root.'); return; }
        expanded.add(row.path); render(true);
        if (!cache.has(row.path)) await load(row.path);
      }
    } else if (!expandOnly) {
      render();
      await preview(row.path);
    }
  }

  async function preview(path) {
    highlighter.cancel();
    previewPath = path;
    const version = ++previewRequest, folderVersion = generation;
    panel.hidden = false;
    get('file-title').textContent = clean(path); get('file-title').title = clean(path);
    get('file-meta').textContent = 'Read only · Loading…'; get('file-content').textContent = '';
    try {
      const data = await request('files', {root, path, hidden});
      if (version !== previewRequest || folderVersion !== generation) return;
      if (data.kind === 'directory') throw new Error('This path is now a folder. Refresh the explorer.');
      get('file-meta').textContent = `Read only · ${(data.size / 1024).toFixed(1)} KB`;
      if (data.kind === 'binary') get('file-content').textContent = 'Binary or non-UTF-8 file — text preview unavailable.';
      else {
        const lines = data.content.replace(/\r\n/g, '\n').replace(/[\x00-\x08\x0b-\x1f\x7f-\x9f]/g,
          c => `\\x${c.charCodeAt(0).toString(16).padStart(2, '0')}`).split('\n');
        const code=lines.slice(0,2000).join('\n'), type=sourceType(path,data.content);
        get('file-meta').textContent += ` · ${type[1]}`;
        get('file-content').dataset.language=type[0] || 'plaintext';
        if(code) get('file-content').replaceChildren(sourceLines(code,null));
        else get('file-content').textContent='Empty file.';
        const html=code ? await highlighter.highlight(code,type[0]) : null;
        if(version!==previewRequest || folderVersion!==generation)return;
        if(html!==null)get('file-content').replaceChildren(sourceLines(code,html));
        else if(type[0] && code.length>64000)get('file-meta').textContent+=' · Large file: plain text';
        if (data.truncated || lines.length > 2000) get('file-meta').textContent += ' · Preview truncated';
      }
    } catch (error) {
      if (version !== previewRequest || folderVersion !== generation) return;
      get('file-meta').textContent = 'Read only'; get('file-content').textContent = error.message;
    }
  }

  function closePreview() {
    highlighter.cancel(); ++previewRequest; previewPath = null; panel.hidden = true; get('file-content').replaceChildren(); focusTerminal();
  }
  async function refresh() {
    highlighter.cancel(); ++generation; cache.clear(); loading.clear(); expanded.clear(); expanded.add('');
    if (visible) await load('');
    if (previewPath) await preview(previewPath);
  }
  async function setRoot(path) {
    if (root !== path) { root = path; selected = ''; closePreview(); }
    get('files-root').textContent = clean(path.split('/').filter(Boolean).pop() || '/');
    get('files-root').title = path;
    await refresh();
  }
  function toggle() {
    dismissMenu();
    visible = !visible; get('explorer').hidden = !visible;
    get('files').setAttribute('aria-pressed', String(visible));
    if (visible) {
      if (!cache.has('')) void load('');
      focusSelected();
    } else closePreview();
  }
  tree.onkeydown = event => {
    const index = rows.findIndex(row => row.path === selected), row = rows[index];
    if (['ArrowUp','ArrowDown','Home','End'].includes(event.key)) {
      const next = event.key === 'Home' ? 0 : event.key === 'End' ? rows.length - 1 : index + (event.key === 'ArrowUp' ? -1 : 1);
      selected = rows[Math.max(0, Math.min(rows.length - 1, next))]?.path || ''; focusSelected();
    } else if (event.key === 'ArrowLeft' && row) {
      if (expanded.has(row.path)) expanded.delete(row.path); else selected = parent(row.path);
      render(true);
    } else if (['ArrowRight','Enter',' '].includes(event.key) && row) void activate(row, event.key === 'ArrowRight');
    else if (event.key === 'Escape') focusTerminal();
    else if (event.key === 'F5') void refresh();
    else return;
    event.preventDefault(); event.stopPropagation();
  };
  panel.onkeydown = event => { if (event.key === 'Escape') { closePreview(); event.preventDefault(); } };
  get('files-refresh').onclick = () => void refresh();
  get('file-reload').onclick = () => { if (previewPath) void preview(previewPath); };
  get('files-folder').onclick = chooseFolder;
  get('files-menu').onclick = () => {
    const anchor=get('files-menu');
    if(anchor.getAttribute('aria-expanded')==='true'){dismissMenu();return;}
    showMenu({anchor,label:'Explorer actions',items:[
      {id:'files-menu-folder',label:'Open folder…',action:chooseFolder},
      {id:'files-menu-refresh',label:'Refresh files',action:()=>void refresh()},
      {id:'files-hidden',label:'Show hidden files',checked:hidden,action:()=>{hidden=!hidden;void refresh();}},
      {id:'files-collapse',label:'Collapse folders',action:()=>{expanded.clear();expanded.add('');render(true);}},
      {id:'files-menu-close',label:'Close Files',action:toggle}
    ]});
  };
  get('file-close').onclick = closePreview;
  return {setRoot, toggle, refresh, preview};
}
