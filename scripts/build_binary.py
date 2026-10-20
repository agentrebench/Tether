#!/usr/bin/env python3
"""Build a standalone CLI for the current OS/architecture (requires .[bundle])."""
from __future__ import annotations

import hashlib
import os
from pathlib import Path
import platform
import shutil
import subprocess
import sys
import tarfile


def main() -> int:
    root = Path(__file__).resolve().parents[1]
    build = root / "build" / "binary"
    source = build / "source"
    package = source / "tether"
    if source.exists():
        shutil.rmtree(source)
    package.mkdir(parents=True)
    # Stage only runtime code and built-in skills. Never collect the checkout's
    # arbitrary data (local config, keys, .venv, desktop build, or test fixtures).
    for path in root.glob("*.py"):
        shutil.copy2(path, package / path.name)
    for name in ("agents", "core", "engine", "runtime", "session", "tools", "ui"):
        for path in (root / name).rglob("*.py"):
            target = package / path.relative_to(root)
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(path, target)
    shutil.copytree(root / "skills_builtin", package / "skills_builtin")
    env = dict(os.environ, PYTHONPATH=str(source))
    env.setdefault("PYINSTALLER_CONFIG_DIR", str(build / "cache"))
    subprocess.run([
        sys.executable, "-m", "PyInstaller", "--noconfirm", "--clean",
        "--onefile", "--name", "tether", "--console",
        "--distpath", str(root / "dist"), "--workpath", str(build / "work"),
        "--specpath", str(build), "--paths", str(source),
        "--collect-submodules", "tether",
        "--add-data", f"{package / 'skills_builtin'}{os.pathsep}tether/skills_builtin",
        str(package / "__main__.py"),
    ], env=env, cwd=build, check=True)

    system = {"Darwin": "darwin", "Linux": "linux"}.get(platform.system())
    arch = {"aarch64": "arm64", "arm64": "arm64", "x86_64": "x86_64", "AMD64": "x86_64"}.get(platform.machine())
    if not system or not arch:
        raise SystemExit("Release archives currently support macOS and Linux on arm64/x86_64.")
    binary = root / "dist" / "tether"
    subprocess.run([str(binary), "--version"], check=True)
    archive = root / "dist" / f"tether-{system}-{arch}.tar.gz"
    with tarfile.open(archive, "w:gz") as bundle:
        bundle.add(binary, arcname="tether")
        bundle.add(root / "LICENSE", arcname="LICENSE")
    checksum = hashlib.sha256(archive.read_bytes()).hexdigest()
    archive.with_name(archive.name + ".sha256").write_text(f"{checksum}  {archive.name}\n")
    print(f"Built {binary}\nRelease archive: {archive}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
