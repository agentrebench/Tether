#!/usr/bin/env python3
"""Run the native explorer in a temporary macOS app with isolated fixture files.

Requires a compiled native app and `npm ci` in desktop/. Opens a test window;
never installs/replaces Tether, reads user shell configuration, or calls a model.
"""
from build_native_web import build_syntax

import argparse
import json
import os
from pathlib import Path
import shlex
import shutil
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--build', type=Path, default=ROOT / 'build/native/explorer')
    parser.add_argument('--output', type=Path, required=True, help='New directory for JSON and snapshots')
    parser.add_argument('--small', action='store_true', help='Use the minimum 680 by 440 window')
    parser.add_argument('--themes', action='store_true', help='Check the bundled theme picker and real preview shells')
    parser.add_argument('--agent-ui', action='store_true', help='Check the agent picker and a model-free conversation fixture')
    parser.add_argument('--startup', action='store_true', help='Measure native pane creation and readiness using isolated shell configuration')
    parser.add_argument('--panes', action='store_true', help='Check Terminal, Agent and Monitor in one resizable session; requires packaged btop')
    parser.add_argument('--resize', action='store_true', help='Exercise divider dragging through native Cocoa mouse input')
    parser.add_argument('--pane-controls', action='store_true', help='Check native right-click moves, title dragging, Agent resize and Explorer menus')
    parser.add_argument('--conversation', action='store_true', help='Check hover scrollbars and CLI conversation colors with a model-free PTY fixture')
    parser.add_argument('--workbench', action='store_true', help='Check source previews, layout and appearance')
    parser.add_argument('--engine', type=Path, help='Use a frozen executable or unpacked engine directory instead of development Python')
    parser.add_argument('--packaged', action='store_true',
                        help='Test the signed app bundle as built, preserving its web assets and bundled engine')
    args = parser.parse_args()
    if args.packaged and args.engine:
        parser.error('--packaged uses the bundled engine; do not also supply --engine')
    if args.packaged:
        subprocess.run(['/usr/bin/codesign', '--verify', '--deep', '--strict',
                        str(args.build / 'Tether.app')], check=True)
    args.output = args.output.resolve()
    args.output.mkdir(parents=True, exist_ok=False)
    with tempfile.TemporaryDirectory(prefix='tether-explorer-') as temp:
        temp = Path(temp)
        app = temp / 'Tether.app'
        shutil.copytree(args.build / 'Tether.app', app, symlinks=True)
        resources = app / 'Contents/Resources'
        resources.mkdir(exist_ok=True)
        if not args.packaged:
            shutil.copytree(ROOT / 'native/web', resources / 'web', dirs_exist_ok=True)
        web = resources / 'web'
        vendor = web / 'vendor'
        if not args.packaged:
            vendor.mkdir(exist_ok=True)
            build_syntax(vendor)
            for package, source, target in (('xterm', 'lib/xterm.js', 'xterm.js'),
                                             ('xterm', 'css/xterm.css', 'xterm.css'),
                                             ('addon-fit', 'lib/addon-fit.js', 'addon-fit.js')):
                shutil.copy2(ROOT / f'desktop/node_modules/@xterm/{package}/{source}', vendor / target)
        smoke = 'conversation_smoke.js' if args.conversation else 'pane_controls_smoke.js' if args.pane_controls else 'resize_smoke.js' if args.resize else 'panes_smoke.js' if args.panes else 'startup_smoke.js' if args.startup else 'agent_ui_smoke.js' if args.agent_ui else 'workbench_smoke.js' if args.workbench else 'theme_smoke.js' if args.themes else 'explorer_smoke.js'
        shutil.copy2(ROOT / 'native/tests' / smoke, web / 'explorer_smoke.js')
        if args.conversation:
            replay = json.loads((ROOT / 'native/tests/fixtures/cli_conversations.json').read_text())
            script = web / 'explorer_smoke.js'
            script.write_text('window.cliReplay = ' + json.dumps(replay) + ';\n' + script.read_text())
        index = web / 'index.html'
        index.write_text(index.read_text().replace('<script src="app.js">', '<script src="explorer_smoke.js"></script><script src="app.js">'))
        binary = resources / 'bin'
        binary.mkdir(exist_ok=True)
        engine = binary / 'tether'
        if args.packaged:
            if not engine.is_file():
                raise SystemExit('The packaged app is missing its bundled engine.')
        elif args.engine:
            if args.engine.is_dir():
                shutil.copytree(args.engine, binary, dirs_exist_ok=True, symlinks=True)
            else:
                shutil.copy2(args.engine, engine)
        else:
            engine.write_text(f'#!/bin/sh\nexec {shlex.quote(sys.executable)} -m tether "$@"\n')
        if not args.packaged:
            engine.chmod(0o755)
        project = temp / 'project'
        project.mkdir()
        (project / 'src').mkdir()
        if args.conversation:
            # A real foreground executable with deterministic transcript output;
            # this is explicitly a fixture, never a model CLI or API request.
            source = project / 'conversation_fixture.c'
            source.write_text('#include <stdio.h>\n#include <unistd.h>\nint main(void) {\n'
                              'for (int i=0;i<90;i++) printf("scrollback line %d\\n",i);\n'
                              'puts("\\033[0m❯ Human question\\n⏺ Assistant response\\n  Continued response");\n'
                              'puts("\\033[31mError: keep red\\033[0m\\n❯ "); fflush(stdout);\n'
                              'sleep(20); return 0; }\n')
            subprocess.run(['/usr/bin/clang', '-Wall', '-Wextra', '-Werror', str(source), '-o', str(project / 'claude')], check=True)
        if args.workbench:
            for i in range(350):
                (project / f'fixture-{i:03}.txt').write_text('fixture')
        (project / 'src/main.py').write_text('# Example project\ndef hello():\n    print("Hello from the explorer")\n')
        if args.workbench:
            (project / 'src/empty.py').write_text('')
            (project / 'src/main.c').write_text('#include <stdio.h>\nint main(void) { return 0; }\n')
            (project / 'src/main.js').write_text('const hello = () => "Hello JavaScript";\n')
            (project / 'src/tool').write_text('#!/usr/bin/env python3\nprint(42)\n')
        (project / 'README.md').write_text('# Tether Explorer\nBrowse your files beside a live terminal.\n')
        (project / '.hidden').write_text('hidden fixture')
        (project / 'data.bin').write_bytes(b'\0binary')
        (project / 'unsafe.html').write_text('<img src=x onerror="window.INJECTED=true">\n')
        (project / 'large.txt').write_text('bounded preview\n' * 30000)
        outside = temp / 'outside'
        outside.mkdir()
        (outside / 'secret.txt').write_text('MUST_NOT_READ')
        (project / 'outside').symlink_to(outside, target_is_directory=True)
        home = temp / 'home'
        home.mkdir()
        (home / '.zshrc').write_text("PROMPT='EXPLORER> '; HISTFILE=''\n")
        config = temp / 'config'
        config.mkdir()
        (config / 'config.json').write_text(json.dumps({'terminal_shell':'zsh', 'terminal_theme':'inherit'}))
        report = project / 'report.json'
        env = dict(os.environ, HOME=str(home), ZDOTDIR=str(home), XDG_CONFIG_HOME=str(home),
                   TETHER_CONFIG_DIR=str(config), TETHER_APP_SMOKE_REPORT=str(report),
                   TETHER_APP_SMOKE_SMALL='1' if args.small else '', PYTHONPATH=str(ROOT.parent),
                   ASAN_OPTIONS='detect_leaks=0')
        if args.agent_ui or args.pane_controls:
            # The tool runner uses NO_COLOR=1; exercise the normal GUI palette.
            env.pop('NO_COLOR', None)
            # Exercise the shipping native -> picker -> cmd_start -> REPL route.
            # Only authentication is a fixture. Never execute a real model CLI.
            fake_bin = temp / 'fixture-bin'
            fake_bin.mkdir()
            codex = fake_bin / 'codex'
            invocations = project / 'codex-invocations.txt'
            codex.write_text('#!/bin/sh\n' +
                             'printf "%s\\n" "$*" >> ' + shlex.quote(str(invocations)) + '\n' +
                             'if [ "$*" = "login status" ]; then\n' +
                             '  echo "Logged in using ChatGPT"\n  exit 0\nfi\nexit 91\n')
            codex.chmod(0o755)
            env['TETHER_CODEX_BIN'] = str(codex)
            env['PATH'] = str(fake_bin) + os.pathsep + env.get('PATH', '')
            (config / 'config.json').write_text(json.dumps({
                'terminal_shell': 'zsh', 'terminal_theme': 'inherit',
                'provider': 'codex', 'api_model': 'gpt-6-astra',
                'context_size': 1000000, 'reasoning_effort': 'medium',
                'codebase_model_enabled': False, 'self_learning': False,
            }))
        with (args.output / 'process.log').open('w') as log:
            subprocess.run([str(app / 'Contents/MacOS/Tether')], env=env, stdout=log, stderr=log,
                           timeout=150 if args.startup else 60, check=True)
        for path in project.glob('report.json*'):
            shutil.copy2(path, args.output / path.name)
        result = json.loads(report.read_text())
        if args.agent_ui or args.pane_controls:
            calls = invocations.read_text().splitlines() if invocations.exists() else []
            result['codexInvocations'] = calls
            if len(calls) < 2 or any(call != 'login status' for call in calls):
                result.update(ok=False, error='Unexpected or missing Codex fixture invocation')
            (args.output / 'report.json').write_text(json.dumps(result, indent=2))
        if args.themes and args.small and result.get('ok'):
            if result.get('viewport') != {'width':680, 'height':440}:
                result.update(ok=False, error='Test window did not retain its requested 680 by 440 size.')
                (args.output / 'report.json').write_text(json.dumps(result, indent=2))
        print(json.dumps(result, indent=2))
        return 0 if result['ok'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
