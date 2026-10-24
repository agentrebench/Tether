"""Bundle the syntax worker from locked, already installed desktop dependencies."""
from pathlib import Path
import json
import shutil
import subprocess

ROOT = Path(__file__).resolve().parents[1]


def build_syntax(vendor: Path):
    vendor.mkdir(parents=True, exist_ok=True)
    result = subprocess.run([str(ROOT / 'desktop/node_modules/.bin/esbuild'),
                    str(ROOT / 'native/web/highlight-worker.entry.js'), '--bundle', '--minify',
                    '--format=iife', '--target=safari17'], check=True, capture_output=True, text=True)
    (vendor / 'highlight-source.js').write_text('window.tetherSyntaxWorkerSource=' + json.dumps(result.stdout) + ';\n')
    shutil.copy2(ROOT / 'desktop/node_modules/highlight.js/LICENSE', vendor / 'highlight.js-LICENSE')
