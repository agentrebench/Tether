"""Skills — on-demand markdown knowledge documents the agent loads only when
needed (progressive disclosure), compatible with the agentskills.io / Claude
Code SKILL.md format.

Layout (single source of truth + scannable extras):

    <repo>/.tether/skills/<name>/SKILL.md   # project-local, highest precedence
    ~/.tether/skills/<name>/SKILL.md        # user-writable
    <package>/skills_builtin/<name>/SKILL.md  # shipped, read-only
    <config.skills_dirs entries>/<name>/SKILL.md

A SKILL.md is YAML frontmatter + a markdown body:

    ---
    name: PR Workflow
    description: Branch, commit, and open a pull request the right way.
    version: 1.0.0
    category: git
    platforms: [macos, linux]
    requires_toolsets: [bash]
    fallback_for_toolsets: [web_premium]
    required_environment_variables: [GITHUB_TOKEN]
    ---
    ## When to Use
    ...

Three-level loading keeps tokens cheap: `skills_list` returns only
name/description/category, `skill_view(name)` loads the full body, and
`skill_view(name, path)` pulls one bundled reference file.

No pyyaml dependency — the frontmatter we emit and consume is a small, flat
subset (scalars, inline `[a, b]` lists, and block `- item` lists), parsed here.
"""
from __future__ import annotations

import os
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path

from .config import CONFIG_DIR

USER_SKILLS_DIR = CONFIG_DIR / "skills"
BUILTIN_SKILLS_DIR = Path(__file__).resolve().parent.parent / "skills_builtin"

# Bundles — tiny YAML aliases that load several skills under one command.
# User bundles live beside the user skills; built-ins ship under a `bundles/`
# subdir of skills_builtin (it has no SKILL.md so the skill scanner ignores it).
USER_BUNDLES_DIR = CONFIG_DIR / "bundles"
BUILTIN_BUNDLES_DIR = BUILTIN_SKILLS_DIR / "bundles"

# Map sys.platform -> the tokens we accept in a skill's `platforms:` list.
_PLATFORM_ALIASES = {
    "darwin": "macos",
    "linux": "linux",
    "win32": "windows",
    "cygwin": "windows",
}


def current_platform() -> str:
    return _PLATFORM_ALIASES.get(sys.platform, sys.platform)


def slugify(name: str) -> str:
    """Canonical command/dir slug for a skill name: 'PR Workflow' -> 'pr-workflow'."""
    s = re.sub(r"[^a-z0-9]+", "-", (name or "").strip().lower())
    return s.strip("-") or "skill"


# --------------------------------------------------------------------------
# Minimal frontmatter parser
# --------------------------------------------------------------------------
def _coerce(raw: str):
    v = raw.strip()
    if not v:
        return ""
    if (v[0], v[-1]) in (('"', '"'), ("'", "'")) and len(v) >= 2:
        return v[1:-1]
    if v.startswith("[") and v.endswith("]"):
        inner = v[1:-1].strip()
        if not inner:
            return []
        return [_coerce(p) for p in _split_inline_list(inner)]
    low = v.lower()
    if low in ("true", "false"):
        return low == "true"
    if re.fullmatch(r"-?\d+", v):
        return int(v)
    return v


def _split_inline_list(inner: str) -> list[str]:
    """Split 'a, "b, c", d' on top-level commas only."""
    parts, buf, quote = [], [], None
    for ch in inner:
        if quote:
            buf.append(ch)
            if ch == quote:
                quote = None
        elif ch in ("'", '"'):
            quote = ch
            buf.append(ch)
        elif ch == ",":
            parts.append("".join(buf))
            buf = []
        else:
            buf.append(ch)
    if buf:
        parts.append("".join(buf))
    return [p for p in (p.strip() for p in parts) if p]


def _parse_flat_mapping(lines: list[str]) -> dict:
    """Parse a flat YAML mapping (scalars, inline `[a, b]` lists, and block
    `- item` lists). Shared by the SKILL.md frontmatter and the bundle files."""
    meta: dict = {}
    pending_key = None  # key currently collecting a block list
    for ln in lines:
        if not ln.strip() or ln.lstrip().startswith("#"):
            continue
        stripped = ln.strip()
        if stripped.startswith("- ") and pending_key is not None:
            meta.setdefault(pending_key, [])
            if isinstance(meta[pending_key], list):
                meta[pending_key].append(_coerce(stripped[2:]))
            continue
        # Hyphens allowed: Claude-style frontmatter uses kebab-case keys
        # (argument-hint) and dropping them silently loses metadata.
        m = re.match(r"^([A-Za-z0-9_-]+)\s*:\s*(.*)$", stripped)
        if not m:
            continue
        key, val = m.group(1), m.group(2)
        if val == "":  # block list / mapping follows
            pending_key = key
            meta.setdefault(key, [])
        else:
            pending_key = None
            meta[key] = _coerce(val)
    return meta


def parse_yaml_flat(text: str) -> dict:
    """Parse a standalone flat-YAML document (a bundle file). No body, no
    frontmatter fences — just the mapping."""
    return _parse_flat_mapping(text.splitlines())


def parse_frontmatter(text: str) -> tuple[dict, str]:
    """Return (frontmatter_dict, body). Tolerates a missing/blank frontmatter."""
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        return {}, text
    fm_lines, body_start = [], None
    for i in range(1, len(lines)):
        if lines[i].strip() == "---":
            body_start = i + 1
            break
        fm_lines.append(lines[i])
    if body_start is None:  # unterminated frontmatter — treat whole thing as body
        return {}, text
    body = "\n".join(lines[body_start:]).strip("\n")
    return _parse_flat_mapping(fm_lines), body


@dataclass
class Skill:
    name: str
    slug: str
    description: str = ""
    version: str = ""
    category: str = "general"
    platforms: list[str] = field(default_factory=list)
    requires_toolsets: list[str] = field(default_factory=list)
    fallback_for_toolsets: list[str] = field(default_factory=list)
    required_environment_variables: list[str] = field(default_factory=list)
    # One line on WHEN this skill applies — shown in skills_list so the model
    # can pick by trigger, not just by topic. Accepts when_to_use/when-to-use.
    when_to_use: str = ""
    # e.g. "<file-or-dir> [notes]" — shown next to /<slug> in the UI.
    argument_hint: str = ""
    body: str = ""
    directory: Path | None = None
    source: str = "user"  # builtin | user | external | project
    writable: bool = True

    @classmethod
    def from_file(cls, path: Path, source: str, writable: bool) -> "Skill | None":
        try:
            text = path.read_text(errors="replace")
        except OSError:
            return None
        meta, body = parse_frontmatter(text)
        name = str(meta.get("name") or path.parent.name)

        def as_list(key):
            v = meta.get(key, [])
            return v if isinstance(v, list) else ([v] if v else [])

        return cls(
            name=name,
            slug=slugify(name),
            description=str(meta.get("description", "")),
            version=str(meta.get("version", "")),
            category=str(meta.get("category", "general")),
            platforms=[str(p).lower() for p in as_list("platforms")],
            requires_toolsets=[str(p) for p in as_list("requires_toolsets")],
            fallback_for_toolsets=[str(p) for p in as_list("fallback_for_toolsets")],
            required_environment_variables=[str(p) for p in as_list("required_environment_variables")],
            when_to_use=str(meta.get("when_to_use") or meta.get("when-to-use") or ""),
            argument_hint=str(meta.get("argument_hint") or meta.get("argument-hint") or ""),
            body=body,
            directory=path.parent,
            source=source,
            writable=writable,
        )

    def is_available(self, platform: str, toolsets: set[str]) -> bool:
        """Visibility gate: OS restriction + conditional activation."""
        if self.platforms and platform not in self.platforms:
            return False
        if self.requires_toolsets and not set(self.requires_toolsets) <= toolsets:
            return False
        # A fallback skill only surfaces when none of its replaced toolsets exist.
        if self.fallback_for_toolsets and (set(self.fallback_for_toolsets) & toolsets):
            return False
        return True

    def metadata_line(self) -> dict:
        return {"name": self.name, "slug": self.slug,
                "category": self.category, "description": self.description}


@dataclass
class Bundle:
    """A tiny alias that loads several skills under one command. Stored as a
    flat-YAML file (name, description, skills: [...]). Member skills are kept as
    slugs so they resolve against whatever the store currently holds."""
    name: str
    slug: str
    description: str = ""
    skills: list[str] = field(default_factory=list)  # member skill slugs
    platforms: list[str] = field(default_factory=list)
    path: Path | None = None
    source: str = "user"  # builtin | user
    writable: bool = True

    @classmethod
    def from_file(cls, path: Path, source: str, writable: bool) -> "Bundle | None":
        try:
            text = path.read_text(errors="replace")
        except OSError:
            return None
        meta = parse_yaml_flat(text)
        name = str(meta.get("name") or path.stem)

        def as_list(key):
            v = meta.get(key, [])
            return v if isinstance(v, list) else ([v] if v else [])

        members = [slugify(str(s)) for s in as_list("skills") if str(s).strip()]
        if not members:
            return None  # a bundle with no skills is not a bundle
        return cls(
            name=name,
            slug=slugify(name) if meta.get("name") else slugify(path.stem),
            description=str(meta.get("description", "")),
            skills=members,
            platforms=[str(p).lower() for p in as_list("platforms")],
            path=path,
            source=source,
            writable=writable,
        )

    def is_available(self, platform: str) -> bool:
        return not self.platforms or platform in self.platforms


class SkillStore:
    """Scans the skill roots fresh on each query so writes (and external edits)
    are picked up without a restart. Roots earlier in the list win on slug
    collision, EXCEPT the user dir overrides builtin so a user can shadow a
    shipped skill."""

    def __init__(self, extra_dirs: list[str] | None = None):
        # (root, source, writable) — order = precedence (first wins), but the
        # user dir is intentionally placed before builtin so it can shadow.
        self.roots: list[tuple[Path, str, bool]] = [(USER_SKILLS_DIR, "user", True)]
        for d in extra_dirs or []:
            p = Path(d).expanduser()
            if p != USER_SKILLS_DIR:
                self.roots.append((p, "external", True))
        self.roots.append((BUILTIN_SKILLS_DIR, "builtin", False))

        # Bundle roots mirror the skill precedence: user shadows builtin.
        self.bundle_roots: list[tuple[Path, str, bool]] = [(USER_BUNDLES_DIR, "user", True)]
        self.bundle_roots.append((BUILTIN_BUNDLES_DIR, "builtin", False))

        # mtime-keyed parse caches: scans still glob (freshness), but a file
        # is only re-read and re-parsed when it actually changed.
        self._skill_cache: dict[Path, tuple[float, "Skill | None"]] = {}
        self._bundle_cache: dict[Path, tuple[float, "Bundle | None"]] = {}

    @staticmethod
    def _project_dir(kind: str) -> Path | None:
        """The current repo's ``.tether/<kind>`` dir, resolved from the
        process cwd at *scan* time (not construction) so a /cd into another
        repo picks up its skills without a restart."""
        try:
            from .codebase_model.service import _resolve_root
            p = _resolve_root(os.getcwd()) / ".tether" / kind
            return p if p.is_dir() else None
        except Exception:
            return None

    def _roots_now(self) -> list[tuple[Path, str, bool]]:
        """Static roots plus the project root, which takes highest precedence
        so a repo can ship skills that shadow user/builtin ones."""
        proj = self._project_dir("skills")
        if proj is not None and all(proj != r for r, _, _ in self.roots):
            return [(proj, "project", True)] + self.roots
        return self.roots

    def _bundle_roots_now(self) -> list[tuple[Path, str, bool]]:
        proj = self._project_dir("bundles")
        if proj is not None and all(proj != r for r, _, _ in self.bundle_roots):
            return [(proj, "project", True)] + self.bundle_roots
        return self.bundle_roots

    def _scan(self) -> dict[str, Skill]:
        found: dict[str, Skill] = {}
        seen_paths: set[Path] = set()
        for root, source, writable in self._roots_now():
            if not root.is_dir():
                continue
            for skill_md in sorted(root.glob("*/SKILL.md")):
                try:
                    mtime = skill_md.stat().st_mtime
                except OSError:
                    continue
                seen_paths.add(skill_md)
                cached = self._skill_cache.get(skill_md)
                if cached is not None and cached[0] == mtime:
                    sk = cached[1]
                else:
                    sk = Skill.from_file(skill_md, source, writable)
                    self._skill_cache[skill_md] = (mtime, sk)
                if sk and sk.slug not in found:  # first root wins
                    found[sk.slug] = sk
        # drop cache entries for files that vanished
        for stale in set(self._skill_cache) - seen_paths:
            del self._skill_cache[stale]
        return found

    # -- queries -----------------------------------------------------------
    def all(self) -> list[Skill]:
        return list(self._scan().values())

    def available(self, toolsets: set[str], platform: str | None = None) -> list[Skill]:
        platform = platform or current_platform()
        return [s for s in self.all() if s.is_available(platform, toolsets)]

    def get(self, name_or_slug: str) -> Skill | None:
        slug = slugify(name_or_slug)
        return self._scan().get(slug)

    # -- bundles -----------------------------------------------------------
    def _scan_bundles(self) -> dict[str, Bundle]:
        found: dict[str, Bundle] = {}
        seen_paths: set[Path] = set()
        for root, source, writable in self._bundle_roots_now():
            if not root.is_dir():
                continue
            files = sorted(list(root.glob("*.yaml")) + list(root.glob("*.yml")))
            for f in files:
                try:
                    mtime = f.stat().st_mtime
                except OSError:
                    continue
                seen_paths.add(f)
                cached = self._bundle_cache.get(f)
                if cached is not None and cached[0] == mtime:
                    b = cached[1]
                else:
                    b = Bundle.from_file(f, source, writable)
                    self._bundle_cache[f] = (mtime, b)
                if b and b.slug not in found:  # first root wins
                    found[b.slug] = b
        for stale in set(self._bundle_cache) - seen_paths:
            del self._bundle_cache[stale]
        return found

    def bundles(self, platform: str | None = None) -> list[Bundle]:
        platform = platform or current_platform()
        return [b for b in self._scan_bundles().values() if b.is_available(platform)]

    def get_bundle(self, name_or_slug: str) -> Bundle | None:
        return self._scan_bundles().get(slugify(name_or_slug))

    def resolve_command(self, name_or_slug: str):
        """Resolve a /<slug> invocation to ('bundle', Bundle) or ('skill', Skill).
        Bundles win on slug collision (per spec). Returns None if neither."""
        slug = slugify(name_or_slug)
        b = self._scan_bundles().get(slug)
        if b is not None:
            return ("bundle", b)
        s = self._scan().get(slug)
        if s is not None:
            return ("skill", s)
        return None

    def bundle_members(self, bundle: Bundle) -> list[Skill]:
        """The resolvable member skills of a bundle, in declared order, skipping
        any that no longer exist."""
        skills = self._scan()
        return [skills[m] for m in bundle.skills if m in skills]

    def read_reference(self, name_or_slug: str, relpath: str) -> tuple[str | None, str]:
        """Read a bundled reference file, refusing escapes outside the skill dir.
        Returns (content_or_None, error_message)."""
        sk = self.get(name_or_slug)
        if sk is None or sk.directory is None:
            return None, f"No such skill: {name_or_slug}"
        base = sk.directory.resolve()
        target = (base / relpath).resolve()
        if base != target and base not in target.parents:
            return None, f"Path escapes the skill directory: {relpath}"
        if not target.is_file():
            return None, f"No such file in skill '{sk.slug}': {relpath}"
        try:
            return target.read_text(errors="replace"), ""
        except OSError as e:
            return None, f"Could not read {relpath}: {e}"

    def diagnostics(self) -> list[str]:
        """A loud pass over every skill/bundle file, reporting the problems
        the tolerant scanner swallows: unreadable files, missing/unterminated
        frontmatter (degraded metadata), nameless/descriptionless skills, and
        bundle members that don't resolve. Returns human-readable warnings."""
        problems: list[str] = []
        for root, _source, _w in self._roots_now():
            if not root.is_dir():
                continue
            for skill_md in sorted(root.glob("*/SKILL.md")):
                where = f"{skill_md.parent.name} ({skill_md})"
                try:
                    text = skill_md.read_text(errors="replace")
                except OSError as e:
                    problems.append(f"{where}: unreadable — {e}")
                    continue
                lines = text.splitlines()
                if not lines or lines[0].strip() != "---":
                    problems.append(
                        f"{where}: no frontmatter — name falls back to the "
                        f"directory name and the description is empty")
                    continue
                meta, body = parse_frontmatter(text)
                if not meta and body == text:
                    problems.append(f"{where}: unterminated frontmatter "
                                    f"(no closing ---) — metadata ignored")
                    continue
                if not meta.get("name"):
                    problems.append(f"{where}: frontmatter has no name")
                if not meta.get("description"):
                    problems.append(f"{where}: no description — skills_list "
                                    f"shows nothing to pick it by")
                if not body.strip():
                    problems.append(f"{where}: empty body")
        skills = self._scan()
        for root, _source, _w in self._bundle_roots_now():
            if not root.is_dir():
                continue
            for f in sorted(list(root.glob("*.yaml")) + list(root.glob("*.yml"))):
                b = Bundle.from_file(f, _source, _w)
                if b is None:
                    problems.append(f"bundle {f.name} ({f}): unparseable or "
                                    f"has no resolvable member skills")
                    continue
                missing = [m for m in b.skills if m not in skills]
                if missing:
                    problems.append(f"bundle {b.slug} ({f}): unknown member "
                                    f"skill(s): {', '.join(missing)}")
        return problems

    # -- mutations (user dir only) ----------------------------------------
    def write(self, name: str, body: str, *, description: str = "",
              version: str = "1.0.0", category: str = "general",
              platforms: list[str] | None = None,
              when_to_use: str = "", argument_hint: str = "") -> Path:
        """Create or overwrite a user skill's SKILL.md, returning its path."""
        slug = slugify(name)
        skill_dir = USER_SKILLS_DIR / slug
        skill_dir.mkdir(parents=True, exist_ok=True)
        def scalar(value: str) -> str:
            # Frontmatter is one key per line: fold newlines (a multi-line
            # description would otherwise be silently truncated on re-parse).
            return " ".join(str(value).split())

        fm = [f"name: {scalar(name)}"]
        if description:
            fm.append(f"description: {scalar(description)}")
        fm.append(f"version: {version}")
        fm.append(f"category: {category}")
        if when_to_use:
            fm.append(f"when_to_use: {scalar(when_to_use)}")
        if argument_hint:
            fm.append(f"argument_hint: {scalar(argument_hint)}")
        if platforms:
            fm.append(f"platforms: [{', '.join(platforms)}]")
        content = "---\n" + "\n".join(fm) + "\n---\n\n" + body.strip() + "\n"
        path = skill_dir / "SKILL.md"
        path.write_text(content)
        return path

    def delete(self, name_or_slug: str) -> bool:
        sk = self.get(name_or_slug)
        if sk is None or sk.directory is None or not sk.writable:
            return False
        import shutil
        shutil.rmtree(sk.directory, ignore_errors=True)
        return True


_DEFAULT_STORE: SkillStore | None = None


def default_store() -> SkillStore:
    """A process-wide store. Rescans on each query, so a single shared instance
    stays fresh; tools and the REPL all read the same one."""
    global _DEFAULT_STORE
    if _DEFAULT_STORE is None:
        try:
            from .config import TetherConfig
            cfg = TetherConfig.load()
            extra = list(getattr(cfg, "skills_dirs", []) or [])
        except Exception:
            extra = []
        _DEFAULT_STORE = SkillStore(extra_dirs=extra)
    return _DEFAULT_STORE
