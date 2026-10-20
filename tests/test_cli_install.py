"""Installation must not follow old symlinks or duplicate shell setup."""
import os
import hashlib
from pathlib import Path
import platform
import subprocess
import tarfile

import pytest


INSTALLER = Path(__file__).resolve().parents[1] / "install.sh"


def fake_binary(path):
    path.write_text('#!/bin/sh\nprintf "tether test\\n"\n')
    path.chmod(0o755)
    return path


def install(binary, destination, *args, env=None):
    return subprocess.run(
        ["sh", str(INSTALLER), "--binary", str(binary), *args],
        env={**os.environ, "TETHER_INSTALL_DIR": str(destination), **(env or {})},
        capture_output=True, text=True,
    )


def test_install_replaces_pipx_symlink_without_overwriting_venv(tmp_path):
    destination = tmp_path / "bin"
    destination.mkdir()
    old = tmp_path / "venv-entry"
    old.write_text("original venv entry")
    (destination / "tether").symlink_to(old)
    result = install(fake_binary(tmp_path / "binary"), destination, "--no-modify-path")
    assert result.returncode == 0, result.stderr
    assert old.read_text() == "original venv entry"
    assert not (destination / "tether").is_symlink()
    assert subprocess.check_output([str(destination / "tether"), "--version"], text=True) == "tether test\n"


def test_zsh_path_setup_is_quoted_and_idempotent(tmp_path):
    destination = tmp_path / "Tether's binaries"
    dotdir = tmp_path / "shell"
    dotdir.mkdir()
    rc = dotdir / ".zshrc"
    rc.write_text("# existing config\n")
    binary = fake_binary(tmp_path / "binary")
    env = {"SHELL": "/bin/zsh", "ZDOTDIR": str(dotdir)}
    for _ in range(2):
        result = install(binary, destination, env=env)
        assert result.returncode == 0, result.stderr
    assert rc.read_text().count("# Tether CLI") == 1
    assert rc.read_text().startswith("# existing config\n")
    check = subprocess.run(["sh", "-c", '. "$1"; command -v tether', "sh", str(rc)],
                           capture_output=True, text=True, check=True)
    assert check.stdout.strip() == str(destination / "tether")


def test_failed_binary_validation_keeps_existing_install(tmp_path):
    destination = tmp_path / "bin"
    destination.mkdir()
    existing = fake_binary(destination / "tether")
    before = existing.read_bytes()
    broken = tmp_path / "broken"
    broken.write_text("#!/bin/sh\nexit 1\n")
    result = install(broken, destination, "--no-modify-path")
    assert result.returncode != 0
    assert existing.read_bytes() == before


@pytest.mark.parametrize("valid_checksum", [True, False])
def test_release_download_checks_checksum_before_installing(tmp_path, valid_checksum):
    assets = tmp_path / "assets"
    assets.mkdir()
    os_name = "darwin" if platform.system() == "Darwin" else "linux"
    arch = "arm64" if platform.machine() in {"arm64", "aarch64"} else "x86_64"
    archive = assets / f"tether-{os_name}-{arch}.tar.gz"
    binary = fake_binary(tmp_path / "binary")
    with tarfile.open(archive, "w:gz") as bundle:
        bundle.add(binary, arcname="tether")
    digest = hashlib.sha256(archive.read_bytes()).hexdigest() if valid_checksum else "0" * 64
    archive.with_name(archive.name + ".sha256").write_text(f"{digest}  {archive.name}\n")
    commands = tmp_path / "commands"
    commands.mkdir()
    curl = commands / "curl"
    curl.write_text('''#!/bin/sh
for arg; do case "$arg" in https:*) url=$arg ;; esac; done
while [ "$1" != -o ]; do shift; done
shift
cp "$TETHER_TEST_ASSETS/${url##*/}" "$1"
''')
    curl.chmod(0o755)
    destination = tmp_path / "installed"
    destination.mkdir()
    existing = destination / "tether"
    existing.write_text("previous install")
    result = subprocess.run(["sh", str(INSTALLER), "--no-modify-path"], capture_output=True, text=True,
                            env={**os.environ, "PATH": str(commands) + os.pathsep + os.environ["PATH"],
                                 "TETHER_TEST_ASSETS": str(assets), "TETHER_INSTALL_DIR": str(destination)})
    assert (result.returncode == 0) == valid_checksum, result.stderr
    if valid_checksum:
        assert existing.read_bytes() == binary.read_bytes()
    else:
        assert existing.read_text() == "previous install"
