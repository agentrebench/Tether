'use strict';
// Palette order: background, surface, raised, border, text, muted, accent,
// selection, keyword, string, number, function, comment, light chrome.
const appThemes = {
  tether:{name:'Tether Dark',p:['#0c1014','#12191f','#19232b','#25323d','#dce7ed','#8395a3','#83e1c2','#294b48','#c9a3e8','#91d6ad','#e8c991','#8bbbef','#8395a3',false]},
  dark:{name:'VS Code Dark',p:['#1e1e1e','#252526','#333333','#454545','#d4d4d4','#999999','#75beff','#264f78','#c586c0','#ce9178','#b5cea8','#dcdcaa','#6a9955',false]},
  light:{name:'VS Code Light',p:['#ffffff','#f3f3f3','#e8e8e8','#cecece','#242424','#616161','#005fb8','#cce4ff','#af00db','#a31515','#098658','#795e26','#008000',true]},
  gruvbox:{name:'Gruvbox Dark',p:['#282828','#32302f','#3c3836','#665c54','#ebdbb2','#a89984','#fabd2f','#504945','#fb4934','#b8bb26','#d3869b','#fabd2f','#928374',false]},
  'gruvbox-light':{name:'Gruvbox Light',p:['#fbf1c7','#f2e5bc','#ebdbb2','#bdae93','#3c3836','#665c54','#9d0006','#d5c4a1','#9d0006','#79740e','#8f3f71','#b57614','#79705b',true]},
  dracula:{name:'Dracula',p:['#282a36','#21222c','#343746','#6272a4','#f8f8f2','#a5aecb','#bd93f9','#44475a','#ff79c6','#f1fa8c','#bd93f9','#50fa7b','#909bbf',false]},
  solarized:{name:'Solarized Dark',p:['#002b36','#073642','#0a404d','#586e75','#93a1a1','#839496','#2aa198','#164652','#859900','#2aa198','#d33682','#b58900','#748a8f',false]}
};
function applyAppTheme(id, panes, send) {
  const theme=appThemes[id]||appThemes.tether;
  const [bg,surface,elevated,line,text,muted,accent,selection,keyword,string,number,fn,comment,light]=theme.p;
  const vars={bg,surface,elevated,line,text,muted,accent,selection,keyword,string,number,function:fn,comment,response:accent,'on-accent':light?'#ffffff':'#101010'};
  for(const [key,value]of Object.entries(vars))document.documentElement.style.setProperty('--'+key,value);
  document.documentElement.style.colorScheme=light?'light':'dark';
  document.documentElement.dataset.theme=appThemes[id]?id:'tether';
  const terminal={background:bg,foreground:text,cursor:accent,selectionBackground:selection,
    black:light?'#353535':bg,red:light?'#a12622':'#f38d91',green:light?'#347329':'#91d6ad',yellow:light?'#875e00':'#e8c991',blue:light?'#145ea8':'#8bbbef',magenta:keyword,cyan:light?'#007578':'#83d4d6',white:light?'#707070':text,
    brightBlack:muted,brightRed:light?'#c23030':'#ffacaf',brightGreen:light?'#267f35':'#b2efca',brightYellow:light?'#977100':'#f7dfad',brightBlue:light?'#005fb8':'#add5ff',brightMagenta:light?'#a03ab8':'#e0c1f8',brightCyan:light?'#007f8b':'#acf0ed',brightWhite:light?'#242424':'#ffffff'};
  for(const pane of panes.values())pane.term.options.theme=terminal;
  send({type:'appearance',light,background:bg});
  return terminal;
}
