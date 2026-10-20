#!/usr/bin/env python3
"""Build a self-contained Apple Silicon macOS 26+ terminal application.

Network downloads require --fetch. Inputs are pinned and hash checked; an
offline rebuild uses build/native/vendor and installed npm dependencies.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import platform
import shutil
import subprocess
import sys
import tarfile
import urllib.request

ROOT = Path(__file__).resolve().parents[1]
VENDOR = ROOT / "build/native/vendor"
BTOP_SHA = "37a9d33566825cff7b0e3e0c4941b2f0a7718405c3c493fb71eaf7100049f429"
ASSETS = {
    "TetherMono.ttf": ("https://raw.githubusercontent.com/ryanoasis/nerd-fonts/v3.4.0/patched-fonts/FiraCode/Regular/FiraCodeNerdFontMono-Regular.ttf", "ad88c69cb6a497db9f2714e4b414817aabbee621484a1560bfdb3fd73abdd564"),
    "FiraCode-LICENSE": ("https://raw.githubusercontent.com/tonsky/FiraCode/6.2/LICENSE", "1d41e10031ab125302780a05ec4c91d218e47db0c7e37cf315cce5e608cdc25c"),
    "NerdFonts-LICENSE": ("https://raw.githubusercontent.com/ryanoasis/nerd-fonts/v3.4.0/LICENSE", "1f6ad4edae6479aaace3112ede5279a23284ae54b2a34db66357aef5f64df160"),
}


def run(*args, **kwargs):
    subprocess.run([str(arg) for arg in args], check=True, **kwargs)


def verify(path, expected):
    if hashlib.sha256(path.read_bytes()).hexdigest() != expected:
        raise SystemExit(f"Checksum mismatch: {path}")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--fetch", action="store_true", help="download missing pinned vendor inputs")
    parser.add_argument("--reuse-engine", action="store_true", help="use an already rebuilt dist/tether")
    args = parser.parse_args()
    if platform.system() != "Darwin" or platform.machine() != "arm64":
        raise SystemExit("This package target currently requires Apple Silicon macOS 26+.")
    VENDOR.mkdir(parents=True, exist_ok=True)
    for name, (url, digest) in ASSETS.items():
        path = VENDOR / name
        if not path.exists() and args.fetch:
            with urllib.request.urlopen(url, timeout=60) as response:
                data = response.read(8 * 1024 * 1024 + 1)
            if len(data) > 8 * 1024 * 1024:
                raise SystemExit(f"Vendor input exceeds size bound: {name}")
            path.write_bytes(data)
        if not path.exists():
            raise SystemExit(f"Missing {name}; rebuild with --fetch.")
        verify(path, digest)
    bottles = list((VENDOR / "cache/downloads").glob("*--btop--1.4.7.arm64_tahoe.bottle.1.tar.gz"))
    if not bottles and args.fetch:
        brew = shutil.which("brew") or "/opt/homebrew/bin/brew"
        run(brew, "fetch", "--bottle-tag=arm64_tahoe", "btop", env=dict(os.environ, HOMEBREW_NO_AUTO_UPDATE="1", HOMEBREW_CACHE=str(VENDOR / "cache")))
        bottles = list((VENDOR / "cache/downloads").glob("*--btop--1.4.7.arm64_tahoe.bottle.1.tar.gz"))
    if len(bottles) != 1:
        raise SystemExit("Missing pinned btop 1.4.7 bottle; use --fetch or supply it in build/native/vendor/cache/downloads.")
    verify(bottles[0], BTOP_SHA)
    for package, version in (("xterm", "6.0.0"), ("addon-fit", "0.11.0")):
        manifest = ROOT / f"desktop/node_modules/@xterm/{package}/package.json"
        if not manifest.exists() or json.loads(manifest.read_text())["version"] != version:
            raise SystemExit("Run npm ci in desktop/ to install the locked terminal renderer.")
    if not args.reuse_engine:
        run(sys.executable, ROOT / "scripts/build_binary.py")
    engine = ROOT / "dist/tether"
    if not engine.exists():
        raise SystemExit("Build dist/tether first, or omit --reuse-engine.")
    cmake = shutil.which("cmake") or "/opt/homebrew/bin/cmake"
    build = ROOT / "build/native/release"
    options = ["-G", "Ninja"] if shutil.which("ninja") and not (build / "CMakeCache.txt").exists() else []
    run(cmake, "-S", ROOT / "native", "-B", build, "-DCMAKE_BUILD_TYPE=Release", *options)
    run(cmake, "--build", build, "--parallel", "4")
    run(shutil.which("ctest") or "/opt/homebrew/bin/ctest", "--test-dir", build, "--output-on-failure")
    app = ROOT / "dist/macos/Tether.app"
    if app.exists():
        shutil.rmtree(app)
    shutil.copytree(build / "Tether.app", app)
    resources = app / "Contents/Resources"
    resources.mkdir(exist_ok=True)
    shutil.copytree(ROOT / "native/web", resources / "web")
    web_vendor = resources / "web/vendor"
    web_vendor.mkdir()
    licenses = resources / "licenses"
    licenses.mkdir()
    for package, source, target in (("xterm", "lib/xterm.js", "xterm.js"), ("xterm", "css/xterm.css", "xterm.css"), ("addon-fit", "lib/addon-fit.js", "addon-fit.js")):
        shutil.copy2(ROOT / f"desktop/node_modules/@xterm/{package}/{source}", web_vendor / target)
    for package in ("xterm", "addon-fit"):
        shutil.copy2(ROOT / f"desktop/node_modules/@xterm/{package}/LICENSE", licenses / (package + "-LICENSE"))
    for name in ASSETS:
        shutil.copy2(VENDOR / name, (web_vendor if name.endswith(".ttf") else licenses) / name)
    (resources / "bin").mkdir()
    shutil.copy2(engine, resources / "bin/tether")
    # Extract only explicitly selected regular files; never tar paths/symlinks.
    with tarfile.open(bottles[0]) as archive:
        for member in archive.getmembers():
            prefix = "btop/1.4.7/"
            relative = member.name.removeprefix(prefix)
            if not member.name.startswith(prefix) or not member.isfile() or ".." in Path(relative).parts:
                continue
            if relative == "bin/btop" or relative.startswith("share/btop/themes/"):
                target = resources / relative
            elif relative in {"LICENSE", "README.md", "sbom.spdx.json"}:
                target = licenses / ("btop-" + relative)
            else:
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            with archive.extractfile(member) as source, target.open("wb") as destination:
                shutil.copyfileobj(source, destination)
            target.chmod(0o755 if relative == "bin/btop" else 0o644)
    shutil.copy2(ROOT / "LICENSE", licenses / "Tether-LICENSE")
    shutil.copy2(ROOT / "desktop/src-tauri/icons/icon.icns", resources / "tether.icns")
    (licenses / "NOTICE.txt").write_text("Tether native terminal 0.4.0\nPython agent engine 0.3.1\nbtop 1.4.7, Apache-2.0, https://github.com/aristocratos/btop\nxterm 6.0.0 / fit 0.11.0, MIT, https://github.com/xtermjs/xterm.js\nFira Code 6.2 patched by Nerd Fonts 3.4.0, OFL-1.1 and accompanying glyph licenses.\nTether Mono is a CSS alias, not a modified font.\nSee accompanying license files and btop source URL for the corresponding source.\n")
    identity = os.environ.get("APPLE_SIGNING_IDENTITY") or "-"
    sign_options = ["--force", "--sign", identity]
    if identity != "-":
        sign_options += ["--options", "runtime", "--timestamp"]
    for binary in (resources / "bin/tether", resources / "bin/btop", app):
        run("/usr/bin/codesign", *sign_options, binary)
    run("/usr/bin/codesign", "--verify", "--deep", "--strict", app)
    run(resources / "bin/btop", "--version")
    (ROOT / "build/native/package.json").write_text(json.dumps({"app":str(app),"btop_sha256":BTOP_SHA,"assets":ASSETS,"platform":platform.platform(),"engine_sha256":hashlib.sha256(engine.read_bytes()).hexdigest()},indent=2))
    print(f"Built {app}\nOpen in Finder or run: open '{app}'")


if __name__ == "__main__":
    main()
