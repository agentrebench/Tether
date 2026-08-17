"""Tether configuration."""
from __future__ import annotations

import copy
import json
import os
import re
import stat
import threading
import time
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8080
DEFAULT_MODEL_PATH = ""
DEFAULT_CONTEXT_SIZE = 131072
DEFAULT_GPU_LAYERS = -1  # offload all
DEFAULT_GPU_MEMORY_UTIL = 0.60
DEFAULT_PARALLEL_SLOTS = 4

_CONFIG_DIR_OVERRIDE = os.environ.get("TETHER_CONFIG_DIR", "").strip()
CONFIG_DIR = (
    Path(_CONFIG_DIR_OVERRIDE).expanduser()
    if _CONFIG_DIR_OVERRIDE
    else Path.home() / ".tether"
)
CONFIG_FILE = CONFIG_DIR / "config.json"
SESSIONS_DIR = CONFIG_DIR / "sessions"
MEMORY_DIR = CONFIG_DIR / "memory"


# Built-in remote provider presets.  This is also the authoritative model
# catalog for the desktop app, so the CLI, bridge, and WebUI cannot quietly
# disagree about endpoint or payload behavior.  Every provider here exposes an
# OpenAI-compatible chat-completions endpoint (Anthropic through its official
# compatibility layer).
REMOTE_PROVIDERS = {
    "codex": {
        "label": "Codex",
        "description": "Use the local Codex CLI and its ChatGPT login.",
        # Uses the local Codex CLI + ~/.codex ChatGPT login, not OpenAI API
        # billing. api_* fields are retained so existing profile plumbing can
        # switch to it like any other provider.
        "api_base_url": "",
        "api_model": "gpt-5.5",
        "api_key_env": "",
        "context_size": 400_000,
        "max_budget_tokens": 100_000_000,
        "omit_sampling": True,
        "requires_api_key": False,
        "models": [
            {
                "id": "gpt-5.5",
                "label": "GPT-5.5",
                "description": "Codex CLI default",
                "context_size": 400_000,
            },
        ],
    },
    "deepseek": {
        "label": "DeepSeek",
        "description": "DeepSeek V4 coding and agent models.",
        "api_base_url": "https://api.deepseek.com",
        "api_model": "deepseek-v4-flash",
        "api_key_env": "DEEPSEEK_API_KEY",
        # V4 supports explicit reasoning effort. Default the everyday Flash
        # model to reasoning-off for low latency; users can select high/max or
        # the Pro model from either interface when a task needs more depth.
        "reasoning_effort": "off",
        "context_size": 1_000_000,
        # max_budget_tokens is a cumulative cost guard, not a context limit.
        # Each turn re-counts the full prompt (history + replayed reasoning),
        # so it grows roughly quadratically with turn count. 100M is a sanity
        # cap for a stuck-loop, not a real session ceiling.
        "max_budget_tokens": 100_000_000,
        "requires_api_key": True,
        "models": [
            {
                "id": "deepseek-v4-flash",
                "label": "DeepSeek V4 Flash",
                "description": "Faster responses for everyday coding",
                "context_size": 1_000_000,
                "reasoning_efforts": ["off", "high", "max"],
                "default_reasoning_effort": "off",
                "max_reasoning_effort": "max",
            },
            {
                "id": "deepseek-v4-pro",
                "label": "DeepSeek V4 Pro",
                "description": "Deeper reasoning; usually slower",
                "context_size": 1_000_000,
                "reasoning_efforts": ["off", "high", "max"],
                "default_reasoning_effort": "high",
                "max_reasoning_effort": "max",
            },
        ],
    },
    "kimi": {
        "label": "Kimi",
        "description": "Moonshot Kimi coding and general agent models.",
        "api_base_url": "https://api.moonshot.ai",
        "api_model": "kimi-k3",
        "api_key_env": "KIMI_API_KEY",
        "api_key_env_aliases": ["MOONSHOT_API_KEY"],
        "context_size": 262_144,
        "max_budget_tokens": 100_000_000,
        "max_tokens_field": "max_completion_tokens",
        "omit_sampling": False,
        "requires_api_key": True,
        "models": [
            {
                "id": "kimi-k3",
                "label": "Kimi K3",
                "description": "Newest Kimi model; always reasons",
                "context_size": 262_144,
                "reasoning_efforts": ["low", "high", "max"],
                "default_reasoning_effort": "low",
                "max_reasoning_effort": "max",
                "thinking_mode": "enabled",
            },
            {
                "id": "kimi-k2.7-code",
                "label": "Kimi K2.7 Code",
                "description": "Coding model with preserved thinking",
                "context_size": 262_144,
                "thinking_mode": "enabled",
            },
            {
                "id": "kimi-k2.6",
                "label": "Kimi K2.6",
                "description": "General model with optional thinking",
                "context_size": 262_144,
                "thinking_modes": ["enabled", "disabled"],
                "default_thinking_mode": "enabled",
            },
        ],
    },
    "openai": {
        "label": "OpenAI",
        "description": "GPT models through the OpenAI API.",
        "api_base_url": "https://api.openai.com",
        # gpt-5.5 (2026-04-23) is the latest standard chat-completions model.
        # The -codex variants on this account exist but live on the Responses
        # API (/v1/responses), which the current backend doesn't drive — they
        # 404 against /v1/chat/completions. Pick a -codex model only after
        # adding Responses-API support to engine/backend.py.
        "api_model": "gpt-5.5",
        "api_key_env": "OPENAI_API_KEY",
        "context_size": 400_000,
        "max_budget_tokens": 100_000_000,
        # gpt-5.x and o-series reject `max_tokens` and require this instead.
        "max_tokens_field": "max_completion_tokens",
        # Same family rejects temperature/top_p != 1 outright.
        "omit_sampling": True,
        "requires_api_key": True,
        "models": [
            {
                "id": "gpt-5.5",
                "label": "GPT-5.5",
                "description": "Most capable GPT model",
                "context_size": 400_000,
                "reasoning_efforts": ["none", "low", "medium", "high", "xhigh"],
                "default_reasoning_effort": "medium",
                "max_reasoning_effort": "xhigh",
            },
            {
                "id": "gpt-5.4",
                "label": "GPT-5.4",
                "description": "Strong general-purpose model",
                "context_size": 1_050_000,
                "reasoning_efforts": ["none", "low", "medium", "high", "xhigh"],
                "default_reasoning_effort": "none",
                "max_reasoning_effort": "xhigh",
            },
            {
                "id": "gpt-5.4-mini",
                "label": "GPT-5.4 mini",
                "description": "Lower latency and cost",
                "context_size": 400_000,
                "reasoning_efforts": ["none", "low", "medium", "high", "xhigh"],
                "default_reasoning_effort": "none",
                "max_reasoning_effort": "xhigh",
            },
            {
                "id": "gpt-5.4-nano",
                "label": "GPT-5.4 nano",
                "description": "Fastest GPT option",
                "context_size": 400_000,
                "reasoning_efforts": ["none", "low", "medium", "high", "xhigh"],
                "default_reasoning_effort": "none",
                "max_reasoning_effort": "xhigh",
            },
        ],
    },
    "glm": {
        "label": "GLM",
        "description": "Z.AI GLM coding and agent models.",
        "api_base_url": "https://api.z.ai/api/paas/v4",
        "api_model": "glm-5.3",
        "api_key_env": "ZAI_API_KEY",
        "api_key_env_aliases": ["GLM_API_KEY"],
        "context_size": 202_752,
        "max_budget_tokens": 100_000_000,
        "requires_api_key": True,
        "models": [
            {
                "id": "glm-5.3",
                "label": "GLM-5.3",
                "description": "Newest flagship agent model",
                "context_size": 202_752,
                "thinking_modes": ["enabled", "disabled"],
                "default_thinking_mode": "enabled",
            },
            {
                "id": "glm-5.2",
                "label": "GLM-5.2",
                "description": "Flagship agent model",
                "context_size": 202_752,
                "thinking_modes": ["enabled", "disabled"],
                "default_thinking_mode": "enabled",
            },
            {
                "id": "glm-5.1",
                "label": "GLM-5.1",
                "description": "Flagship agent model",
                "context_size": 202_752,
                "thinking_modes": ["enabled", "disabled"],
                "default_thinking_mode": "enabled",
            },
            {
                "id": "glm-5-turbo",
                "label": "GLM-5 Turbo",
                "description": "Faster GLM-5 agent model",
                "context_size": 202_752,
                "thinking_modes": ["enabled", "disabled"],
                "default_thinking_mode": "enabled",
            },
            {
                "id": "glm-5",
                "label": "GLM-5",
                "description": "Flagship reasoning model",
                "context_size": 202_752,
                "thinking_modes": ["enabled", "disabled"],
                "default_thinking_mode": "enabled",
            },
            {
                "id": "glm-4.7",
                "label": "GLM-4.7",
                "description": "Balanced agent model",
                "context_size": 202_752,
                "thinking_modes": ["enabled", "disabled"],
                "default_thinking_mode": "enabled",
            },
            {
                "id": "glm-4.7-flash",
                "label": "GLM-4.7 Flash",
                "description": "Fast GLM option",
                "context_size": 202_752,
                "thinking_modes": ["enabled", "disabled"],
                "default_thinking_mode": "disabled",
            },
        ],
    },
    "anthropic": {
        "label": "Anthropic",
        "description": "Claude through Anthropic's OpenAI compatibility API.",
        "api_base_url": "https://api.anthropic.com/v1",
        "models_auth": "anthropic",  # /v1/models wants x-api-key, not Bearer
        "api_model": "claude-opus-5",
        "api_key_env": "ANTHROPIC_API_KEY",
        "context_size": 1_000_000,
        "max_budget_tokens": 100_000_000,
        "requires_api_key": True,
        "models": [
            {
                "id": "claude-opus-5",
                "label": "Claude Opus 5",
                "description": "Most capable Claude model",
                "context_size": 1_000_000,
            },
            {
                "id": "claude-sonnet-5",
                "label": "Claude Sonnet 5",
                "description": "Balanced Claude model",
                "context_size": 1_000_000,
            },
        ],
    },
}


# Live model discovery -------------------------------------------------------
#
# The built-in catalog above is a snapshot and goes stale the day a provider
# ships a new model. Providers that speak the OpenAI-compatible ``GET /models``
# are asked what they actually serve; ids the catalog does not know are
# synthesized from the preset's default model so they can be selected with
# sensible reasoning/thinking controls. Results are cached per process.
_DISCOVERY_TTL_SECONDS = 600
_discovered_models: dict[str, tuple[float, list[str]]] = {}
_discovery_lock = threading.Lock()


def _models_endpoint(provider: str) -> str:
    preset = REMOTE_PROVIDERS.get(provider, {})
    if "models_endpoint" in preset and preset.get("models_endpoint") is None:
        return ""  # explicitly no discovery for this provider
    explicit = preset.get("models_endpoint")
    if explicit:
        return str(explicit)
    base = str(preset.get("api_base_url", "")).rstrip("/")
    if not base:
        return ""
    # Same rule as the chat-completions URL: honour a versioned base
    # (…/v4 → …/v4/models), otherwise assume /v1.
    if re.search(r"/v\d+(?:beta\d*)?$", base):
        return f"{base}/models"
    return f"{base}/v1/models"


def _discovery_headers(provider: str, key: str) -> dict[str, str]:
    preset = REMOTE_PROVIDERS.get(provider, {})
    if preset.get("models_auth") == "anthropic":
        return {"x-api-key": key, "anthropic-version": "2023-06-01", "Accept": "application/json"}
    return {"Authorization": f"Bearer {key}", "Accept": "application/json"}


# Ids that are real but not chat/agent models (embeddings, speech, image,
# dated snapshots, legacy families). Everything else is kept, so a brand-new
# name the catalog has never heard of still shows up.
_DISCOVERY_EXCLUDE = {
    "openai": re.compile(
        r"(embedding|tts|whisper|transcribe|realtime|audio|image|dall-e|moderation|"
        r"davinci|babbage|instruct|search-preview|codex|computer-use|sora|omni-)"
        r"|-\d{4}-\d{2}-\d{2}$|-\d{4}$",
        re.IGNORECASE,
    ),
    "kimi": re.compile(r"^moonshot-v1|vision"),
}


def _discovery_key(config: "TetherConfig", provider: str) -> str:
    for env_name in config.api_key_env_names(provider):
        value = os.environ.get(env_name, "").strip()
        if value:
            return value
    return config.stored_api_key(provider)


def discover_provider_models(
    config: "TetherConfig",
    provider: str,
    *,
    timeout: float = 4.0,
    force: bool = False,
) -> list[str]:
    """Return live model ids for ``provider`` (cached), or [] when unavailable.

    Never raises: network errors, auth failures, and unexpected shapes all
    degrade to an empty list so the built-in catalog is still usable offline.
    """
    endpoint = _models_endpoint(provider)
    if not endpoint or provider not in REMOTE_PROVIDERS:
        return []
    now = time.monotonic()
    with _discovery_lock:
        cached = _discovered_models.get(provider)
        if cached and not force and now - cached[0] < _DISCOVERY_TTL_SECONDS:
            return list(cached[1])
    key = _discovery_key(config, provider)
    if not key and REMOTE_PROVIDERS[provider].get("requires_api_key", True):
        return []
    ids: list[str] = []
    exclude = _DISCOVERY_EXCLUDE.get(provider)
    try:
        req = urllib.request.Request(endpoint, headers=_discovery_headers(provider, key))
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read().decode("utf-8", errors="replace"))
        rows = data.get("data") if isinstance(data, dict) else data
        if isinstance(rows, list):
            for row in rows:
                model_id = row.get("id") if isinstance(row, dict) else None
                if not isinstance(model_id, str) or not model_id.strip():
                    continue
                model_id = model_id.strip()
                if exclude is not None and exclude.search(model_id):
                    continue
                ids.append(model_id)
    except Exception:
        ids = []
    with _discovery_lock:
        # Cache failures too (briefly) so a dead endpoint is not re-hit on
        # every catalog build; a later force refresh will retry.
        _discovered_models[provider] = (now, ids)
    return list(ids)


def _synthesized_model(provider: str, model_id: str) -> dict:
    """Build a catalog entry for a live-discovered id from the preset's default."""
    preset = REMOTE_PROVIDERS.get(provider, {})
    template = next(
        (m for m in preset.get("models", []) if m.get("id") == preset.get("api_model")),
        (preset.get("models") or [{}])[0],
    )
    entry = {
        key: copy.deepcopy(value)
        for key, value in template.items()
        if key in {
            "context_size", "reasoning_efforts", "default_reasoning_effort",
            "thinking_modes", "default_thinking_mode", "thinking_mode",
            "max_tokens_field", "omit_sampling",
        }
    }
    entry.update({
        "id": model_id,
        "label": model_id,
        "description": "Reported by the provider API",
        "discovered": True,
    })
    entry.setdefault("context_size", preset.get("context_size", DEFAULT_CONTEXT_SIZE))
    return entry


def provider_models(provider: str, config: "TetherConfig | None" = None) -> list[dict]:
    """Catalog models for ``provider``, with live-discovered ids merged in.

    Discovered ids the catalog does not list are placed first (they are
    almost always the newest releases). Pass ``config`` to enable discovery;
    without it this is the static catalog.
    """
    preset = REMOTE_PROVIDERS.get(provider, {})
    known = copy.deepcopy(preset.get("models", []))
    if config is None:
        return known
    known_ids = {m.get("id") for m in known}
    extra = [
        _synthesized_model(provider, model_id)
        for model_id in discover_provider_models(config, provider)
        if model_id not in known_ids
    ]
    merged = extra + known
    # Newest first across catalog and discovered ids alike (stable, so the
    # curated catalog order breaks ties).
    merged.sort(key=lambda m: _version_sort_key(str(m.get("id", ""))), reverse=True)
    return merged


def _version_sort_key(model_id: str) -> tuple:
    parts = re.split(r"[-._]", model_id)
    key = []
    for part in parts:
        key.append((1, int(part), "") if part.isdigit() else (0, 0, part))
    # Terminal marker so a base id ("gpt-5.4") sorts above its variants
    # ("gpt-5.4-mini") instead of below them.
    key.append((1, -1, ""))
    return tuple(key)


def provider_model(provider: str, model_id: str, config: "TetherConfig | None" = None) -> dict | None:
    """Return model metadata from the catalog, or a synthesized entry for an id
    the provider reports live (only when ``config`` is given for discovery)."""
    preset = REMOTE_PROVIDERS.get(provider, {})
    found = next(
        (model for model in preset.get("models", []) if model.get("id") == model_id),
        None,
    )
    if found is not None or config is None:
        return found
    if model_id in discover_provider_models(config, provider):
        return _synthesized_model(provider, model_id)
    return None


def apply_provider_selection(
    config: "TetherConfig",
    provider: str,
    model_id: str = "",
    *,
    reasoning_effort: str | None = None,
    thinking_mode: str | None = None,
    base_url: str = "",
    api_key_env: str = "",
) -> None:
    """Validate and apply a provider/model selection to a live config."""
    provider = provider.strip().lower()
    was_remote = config.is_remote
    legacy_key = (config.api_key or "").strip()
    if legacy_key and config.provider:
        config.api_keys.setdefault(config.provider, legacy_key)
        config.api_key = ""
    if provider == "local":
        config.provider = "local"
        config.api_base_url = ""
        config.api_model = ""
        config.api_key_env = ""
        config.reasoning_effort = ""
        config.reasoning_effort_max = ""
        config.thinking_mode = ""
        config.max_tokens_field = "max_tokens"
        config.omit_sampling = False
        # Only restore the stashed local context when actually switching back
        # from a remote provider; a local->local call must keep the user's
        # current context_size instead of resetting it to the default.
        # The desktop also uses the stash to hand over a newly selected local
        # model's context length, so honour it whenever one is present.
        if was_remote or config.local_context_size:
            config.context_size = config.local_context_size or DEFAULT_CONTEXT_SIZE
        config.local_context_size = 0
        return

    if provider == "custom":
        if not base_url.strip() or not model_id.strip():
            raise ValueError("Custom providers require an API base URL and model name.")
        if not config.is_remote and not config.local_context_size:
            config.local_context_size = config.context_size
        config.provider = "custom"
        config.api_base_url = base_url.rstrip("/")
        config.api_model = model_id.strip()
        config.api_key_env = api_key_env.strip()
        config.reasoning_effort = (reasoning_effort or "").strip()
        config.reasoning_effort_max = config.reasoning_effort
        config.thinking_mode = (thinking_mode or "").strip()
        config.max_tokens_field = "max_tokens"
        config.omit_sampling = bool(config.reasoning_effort)
        return

    preset = REMOTE_PROVIDERS.get(provider)
    if preset is None:
        raise ValueError(f"Unknown provider: {provider}")
    selected_id = model_id.strip() or str(preset["api_model"])
    model = provider_model(provider, selected_id, config)
    if model is None:
        raise ValueError(f"Unknown {provider} model: {selected_id}")
    if not config.is_remote and not config.local_context_size:
        config.local_context_size = config.context_size

    allowed_efforts = list(model.get("reasoning_efforts", []))
    selected_effort = (
        reasoning_effort
        if reasoning_effort is not None
        else model.get("default_reasoning_effort", preset.get("reasoning_effort", ""))
    )
    selected_effort = str(selected_effort or "")
    if allowed_efforts and selected_effort not in allowed_efforts:
        raise ValueError(f"Unsupported reasoning effort for {selected_id}: {selected_effort}")

    allowed_thinking = list(model.get("thinking_modes", []))
    selected_thinking = (
        thinking_mode
        if thinking_mode is not None
        else model.get("default_thinking_mode", model.get("thinking_mode", ""))
    )
    selected_thinking = str(selected_thinking or "")
    if allowed_thinking and selected_thinking not in allowed_thinking:
        raise ValueError(f"Unsupported thinking mode for {selected_id}: {selected_thinking}")
    if provider == "deepseek" and allowed_efforts:
        selected_thinking = "disabled" if selected_effort == "off" else "enabled"
        selected_effort = "" if selected_effort == "off" else selected_effort

    config.provider = provider
    config.api_base_url = str(preset["api_base_url"])
    config.api_model = selected_id
    config.api_key_env = str(preset.get("api_key_env", ""))
    config.context_size = int(model.get("context_size", preset.get("context_size", config.context_size)))
    config.max_budget_tokens = int(preset.get("max_budget_tokens", config.max_budget_tokens))
    config.max_tokens_field = str(preset.get("max_tokens_field", "max_tokens"))
    config.reasoning_effort = selected_effort
    config.reasoning_effort_max = str(model.get("max_reasoning_effort", ""))
    config.thinking_mode = selected_thinking
    config.omit_sampling = bool(preset.get("omit_sampling", False) or selected_effort or selected_thinking == "enabled")


@dataclass
class TetherConfig:
    # server settings
    host: str = DEFAULT_HOST
    port: int = DEFAULT_PORT
    model_path: str = DEFAULT_MODEL_PATH
    context_size: int = DEFAULT_CONTEXT_SIZE
    gpu_layers: int = DEFAULT_GPU_LAYERS
    gpu_memory_utilization: float = DEFAULT_GPU_MEMORY_UTIL
    parallel_slots: int = DEFAULT_PARALLEL_SLOTS
    cache_type_k: str = "f16"
    cache_type_v: str = "f16"

    # engine settings
    max_turns: int = 50
    max_budget_tokens: int = 200_000
    # Per-call output cap. The old hardcoded 4096 truncated long tool calls
    # (e.g. a file_write with substantial content) mid-arguments, producing
    # invalid JSON that the model couldn't recover from. This is a ceiling,
    # not a target, so raising it costs nothing on short replies.
    max_output_tokens: int = 8192
    compact_after_turns: int = 20
    temperature: float = 0.7
    top_p: float = 0.9

    # self-learning — when on, completed multi-step submissions are recorded
    # and recurring (similar prompt + similar tool sequence) workflows are
    # detected, prompting you to save them as a reusable SKILL.md (procedural
    # memory). Opt-in: off by default, toggled via /learn auto on.
    self_learning: bool = False

    # Desktop-only cross-session context. The terminal keeps its established
    # memory behavior; the app bridge passes this flag explicitly when it
    # constructs QueryEngine so a fresh desktop launch is private by default.
    desktop_memory_enabled: bool = False

    # Skills — extra directories scanned for <name>/SKILL.md alongside the
    # built-ins and ~/.tether/skills. When write_approval is on, the agent's
    # skill_manage writes require explicit approval before touching disk.
    skills_dirs: list[str] = field(default_factory=list)

    # Persistent codebase mental model (docs/codebase-mental-model/). On by
    # default: the model_query/model_record/model_check tools are registered, the
    # repo auto-indexes on first launch, and the agent's own edits incrementally
    # refresh the substrate index. The store lives under
    # ~/.tether/models/<repo-hash>.db. Set to false to turn the whole feature
    # off. write_approval gates the model_record write tool behind the normal
    # approval flow.
    codebase_model_enabled: bool = True
    codebase_model_max_beliefs: int = 500
    codebase_model_max_files: int = 10000
    codebase_model_write_approval: bool = True
    # skill_manage writes SKILL.md files to disk — gate behind approval by
    # default so the model can't grow its own procedural memory silently.
    skills_write_approval: bool = True

    # permissions
    auto_approve_reads: bool = True
    auto_approve_edits: bool = True
    auto_approve_bash: bool = False
    deny_tools: list[str] = field(default_factory=list)
    deny_prefixes: list[str] = field(default_factory=list)

    # remote provider settings — when provider != "local", the backend talks
    # to an OpenAI-compatible endpoint at api_base_url instead of the local
    # llama-server. Auth resolves env-var first, then stored api_key.
    provider: str = "local"
    api_base_url: str = ""
    api_model: str = ""
    api_key_env: str = ""
    # Optional persisted key. Trade-off: convenience vs. plaintext on disk.
    # Prefer setting $api_key_env; this is the fallback for users who'd
    # rather not export an env var every shell. Saved with file mode 0600.
    api_key: str = ""
    # New desktop flows store a separate key for each provider so switching
    # from DeepSeek to Kimi cannot accidentally reuse the wrong credential.
    # Values remain write-only at the bridge boundary and config.json is 0600.
    api_keys: dict[str, str] = field(default_factory=dict)
    # When non-empty, treat this as a thinking/reasoning model: drop sampler
    # params (temperature, top_p) that the API rejects, and send
    # `reasoning_effort` ("high" or "max") in the request payload.
    reasoning_effort: str = ""
    reasoning_effort_max: str = ""
    # OpenAI-compatible thinking controls used by DeepSeek, Kimi, and GLM.
    # Empty means the selected model/provider does not use this payload field.
    thinking_mode: str = ""
    # Stashed local context_size so we can restore it when switching off
    # a remote provider — remote presets often want a much larger window
    # than the local model can fit.
    local_context_size: int = 0

    # User-supplied directories to scan for GGUF model files. These are
    # merged with the built-in search paths in cli.discover_models().
    gguf_dirs: list[str] = field(default_factory=list)

    # Which JSON field to use for the per-request output cap. OpenAI's
    # newer models (gpt-5.x, o-series) deprecated `max_tokens` in favor
    # of `max_completion_tokens` and 400 if you send the old name.
    # DeepSeek, llama-server, and most OpenAI-compatible servers still
    # accept `max_tokens`. Override per-provider.
    max_tokens_field: str = "max_tokens"

    # gpt-5.x and o-series reject any temperature != 1 and any top_p != 1
    # with "Unsupported value". When True, the backend simply omits these
    # fields instead of guessing a default the model will accept.
    omit_sampling: bool = False

    @property
    def is_remote(self) -> bool:
        return self.provider != "local"

    @property
    def is_codex(self) -> bool:
        return self.provider == "codex"

    @property
    def server_url(self) -> str:
        if self.is_remote:
            return self.api_base_url
        return f"http://{self.host}:{self.port}"

    def stored_api_key(self, provider: str | None = None) -> str:
        """Return a stored key without weakening legacy config compatibility."""
        target = provider or self.provider
        mapped = (self.api_keys or {}).get(target, "").strip()
        if mapped:
            return mapped
        # Old configs had one key belonging to the active provider.
        if target == self.provider:
            return (self.api_key or "").strip()
        return ""

    def api_key_env_names(self, provider: str | None = None) -> list[str]:
        target = provider or self.provider
        preset = REMOTE_PROVIDERS.get(target, {})
        names: list[str] = []
        if target == self.provider and self.api_key_env:
            names.append(self.api_key_env)
        for name in [preset.get("api_key_env", ""), *preset.get("api_key_env_aliases", [])]:
            value = str(name or "")
            if value and value not in names:
                names.append(value)
        return names

    def has_api_key(self, provider: str | None = None) -> bool:
        target = provider or self.provider
        preset = REMOTE_PROVIDERS.get(target, {})
        if preset.get("requires_api_key") is False or target == "local":
            return True
        env_set = any(os.environ.get(name, "").strip() for name in self.api_key_env_names(target))
        return bool(env_set or self.stored_api_key(target))

    # backward compat
    @property
    def llama_url(self) -> str:
        return self.server_url

    @property
    def llama_port(self) -> int:
        return self.port

    @llama_port.setter
    def llama_port(self, value: int) -> None:
        self.port = value

    def save(self, path: Path | None = None) -> None:
        target = path or CONFIG_FILE
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(self.__dict__, indent=2))
        # Config may now contain an API key. Restrict to the owner so
        # other accounts on the box can't read it.
        try:
            os.chmod(target, stat.S_IRUSR | stat.S_IWUSR)
        except OSError:
            pass

    @classmethod
    def load(cls, path: Path | None = None) -> TetherConfig:
        target = path or CONFIG_FILE
        if target.exists():
            data = json.loads(target.read_text())
            if "llama_port" in data and "port" not in data:
                data["port"] = data.pop("llama_port")
            if "llama_host" in data and "host" not in data:
                data["host"] = data.pop("llama_host")
            data.pop("llama_port", None)
            data.pop("llama_host", None)
            data.pop("tensor_parallel_size", None)
            # The previously shipped example used a profile-style shape with
            # `default_provider` plus a `providers` object. Migrate it on read
            # so existing installs keep their selected provider and keys.
            profiles = data.pop("providers", None)
            default_provider = str(data.pop("default_provider", "") or "")
            if isinstance(profiles, dict):
                active = str(data.get("provider") or default_provider or "local")
                data["provider"] = active
                profile = profiles.get(active)
                if isinstance(profile, dict):
                    for key in (
                        "api_base_url",
                        "api_model",
                        "api_key_env",
                        "api_key",
                        "reasoning_effort",
                        "context_size",
                        "max_budget_tokens",
                        "max_tokens_field",
                        "omit_sampling",
                    ):
                        if key in profile:
                            data[key] = profile[key]
                mapped = dict(data.get("api_keys") or {})
                for provider_id, provider_profile in profiles.items():
                    if isinstance(provider_profile, dict):
                        key = str(provider_profile.get("api_key", "") or "").strip()
                        if key:
                            mapped[str(provider_id)] = key
                data["api_keys"] = mapped
            # A flat legacy key belongs to the provider that was active when
            # the file was loaded. Move it into that provider's slot before a
            # future runtime switch could reinterpret it as another key.
            legacy_key = str(data.get("api_key", "") or "").strip()
            active_provider = str(data.get("provider", "") or "")
            if legacy_key and active_provider:
                mapped = dict(data.get("api_keys") or {})
                mapped.setdefault(active_provider, legacy_key)
                data["api_keys"] = mapped
                data["api_key"] = ""
            return cls(**{k: v for k, v in data.items() if k in cls.__dataclass_fields__})
        return cls()
