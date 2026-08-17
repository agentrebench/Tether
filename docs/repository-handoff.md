# Clean repository handoff

The tracked example configuration contains provider metadata and empty key
fields only. Runtime credentials belong in provider environment variables or
`~/.tether/config.json`; neither is part of the repository.

For a new repository, prefer exporting the source tree rather than copying the
working directory wholesale:

```bash
./scripts/export_clean.sh /path/to/new-tether
cd /path/to/new-tether
git init
```

The export excludes Git history, local editor/agent state, caches, virtual
environments, Python and Rust build products, `node_modules`, packaged apps,
local `.env` files, and `config/config.json`. It deliberately retains lockfiles,
the sanitized `config/config.json.example`, desktop sources, tests, docs, and CI
workflows.

Before publishing the new repository, run:

```bash
python3 -m venv .venv
.venv/bin/python -m pip install --upgrade pip
.venv/bin/python -m pip install -e '.[dev]'
.venv/bin/python -m pytest -q

cd desktop
npm ci
npm run build
cargo fmt --check --manifest-path src-tauri/Cargo.toml
cargo clippy --manifest-path src-tauri/Cargo.toml --all-targets --all-features -- -D warnings
cargo test --manifest-path src-tauri/Cargo.toml
```

Creating the virtual environment and installing the editable package are
required in a fresh clone: the repository root is mapped to the `tether`
package by `pyproject.toml`, and pytest is a development dependency rather than
a runtime dependency.

Do not copy a populated `~/.tether/config.json`, shell startup files containing
provider keys, generated `dist/` installers, or an existing `.git/` directory
into the new repository.
