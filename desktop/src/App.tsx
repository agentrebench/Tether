import { invoke } from "@tauri-apps/api/core";
import { listen, type UnlistenFn } from "@tauri-apps/api/event";
import { getCurrentWindow } from "@tauri-apps/api/window";
import { open } from "@tauri-apps/plugin-dialog";
import {
  AlertTriangle,
  ArrowDown,
  ArrowLeft,
  ArrowRight,
  ArrowUp,
  Bot,
  BrainCircuit,
  Check,
  ChevronRight,
  CircleHelp,
  CircleStop,
  Cloud,
  Cpu,
  Eye,
  EyeOff,
  FileCode2,
  Folder,
  FolderOpen,
  KeyRound,
  LockKeyhole,
  ListChecks,
  MessageSquarePlus,
  Moon,
  Paperclip,
  FileText,
  Image as ImageIcon,
  ClipboardList,
  Plus,
  RotateCcw,
  Save,
  Server,
  Settings2,
  ShieldCheck,
  Sparkles,
  Sun,
  TerminalSquare,
  Workflow,
  Wrench,
  X,
} from "lucide-react";
import hljs from "highlight.js/lib/core";
import bash from "highlight.js/lib/languages/bash";
import c from "highlight.js/lib/languages/c";
import cpp from "highlight.js/lib/languages/cpp";
import csharp from "highlight.js/lib/languages/csharp";
import clojure from "highlight.js/lib/languages/clojure";
import css from "highlight.js/lib/languages/css";
import go from "highlight.js/lib/languages/go";
import ini from "highlight.js/lib/languages/ini";
import java from "highlight.js/lib/languages/java";
import javascript from "highlight.js/lib/languages/javascript";
import json from "highlight.js/lib/languages/json";
import kotlin from "highlight.js/lib/languages/kotlin";
import lisp from "highlight.js/lib/languages/lisp";
import markdown from "highlight.js/lib/languages/markdown";
import php from "highlight.js/lib/languages/php";
import python from "highlight.js/lib/languages/python";
import ruby from "highlight.js/lib/languages/ruby";
import rust from "highlight.js/lib/languages/rust";
import scheme from "highlight.js/lib/languages/scheme";
import sql from "highlight.js/lib/languages/sql";
import swift from "highlight.js/lib/languages/swift";
import typescript from "highlight.js/lib/languages/typescript";
import xml from "highlight.js/lib/languages/xml";
import yaml from "highlight.js/lib/languages/yaml";
import { Children, memo, useCallback, useEffect, useMemo, useRef, useState } from "react";
import ReactMarkdown, { type Components } from "react-markdown";
import remarkGfm from "remark-gfm";
import type {
  AgentSnapshot,
  AgentStatus,
  AgentToolCall,
  ApprovalRequest,
  BridgeEnvelope,
  BridgePayload,
  ChatMessage,
  ComposerAttachment,
  ConnectionState,
  ProviderModel,
  ProviderOption,
  QuestionRequest,
  TodoItem,
  ToolActivity,
  TurnSegment,
  WorkspacePreview,
} from "./types";
import appIcon from "../src-tauri/icons/128x128.png";

const PROJECT_STORAGE_KEY = "tether.projectPath";
const THEME_STORAGE_KEY = "tether.theme";
const ONBOARDING_STORAGE_KEY = "tether.onboarding.v1";
const PREVIEW_WIDTH_STORAGE_KEY = "tether.previewWidth";
const PREVIEW_MIN_WIDTH = 300;
const STREAM_FLUSH_INTERVAL_MS = 40;
const OPENING_STATEMENT_MAX_CHARS = 280;
const PREVIEW_DIVIDER_WIDTH = 6;
type Theme = "light" | "dark";

function maxPreviewWidth(viewportWidth = window.innerWidth): number {
  const compact = viewportWidth <= 1050;
  const sidebarWidth = compact ? 226 : 258;
  const mainMinWidth = compact ? 330 : 360;
  return Math.max(
    PREVIEW_MIN_WIDTH,
    viewportWidth - sidebarWidth - mainMinWidth - PREVIEW_DIVIDER_WIDTH,
  );
}

function clampPreviewWidth(width: number): number {
  return Math.min(Math.max(width, PREVIEW_MIN_WIDTH), maxPreviewWidth());
}

const HIGHLIGHT_LANGUAGES = {
  bash,
  c,
  cpp,
  csharp,
  clojure,
  css,
  go,
  ini,
  java,
  javascript,
  json,
  kotlin,
  lisp,
  markdown,
  php,
  python,
  ruby,
  rust,
  scheme,
  sql,
  swift,
  typescript,
  xml,
  yaml,
};

Object.entries(HIGHLIGHT_LANGUAGES).forEach(([name, language]) => {
  hljs.registerLanguage(name, language);
});

const LANGUAGE_BY_EXTENSION: Record<string, keyof typeof HIGHLIGHT_LANGUAGES> = {
  asd: "lisp",
  bash: "bash",
  c: "c",
  cc: "cpp",
  conf: "ini",
  cpp: "cpp",
  cs: "csharp",
  cl: "lisp",
  clj: "clojure",
  cljc: "clojure",
  cljs: "clojure",
  css: "css",
  edn: "clojure",
  el: "lisp",
  fish: "bash",
  fnl: "lisp",
  go: "go",
  h: "c",
  hpp: "cpp",
  htm: "xml",
  html: "xml",
  hy: "lisp",
  ini: "ini",
  java: "java",
  js: "javascript",
  jsx: "javascript",
  json: "json",
  kt: "kotlin",
  kts: "kotlin",
  lisp: "lisp",
  lsp: "lisp",
  md: "markdown",
  mdx: "markdown",
  php: "php",
  py: "python",
  rb: "ruby",
  rkt: "scheme",
  rktl: "scheme",
  rs: "rust",
  scm: "scheme",
  scrbl: "scheme",
  scss: "css",
  sh: "bash",
  sld: "scheme",
  sls: "scheme",
  sql: "sql",
  ss: "scheme",
  svelte: "xml",
  swift: "swift",
  toml: "ini",
  ts: "typescript",
  tsx: "typescript",
  vue: "xml",
  xml: "xml",
  yaml: "yaml",
  yml: "yaml",
  zsh: "bash",
};

const PATH_MENTION_RE = /((?:\.{1,2}\/|\/)?(?:[A-Za-z0-9_@.+-]+\/)+(?:[A-Za-z0-9_@.+-]+\.(?:tsx?|jsx?|py|rs|go|java|kt|swift|c|h|cpp|hpp|cs|rb|php|scala|sh|bash|zsh|fish|json|ya?ml|toml|ini|cfg|conf|mdx?|txt|sql|graphql|proto|html|css|scss|sass|less|vue|svelte|lock|xml|csv|rktl?|scrbl|scm|sld|sls|ss|lisp|lsp|cl|asd|clj[sc]?|edn|el|fnl|hy)(?::\d+(?::\d+)?)?)?|(?:(?:[A-Za-z0-9_@.+-]+\.(?:tsx?|jsx?|py|rs|go|java|kt|swift|c|h|cpp|hpp|cs|rb|php|scala|sh|bash|zsh|fish|json|ya?ml|toml|ini|cfg|conf|mdx?|txt|sql|graphql|proto|html|css|scss|sass|less|vue|svelte|lock|xml|csv|rktl?|scrbl|scm|sld|sls|ss|lisp|lsp|cl|asd|clj[sc]?|edn|el|fnl|hy))|Dockerfile|Makefile|LICENSE)(?::\d+(?::\d+)?)?)(?![A-Za-z0-9_@.+-])/g;

interface PathReference {
  path: string;
  line: number | null;
}

interface SlashCommandOption {
  command: string;
  description: string;
  category?: string;
}

interface SlashCommandContext {
  start: number;
  end: number;
  query: string;
}

interface TextRange {
  start: number;
  end: number;
}

const FALLBACK_SLASH_COMMANDS: SlashCommandOption[] = [
  { command: "/help", description: "Show all commands" },
  { command: "/plan", description: "Toggle plan mode" },
  { command: "/plan on", description: "Force plan on" },
  { command: "/plan off", description: "Force plan off" },
  { command: "/why", description: "Reasoning for an edited line" },
  { command: "/clear", description: "Clear the context" },
  { command: "/compact", description: "Summarize + trim context" },
  { command: "/history", description: "Event history" },
  { command: "/usage", description: "Token usage" },
  { command: "/context", description: "Context-window usage" },
  { command: "/session", description: "Session info" },
  { command: "/cd", description: "Change directory" },
  { command: "/save", description: "Save session to disk" },
  { command: "/resume", description: "Restore a saved conversation" },
  { command: "/memory", description: "Persistent memory" },
  { command: "/memory show", description: "Show memory" },
  { command: "/memory clear", description: "Clear memory" },
  { command: "/selfcheck", description: "Run a self-check" },
  { command: "/summaries", description: "Conversation summaries" },
  { command: "/provider", description: "Switch model provider" },
  { command: "/model", description: "LLM model & token usage" },
  { command: "/model stats", description: "Show the current model" },
  { command: "/model local", description: "Switch to local GGUF" },
  { command: "/model api", description: "Switch to your API model" },
  { command: "/learn", description: "Author a skill" },
  { command: "/learn auto", description: "Toggle auto-learning" },
  { command: "/learn this chat", description: "Skill from this chat" },
  { command: "/skills", description: "List / show / forget skills" },
  { command: "/skills show", description: "Show a skill" },
  { command: "/skills forget", description: "Delete a user skill" },
  { command: "/skills doctor", description: "Check skill files for problems" },
  { command: "/persistence", description: "Codebase mental model" },
  { command: "/persistence status", description: "Model status" },
  { command: "/persistence build", description: "Full re-index" },
  { command: "/persistence sync", description: "Incremental refresh" },
  { command: "/persistence check", description: "Run invariants vs. diff" },
  { command: "/persistence ask", description: "Query the model" },
  { command: "/persistence gc", description: "Remove orphaned model DBs" },
  { command: "/also", description: "Queue a follow-up mid-turn" },
  { command: "/redirect", description: "Steer the current turn" },
  { command: "/stop", description: "Interrupt the turn" },
  { command: "/quit", description: "Exit" },
  { command: "/exit", description: "Exit" },
];

function slashCommandContextAt(
  value: string,
  cursor: number,
  commands: SlashCommandOption[],
): SlashCommandContext | null {
  const safeCursor = Math.max(0, Math.min(cursor, value.length));
  const beforeCursor = value.slice(0, safeCursor);
  let slash = beforeCursor.lastIndexOf("/");

  while (slash >= 0) {
    const hasTokenBoundary = slash === 0 || /\s/.test(value[slash - 1]);
    const query = value.slice(slash, safeCursor);
    const staysOnLine = !query.includes("\n");
    const normalized = query.toLowerCase();
    const hasCompletion = commands.some((item) => (
      item.command.toLowerCase().startsWith(normalized)
    ));

    if (hasTokenBoundary && staysOnLine && hasCompletion) {
      let end = safeCursor;
      while (end < value.length && !/\s/.test(value[end])) end += 1;
      return { start: slash, end, query };
    }
    slash = beforeCursor.lastIndexOf("/", slash - 1);
  }
  return null;
}

function composerCommandRanges(
  value: string,
  commands: SlashCommandOption[],
  activeContext: SlashCommandContext | null,
): TextRange[] {
  const normalizedValue = value.toLowerCase();
  const commandNames = [...new Set(commands.map((item) => item.command.toLowerCase()))]
    .sort((left, right) => right.length - left.length);
  const ranges: TextRange[] = [];

  for (let index = 0; index < value.length; index += 1) {
    if (value[index] !== "/" || (index > 0 && !/\s/.test(value[index - 1]))) continue;
    const match = commandNames.find((command) => {
      if (!normalizedValue.startsWith(command, index)) return false;
      const next = value[index + command.length];
      return next === undefined || /[\s.,!?;:)}\]]/.test(next);
    });
    if (!match) continue;
    ranges.push({ start: index, end: index + match.length });
    index += match.length - 1;
  }

  if (activeContext && activeContext.query) {
    ranges.push({ start: activeContext.start, end: activeContext.start + activeContext.query.length });
  }

  return ranges
    .sort((left, right) => left.start - right.start || left.end - right.end)
    .reduce<TextRange[]>((merged, range) => {
      const previous = merged.at(-1);
      if (previous && range.start <= previous.end) {
        previous.end = Math.max(previous.end, range.end);
      } else {
        merged.push({ ...range });
      }
      return merged;
    }, []);
}

function uid(): string {
  return crypto.randomUUID();
}

function basename(path: string): string {
  const clean = path.replace(/[\\/]+$/, "");
  return clean.split(/[\\/]/).pop() || clean;
}

function reasoningEffortLabel(value: string): string {
  if (value === "none" || value === "off") return `${value === "none" ? "None" : "Off"} (fastest)`;
  if (value === "low") return "Low (faster)";
  if (value === "medium") return "Medium (balanced)";
  if (value === "high") return "High (slower)";
  if (value === "max" || value === "xhigh") return `${value === "max" ? "Max" : "Extra high"} (slowest)`;
  return value;
}

function parsePathReference(value: string, allowUnknownFile = false): PathReference | null {
  const candidate = value.trim();
  if (!candidate || candidate.includes("://") || candidate.startsWith("#")) return null;
  const match = candidate.match(/^(.*?)(?::(\d+)(?::\d+)?)?$/);
  if (!match) return null;
  const path = match[1];
  const isDirectory = path.endsWith("/");
  const isKnownFile = /(?:^|\/)(?:Dockerfile|Makefile|LICENSE)$/.test(path)
    || /\.(?:tsx?|jsx?|py|rs|go|java|kt|swift|c|h|cpp|hpp|cs|rb|php|scala|sh|bash|zsh|fish|json|ya?ml|toml|ini|cfg|conf|mdx?|txt|sql|graphql|proto|html|css|scss|sass|less|vue|svelte|lock|xml|csv|rktl?|scrbl|scm|sld|sls|ss|lisp|lsp|cl|asd|clj[sc]?|edn|el|fnl|hy)$/.test(path);
  if (!isDirectory && !isKnownFile && (!allowUnknownFile || /\s/.test(path))) return null;
  return { path, line: match[2] ? Number(match[2]) : null };
}

function languageForPath(path: string): keyof typeof HIGHLIGHT_LANGUAGES | null {
  const name = basename(path).toLowerCase();
  if (["dockerfile", "makefile"].includes(name)) return "bash";
  const extension = name.includes(".") ? name.split(".").pop() ?? "" : "";
  return LANGUAGE_BY_EXTENSION[extension] ?? null;
}

function highlightSourceLine(line: string, language: keyof typeof HIGHLIGHT_LANGUAGES): string {
  try {
    return hljs.highlight(line || " ", { language, ignoreIllegals: true }).value;
  } catch {
    return (line || " ")
      .replaceAll("&", "&amp;")
      .replaceAll("<", "&lt;")
      .replaceAll(">", "&gt;");
  }
}

function pathAwareText(value: string, onOpenPath: (reference: string) => void): React.ReactNode {
  return value.split(PATH_MENTION_RE).map((part, index) => {
    if (!parsePathReference(part)) return part;
    return (
      <button
        type="button"
        className="path-link"
        key={`${part}-${index}`}
        onClick={() => onOpenPath(part)}
        title={`Open ${part}`}
      >
        {part}
      </button>
    );
  });
}

function pathAwareChildren(
  children: React.ReactNode,
  onOpenPath: (reference: string) => void,
): React.ReactNode {
  return Children.map(children, (child) => (
    typeof child === "string" ? pathAwareText(child, onOpenPath) : child
  ));
}

function asString(value: unknown, fallback = ""): string {
  return typeof value === "string" ? value : fallback;
}

function asStringArray(value: unknown): string[] {
  return Array.isArray(value) ? value.filter((item): item is string => typeof item === "string") : [];
}

function asRecord(value: unknown): Record<string, unknown> {
  return value && typeof value === "object" && !Array.isArray(value)
    ? (value as Record<string, unknown>)
    : {};
}

function asFiniteNumber(value: unknown, fallback = 0): number {
  return typeof value === "number" && Number.isFinite(value) ? value : fallback;
}

function asAgentStatus(value: unknown): AgentStatus {
  const status = asString(value).toLowerCase();
  if (["queued", "pending"].includes(status)) return "queued";
  if (["completed", "complete", "done", "succeeded", "success", "max_turns"].includes(status)) return "completed";
  if (["failed", "error"].includes(status)) return "failed";
  if (["cancelled", "canceled", "stopped", "denied", "approval_denied"].includes(status)) return "cancelled";
  return "running";
}

function asAgentToolStatus(value: unknown, isError: boolean): AgentToolCall["status"] {
  const status = asString(value).toLowerCase();
  if (["queued", "pending"].includes(status)) return "queued";
  if (["completed", "complete", "done", "succeeded", "success", "max_turns"].includes(status)) return "completed";
  if (["denied", "approval_denied"].includes(status)) return "denied";
  if (isError) return "failed";
  if (["failed", "error", "cancelled", "canceled"].includes(status)) return "failed";
  return "running";
}

function asAgentSnapshot(payload: BridgePayload): AgentSnapshot | null {
  const turnId = asString(payload.id).trim();
  const agentId = asString(payload.agent_id).trim();
  if (!turnId || !agentId) return null;
  const agentNumber = Math.max(1, Math.trunc(asFiniteNumber(payload.agent_number, 1)));
  const toolCalls = Array.isArray(payload.tool_calls)
    ? payload.tool_calls.flatMap((item, index) => {
        const tool = asRecord(item);
        const name = asString(tool.name, "tool").trim() || "tool";
        const isError = tool.is_error === true;
        return [{
          id: asString(tool.id, `${agentId}-tool-${index}`),
          name,
          status: asAgentToolStatus(tool.status, isError),
          argsPreview: asString(tool.args_preview),
          isError,
        } satisfies AgentToolCall];
      })
    : [];
  return {
    turnId,
    agentId,
    agentNumber,
    revision: Math.max(0, Math.trunc(asFiniteNumber(payload.revision))),
    label: asString(payload.label, `Agent ${agentNumber}`).trim() || `Agent ${agentNumber}`,
    agentType: asString(payload.agent_type).trim(),
    task: asString(payload.task).trim(),
    status: asAgentStatus(payload.status),
    activity: asString(payload.activity).trim(),
    toolCalls,
    output: asString(payload.output),
    tokens: Math.max(0, Math.trunc(asFiniteNumber(payload.tokens))),
    elapsedSeconds: Math.max(0, asFiniteNumber(payload.elapsed_seconds)),
  };
}

function upsertAgentSnapshot(current: AgentSnapshot[], next: AgentSnapshot): AgentSnapshot[] {
  const existing = current.find((agent) => agent.agentId === next.agentId);
  if (existing && existing.revision > next.revision) return current;
  const updated = existing
    ? current.map((agent) => agent.agentId === next.agentId ? next : agent)
    : [...current, next];
  return updated.sort((left, right) => (
    left.agentNumber - right.agentNumber || left.agentId.localeCompare(right.agentId)
  ));
}

function queueClearReason(value: unknown): string {
  const reason = asString(value).trim();
  if (reason === "approval_denied") return "Cleared because approval was denied.";
  if (["cancelled", "canceled", "user_cancelled"].includes(reason)) return "Cleared when the active turn was stopped.";
  if (reason === "turn_worker_failed") return "Cleared because the active turn could not continue.";
  return reason ? reason.replaceAll("_", " ") : "The queued follow-up was cleared.";
}

function asTodos(value: unknown): TodoItem[] {
  if (!Array.isArray(value)) return [];
  return value.flatMap((item) => {
    const todo = asRecord(item);
    const content = asString(todo.content).trim();
    const status = asString(todo.status);
    if (!content || !["pending", "in_progress", "completed"].includes(status)) return [];
    return [{
      content,
      status: status as TodoItem["status"],
      active_form: asString(todo.active_form) || undefined,
    }];
  });
}

function asQuestionRequest(payload: BridgePayload): QuestionRequest | null {
  if (!Array.isArray(payload.questions)) return null;
  const questions = payload.questions.flatMap((item, index) => {
    const question = asRecord(item);
    const text = asString(question.question).trim();
    const options = asStringArray(question.options).filter(Boolean);
    if (!text || options.length < 1) return [];
    return [{ id: asString(question.id, `question-${index}`), question: text, options }];
  });
  return questions.length
    ? { id: asString(payload.request_id), questions }
    : null;
}

function asProviderCatalog(value: unknown): ProviderOption[] {
  if (!Array.isArray(value)) return [];
  return value.flatMap((item) => {
    const provider = asRecord(item);
    const id = asString(provider.id);
    if (!id) return [];
    const models = Array.isArray(provider.models)
      ? provider.models.flatMap((candidate) => {
          const model = asRecord(candidate);
          const modelId = asString(model.id);
          if (!modelId) return [];
          return [{
            ...model,
            id: modelId,
            label: asString(model.label, modelId),
            description: asString(model.description),
            reasoning_efforts: asStringArray(model.reasoning_efforts),
            thinking_modes: asStringArray(model.thinking_modes),
          } as ProviderModel];
        })
      : [];
    return [{
      id,
      label: asString(provider.label, id),
      description: asString(provider.description),
      requires_api_key: provider.requires_api_key === true,
      api_key_configured: provider.api_key_configured === true,
      api_key_env: asString(provider.api_key_env),
      models,
    }];
  });
}

function asToolDescriptions(value: unknown): Record<string, string> {
  if (!Array.isArray(value)) return {};
  return Object.fromEntries(value.flatMap((item) => {
    const tool = asRecord(item);
    const name = asString(tool.name).trim();
    const description = asString(tool.description).trim().replace(/\s+/g, " ");
    return name && description ? [[name, description]] : [];
  }));
}

function asCommandCatalog(value: unknown): SlashCommandOption[] {
  if (!Array.isArray(value)) return [];
  return value.flatMap((item) => {
    const command = asRecord(item);
    const name = asString(command.command).trim();
    const description = asString(command.description).trim();
    if (!name.startsWith("/") || !description) return [];
    return [{
      command: name,
      description,
      category: asString(command.category, "command"),
    }];
  });
}

interface RuntimeSelection {
  provider: string;
  model: string;
  reasoningEffort: string;
  thinkingMode: string;
  apiKey: string;
  apiBaseUrl: string;
  apiKeyEnv: string;
}

type SessionSetting = "memory_enabled" | "plan_mode";

function apiKeyFailure(value: string): string | null {
  const key = value.trim();
  if (!key) return null;
  if (!/^[\x21-\x7e]+$/.test(key)) return "Use printable characters without spaces.";
  if (/^[A-Za-z_][A-Za-z0-9_]*=/.test(key)) return "Paste only the key value, not NAME=value.";
  if (key.length >= 2 && ((key.startsWith('"') && key.endsWith('"')) || (key.startsWith("'") && key.endsWith("'")))) {
    return "Remove the surrounding quotes.";
  }
  return null;
}

export default function App() {
  const [theme, setTheme] = useState<Theme>(() => {
    const saved = localStorage.getItem(THEME_STORAGE_KEY);
    if (saved === "light" || saved === "dark") return saved;
    return window.matchMedia("(prefers-color-scheme: light)").matches ? "light" : "dark";
  });
  const [projectPath, setProjectPath] = useState<string | null>(null);
  const [connection, setConnection] = useState<ConnectionState>("no-project");
  const [provider, setProvider] = useState("—");
  const [model, setModel] = useState("—");
  const [keyConfigured, setKeyConfigured] = useState(true);
  const [providers, setProviders] = useState<ProviderOption[]>([]);
  const [toolDescriptions, setToolDescriptions] = useState<Record<string, string>>({});
  const [commands, setCommands] = useState<SlashCommandOption[]>(FALLBACK_SLASH_COMMANDS);
  const [reasoningEffort, setReasoningEffort] = useState("");
  const [thinkingMode, setThinkingMode] = useState("");
  const [customBaseUrl, setCustomBaseUrl] = useState("");
  const [customKeyEnv, setCustomKeyEnv] = useState("");
  const [settingsOpen, setSettingsOpen] = useState(false);
  const [onboardingOpen, setOnboardingOpen] = useState(
    () => localStorage.getItem(ONBOARDING_STORAGE_KEY) !== "complete",
  );
  const [runtimeSaving, setRuntimeSaving] = useState(false);
  const [sessionSettingSaving, setSessionSettingSaving] = useState<SessionSetting | null>(null);
  const [memoryEnabled, setMemoryEnabled] = useState(false);
  const [planMode, setPlanMode] = useState(false);
  const [messages, setMessages] = useState<ChatMessage[]>([]);
  const [draft, setDraft] = useState("");
  const [working, setWorking] = useState(false);
  const [activity, setActivity] = useState("");
  const [activityElapsed, setActivityElapsed] = useState(0);
  const [environment, setEnvironment] = useState<EnvironmentReport | null>(null);
  const [attachments, setAttachments] = useState<ComposerAttachment[]>([]);
  const [recentProject, setRecentProject] = useState<string | null>(() => localStorage.getItem(PROJECT_STORAGE_KEY));
  const [setupOpen, setSetupOpen] = useState(false);
  const [setupInitialStep, setSetupInitialStep] = useState<SetupStep | null>(null);
  const [approval, setApproval] = useState<ApprovalRequest | null>(null);
  const [approvalDecision, setApprovalDecision] = useState<"allow_once" | "allow_session" | "deny" | null>(null);
  const [questions, setQuestions] = useState<QuestionRequest | null>(null);
  const [directionPrompt, setDirectionPrompt] = useState<{ id: string; message: string } | null>(null);
  const [composerFocusRequest, setComposerFocusRequest] = useState(0);
  const [todos, setTodos] = useState<TodoItem[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [engineLog, setEngineLog] = useState("");
  const [workspacePreview, setWorkspacePreview] = useState<WorkspacePreview | null>(null);
  const [previewLine, setPreviewLine] = useState<number | null>(null);
  const [previewLoading, setPreviewLoading] = useState(false);
  const [previewError, setPreviewError] = useState("");
  const [previewWidth, setPreviewWidth] = useState(() => {
    const saved = Number(localStorage.getItem(PREVIEW_WIDTH_STORAGE_KEY));
    return clampPreviewWidth(Number.isFinite(saved) && saved > 0 ? saved : window.innerWidth * 0.38);
  });
  const [previewResizing, setPreviewResizing] = useState(false);
  const [followingOutput, setFollowingOutput] = useState(true);
  const bridgeIdRef = useRef("");
  const previewRequestRef = useRef(0);
  const conversationRef = useRef<HTMLElement>(null);
  const autoFollowRef = useRef(true);
  const pendingTextRef = useRef<Map<string, string>>(new Map());
  // Live code for write-ish calls, keyed by turn id, then by tool name: the
  // model's argument stream is decoded engine-side and arrives as deltas.
  const pendingCodeRef = useRef<Map<string, Map<string, { delta: string; path: string }>>>(new Map());
  const agentSnapshotsRef = useRef<Map<string, AgentSnapshot[]>>(new Map());
  const approvalInFlightRef = useRef("");
  const streamFrameRef = useRef<number | null>(null);
  const streamTimeoutRef = useRef<number | null>(null);
  const lastStreamFlushRef = useRef(0);
  const previewResizeRef = useRef<{ pointerId: number; startX: number; startWidth: number } | null>(null);

  useEffect(() => {
    document.documentElement.dataset.theme = theme;
    document.documentElement.style.colorScheme = theme;
    const backgroundColor = theme === "dark" ? "#0b0b11" : "#f6f6fa";
    document.documentElement.style.backgroundColor = backgroundColor;
    document.body.style.backgroundColor = backgroundColor;
    void getCurrentWindow().setBackgroundColor(backgroundColor).catch(() => undefined);
    localStorage.setItem(THEME_STORAGE_KEY, theme);
  }, [theme]);

  useEffect(() => {
    localStorage.setItem(PREVIEW_WIDTH_STORAGE_KEY, String(Math.round(previewWidth)));
  }, [previewWidth]);

  useEffect(() => {
    const keepPreviewInBounds = () => setPreviewWidth((current) => clampPreviewWidth(current));
    window.addEventListener("resize", keepPreviewInBounds);
    return () => window.removeEventListener("resize", keepPreviewInBounds);
  }, []);

  const applySessionPayload = useCallback((payload: BridgePayload) => {
    if (typeof payload.memory_enabled === "boolean") setMemoryEnabled(payload.memory_enabled);
    if (typeof payload.plan_mode === "boolean") setPlanMode(payload.plan_mode);
  }, []);

  const applyRuntimePayload = useCallback((payload: BridgePayload) => {
    setProvider(asString(payload.provider, "Unknown"));
    setModel(asString(payload.model, "Unknown"));
    setKeyConfigured(payload.api_key_configured !== false);
    setProviders(asProviderCatalog(payload.providers));
    if ("tools" in payload) setToolDescriptions(asToolDescriptions(payload.tools));
    if ("commands" in payload) {
      const catalog = asCommandCatalog(payload.commands);
      if (catalog.length > 0) setCommands(catalog);
    }
    setReasoningEffort(asString(payload.reasoning_effort));
    setThinkingMode(asString(payload.thinking_mode));
    setCustomBaseUrl(asString(payload.api_base_url));
    setCustomKeyEnv(asString(payload.api_key_env));
    if ("todos" in payload) setTodos(asTodos(payload.todos));
    applySessionPayload(payload);
  }, [applySessionPayload]);

  const flushStreamText = useCallback(() => {
    streamFrameRef.current = null;
    streamTimeoutRef.current = null;
    const pending = pendingTextRef.current;
    const pendingCode = pendingCodeRef.current;
    if (pending.size === 0 && pendingCode.size === 0) return;
    pendingTextRef.current = new Map();
    pendingCodeRef.current = new Map();
    lastStreamFlushRef.current = performance.now();
    setMessages((current) => current.map((message) => {
      const delta = pending.get(message.id);
      const codeDeltas = pendingCode.get(message.id);
      if (!delta && !codeDeltas) return message;
      let next = message;
      if (delta) {
        next = { ...next, text: next.text + delta, segments: appendTextSegment(next.segments, delta) };
      }
      if (codeDeltas) {
        for (const [name, code] of codeDeltas) {
          next = {
            ...next,
            toolActivities: appendLiveCode(next.toolActivities ?? [], name, code.delta, code.path),
            segments: appendLiveCodeInSegments(next.segments, name, code.delta, code.path),
          };
        }
      }
      return next;
    }));
  }, []);

  const queueToolCode = useCallback((turnId: string, name: string, delta: string, path: string) => {
    if (!turnId || !name || !delta) return;
    const perTurn = pendingCodeRef.current.get(turnId) ?? new Map<string, { delta: string; path: string }>();
    const existing = perTurn.get(name);
    perTurn.set(name, { delta: (existing?.delta ?? "") + delta, path: path || existing?.path || "" });
    pendingCodeRef.current.set(turnId, perTurn);
    if (streamFrameRef.current === null && streamTimeoutRef.current === null) {
      streamFrameRef.current = window.requestAnimationFrame(flushStreamText);
    }
  }, [flushStreamText]);

  const queueStreamText = useCallback((turnId: string, delta: string) => {
    if (!turnId || !delta) return;
    pendingTextRef.current.set(
      turnId,
      (pendingTextRef.current.get(turnId) ?? "") + delta,
    );
    if (streamFrameRef.current === null && streamTimeoutRef.current === null) {
      // Each flush re-parses the streaming message's markdown; cap it at
      // ~25 fps so a fast token stream does not spend its time in remark.
      const wait = Math.max(0, STREAM_FLUSH_INTERVAL_MS - (performance.now() - lastStreamFlushRef.current));
      if (wait > 0) {
        streamTimeoutRef.current = window.setTimeout(() => {
          streamTimeoutRef.current = null;
          streamFrameRef.current = window.requestAnimationFrame(flushStreamText);
        }, wait);
      } else {
        streamFrameRef.current = window.requestAnimationFrame(flushStreamText);
      }
    }
  }, [flushStreamText]);

  const cancelScheduledFlush = useCallback(() => {
    if (streamTimeoutRef.current !== null) {
      window.clearTimeout(streamTimeoutRef.current);
      streamTimeoutRef.current = null;
    }
    if (streamFrameRef.current !== null) {
      window.cancelAnimationFrame(streamFrameRef.current);
      streamFrameRef.current = null;
    }
  }, []);

  // Text is throttled but tool/turn events are not: any event that changes the
  // segment structure must first land the text that arrived before it, or the
  // tail of the prose ends up *below* the tool cards it preceded.
  const flushStreamTextNow = useCallback(() => {
    cancelScheduledFlush();
    flushStreamText();
  }, [cancelScheduledFlush, flushStreamText]);

  const discardQueuedText = useCallback((turnId?: string) => {
    if (turnId) pendingTextRef.current.delete(turnId);
    else pendingTextRef.current.clear();
    if (turnId) pendingCodeRef.current.delete(turnId);
    else pendingCodeRef.current.clear();
    if (pendingTextRef.current.size === 0 && pendingCodeRef.current.size === 0) cancelScheduledFlush();
  }, [cancelScheduledFlush]);

  useEffect(() => () => discardQueuedText(), [discardQueuedText]);

  // Count seconds since the current activity phase began. Reasoning models can
  // sit in "Reasoning…" for a minute with no visible tokens; a ticking clock
  // is the difference between "working" and "hung" to the person watching.
  useEffect(() => {
    setActivityElapsed(0);
    if (!working || !activity) return;
    const startedAt = performance.now();
    const timer = window.setInterval(() => {
      setActivityElapsed(Math.floor((performance.now() - startedAt) / 1000));
    }, 1000);
    return () => window.clearInterval(timer);
  }, [activity, working]);

  const handleBridgePayload = useCallback((payload: BridgePayload) => {
    switch (payload.type) {
      case "hello":
        if (Math.trunc(asFiniteNumber(payload.protocol)) < 3) {
          setConnection("failed");
          setWorking(false);
          setActivity("");
          setError(
            "The installed Tether CLI is too old for this desktop build. Update the CLI, then reopen the workspace.",
          );
          break;
        }
        applyRuntimePayload(payload);
        setConnection("ready");
        setActivity("");
        setError(null);
        break;

      case "turn_started": {
        const turnId = asString(payload.id, uid());
        autoFollowRef.current = true;
        setFollowingOutput(true);
        setWorking(true);
        setActivity("Tether is working…");
        setMessages((current) => {
          const updated = current.map((message) => {
            if (message.role !== "user") return message;
            if (message.turnId === turnId) {
              return { ...message, queueState: "active" as const, queuePosition: undefined, queueReason: undefined };
            }
            if (message.queueState === "queued" && message.queuePosition) {
              return { ...message, queuePosition: Math.max(1, message.queuePosition - 1) };
            }
            return message;
          });
          if (updated.some((message) => message.role === "assistant" && message.id === turnId)) return updated;
          return [
            ...updated,
            {
              id: turnId,
              turnId,
              role: "assistant",
              text: "",
              toolCalls: [],
              agents: agentSnapshotsRef.current.get(turnId) ?? [],
              streaming: true,
            },
          ];
        });
        break;
      }

      case "turn_queued": {
        const turnId = asString(payload.id);
        const position = Math.max(1, Math.trunc(asFiniteNumber(payload.position, 1)));
        setWorking(true);
        setMessages((current) => current.map((message) => (
          message.role === "user" && message.turnId === turnId
            ? {
                ...message,
                queueState: "queued",
                queuePosition: position,
                queueReason: undefined,
              }
            : message
        )));
        setActivity(`Follow-up queued · position ${position}`);
        break;
      }

      case "turn_queue_cleared": {
        const ids = new Set(asStringArray(payload.ids));
        const reason = queueClearReason(payload.reason);
        setMessages((current) => current.map((message) => (
          message.role === "user" && message.turnId && ids.has(message.turnId)
            ? {
                ...message,
                queueState: "cleared",
                queuePosition: undefined,
                queueReason: reason,
              }
            : message
        )));
        break;
      }

      case "bridge_idle":
        setWorking(false);
        setApproval(null);
        setApprovalDecision(null);
        approvalInFlightRef.current = "";
        setQuestions(null);
        setActivity("");
        break;

      case "direction_required": {
        const message = asString(payload.message, "What should Tether do instead?");
        setDirectionPrompt({ id: asString(payload.id, uid()), message });
        setComposerFocusRequest((current) => current + 1);
        break;
      }

      case "agent_updated": {
        const agent = asAgentSnapshot(payload);
        if (!agent) break;
        const agents = upsertAgentSnapshot(
          agentSnapshotsRef.current.get(agent.turnId) ?? [],
          agent,
        );
        agentSnapshotsRef.current.set(agent.turnId, agents);
        setMessages((current) => current.map((message) => (
          message.role === "assistant" && (message.turnId === agent.turnId || message.id === agent.turnId)
            ? { ...message, agents }
            : message
        )));
        break;
      }

      case "approval_resolved": {
        const requestId = asString(payload.request_id);
        if (requestId && approvalInFlightRef.current && requestId !== approvalInFlightRef.current) break;
        const decision = asString(payload.decision);
        const accepted = payload.accepted !== false;
        const stale = payload.stale === true;
        approvalInFlightRef.current = "";
        setApprovalDecision(null);
        setApproval((current) => !requestId || current?.id === requestId ? null : current);
        if (!accepted || stale) {
          setError(stale ? "That approval request is no longer active." : "The approval response was not accepted.");
        } else {
          setError(null);
          setActivity(decision === "deny" ? "Stopping safely…" : "Running approved action…");
        }
        break;
      }

      case "turn_delta": {
        const turnId = asString(payload.id);
        const kind = asString(payload.kind);
        if (kind === "thinking") {
          setActivity("Reasoning…");
        } else if (kind === "tool_running") {
          flushStreamTextNow();
          const tool = asString(payload.tool, "tool");
          const callId = asString(payload.tool_call_id);
          setActivity(`Running ${tool.replaceAll("_", " ")}…`);
          setMessages((current) => current.map((message) => (
            message.id === turnId
              ? (() => {
                  const activity: ToolActivity = {
                    id: callId,
                    name: tool,
                    status: "running",
                    argsPreview: asString(payload.tool_args_preview),
                    outputPreview: "",
                    displayKind: asString(payload.display_kind, "generic"),
                    errorCode: "",
                    metadata: asRecord(payload.metadata),
                  };
                  return {
                    ...message,
                    toolCalls: [...new Set([...(message.toolCalls ?? []), tool])],
                    toolActivities: updateRunningActivity(message.toolActivities ?? [], activity),
                    segments: startToolInSegments(message.segments, activity),
                  };
                })()
              : message
          )));
        } else if (kind === "tool_done") {
          flushStreamTextNow();
          setActivity(payload.is_error === true ? "A tool needs attention…" : "Reviewing the result…");
          const tool = asString(payload.tool, "tool");
          const callId = asString(payload.tool_call_id);
          setMessages((current) => current.map((message) => (
            message.id === turnId
              ? (() => {
                  const activity: ToolActivity = {
                    id: callId,
                    name: tool,
                    status: payload.is_error === true ? "failed" : "completed",
                    argsPreview: "",
                    outputPreview: asString(payload.tool_output_preview),
                    displayKind: asString(payload.display_kind, "generic"),
                    errorCode: asString(payload.error_code),
                    metadata: asRecord(payload.metadata),
                  };
                  return {
                    ...message,
                    toolActivities: finishActivity(message.toolActivities ?? [], activity),
                    segments: finishToolInSegments(message.segments, activity),
                  };
                })()
              : message
          )));
        } else if (kind === "tool_code") {
          const meta = asRecord(payload.metadata);
          setActivity(`Writing ${asString(meta.path) ? shortPath(asString(meta.path)) : "code"}…`);
          queueToolCode(turnId, asString(payload.tool, ""), asString(payload.text), asString(meta.path));
        } else if (kind === "text") {
          setActivity("Writing response…");
          queueStreamText(turnId, asString(payload.text));
        } else if (kind === "checkpoint") {
          setActivity("Preparing a safe checkpoint…");
        }
        break;
      }

      case "approval_required":
        approvalInFlightRef.current = "";
        setApprovalDecision(null);
        setApproval({
          id: asString(payload.request_id),
          tool: asString(payload.tool, "tool"),
          arguments: asRecord(payload.arguments),
        });
        setActivity("Waiting for your approval");
        break;

      case "questions_required":
        setQuestions(asQuestionRequest(payload));
        setActivity("Waiting for your answers");
        break;

      case "todo_updated":
        setTodos(asTodos(payload.todos));
        break;

      case "turn_completed": {
        const usage = asRecord(payload.usage);
        const tokenCount = typeof usage.total_tokens === "number" ? usage.total_tokens : 0;
        const turnId = asString(payload.id);
        flushStreamTextNow();
        discardQueuedText(turnId);
        setMessages((current) => {
          const updated = current.map((message) => (
            message.role === "user" && message.turnId === turnId
              ? {
                  ...message,
                  queueState: undefined,
                  queuePosition: undefined,
                  queueReason: undefined,
                }
              : message
          ));
          const output = asString(payload.output, "(No response)");
          const completed = {
            id: turnId || uid(),
            turnId,
            role: "assistant" as const,
            text: output,
            toolCalls: asStringArray(payload.tool_calls),
            tokenCount,
            streaming: false,
            stopReason: asString(payload.stop_reason),
          };
          return updated.some((message) => message.role === "assistant" && message.id === turnId)
            ? updated.map((message) => message.role === "assistant" && message.id === turnId
              ? { ...message, ...completed, segments: finalizeSegments(message.segments, output) }
              : message)
            : [...updated, { ...completed, agents: agentSnapshotsRef.current.get(turnId) ?? [] }];
        });
        setActivity("Finishing turn…");
        break;
      }

      case "turn_failed": {
        const message = asString(payload.message, "The turn failed.");
        const turnId = asString(payload.id);
        flushStreamTextNow();
        discardQueuedText(turnId);
        setMessages((current) => {
          const updated = current.map((item) => (
            item.role === "user" && item.turnId === turnId
              ? { ...item, queueState: undefined, queuePosition: undefined, queueReason: undefined }
              : item
          ));
          const failedText = `Engine error: ${message}`;
          const failed = {
            id: turnId || uid(),
            turnId,
            role: "assistant" as const,
            text: failedText,
            streaming: false,
          };
          return updated.some((item) => item.role === "assistant" && item.id === turnId)
            ? updated.map((item) => item.role === "assistant" && item.id === turnId
              ? { ...item, ...failed, segments: finalizeSegments(item.segments, failedText) }
              : item)
            : [...updated, { ...failed, agents: agentSnapshotsRef.current.get(turnId) ?? [] }];
        });
        setActivity("Finishing turn…");
        setError(message);
        break;
      }

      case "model_learned": {
        // Automatic learning recorded cited beliefs from the last turn.
        const count = Math.trunc(asFiniteNumber(payload.count, 0));
        if (count > 0) setActivity(`Learned ${count} fact${count === 1 ? "" : "s"} about this project`);
        break;
      }

      case "attachments_resolved": {
        const turnId = asString(payload.id);
        const notes = Array.isArray(payload.attachments) ? payload.attachments.map((n) => asRecord(n)) : [];
        setMessages((current) => current.map((message) => (
          message.role === "user" && message.turnId === turnId && message.attachments
            ? {
                ...message,
                attachments: message.attachments.map((a, index) => {
                  const note = notes[index];
                  return note ? { ...a, ok: note.ok !== false, detail: asString(note.detail) } : a;
                }),
              }
            : message
        )));
        const failed = notes.filter((n) => n.ok === false);
        if (failed.length > 0) setError(`Could not attach ${failed.map((n) => asString(n.name)).join(", ")}: ${asString(failed[0].detail)}`);
        break;
      }

      case "providers_updated":
        // Live model discovery finished (background); refresh the catalog
        // only — never disturb the open settings sheet or the active model.
        setProviders(asProviderCatalog(payload.providers));
        break;

      case "runtime_configured":
        applyRuntimePayload(payload);
        setRuntimeSaving(false);
        setSettingsOpen(false);
        setActivity("Runtime updated");
        setError(null);
        break;

      case "session_configured":
        applySessionPayload(payload);
        setSessionSettingSaving(null);
        setError(null);
        break;

      case "session_config_failed":
        setSessionSettingSaving(null);
        setError(asString(payload.message, "Could not update the session settings."));
        break;

      case "catalog_updated":
        applyRuntimePayload(payload);
        setRuntimeSaving(false);
        setActivity("Local model directory added");
        break;

      case "runtime_config_failed":
        setRuntimeSaving(false);
        setError(asString(payload.message, "Could not update the runtime."));
        break;

      case "command_result": {
        applySessionPayload(payload);
        const commandId = asString(payload.id, uid());
        const commandMessage = asString(payload.message, "Command completed.");
        const resultMessage = {
          id: `result-${commandId}`,
          role: "assistant" as const,
          text: commandMessage,
        };
        setMessages((current) => current.some((message) => message.id === resultMessage.id)
          ? current.map((message) => message.id === resultMessage.id ? resultMessage : message)
          : [...current, resultMessage]);
        setWorking(false);
        setActivity("");
        setError(payload.ok === false ? commandMessage : null);
        break;
      }

      case "command_progress":
        setWorking(true);
        setActivity(asString(payload.message, "Running command…"));
        break;

      case "session_reset":
        discardQueuedText();
        agentSnapshotsRef.current.clear();
        approvalInFlightRef.current = "";
        applySessionPayload(payload);
        setMessages([]);
        setTodos(asTodos(payload.todos));
        setWorking(false);
        setApproval(null);
        setApprovalDecision(null);
        setQuestions(null);
        setDirectionPrompt(null);
        setSessionSettingSaving(null);
        setError(null);
        setActivity("New session ready");
        break;

      case "cancel_requested":
        setActivity("Stopping safely…");
        break;

      case "bridge_log":
        setEngineLog(asString(payload.message));
        break;

      case "bridge_stopped":
        discardQueuedText();
        agentSnapshotsRef.current.clear();
        approvalInFlightRef.current = "";
        setConnection("failed");
        setWorking(false);
        setApproval(null);
        setApprovalDecision(null);
        setQuestions(null);
        setDirectionPrompt(null);
        setActivity("");
        setError(asString(payload.message, "The Tether engine stopped."));
        break;

      case "error": {
        const message = asString(payload.message, "Unknown engine error");
        const turnId = asString(payload.id);
        if (turnId) {
          setMessages((current) => current.map((item) => (
            item.role === "user" && item.turnId === turnId
              ? {
                  ...item,
                  queueState: "cleared",
                  queuePosition: undefined,
                  queueReason: message,
                }
              : item
          )));
        }
        setError(message);
        setRuntimeSaving(false);
        setSessionSettingSaving(null);
        break;
      }
    }
  }, [applyRuntimePayload, applySessionPayload, discardQueuedText, queueStreamText]);

  const refreshEnvironment = useCallback(async (): Promise<EnvironmentReport | null> => {
    try {
      const report = await invoke<EnvironmentReport>("check_environment");
      setEnvironment(report);
      return report;
    } catch {
      return null;
    }
  }, []);

  const openSetup = useCallback((step: SetupStep | null = null) => {
    setSetupInitialStep(step);
    setSetupOpen(true);
    void refreshEnvironment();
  }, [refreshEnvironment]);

  const connectProject = useCallback(async (path: string) => {
    const bridgeId = uid();
    bridgeIdRef.current = bridgeId;
    discardQueuedText();
    agentSnapshotsRef.current.clear();
    approvalInFlightRef.current = "";
    setProjectPath(path || null);
    setConnection("connecting");
    setMessages([]);
    setTodos([]);
    setWorking(false);
    setApproval(null);
    setApprovalDecision(null);
    setQuestions(null);
    setDirectionPrompt(null);
    setRuntimeSaving(false);
    setSessionSettingSaving(null);
    setSettingsOpen(false);
    setMemoryEnabled(false);
    setPlanMode(false);
    setProvider("—");
    setModel("—");
    setActivity("Starting the Tether engine…");
    setError(null);
    setEngineLog("");
    previewRequestRef.current += 1;
    setWorkspacePreview(null);
    setPreviewLine(null);
    setPreviewLoading(false);
    setPreviewError("");
    autoFollowRef.current = true;
    setFollowingOutput(true);
    if (path) {
      localStorage.setItem(PROJECT_STORAGE_KEY, path);
      setRecentProject(path);
    }
    try {
      await invoke<string>("start_bridge", { project: path, bridgeId });
    } catch (caught) {
      const message = String(caught);
      setConnection("failed");
      setActivity("");
      setError(message);
      if (/not found/i.test(message)) {
        setSetupInitialStep(null);
        setSetupOpen(true);
        void refreshEnvironment();
      }
    }
  }, [discardQueuedText, refreshEnvironment]);

  const openWorkspacePath = useCallback(async (reference: string) => {
    const parsed = parsePathReference(reference, true);
    if (!parsed) return;
    const requestId = previewRequestRef.current + 1;
    previewRequestRef.current = requestId;
    setPreviewLine(parsed.line);
    setPreviewLoading(true);
    setPreviewError("");
    try {
      const result = await invoke<WorkspacePreview>("read_workspace_entry", { path: parsed.path });
      if (previewRequestRef.current !== requestId) return;
      setWorkspacePreview(result);
    } catch (caught) {
      if (previewRequestRef.current !== requestId) return;
      setWorkspacePreview(null);
      setPreviewError(String(caught));
    } finally {
      if (previewRequestRef.current === requestId) setPreviewLoading(false);
    }
  }, []);

  const closeWorkspacePreview = useCallback(() => {
    previewRequestRef.current += 1;
    setWorkspacePreview(null);
    setPreviewLine(null);
    setPreviewLoading(false);
    setPreviewError("");
  }, []);

  useEffect(() => {
    let disposed = false;
    let unlisten: UnlistenFn | undefined;

    void (async () => {
      unlisten = await listen<BridgeEnvelope>("bridge-event", (event) => {
        if (event.payload.bridgeId !== bridgeIdRef.current) return;
        handleBridgePayload(event.payload.payload);
      });
      if (disposed) {
        unlisten();
        return;
      }
      const report = await refreshEnvironment();
      if (report && !report.ready) {
        setSetupOpen(true);
        return; // connecting would just fail; the setup dialog continues for us
      }
      // Always start blank: a general session on the scratch workspace. The
      // last project is only offered as a shortcut in the sidebar.
      await connectProject("");
    })();

    return () => {
      disposed = true;
      unlisten?.();
      void invoke("stop_bridge");
    };
  }, [connectProject, handleBridgePayload, refreshEnvironment]);

  const handleConversationScroll = useCallback(() => {
    const conversation = conversationRef.current;
    if (!conversation) return;
    const shouldFollow = conversation.scrollHeight - conversation.scrollTop - conversation.clientHeight < 56;
    autoFollowRef.current = shouldFollow;
    setFollowingOutput((current) => current === shouldFollow ? current : shouldFollow);
  }, []);

  const jumpToLatest = useCallback(() => {
    const conversation = conversationRef.current;
    if (!conversation) return;
    autoFollowRef.current = true;
    setFollowingOutput(true);
    conversation.scrollTo({ top: conversation.scrollHeight, behavior: "smooth" });
  }, []);

  useEffect(() => {
    if (!autoFollowRef.current) return;
    const frame = window.requestAnimationFrame(() => {
      const conversation = conversationRef.current;
      if (conversation && autoFollowRef.current) conversation.scrollTop = conversation.scrollHeight;
    });
    return () => window.cancelAnimationFrame(frame);
  }, [messages]);

  const chooseProject = async () => {
    const selection = await open({
      directory: true,
      multiple: false,
      title: "Choose a project for Tether",
    });
    if (typeof selection === "string") await connectProject(selection);
  };

  const saveRuntime = async (selection: RuntimeSelection) => {
    if (working || runtimeSaving) return;
    setRuntimeSaving(true);
    setError(null);
    setActivity(selection.provider === "local" ? "Starting local model…" : "Updating runtime…");
    try {
      await invoke("send_bridge", {
        message: {
          type: "configure_runtime",
          provider: selection.provider,
          model: selection.model,
          reasoning_effort: selection.reasoningEffort,
          thinking_mode: selection.thinkingMode,
          api_key: selection.apiKey,
          api_base_url: selection.apiBaseUrl,
          api_key_env: selection.apiKeyEnv,
        },
      });
    } catch (caught) {
      setRuntimeSaving(false);
      setActivity("");
      setError(String(caught));
    }
  };

  const addModelDirectory = async () => {
    const selection = await open({
      directory: true,
      multiple: false,
      title: "Choose a folder containing GGUF models",
    });
    if (typeof selection !== "string") return;
    setRuntimeSaving(true);
    try {
      await invoke("send_bridge", {
        message: { type: "add_model_directory", path: selection },
      });
    } catch (caught) {
      setRuntimeSaving(false);
      setError(String(caught));
    }
  };

  const attachFiles = async () => {
    const selection = await open({
      multiple: true,
      directory: false,
      title: "Attach files",
      filters: [
        { name: "Documents & code", extensions: ["pdf", "txt", "md", "markdown", "rst", "csv", "json", "yaml", "yml", "toml", "xml", "html", "css", "log", "py", "js", "jsx", "ts", "tsx", "go", "rs", "java", "kt", "swift", "c", "h", "cpp", "hpp", "cs", "rb", "php", "sh", "sql", "lua", "r", "dart", "vue", "svelte", "tf", "proto"] },
        { name: "Images", extensions: ["png", "jpg", "jpeg", "gif", "webp"] },
        { name: "All files", extensions: ["*"] },
      ],
    });
    const paths = Array.isArray(selection) ? selection : typeof selection === "string" ? [selection] : [];
    if (paths.length === 0) return;
    setAttachments((current) => [
      ...current,
      ...paths
        .filter((path) => !current.some((a) => a.path === path))
        .map((path) => ({ id: uid(), kind: "file" as const, name: basename(path), path })),
    ]);
  };

  const addPasteAttachment = (text: string) => {
    const lines = text.split(/\r?\n/).length;
    setAttachments((current) => [
      ...current,
      { id: uid(), kind: "paste", name: `Pasted text (${lines} lines)`, text, lines },
    ]);
  };

  const configureSession = async (setting: SessionSetting, enabled: boolean) => {
    if (connection !== "ready" || working || sessionSettingSaving !== null) return;
    setSessionSettingSaving(setting);
    setError(null);
    try {
      await invoke("send_bridge", {
        message: { type: "configure_session", [setting]: enabled },
      });
    } catch (caught) {
      setSessionSettingSaving(null);
      setError(String(caught));
    }
  };

  const submitConversationPrompt = useCallback(async (
    prompt: string,
    displayText = prompt,
    attached: ComposerAttachment[] = [],
  ) => {
    if ((!prompt && attached.length === 0) || connection !== "ready") return;
    const turnId = uid();
    const wasWorking = working;
    autoFollowRef.current = true;
    setFollowingOutput(true);
    setMessages((current) => [...current, {
      id: `user-${turnId}`,
      turnId,
      role: "user",
      text: displayText,
      attachments: attached.length > 0 ? attached.map((a) => ({ ...a, text: undefined })) : undefined,
      queueState: "submitting",
    }]);
    setDraft("");
    setAttachments([]);
    setDirectionPrompt(null);
    setWorking(true);
    setActivity(wasWorking ? "Queuing your follow-up…" : "Queuing your request…");
    setError(null);
    try {
      await invoke("send_bridge", {
        message: {
          type: "submit",
          id: turnId,
          prompt,
          attachments: attached.map((a) => (
            a.kind === "paste"
              ? { kind: "paste", name: a.name, text: a.text ?? "" }
              : { kind: "file", name: a.name, path: a.path ?? "" }
          )),
        },
      });
    } catch (caught) {
      const message = String(caught);
      setMessages((current) => current.map((item) => (
        item.role === "user" && item.turnId === turnId
          ? { ...item, queueState: "cleared", queueReason: message }
          : item
      )));
      if (!wasWorking) {
        setWorking(false);
        setActivity("");
      }
      setError(message);
    }
  }, [connection, working]);

  const continueWorkflow = useCallback(() => {
    void submitConversationPrompt(
      "Continue the active workflow from the exact checkpoint. Do not repeat completed work or tool calls. Finish the remaining work and verify it.",
      "Continue workflow",
    );
  }, [submitConversationPrompt]);

  const sendMessage = async () => {
    const prompt = draft.trim();
    if ((!prompt && attachments.length === 0) || connection !== "ready") return;
    if (prompt.startsWith("/")) {
      const alsoMatch = prompt.match(/^\/also(?:\s+([\s\S]+))?$/i);
      if (alsoMatch) {
        const followUp = (alsoMatch[1] ?? "").trim();
        if (!followUp) {
          setError("Add the follow-up after `/also`.");
          return;
        }
        await submitConversationPrompt(followUp, followUp);
        return;
      }
      if (prompt.toLowerCase() === "/stop") {
        if (!working) {
          setError("There is no active turn to stop.");
          return;
        }
        setDraft("");
        await cancelTurn();
        return;
      }
      if (working) {
        setError("That command cannot run during an active turn. Use `/also <follow-up>` or `/stop`.");
        return;
      }
      setDraft("");
      if (["/help", "/", "/?"].includes(prompt.toLowerCase())) {
        setOnboardingOpen(true);
        return;
      }
      if (prompt.toLowerCase() === "/clear") {
        await resetSession();
        return;
      }
      if (/^\/model\s+(?:local|api)(?:\s|$)/i.test(prompt) || /^\/provider\s+\S/i.test(prompt)) {
        setSettingsOpen(true);
        return;
      }
      const commandId = uid();
      setMessages((current) => [...current, { id: `command-${commandId}`, role: "user", text: prompt }]);
      setWorking(true);
      setActivity(`Running ${prompt.split(/\s+/, 1)[0]}…`);
      setError(null);
      try {
        await invoke("send_bridge", {
          message: { type: "command", id: commandId, command: prompt },
        });
      } catch (caught) {
        setWorking(false);
        setActivity("");
        setError(String(caught));
      }
      return;
    }
    await submitConversationPrompt(prompt || "(see attachments)", prompt, attachments);
  };

  const cancelTurn = async () => {
    if (!working) return;
    setActivity("Stopping safely…");
    try {
      await invoke("send_bridge", { message: { type: "cancel" } });
    } catch (caught) {
      setError(String(caught));
    }
  };

  const resetSession = async () => {
    if (connection !== "ready" || working) return;
    autoFollowRef.current = true;
    setFollowingOutput(true);
    await invoke("send_bridge", { message: { type: "reset" } });
  };

  const answerApproval = async (decision: "allow_once" | "allow_session" | "deny") => {
    if (!approval || approvalInFlightRef.current) return;
    const requestId = approval.id;
    approvalInFlightRef.current = requestId;
    setApprovalDecision(decision);
    setError(null);
    setActivity("Sending your decision…");
    try {
      await invoke("send_bridge", {
        message: {
          type: "approval_response",
          request_id: requestId,
          decision,
        },
      });
    } catch (caught) {
      if (approvalInFlightRef.current === requestId) approvalInFlightRef.current = "";
      setApprovalDecision(null);
      setActivity("Waiting for your approval");
      setError(String(caught));
    }
  };

  const answerQuestions = async (answers: Array<{ question: string; answer: string }>) => {
    if (!questions) return;
    await invoke("send_bridge", {
      message: {
        type: "questions_response",
        request_id: questions.id,
        answers,
      },
    });
    setQuestions(null);
    setActivity(answers.length ? "Applying your answers…" : "Continuing with defaults…");
  };

  const statusLabel = useMemo(() => {
    if (connection === "ready") return "Engine ready";
    if (connection === "connecting") return "Connecting";
    if (connection === "failed") return "Needs attention";
    return "Choose a project";
  }, [connection]);

  const projectName = projectPath ? basename(projectPath) : "General session";
  const providerLabel = providers.find((item) => item.id === provider)?.label ?? provider;
  const modelLabel = providers
    .find((item) => item.id === provider)
    ?.models.find((item) => item.id === model)?.label ?? model;
  const latencyProfile = reasoningEffort
    ? `${reasoningEffort} reasoning`
    : thinkingMode
      ? `Thinking ${thinkingMode}`
      : "Standard";
  const slowLatencyProfile = reasoningEffort === "max" || reasoningEffort === "xhigh";
  const dismissOnboarding = () => {
    localStorage.setItem(ONBOARDING_STORAGE_KEY, "complete");
    setOnboardingOpen(false);
  };

  const startWindowDrag = (event: React.MouseEvent<HTMLDivElement>) => {
    if (event.button !== 0) return;
    void getCurrentWindow().startDragging().catch(() => undefined);
  };

  const startPreviewResize = (event: React.PointerEvent<HTMLDivElement>) => {
    if (event.button !== 0) return;
    event.preventDefault();
    event.currentTarget.setPointerCapture(event.pointerId);
    previewResizeRef.current = {
      pointerId: event.pointerId,
      startX: event.clientX,
      startWidth: previewWidth,
    };
    setPreviewResizing(true);
  };

  const movePreviewResize = (event: React.PointerEvent<HTMLDivElement>) => {
    const resize = previewResizeRef.current;
    if (!resize || resize.pointerId !== event.pointerId) return;
    setPreviewWidth(clampPreviewWidth(resize.startWidth + resize.startX - event.clientX));
  };

  const stopPreviewResize = (event: React.PointerEvent<HTMLDivElement>) => {
    const resize = previewResizeRef.current;
    if (!resize || resize.pointerId !== event.pointerId) return;
    if (event.currentTarget.hasPointerCapture(event.pointerId)) {
      event.currentTarget.releasePointerCapture(event.pointerId);
    }
    previewResizeRef.current = null;
    setPreviewResizing(false);
  };

  const resizePreviewWithKeyboard = (event: React.KeyboardEvent<HTMLDivElement>) => {
    const changes: Record<string, number> = { ArrowLeft: 24, ArrowRight: -24 };
    if (event.key in changes) {
      event.preventDefault();
      setPreviewWidth((current) => clampPreviewWidth(current + changes[event.key]));
    } else if (event.key === "Home") {
      event.preventDefault();
      setPreviewWidth(PREVIEW_MIN_WIDTH);
    } else if (event.key === "End") {
      event.preventDefault();
      setPreviewWidth(maxPreviewWidth());
    }
  };

  const previewVisible = Boolean(workspacePreview || previewLoading || previewError);
  const effectivePreviewWidth = clampPreviewWidth(previewWidth);

  return (
    <div
      className={`app-shell ${previewVisible ? "preview-open" : ""} ${previewResizing ? "preview-resizing" : ""}`}
      style={{ "--preview-width": `${effectivePreviewWidth}px` } as React.CSSProperties}
    >
      <div
        className="window-drag-strip"
        data-tauri-drag-region
        onMouseDown={startWindowDrag}
        aria-hidden="true"
      />
      <aside className="sidebar" data-tauri-drag-region>
        <div className="brand-row" data-tauri-drag-region>
          <div className="brand" data-tauri-drag-region>
            <div className="brand-mark"><img src={appIcon} alt="" /></div>
            <div>
              <strong>Tether</strong>
            </div>
          </div>
          <div className="brand-controls">
            <button
              className="theme-toggle"
              onClick={() => openSetup(null)}
              aria-label="Check installation"
              title="Check installation: Tether CLI, llama.cpp, tools"
            >
              <Wrench size={15} />
            </button>
            <button
              className="theme-toggle"
              onClick={() => setOnboardingOpen(true)}
              aria-label="Open Tether guide"
              title="Open Tether guide"
            >
              <CircleHelp size={15} />
            </button>
            <button
              className="theme-toggle"
              onClick={() => setTheme((current) => current === "dark" ? "light" : "dark")}
              aria-label={`Use ${theme === "dark" ? "light" : "dark"} theme`}
              title={`Use ${theme === "dark" ? "light" : "dark"} theme`}
            >
              {theme === "dark" ? <Sun size={15} /> : <Moon size={15} />}
            </button>
          </div>
        </div>

        <button
          className="primary-action"
          onClick={() => void resetSession()}
          disabled={connection !== "ready" || working}
        >
          <MessageSquarePlus size={16} />
          New session
        </button>

        <SidebarSection title="Workspace">
          <button
            className={`workspace-card ${projectPath ? "" : "active"}`}
            onClick={() => { if (projectPath) void connectProject(""); }}
            title="Chat without a project; files go to ~/.tether/scratch"
          >
            <MessageSquarePlus size={17} />
            <span>
              <strong>General chat</strong>
              <small>{projectPath ? "Switch to no project" : "Active · no project"}</small>
            </span>
            {!projectPath && <Check size={15} />}
          </button>
          {projectPath ? (
            <button className="workspace-card active" onClick={() => void chooseProject()} title={projectPath}>
              <Folder size={17} />
              <span>
                <strong>{projectName}</strong>
                <small>Active · change project</small>
              </span>
              <ChevronRight size={15} />
            </button>
          ) : recentProject ? (
            <button className="workspace-card" onClick={() => void connectProject(recentProject)} title={recentProject}>
              <Folder size={17} />
              <span>
                <strong>{basename(recentProject)}</strong>
                <small>Recent · open</small>
              </span>
              <ChevronRight size={15} />
            </button>
          ) : null}
          {!projectPath && (
            <button className="workspace-card" onClick={() => void chooseProject()}>
              <FolderOpen size={17} />
              <span>
                <strong>Choose project…</strong>
                <small>Work on a codebase</small>
              </span>
              <ChevronRight size={15} />
            </button>
          )}
        </SidebarSection>

        <SidebarSection title="Runtime">
          <button
            className="runtime-card"
            onClick={() => setSettingsOpen(true)}
            disabled={connection !== "ready" || working}
            title="Change provider, model, and credentials"
          >
            <RuntimeRow icon={<Server size={15} />} label="Provider" value={providerLabel} />
            <RuntimeRow icon={<Cpu size={15} />} label="Model" value={modelLabel} />
            <RuntimeRow
              icon={<BrainCircuit size={15} />}
              label="Latency profile"
              value={latencyProfile}
              warning={slowLatencyProfile}
            />
            <RuntimeRow
              icon={<span className={`status-dot ${connection}`} />}
              label="Status"
              value={statusLabel}
            />
            {!keyConfigured && (
              <RuntimeRow icon={<KeyRound size={15} />} label="Credentials" value="API key missing" warning />
            )}
            <Settings2 className="runtime-settings-icon" size={14} />
          </button>
          <div className="session-toggle-list" aria-label="Session settings">
            <SessionToggle
              icon={<Save size={15} />}
              label="Memory"
              description={memoryEnabled
                ? "Load saved context across launches"
                : "Start each launch with fresh context"}
              enabled={memoryEnabled}
              pending={sessionSettingSaving === "memory_enabled"}
              disabled={connection !== "ready" || working || sessionSettingSaving !== null}
              onToggle={() => void configureSession("memory_enabled", !memoryEnabled)}
            />
            <SessionToggle
              icon={<ListChecks size={15} />}
              label="Plan mode"
              description={planMode
                ? "Investigate first, then plan changes"
                : "Use normal execution mode"}
              enabled={planMode}
              pending={sessionSettingSaving === "plan_mode"}
              disabled={connection !== "ready" || working || sessionSettingSaving !== null}
              onToggle={() => void configureSession("plan_mode", !planMode)}
            />
          </div>
        </SidebarSection>

        {todos.length > 0 && (
          <SidebarSection title="Tasks">
            <TodoList todos={todos} />
          </SidebarSection>
        )}

        <div className="sidebar-spacer" />

        {error && (
          <div className="error-card">
            <AlertTriangle size={16} />
            <span>{error}</span>
          </div>
        )}

        <div className="scope-note">
          <ShieldCheck size={13} />
          Local-first · project scoped
        </div>
      </aside>

      <main className="main-panel">
        {(
          <>
            <header className="topbar" data-tauri-drag-region>
              <div className="project-heading" data-tauri-drag-region>
                <strong>{projectPath ? projectName : "General session"}</strong>
                <span>{projectPath ?? "No project selected — files land in ~/.tether/scratch. Choose a project to work on a codebase."}</span>
              </div>
              <div className="topbar-actions">
                {working && (
                  <button className="stop-button" onClick={() => void cancelTurn()}>
                    <CircleStop size={15} /> Stop
                  </button>
                )}
                <div className={`connection-pill ${connection}`}>
                  <span />
                  {connection === "ready" ? "Connected" : statusLabel}
                </div>
              </div>
            </header>

            <section
              className="conversation"
              ref={conversationRef}
              onScroll={handleConversationScroll}
            >
              {messages.length === 0 ? (
                <EmptyConversation onSuggestion={setDraft} />
              ) : (
                <div className="message-list">
                  {messages.map((message) => (
                    <MessageView
                      key={message.id}
                      message={message}
                      onOpenPath={openWorkspacePath}
                      toolDescriptions={toolDescriptions}
                      onContinue={continueWorkflow}
                    />
                  ))}
                </div>
              )}
            </section>

            {!followingOutput && working && (
              <button className="jump-latest" onClick={jumpToLatest}>
                <ArrowDown size={13} /> Follow output
              </button>
            )}

            {activity && (
              <div className={`activity-row ${working ? "working" : ""}`}>
                {working && <span className="activity-spinner" />}
                <span>{activity}</span>
                {working && activityElapsed >= 3 && <small className="activity-elapsed">{formatElapsed(activityElapsed)}</small>}
              </div>
            )}

            <Composer
              value={draft}
              setValue={setDraft}
              commands={commands}
              disabled={connection !== "ready"}
              directionPrompt={directionPrompt?.message ?? ""}
              focusRequest={composerFocusRequest}
              send={() => void sendMessage()}
              attachments={attachments}
              onAttachFiles={() => void attachFiles()}
              onPasteText={addPasteAttachment}
              onRemoveAttachment={(id) => setAttachments((current) => current.filter((a) => a.id !== id))}
            />
          </>
        )}
      </main>

      {previewVisible && (
        <>
          <div
            className="workspace-resize-handle"
            role="separator"
            aria-label="Resize code preview"
            aria-orientation="vertical"
            aria-valuemin={PREVIEW_MIN_WIDTH}
            aria-valuemax={maxPreviewWidth()}
            aria-valuenow={Math.round(effectivePreviewWidth)}
            tabIndex={0}
            onPointerDown={startPreviewResize}
            onPointerMove={movePreviewResize}
            onPointerUp={stopPreviewResize}
            onPointerCancel={stopPreviewResize}
            onKeyDown={resizePreviewWithKeyboard}
          />
          <WorkspacePanel
            preview={workspacePreview}
            highlightedLine={previewLine}
            loading={previewLoading}
            error={previewError}
            onOpenPath={(path) => void openWorkspacePath(path)}
            onClose={closeWorkspacePreview}
          />
        </>
      )}

      {approval && (
        <ApprovalDialog
          request={approval}
          pending={approvalDecision !== null}
          onDecision={(decision) => void answerApproval(decision)}
        />
      )}

      {questions && (
        <QuestionDialog request={questions} onAnswer={answerQuestions} />
      )}

      {settingsOpen && (
        <RuntimeDialog
          providers={providers}
          currentProvider={provider}
          currentModel={model}
          currentReasoningEffort={reasoningEffort}
          currentThinkingMode={thinkingMode}
          customBaseUrl={customBaseUrl}
          customKeyEnv={customKeyEnv}
          saving={runtimeSaving}
          onClose={() => !runtimeSaving && setSettingsOpen(false)}
          onSave={(selection) => void saveRuntime(selection)}
          onAddModelDirectory={() => void addModelDirectory()}
          localReady={environment?.localReady ?? true}
          onSetupLocal={() => { setSettingsOpen(false); openSetup("build_llama"); }}
        />
      )}

      {setupOpen && (
        <SetupDialog
          report={environment}
          initialStep={setupInitialStep}
          onRefresh={refreshEnvironment}
          onClose={() => setSetupOpen(false)}
          onReady={() => {
            setSetupOpen(false);
            setError(null);
            void connectProject("");
          }}
        />
      )}

      {onboardingOpen && (
        <OnboardingDialog
          hasProject={Boolean(projectPath)}
          onClose={dismissOnboarding}
          onChooseProject={() => {
            dismissOnboarding();
            void chooseProject();
          }}
        />
      )}

      {engineLog && connection === "failed" && (
        <button className="engine-log" title={engineLog} onClick={() => setEngineLog("")}>
          <TerminalSquare size={14} /> Engine log available <X size={13} />
        </button>
      )}
    </div>
  );
}

interface EnvironmentReport {
  platform: string;
  pythonPath: string | null;
  pythonVersion: string | null;
  pythonOk: boolean;
  pipx: boolean;
  homebrew: boolean;
  git: boolean;
  cmake: boolean;
  xcodeClt: boolean;
  bwrap: boolean;
  tetherPath: string | null;
  tetherVersion: string | null;
  llamaServer: boolean;
  ready: boolean;
  localReady: boolean;
}

type SetupStep = "install_cli" | "build_llama";

/** First-run / repair flow: shows what is installed and runs the install or
 * build scripts in the Rust host, streaming their output here. Nothing needs
 * a terminal; nothing needs sudo. */
function SetupDialog({
  report,
  initialStep,
  onRefresh,
  onClose,
  onReady,
}: {
  report: EnvironmentReport | null;
  initialStep: SetupStep | null;
  onRefresh: () => Promise<EnvironmentReport | null>;
  onClose: () => void;
  onReady: () => void;
}) {
  const [running, setRunning] = useState<SetupStep | null>(null);
  const [lines, setLines] = useState<string[]>([]);
  const [outcome, setOutcome] = useState<{ step: SetupStep; ok: boolean; code: number } | null>(null);
  const setupIdRef = useRef("");
  const logRef = useRef<HTMLPreElement>(null);
  const autoStarted = useRef(false);

  useEffect(() => {
    let unlistenLog: UnlistenFn | undefined;
    let unlistenDone: UnlistenFn | undefined;
    void (async () => {
      unlistenLog = await listen<{ setupId: string; stream: string; line: string }>("setup-log", (event) => {
        if (event.payload.setupId !== setupIdRef.current) return;
        setLines((current) => [...current.slice(-2000), event.payload.line]);
      });
      unlistenDone = await listen<{ setupId: string; step: SetupStep; code: number; ok: boolean }>("setup-done", (event) => {
        if (event.payload.setupId !== setupIdRef.current) return;
        setRunning(null);
        setOutcome({ step: event.payload.step, ok: event.payload.ok, code: event.payload.code });
        void onRefresh().then((fresh) => {
          if (event.payload.ok && event.payload.step === "install_cli" && fresh?.ready) onReady();
        });
      });
    })();
    return () => { unlistenLog?.(); unlistenDone?.(); };
  }, [onRefresh, onReady]);

  useEffect(() => {
    if (logRef.current) logRef.current.scrollTop = logRef.current.scrollHeight;
  }, [lines]);

  const run = useCallback(async (step: SetupStep) => {
    if (running) return;
    const setupId = uid();
    setupIdRef.current = setupId;
    setLines([]);
    setOutcome(null);
    setRunning(step);
    try {
      await invoke("run_setup_step", { step, setupId });
    } catch (caught) {
      setRunning(null);
      setLines([String(caught)]);
      setOutcome({ step, ok: false, code: -1 });
    }
  }, [running]);

  useEffect(() => {
    if (initialStep && !autoStarted.current && report) {
      autoStarted.current = true;
      void run(initialStep);
    }
  }, [initialStep, report, run]);

  const isMac = report?.platform === "macos";
  const isLinux = report?.platform === "linux";
  const rows: Array<{ label: string; ok: boolean; detail: string; required: boolean }> = report ? [
    {
      label: "Python 3.10+",
      ok: report.pythonOk,
      detail: report.pythonVersion ? `${report.pythonVersion} · ${report.pythonPath}` : "not found",
      required: !report.ready,
    },
    { label: "pipx", ok: report.pipx, detail: report.pipx ? "installed" : (report.homebrew ? "will be installed with Homebrew" : "will be installed for your user"), required: !report.ready },
    { label: "Tether CLI", ok: report.ready, detail: report.tetherPath ? `${report.tetherVersion ?? ""} · ${report.tetherPath}` : "not installed", required: true },
    { label: "llama.cpp (local models)", ok: report.llamaServer, detail: report.llamaServer ? "llama-server built" : "not built — only needed for local GGUF models", required: false },
    ...(isMac ? [{ label: "Xcode command line tools", ok: report.xcodeClt, detail: report.xcodeClt ? "installed" : "run xcode-select --install (needed for git and building)", required: false }] : []),
    { label: "git", ok: report.git, detail: report.git ? "installed" : "needed to install the CLI and build llama.cpp", required: false },
    { label: "cmake", ok: report.cmake, detail: report.cmake ? "installed" : (report.homebrew ? "will be installed with Homebrew when building llama.cpp" : "needed to build llama.cpp"), required: false },
    ...(isLinux ? [{ label: "bubblewrap", ok: report.bwrap, detail: report.bwrap ? "installed" : "sudo apt install bubblewrap — required for the shell sandbox", required: true }] : []),
  ] : [];

  return (
    <div className="modal-backdrop">
      <div className="onboarding-dialog setup-dialog" role="dialog" aria-modal="true" aria-labelledby="setup-title">
        <header>
          <div className="onboarding-brand">
            <img src={appIcon} alt="" />
            <div>
              <strong>Set up Tether</strong>
              <span>{report?.ready ? "Everything the app needs is installed." : "The app needs the Tether engine on this Mac. This installs it for you."}</span>
            </div>
          </div>
          {report?.ready && (
            <button type="button" className="icon-button" onClick={onClose} aria-label="Close" disabled={Boolean(running)}>
              <X size={15} />
            </button>
          )}
        </header>
        <div className="setup-body">
          <h2 id="setup-title">What&apos;s installed</h2>
          {!report ? (
            <p className="setup-muted">Checking this machine…</p>
          ) : (
            <ul className="setup-checks">
              {rows.map((row) => (
                <li key={row.label} className={row.ok ? "ok" : row.required ? "missing" : "optional"}>
                  <span className="setup-mark">{row.ok ? <Check size={13} /> : row.required ? <X size={13} /> : <CircleHelp size={13} />}</span>
                  <div>
                    <strong>{row.label}</strong>
                    <small>{row.detail}</small>
                  </div>
                </li>
              ))}
            </ul>
          )}

          <div className="setup-actions">
            {!report?.ready && (
              <button type="button" className="primary-action" onClick={() => void run("install_cli")} disabled={Boolean(running) || !report}>
                {running === "install_cli" ? <span className="activity-spinner" /> : <ArrowDown size={15} />}
                {running === "install_cli" ? "Installing Tether CLI…" : "Install Tether CLI"}
              </button>
            )}
            {report?.ready && !report.llamaServer && (
              <button type="button" className="primary-action" onClick={() => void run("build_llama")} disabled={Boolean(running)}>
                {running === "build_llama" ? <span className="activity-spinner" /> : <Cpu size={15} />}
                {running === "build_llama" ? "Building llama.cpp…" : "Set up local models (build llama.cpp)"}
              </button>
            )}
            {report?.ready && (
              <button type="button" className="secondary-action" onClick={() => void run("install_cli")} disabled={Boolean(running)}>
                <RotateCcw size={14} /> Reinstall / update CLI
              </button>
            )}
            <button type="button" className="secondary-action" onClick={() => void onRefresh()} disabled={Boolean(running)}>
              Re-check
            </button>
          </div>

          {!report?.ready && report && !report.pythonOk && !report.homebrew && (
            <p className="setup-note">
              <AlertTriangle size={13} /> Python 3.10 or newer was not found and Homebrew is not installed. Install Python from python.org (or Homebrew from brew.sh), then Re-check.
            </p>
          )}

          {(lines.length > 0 || running) && (
            <pre className="setup-log" ref={logRef} aria-live="polite">{lines.join("\n") || "Starting…"}</pre>
          )}
          {outcome && (
            <p className={`setup-outcome ${outcome.ok ? "ok" : "failed"}`}>
              {outcome.ok
                ? (outcome.step === "install_cli" ? "Tether CLI installed." : "llama.cpp built. Pick a GGUF model in Runtime.")
                : `The step failed (exit ${outcome.code}). The log above has the details; fix what it names and try again.`}
            </p>
          )}
        </div>
        <footer>
          <small className="setup-muted">
            Installs go to your user account only: pipx under ~/.local, llama.cpp under ~/.tether. No sudo.
          </small>
          {report?.ready && (
            <button type="button" className="primary-action" onClick={onClose} disabled={Boolean(running)}>Continue</button>
          )}
        </footer>
      </div>
    </div>
  );
}

function SidebarSection({ title, children }: { title: string; children: React.ReactNode }) {
  return (
    <section className="sidebar-section">
      <h2>{title}</h2>
      {children}
    </section>
  );
}

function OnboardingDialog({
  hasProject,
  onClose,
  onChooseProject,
}: {
  hasProject: boolean;
  onClose: () => void;
  onChooseProject: () => void;
}) {
  const capabilities = [
    {
      icon: <BrainCircuit size={19} />,
      title: "Understand before acting",
      text: "Explore with file search, cited reads, and optional LSP navigation before changing code.",
    },
    {
      icon: <Wrench size={19} />,
      title: "Run the whole loop",
      text: "Plan, edit, run tests, manage background jobs, inspect results, and iterate to a verified outcome.",
    },
    {
      icon: <Cloud size={19} />,
      title: "Bring any model",
      text: "Use local GGUF models or DeepSeek, Kimi, OpenAI, GLM, Anthropic, Codex, and compatible APIs.",
    },
    {
      icon: <ShieldCheck size={19} />,
      title: "See and control actions",
      text: "Follow live tool traces, answer clarifying questions, and approve restricted work inside one project boundary.",
    },
  ];

  return (
    <div className="modal-backdrop onboarding-backdrop" onMouseDown={(event) => {
      if (event.target === event.currentTarget) onClose();
    }}>
      <section className="onboarding-dialog" role="dialog" aria-modal="true" aria-labelledby="onboarding-title">
        <header>
          <div className="onboarding-brand">
            <img src={appIcon} alt="" />
            <span>WELCOME TO TETHER</span>
          </div>
          <button className="icon-button" onClick={onClose} aria-label="Close guide"><X size={16} /></button>
        </header>

        <div className="onboarding-body">
          <div className="onboarding-hero">
            <p className="onboarding-kicker">A model-independent developer harness</p>
            <h2 id="onboarding-title">Bring a project. Keep the understanding.</h2>
            <p>
              The model is replaceable. Tether is the project-scoped agent layer around it: tools,
              workflows, guardrails, and knowledge that can survive a provider change.
            </p>
          </div>

          <div className="mental-model" aria-label="How Tether works">
            <div><span>1</span><strong>Scope</strong><small>Choose one project</small></div>
            <ArrowRight size={15} />
            <div><span>2</span><strong>Agent loop</strong><small>Inspect · act · verify</small></div>
            <ArrowRight size={15} />
            <div><span>3</span><strong>Compound</strong><small>Reuse checked context</small></div>
          </div>

          <div className="capability-grid">
            {capabilities.map((capability) => (
              <article key={capability.title}>
                <div>{capability.icon}</div>
                <span><strong>{capability.title}</strong><small>{capability.text}</small></span>
              </article>
            ))}
          </div>

          <div className="moat-note">
            <Workflow size={20} />
            <div>
              <strong>What separates Tether</strong>
              <p>
                Most harnesses start with a blank chat and end with a transcript. Tether’s moat is the
                direction toward checkable, cited, model-independent project knowledge—so accumulated
                understanding can improve safety, speed, and completion quality over time.
              </p>
            </div>
          </div>
        </div>

        <footer>
          <button className="secondary-button" onClick={onClose}>Explore the interface</button>
          {hasProject ? (
            <button className="save-button onboarding-primary" onClick={onClose}>
              Start building <ArrowRight size={15} />
            </button>
          ) : (
            <button className="save-button onboarding-primary" onClick={onChooseProject}>
              <FolderOpen size={15} /> Choose a project
            </button>
          )}
        </footer>
      </section>
    </div>
  );
}

function updateRunningActivity(current: ToolActivity[], next: ToolActivity): ToolActivity[] {
  if (next.id) {
    const index = current.findIndex((item) => item.id === next.id);
    if (index >= 0) return current.map((item, i) => i === index ? { ...item, ...next } : item);
    const announced = findLastActivity(current, (item) => item.name === next.name && item.status === "running");
    if (announced >= 0) return current.map((item, i) => i === announced ? { ...item, ...next } : item);
  }
  return [...current, next];
}

function finishActivity(current: ToolActivity[], next: ToolActivity): ToolActivity[] {
  let index = next.id ? current.findIndex((item) => item.id === next.id) : -1;
  if (index < 0) index = findLastActivity(current, (item) => item.name === next.name && item.status === "running");
  if (index < 0) return [...current, { ...next, id: next.id || `finished-${current.length}` }];
  return current.map((item, i) => i === index
    ? {
        ...item,
        ...next,
        id: next.id || item.id,
        argsPreview: next.argsPreview || item.argsPreview,
        displayKind: next.displayKind === "generic" ? item.displayKind : next.displayKind,
      }
    : item);
}

function appendLiveCode(activities: ToolActivity[], name: string, delta: string, path: string): ToolActivity[] {
  const index = findLastActivity(activities, (item) => item.name === name && item.status === "running");
  if (index < 0) return activities;
  return activities.map((item, i) => {
    if (i !== index) return item;
    const meta = item.metadata ?? {};
    const code = (typeof meta.code === "string" ? meta.code : "") + delta;
    return { ...item, metadata: { ...meta, code, path: (typeof meta.path === "string" && meta.path) || path, live: true } };
  });
}

function appendLiveCodeInSegments(segments: TurnSegment[] | undefined, name: string, delta: string, path: string): TurnSegment[] | undefined {
  if (!segments) return segments;
  for (let index = segments.length - 1; index >= 0; index -= 1) {
    const segment = segments[index];
    if (segment.kind !== "tools") continue;
    if (!segment.activities.some((item) => item.name === name && item.status === "running")) continue;
    return segments.map((item, i) => (
      i === index && item.kind === "tools"
        ? { kind: "tools", activities: appendLiveCode(item.activities, name, delta, path) }
        : item
    ));
  }
  return segments;
}

function appendTextSegment(segments: TurnSegment[] | undefined, delta: string): TurnSegment[] {
  const current = segments ?? [];
  const last = current[current.length - 1];
  if (last && last.kind === "text") {
    return [...current.slice(0, -1), { kind: "text", text: last.text + delta }];
  }
  return [...current, { kind: "text", text: delta }];
}

function startToolInSegments(segments: TurnSegment[] | undefined, activity: ToolActivity): TurnSegment[] {
  const current = segments ?? [];
  const last = current[current.length - 1];
  if (last && last.kind === "tools") {
    return [...current.slice(0, -1), { kind: "tools", activities: updateRunningActivity(last.activities, activity) }];
  }
  // The engine announces a call by name first and later re-sends it with an
  // id; if that announcement landed in an earlier segment, update it there
  // (mirrors updateRunningActivity's whole-list search) instead of opening a
  // second card that leaves the first spinning forever.
  if (activity.id) {
    for (let index = current.length - 1; index >= 0; index -= 1) {
      const segment = current[index];
      if (segment.kind !== "tools") continue;
      const announced = segment.activities.some((item) => (
        item.id === activity.id || (item.name === activity.name && item.status === "running")
      ));
      if (!announced) continue;
      return current.map((item, i) => (
        i === index && item.kind === "tools"
          ? { kind: "tools", activities: updateRunningActivity(item.activities, activity) }
          : item
      ));
    }
  }
  return [...current, { kind: "tools", activities: [activity] }];
}

function finishToolInSegments(segments: TurnSegment[] | undefined, activity: ToolActivity): TurnSegment[] {
  const current = segments ?? [];
  // Locate the segment holding this call (by id, else the last running call
  // of the same name), searching from the end so a re-used tool name in an
  // earlier batch is not updated by mistake.
  for (let index = current.length - 1; index >= 0; index -= 1) {
    const segment = current[index];
    if (segment.kind !== "tools") continue;
    const owns = activity.id
      ? segment.activities.some((item) => item.id === activity.id)
      : segment.activities.some((item) => item.name === activity.name && item.status === "running");
    if (!owns) continue;
    return current.map((item, i) => (
      i === index && item.kind === "tools"
        ? { kind: "tools", activities: finishActivity(item.activities, activity) }
        : item
    ));
  }
  // Never announced (non-streaming backend): attach to the trailing tools
  // segment, or open a new one after whatever text came before.
  const last = current[current.length - 1];
  if (last && last.kind === "tools") {
    return [...current.slice(0, -1), { kind: "tools", activities: finishActivity(last.activities, activity) }];
  }
  return [...current, { kind: "tools", activities: finishActivity([], activity) }];
}

function finalizeSegments(segments: TurnSegment[] | undefined, output: string): TurnSegment[] | undefined {
  const current = segments ?? [];
  if (current.length === 0) return undefined; // nothing streamed: render `text` directly
  const last = current[current.length - 1];
  if (last.kind === "text") {
    // The engine's `output` is the authoritative final response (it may be
    // trimmed/coerced relative to the raw stream); swap it in place.
    return [...current.slice(0, -1), { kind: "text", text: output }];
  }
  // Tools were the last thing streamed (e.g. a backend that does not stream
  // text): the final answer belongs *after* them.
  return [...current, { kind: "text", text: output }];
}

function findLastActivity(
  activities: ToolActivity[],
  predicate: (activity: ToolActivity) => boolean,
): number {
  for (let index = activities.length - 1; index >= 0; index -= 1) {
    if (predicate(activities[index])) return index;
  }
  return -1;
}

function TodoList({ todos }: { todos: TodoItem[] }) {
  return (
    <div className="todo-list">
      {todos.map((todo, index) => (
        <div className={`todo-item ${todo.status}`} key={`${todo.content}-${index}`}>
          <span>{todo.status === "completed" ? "✓" : todo.status === "in_progress" ? "●" : "○"}</span>
          <p>{todo.status === "in_progress" && todo.active_form ? todo.active_form : todo.content}</p>
        </div>
      ))}
    </div>
  );
}

function RuntimeRow({
  icon,
  label,
  value,
  warning = false,
}: {
  icon: React.ReactNode;
  label: string;
  value: string;
  warning?: boolean;
}) {
  return (
    <div className={`runtime-row ${warning ? "warning" : ""}`}>
      <div className="runtime-icon">{icon}</div>
      <div>
        <small>{label}</small>
        <span>{value}</span>
      </div>
    </div>
  );
}

function SessionToggle({
  icon,
  label,
  description,
  enabled,
  pending,
  disabled,
  onToggle,
}: {
  icon: React.ReactNode;
  label: string;
  description: string;
  enabled: boolean;
  pending: boolean;
  disabled: boolean;
  onToggle: () => void;
}) {
  return (
    <button
      type="button"
      className={`session-toggle ${enabled ? "enabled" : ""}`}
      role="switch"
      aria-checked={enabled}
      aria-busy={pending}
      disabled={disabled}
      onClick={onToggle}
    >
      <span className="session-toggle-icon">{icon}</span>
      <span className="session-toggle-copy">
        <strong>{label}</strong>
        <small>{description}</small>
      </span>
      <span className="session-toggle-control" aria-hidden="true">
        <small>{pending ? "Saving…" : enabled ? "On" : "Off"}</small>
        <span className="session-toggle-track"><span /></span>
      </span>
    </button>
  );
}

function RuntimeDialog({
  providers,
  currentProvider,
  currentModel,
  currentReasoningEffort,
  currentThinkingMode,
  customBaseUrl,
  customKeyEnv,
  saving,
  onClose,
  onSave,
  onAddModelDirectory,
  localReady,
  onSetupLocal,
}: {
  providers: ProviderOption[];
  currentProvider: string;
  currentModel: string;
  currentReasoningEffort: string;
  currentThinkingMode: string;
  customBaseUrl: string;
  customKeyEnv: string;
  saving: boolean;
  onClose: () => void;
  onSave: (selection: RuntimeSelection) => void;
  onAddModelDirectory: () => void;
  localReady: boolean;
  onSetupLocal: () => void;
}) {
  const initialProvider = providers.some((item) => item.id === currentProvider)
    ? currentProvider
    : (providers[0]?.id ?? "local");
  const initialProviderOption = providers.find((item) => item.id === initialProvider);
  const initialModel = initialProvider === "custom"
    ? currentModel
    : initialProviderOption?.models.some((item) => item.id === currentModel)
      ? currentModel
      : (initialProviderOption?.models[0]?.id ?? "");
  const initialModelOption = initialProviderOption?.models.find((item) => item.id === initialModel);
  const initialEffort = currentReasoningEffort
    || (currentThinkingMode === "disabled" && initialModelOption?.reasoning_efforts?.includes("off") ? "off" : "")
    || initialModelOption?.default_reasoning_effort
    || initialModelOption?.reasoning_efforts?.[0]
    || "";
  const [selectedProvider, setSelectedProvider] = useState(initialProvider);
  const [selectedModel, setSelectedModel] = useState(initialModel);
  const [effort, setEffort] = useState(initialEffort);
  const [thinking, setThinking] = useState(currentThinkingMode);
  const [apiKey, setApiKey] = useState("");
  const [showKey, setShowKey] = useState(false);
  const [baseUrl, setBaseUrl] = useState(customBaseUrl);
  const [keyEnv, setKeyEnv] = useState(customKeyEnv);

  const provider = providers.find((item) => item.id === selectedProvider);
  const model = provider?.models.find((item) => item.id === selectedModel);
  useEffect(() => {
    if (selectedProvider === "custom" || model || !provider?.models[0]) return;
    const first = provider.models[0];
    setSelectedModel(first.id);
    setEffort(first.default_reasoning_effort ?? first.reasoning_efforts?.[0] ?? "");
    setThinking(first.default_thinking_mode ?? first.thinking_mode ?? "");
  }, [model, provider, selectedProvider]);
  const requiresModel = selectedProvider !== "custom";
  const needsKey = provider?.requires_api_key === true
    && provider.api_key_configured !== true
    && !apiKey.trim();
  const keyFailure = apiKeyFailure(apiKey);
  const invalid = !provider
    || (requiresModel && !selectedModel)
    || (selectedProvider === "custom" && (!selectedModel.trim() || !baseUrl.trim()))
    || needsKey
    || keyFailure !== null;

  const chooseProvider = (id: string) => {
    const next = providers.find((item) => item.id === id);
    const first = next?.models[0];
    setSelectedProvider(id);
    setSelectedModel(first?.id ?? "");
    setEffort(first?.default_reasoning_effort ?? first?.reasoning_efforts?.[0] ?? "");
    setThinking(first?.default_thinking_mode ?? first?.thinking_mode ?? "");
    setApiKey("");
  };

  const chooseModel = (id: string) => {
    const next = provider?.models.find((item) => item.id === id);
    setSelectedModel(id);
    setEffort(next?.default_reasoning_effort ?? next?.reasoning_efforts?.[0] ?? "");
    setThinking(next?.default_thinking_mode ?? next?.thinking_mode ?? "");
  };

  return (
    <div className="modal-backdrop">
      <form
        className="runtime-dialog"
        role="dialog"
        aria-modal="true"
        aria-labelledby="runtime-title"
        onSubmit={(event) => {
          event.preventDefault();
          if (invalid || saving) return;
          onSave({
            provider: selectedProvider,
            model: selectedModel,
            reasoningEffort: effort,
            thinkingMode: thinking,
            apiKey,
            apiBaseUrl: baseUrl,
            apiKeyEnv: keyEnv,
          });
        }}
      >
        <header>
          <div>
            <h2 id="runtime-title">Model runtime</h2>
            <p>Choose where Tether runs. Changes are saved for the CLI and desktop app.</p>
          </div>
          <button type="button" className="icon-button" onClick={onClose} disabled={saving} aria-label="Close">
            <X size={16} />
          </button>
        </header>

        <div className="runtime-form-grid">
          <label className="form-field">
            <span>Provider</span>
            <select value={selectedProvider} onChange={(event) => chooseProvider(event.target.value)} disabled={saving}>
              {providers.map((item) => <option key={item.id} value={item.id}>{item.label}</option>)}
            </select>
            <small>{provider?.description}</small>
          </label>

          {selectedProvider === "local" && !localReady ? (
            <div className="local-empty">
              <Cpu size={22} />
              <div>
                <strong>Local models need llama.cpp</strong>
                <span>Tether builds it for you (a few minutes, once). Then add a GGUF folder.</span>
              </div>
              <button type="button" onClick={onSetupLocal} disabled={saving}>
                <Wrench size={14} /> Build llama.cpp
              </button>
            </div>
          ) : selectedProvider === "local" && provider?.models.length === 0 ? (
            <div className="local-empty">
              <Cpu size={22} />
              <div>
                <strong>No local GGUF models found</strong>
                <span>Add a folder that contains .gguf files.</span>
              </div>
              <button type="button" onClick={onAddModelDirectory} disabled={saving}>
                <Plus size={14} /> Add model folder
              </button>
            </div>
          ) : selectedProvider === "custom" ? (
            <>
              <label className="form-field full-width">
                <span>API base URL</span>
                <input
                  value={baseUrl}
                  onChange={(event) => setBaseUrl(event.target.value)}
                  placeholder="https://api.example.com/v1"
                  disabled={saving}
                />
              </label>
              <label className="form-field">
                <span>Model name</span>
                <input value={selectedModel} onChange={(event) => setSelectedModel(event.target.value)} placeholder="model-id" disabled={saving} />
              </label>
              <label className="form-field">
                <span>API key environment variable</span>
                <input value={keyEnv} onChange={(event) => setKeyEnv(event.target.value)} placeholder="MY_PROVIDER_API_KEY" disabled={saving} />
              </label>
            </>
          ) : (
            <label className="form-field full-width">
              <span>Model</span>
              <select value={selectedModel} onChange={(event) => chooseModel(event.target.value)} disabled={saving}>
                {provider?.models.map((item) => <option key={item.id} value={item.id}>{item.label}</option>)}
              </select>
              {model?.description && <small>{model.description}</small>}
            </label>
          )}

          {(model?.reasoning_efforts?.length ?? 0) > 0 && (
            <label className="form-field">
              <span>Reasoning effort</span>
              <select value={effort} onChange={(event) => setEffort(event.target.value)} disabled={saving}>
                {model!.reasoning_efforts!.map((item) => (
                  <option key={item} value={item}>{reasoningEffortLabel(item)}</option>
                ))}
              </select>
            </label>
          )}

          {(model?.thinking_modes?.length ?? 0) > 0 && (
            <label className="form-field">
              <span>Thinking</span>
              <select value={thinking} onChange={(event) => setThinking(event.target.value)} disabled={saving}>
                {model!.thinking_modes!.map((item) => <option key={item} value={item}>{item}</option>)}
              </select>
            </label>
          )}

          {provider?.requires_api_key && (
            <label className="form-field full-width">
              <span>API key</span>
              <div className="secret-input">
                <input
                  type={showKey ? "text" : "password"}
                  value={apiKey}
                  onChange={(event) => setApiKey(event.target.value)}
                  placeholder={provider.api_key_configured ? "Key configured — leave blank to keep it" : "Paste API key"}
                  autoComplete="off"
                  disabled={saving}
                />
                <button type="button" onClick={() => setShowKey((value) => !value)} aria-label={showKey ? "Hide key" : "Show key"}>
                  {showKey ? <EyeOff size={15} /> : <Eye size={15} />}
                </button>
              </div>
              <small>
                {keyFailure
                  ? <span className="field-error">{keyFailure}</span>
                  : provider.api_key_configured
                  ? "A key is already configured. New values replace only this provider's stored key."
                  : provider.api_key_env
                    ? `Stored in ~/.tether/config.json with owner-only permissions, or set ${provider.api_key_env}.`
                    : "Stored in ~/.tether/config.json with owner-only permissions."}
              </small>
            </label>
          )}
        </div>

        <footer>
          {selectedProvider === "local" && (provider?.models.length ?? 0) > 0 && (
            <button type="button" className="secondary-button" onClick={onAddModelDirectory} disabled={saving}>
              <Plus size={14} /> Add folder
            </button>
          )}
          <span />
          <button type="button" className="secondary-button" onClick={onClose} disabled={saving}>Cancel</button>
          <button type="submit" className="save-button" disabled={invalid || saving}>
            <Save size={15} /> {saving ? "Applying…" : "Use runtime"}
          </button>
        </footer>
      </form>
    </div>
  );
}


function EmptyConversation({ onSuggestion }: { onSuggestion: (value: string) => void }) {
  const suggestions = [
    ["Understand", "Map the architecture and explain the main execution flow."],
    ["Investigate", "Find the highest-risk unfinished behavior in this repository."],
    ["Build", "Review the current worktree and help me finish the active change."],
  ];
  return (
    <div className="empty-state">
      <div className="empty-icon"><Sparkles size={28} /></div>
      <h1>What should we build?</h1>
      <p>Ask about the codebase, investigate a bug, or describe a concrete change.</p>
      <div className="suggestions">
        {suggestions.map(([label, prompt]) => (
          <button key={label} onClick={() => onSuggestion(prompt)}>
            <FileCode2 size={16} />
            <span><strong>{label}</strong><small>{prompt}</small></span>
            <ChevronRight size={15} />
          </button>
        ))}
      </div>
    </div>
  );
}

const MarkdownView = memo(function MarkdownView({
  text,
  onOpenPath,
}: {
  text: string;
  onOpenPath: (reference: string) => void;
}) {
  const components = useMemo<Components>(() => ({
    p: ({ children }) => <p>{pathAwareChildren(children, onOpenPath)}</p>,
    li: ({ children }) => <li>{pathAwareChildren(children, onOpenPath)}</li>,
    code: ({ children, className }) => {
      const value = String(children);
      if (!className && !value.includes("\n") && parsePathReference(value)) {
        return (
          <button type="button" className="path-link code-path-link" onClick={() => onOpenPath(value)}>
            {value}
          </button>
        );
      }
      return <code className={className}>{children}</code>;
    },
    a: ({ href, children }) => {
      const decoded = (() => {
        try { return decodeURIComponent(href ?? ""); } catch { return href ?? ""; }
      })();
      if (href && parsePathReference(decoded, true)) {
        return (
          <button type="button" className="path-link" onClick={() => onOpenPath(decoded)}>
            {children}
          </button>
        );
      }
      return <a href={href} target="_blank" rel="noreferrer">{children}</a>;
    },
  }), [onOpenPath]);

  return <ReactMarkdown remarkPlugins={[remarkGfm]} components={components}>{text}</ReactMarkdown>;
});

interface AggregatedToolActivity extends Omit<ToolActivity, "narration"> {
  count: number;
  /** What the model said right before each of these calls, in order. Shown
   * only when the card is expanded. */
  narration: string[];
  /** Every individual call folded into this card, in order — the expanded
   * view renders each (diffs for edits, commands for bash, …). */
  calls: ToolActivity[];
}

function aggregateToolActivities(activities: ToolActivity[]): AggregatedToolActivity[] {
  const aggregated = new Map<string, AggregatedToolActivity>();
  for (const activity of activities) {
    const current = aggregated.get(activity.name);
    const note = activity.narration?.trim();
    const { narration: _ignored, ...plain } = activity;
    if (!current) {
      aggregated.set(activity.name, { ...plain, count: 1, narration: note ? [note] : [], calls: [plain] });
      continue;
    }
    const narration = note && !current.narration.includes(note) ? [...current.narration, note] : current.narration;
    const status = current.status === "failed" || activity.status === "failed"
      ? "failed"
      : current.status === "running" || activity.status === "running"
        ? "running"
        : "completed";
    aggregated.set(activity.name, {
      ...current,
      ...plain,
      status,
      narration,
      calls: [...current.calls, plain],
      count: current.count + 1,
      argsPreview: activity.argsPreview || current.argsPreview,
      outputPreview: activity.outputPreview || current.outputPreview,
      errorCode: activity.errorCode || current.errorCode,
    });
  }
  return [...aggregated.values()];
}

function countToolNames(toolNames: string[]): Array<{ name: string; count: number }> {
  const counts = new Map<string, number>();
  for (const name of toolNames) counts.set(name, (counts.get(name) ?? 0) + 1);
  return [...counts].map(([name, count]) => ({ name, count }));
}

const DEFAULT_TOOL_DESCRIPTIONS: Record<string, string> = {
  agent: "Delegates a focused subtask to another project-scoped agent.",
  ask_user: "Pauses so Tether can ask you for a decision or missing detail.",
  bash: "Runs a shell command inside the selected project's safety boundary.",
  file_edit: "Applies a targeted change to an existing workspace file.",
  file_read: "Reads a workspace file so the model can inspect its contents.",
  file_write: "Creates or replaces a file inside the selected workspace.",
  glob: "Finds files whose paths match a pattern.",
  grep: "Searches workspace file contents for text or code patterns.",
  job_kill: "Stops a background command started by Tether.",
  job_list: "Lists background commands owned by this session.",
  job_output: "Reads the latest output from a background command.",
  lsp: "Uses a language server for definitions, references, hover, or symbols.",
  skill_manage: "Creates or updates a reusable Tether workflow skill.",
  skill_view: "Loads the instructions for a selected workflow skill.",
  skills_list: "Lists workflow skills available to this session.",
  todo_write: "Updates the visible task plan and completion state.",
  web_fetch: "Reads a web page requested for the current task.",
};

function conciseToolDescription(name: string, descriptions: Record<string, string>): string {
  const description = descriptions[name] || DEFAULT_TOOL_DESCRIPTIONS[name]
    || `Uses Tether's ${name.replaceAll("_", " ")} capability for this task.`;
  return description.length > 170 ? `${description.slice(0, 167).trimEnd()}…` : description;
}

function formatElapsed(seconds: number): string {
  if (seconds < 60) return `${seconds}s`;
  const minutes = Math.floor(seconds / 60);
  return `${minutes}m ${String(seconds % 60).padStart(2, "0")}s`;
}

function queueStateLabel(message: ChatMessage): string {
  if (message.queueState === "submitting") return "sending";
  if (message.queueState === "queued") {
    return message.queuePosition ? `queued · position ${message.queuePosition}` : "queued";
  }
  if (message.queueState === "active") return "running";
  if (message.queueState === "cleared") return "not run";
  return "";
}

const MessageView = memo(function MessageView({
  message,
  onOpenPath,
  toolDescriptions,
  onContinue,
}: {
  message: ChatMessage;
  onOpenPath: (reference: string) => void;
  toolDescriptions: Record<string, string>;
  onContinue: () => void;
}) {
  const agents = [...(message.agents ?? [])].sort((left, right) => (
    left.agentNumber - right.agentNumber || left.agentId.localeCompare(right.agentId)
  ));
  const hasDedicatedAgents = agents.length > 0;
  const keepActivity = (activity: ToolActivity) => !hasDedicatedAgents || activity.name !== "agent";
  const toolActivities = aggregateToolActivities((message.toolActivities ?? []).filter(keepActivity));
  const fallbackTools = toolActivities.length === 0
    ? countToolNames((message.toolCalls ?? []).filter((name) => !hasDedicatedAgents || name !== "agent"))
    : [];
  const queueLabel = queueStateLabel(message);
  const segments = message.role === "assistant" && message.segments && message.segments.length > 0
    ? message.segments
    : null;
  const renderToolCards = (activities: ToolActivity[], keyPrefix: string) => {
    const aggregated = aggregateToolActivities(activities.filter(keepActivity));
    return aggregated.map((activity) => (
      <ToolActivityView
        key={`${keyPrefix}-${activity.name}`}
        activity={{ ...activity, narration: undefined }}
        count={activity.count}
        narration={activity.narration}
        calls={activity.calls}
        description={conciseToolDescription(activity.name, toolDescriptions)}
      />
    ));
  };
  const renderToolBlock = (activities: ToolActivity[], key: string) => {
    const cards = renderToolCards(activities, key);
    if (cards.length === 0) return null;
    return <div className="tool-activities" key={key}>{cards}</div>;
  };
  // Shape of a turn: opening prose → ONE stacked block of tool cards (with the
  // model's in-between narration demoted to small captions inside the stack) →
  // the final answer, full-size, at the bottom. While still streaming, the
  // trailing text may yet turn out to be narration; it is promoted/demoted as
  // soon as the next tool card arrives.
  const renderSegments = (list: TurnSegment[]) => {
    const firstTools = list.findIndex((segment) => segment.kind === "tools");
    if (firstTools < 0) {
      return list.map((segment, index) => (
        segment.kind === "text" && segment.text
          ? <MarkdownView key={`text-${index}`} text={segment.text} onOpenPath={onOpenPath} />
          : null
      ));
    }
    let lastTools = -1;
    list.forEach((segment, index) => { if (segment.kind === "tools") lastTools = index; });
    // Prose written BEFORE any tool call is the opening statement. If tools ran
    // first (turns often begin with an automatic model_query/bash), the model's
    // first prose still counts as the opening only when it is a short intent
    // line ("I'll review the core files — reading X, Y, Z."); a long first
    // text after tools is reasoning/narration and belongs inside the tool
    // cards, not in the reader's face.
    // An opening statement is short by nature ("I'll review X, Y, Z."). A long
    // first text before the tools is a draft answer or reasoning — that is
    // narration too, and it lives inside the cards.
    const firstText = list.findIndex((segment) => segment.kind === "text" && segment.text.trim());
    const firstTextIsShort = firstText >= 0
      && list[firstText].kind === "text"
      && (list[firstText] as { kind: "text"; text: string }).text.trim().length <= OPENING_STATEMENT_MAX_CHARS;
    const openingEnd = firstText >= 0 && firstText < lastTools && firstTextIsShort
      ? firstText + 1
      : firstTools;
    // Everything up to openingEnd that is NOT the short opening line (i.e. a
    // long draft written before the first tool call) is folded into the
    // middle so it becomes card narration rather than page prose.
    const openingRaw = list.slice(0, openingEnd);
    const opening = openingRaw.filter((segment) => segment.kind !== "text" || segment.text.trim().length <= OPENING_STATEMENT_MAX_CHARS);
    const demoted = openingRaw.filter((segment) => segment.kind === "text" && segment.text.trim().length > OPENING_STATEMENT_MAX_CHARS);
    const middle = [...demoted, ...list.slice(openingEnd, lastTools + 1)];
    const closing = list.slice(lastTools + 1);
    // Every tool call after the opening is folded into ONE aggregated block
    // (bash ×5, file read ×3 …) that keeps growing while the turn runs. The
    // model's between-tool narration is not rendered here; the activity row
    // already says what is running.
    let pendingNarration = "";
    const middleActivities: ToolActivity[] = [];
    for (const segment of middle) {
      if (segment.kind === "text") {
        pendingNarration = [pendingNarration, segment.text.trim()].filter(Boolean).join("\n");
        continue;
      }
      segment.activities.forEach((activity, index) => {
        // The narration belongs to the first call of the batch it introduced.
        middleActivities.push(index === 0 && pendingNarration ? { ...activity, narration: pendingNarration } : activity);
      });
      pendingNarration = "";
    }
    const stack = renderToolCards(middleActivities, "tool-stack");
    return [
      ...opening.map((segment, index) => (
        segment.kind === "text"
          ? (segment.text ? <MarkdownView key={`text-${index}`} text={segment.text} onOpenPath={onOpenPath} /> : null)
          : renderToolBlock(segment.activities, `tools-${index}`)
      )),
      stack.length > 0 ? <div className="tool-activities" key="tool-stack">{stack}</div> : null,
      ...closing.map((segment, offset) => {
        if (segment.kind !== "text" || !segment.text) return null;
        // While the turn is still running, text after the tool block may be
        // the answer or just "let me check X…" before the next call. Narration
        // is short; an answer keeps growing. So: stream it as a quiet live
        // line only while it is short, and as full markdown — identical to the
        // finished view — as soon as it is longer than an opening line. If a
        // tool call follows after all, the text moves into that tool's card.
        if (message.streaming && segment.text.trim().length <= OPENING_STATEMENT_MAX_CHARS) {
          return <p className="tool-live-text" key={`live-${lastTools + 1 + offset}`}>{segment.text.trim()}</p>;
        }
        return <MarkdownView key={`text-${lastTools + 1 + offset}`} text={segment.text} onOpenPath={onOpenPath} />;
      }),
    ];
  };
  return (
    <article className={`message ${message.role}`}>
      {message.role === "assistant" && <div className="assistant-avatar"><Bot size={17} /></div>}
      <div className="message-content">
        {segments ? (
          renderSegments(segments)
        ) : message.role === "assistant" ? (
          <MarkdownView text={message.text} onOpenPath={onOpenPath} />
        ) : (
          <p>{message.text}</p>
        )}
        {message.role === "user" && message.attachments && message.attachments.length > 0 && (
          <AttachmentChips attachments={message.attachments} />
        )}
        {message.role === "user" && queueLabel && (
          <div className={`queue-state ${message.queueState}`} title={message.queueReason || undefined}>
            <span />
            <small>{queueLabel}</small>
            {message.queueState === "cleared" && message.queueReason && <em>{message.queueReason}</em>}
          </div>
        )}
        {!segments && toolActivities.length > 0 && renderToolBlock(message.toolActivities ?? [], "tools")}
        {agents.length > 0 && (
          <div className="agent-activities" aria-label="Sub-agent activity">
            {agents.map((agent) => (
              <AgentActivityView key={agent.agentId} agent={agent} onOpenPath={onOpenPath} />
            ))}
          </div>
        )}
        {(fallbackTools.length > 0 || (message.tokenCount ?? 0) > 0) && (
          <footer className="message-meta">
            {fallbackTools.map((tool) => (
              <span key={tool.name}>
                {tool.name.replaceAll("_", " ")}
                {tool.count > 1 && <b>×{tool.count}</b>}
              </span>
            ))}
            {(message.tokenCount ?? 0) > 0 && <small>{message.tokenCount!.toLocaleString()} tokens</small>}
          </footer>
        )}
        {message.stopReason === "max_turns_reached" && (
          <button type="button" className="continue-workflow" onClick={onContinue}>
            <RotateCcw size={13} /> Continue workflow
          </button>
        )}
      </div>
    </article>
  );
});

function parentWorkspacePath(path: string): string | null {
  if (!path || path === ".") return null;
  const segments = path.replace(/\/$/, "").split("/");
  segments.pop();
  return segments.join("/") || ".";
}

function formatFileSize(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
  return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
}

function WorkspacePanel({
  preview,
  highlightedLine,
  loading,
  error,
  onOpenPath,
  onClose,
}: {
  preview: WorkspacePreview | null;
  highlightedLine: number | null;
  loading: boolean;
  error: string;
  onOpenPath: (path: string) => void;
  onClose: () => void;
}) {
  const highlightedLineRef = useRef<HTMLLIElement>(null);
  const parentPath = preview?.kind === "directory" ? parentWorkspacePath(preview.path) : null;
  const sourceLines = useMemo(
    () => preview?.kind === "file" ? preview.content.split(/\r?\n/) : [],
    [preview],
  );
  const sourceLanguage = useMemo(
    () => preview?.kind === "file" ? languageForPath(preview.path) : null,
    [preview?.kind, preview?.path],
  );
  const highlightedLines = useMemo(
    () => sourceLanguage ? sourceLines.map((line) => highlightSourceLine(line, sourceLanguage)) : [],
    [sourceLanguage, sourceLines],
  );

  useEffect(() => {
    highlightedLineRef.current?.scrollIntoView({ block: "center" });
  }, [preview?.path, highlightedLine]);

  return (
    <aside className="workspace-panel" aria-label="Workspace code preview">
      <header>
        <div>
          <span>{preview?.kind === "directory" ? "WORKSPACE" : "CODE PREVIEW"}</span>
          <strong title={preview?.path}>{preview?.path || "Opening…"}</strong>
        </div>
        <button className="icon-button" onClick={onClose} aria-label="Close code preview"><X size={15} /></button>
      </header>

      {loading && !preview && (
        <div className="preview-status"><span className="activity-spinner" /> Opening workspace path…</div>
      )}

      {error && (
        <div className="preview-error">
          <AlertTriangle size={18} />
          <strong>Could not open that path</strong>
          <span>{error}</span>
        </div>
      )}

      {preview?.kind === "directory" && (
        <div className="directory-browser">
          {parentPath && (
            <button className="directory-entry parent-entry" onClick={() => onOpenPath(`${parentPath}/`)}>
              <ArrowLeft size={15} /><span><strong>..</strong><small>Parent directory</small></span>
            </button>
          )}
          {preview.entries.map((entry) => (
            <button
              className="directory-entry"
              key={entry.path}
              onClick={() => onOpenPath(entry.isDirectory ? `${entry.path}/` : entry.path)}
            >
              {entry.isDirectory ? <Folder size={15} /> : <FileCode2 size={15} />}
              <span>
                <strong>{entry.name}</strong>
                <small>{entry.isDirectory ? "Directory" : formatFileSize(entry.sizeBytes)}</small>
              </span>
              <ChevronRight size={14} />
            </button>
          ))}
          {preview.entries.length === 0 && <div className="preview-status">This directory is empty.</div>}
          {preview.truncated && <div className="preview-limit">Showing the first 500 entries.</div>}
        </div>
      )}

      {preview?.kind === "file" && (
        <>
          <div className="code-preview-meta">
            <span>{sourceLanguage ?? "plain text"}</span>
            <span>{formatFileSize(preview.sizeBytes)}</span>
            {highlightedLine && <span>Line {highlightedLine}</span>}
            {preview.truncated && <span>Preview capped at 512 KB</span>}
          </div>
          <ol className="code-preview-lines">
            {sourceLines.map((line, index) => {
              const lineNumber = index + 1;
              const highlighted = lineNumber === highlightedLine;
              return (
                <li
                  key={lineNumber}
                  className={highlighted ? "highlighted" : ""}
                  ref={highlighted ? highlightedLineRef : undefined}
                >
                  {sourceLanguage ? (
                    <code
                      className={`hljs language-${sourceLanguage}`}
                      dangerouslySetInnerHTML={{ __html: highlightedLines[index] ?? " " }}
                    />
                  ) : (
                    <code>{line || " "}</code>
                  )}
                </li>
              );
            })}
          </ol>
        </>
      )}

      {loading && preview && <div className="preview-loading-overlay"><span className="activity-spinner" /></div>}
    </aside>
  );
}

const DIFF_LINE_CLASS = (line: string): string => {
  if (line.startsWith("+++") || line.startsWith("---")) return "diff-file";
  if (line.startsWith("@@")) return "diff-hunk";
  if (line.startsWith("+")) return "diff-add";
  if (line.startsWith("-")) return "diff-del";
  if (line.startsWith("...")) return "diff-meta";
  return "diff-ctx";
};

/** A unified diff (or plain code) rendered line-by-line so additions and
 * deletions are coloured. Wide content scrolls inside the block. */
function DiffView({ text, kind = "diff", follow = false }: { text: string; kind?: "diff" | "code"; follow?: boolean }) {
  const lines = text.replace(/\n$/, "").split("\n");
  const ref = useRef<HTMLPreElement>(null);
  useEffect(() => {
    // Keep the newest line in view while code is being typed.
    if (follow && ref.current) ref.current.scrollTop = ref.current.scrollHeight;
  }, [text, follow]);
  return (
    <pre className={`code-change ${kind}`} ref={ref}>
      {lines.map((line, index) => (
        <span key={index} className={kind === "diff" ? DIFF_LINE_CLASS(line) : "diff-ctx"}>{line || " "}{"\n"}</span>
      ))}
    </pre>
  );
}

function shortPath(path: string): string {
  const parts = path.split("/").filter(Boolean);
  return parts.length > 3 ? `…/${parts.slice(-3).join("/")}` : path;
}

/** One call inside an aggregated card: edits and writes get a real diff (or
 * the new file's content, or the code being written while the call runs),
 * bash gets its command and output, everything else args + output. */
function ToolCallDetail({ call, showHeader }: { call: ToolActivity; showHeader: boolean }) {
  const meta = call.metadata ?? {};
  const path = typeof meta.path === "string" ? meta.path : "";
  const diff = typeof meta.diff === "string" ? meta.diff : "";
  const content = typeof meta.content === "string" ? meta.content : "";
  const pendingCode = typeof meta.code === "string" ? meta.code : "";
  const command = typeof meta.command === "string" ? meta.command : "";
  const isWrite = call.name === "file_edit" || call.name === "file_write";
  const opLabel = typeof meta.operation === "string" ? meta.operation : "";
  const summary = typeof meta.summary === "string" ? meta.summary : "";
  return (
    <div className={`tool-call ${call.status}`}>
      {showHeader && (
        <div className="tool-call-head">
          <span className="tool-status" />
          {isWrite && path ? <code title={path}>{shortPath(path)}</code> : <code>{call.argsPreview || call.name}</code>}
          <small>{opLabel}{summary ? ` · ${summary}` : ""}{call.status === "running" ? " · writing…" : ""}</small>
        </div>
      )}
      {isWrite && diff && <DiffView text={diff} kind="diff" />}
      {isWrite && !diff && content && <DiffView text={content} kind="code" />}
      {isWrite && !diff && !content && pendingCode && <DiffView text={pendingCode} kind="code" follow={call.status === "running"} />}
      {!isWrite && command && <DiffView text={`$ ${command}`} kind="code" />}
      {!isWrite && !command && call.argsPreview && <code className="tool-args">{call.argsPreview}</code>}
      {!isWrite && call.outputPreview && <pre className="tool-output">{call.outputPreview}</pre>}
      {call.errorCode && <small className="tool-error">{call.errorCode}</small>}
    </div>
  );
}

function ToolActivityView({
  activity,
  count,
  description,
  narration = [],
  calls,
}: {
  activity: ToolActivity;
  count: number;
  description?: string;
  narration?: string[];
  calls?: ToolActivity[];
}) {
  const label = activity.name.replaceAll("_", " ");
  const blurb = description ?? conciseToolDescription(activity.name, {});
  const detailCalls = calls && calls.length > 0 ? calls : [activity];
  const isWrite = activity.name === "file_edit" || activity.name === "file_write";
  const touched = isWrite
    ? [...new Set(detailCalls.map((c) => (typeof c.metadata?.path === "string" ? shortPath(c.metadata.path as string) : "")).filter(Boolean))]
    : [];
  return (
    <details className={`tool-activity ${activity.status}`} open={activity.status === "failed" || (isWrite && activity.status === "running")}>
      <summary>
        <span className="tool-status" />
        <strong>
          {label}{count > 1 && <b className="tool-count">×{count}</b>}
          {touched.length > 0 && <em className="tool-files">{touched.slice(0, 3).join(", ")}{touched.length > 3 ? ` +${touched.length - 3}` : ""}</em>}
        </strong>
        <small>{activity.status === "running" ? "running" : activity.status}</small>
      </summary>
      {(blurb || narration.length > 0 || detailCalls.length > 0) && (
        <div className="tool-activity-body">
          <p>{blurb}</p>
          {narration.length > 0 && (
            <ul className="tool-narration" aria-label="What the model said before these calls">
              {narration.map((line, index) => <li key={index}>{line}</li>)}
            </ul>
          )}
          <div className="tool-calls">
            {detailCalls.map((call, index) => (
              <ToolCallDetail key={call.id || index} call={call} showHeader={detailCalls.length > 1 || isWrite} />
            ))}
          </div>
        </div>
      )}
    </details>
  );
}

function formatAgentElapsed(seconds: number): string {
  if (seconds < 1) return `${Math.round(seconds * 1000)}ms`;
  if (seconds < 60) return `${seconds.toFixed(seconds < 10 ? 1 : 0)}s`;
  const minutes = Math.floor(seconds / 60);
  const remainder = Math.round(seconds % 60);
  return `${minutes}m ${remainder}s`;
}

function AgentActivityView({
  agent,
  onOpenPath,
}: {
  agent: AgentSnapshot;
  onOpenPath: (reference: string) => void;
}) {
  const [expanded, setExpanded] = useState(agent.status === "running" || agent.status === "failed");
  return (
    <details
      className={`agent-activity ${agent.status}`}
      open={expanded}
      onToggle={(event) => setExpanded(event.currentTarget.open)}
    >
      <summary>
        <span className="agent-number" aria-label={`Agent ${agent.agentNumber}`}>{agent.agentNumber}</span>
        <div>
          <strong>{agent.label || `Agent ${agent.agentNumber}`}</strong>
          <small>{agent.agentType || "sub-agent"}</small>
        </div>
        <span className={`agent-status ${agent.status}`}>{agent.status}</span>
      </summary>
      <div className="agent-activity-body">
        {agent.task && <p className="agent-task">{agent.task}</p>}
        {agent.activity && <p className="agent-current"><span />{agent.activity}</p>}
        {agent.toolCalls.length > 0 && (
          <div className="agent-tool-list" aria-label={`Agent ${agent.agentNumber} tool calls`}>
            {agent.toolCalls.map((tool) => (
              <div key={tool.id} className={`agent-tool ${tool.status}`}>
                <span />
                <strong>{tool.name.replaceAll("_", " ")}</strong>
                {tool.argsPreview && <code title={tool.argsPreview}>{tool.argsPreview}</code>}
                <small>{tool.status}</small>
              </div>
            ))}
          </div>
        )}
        {agent.output && (
          <div className="agent-output">
            <MarkdownView text={agent.output} onOpenPath={onOpenPath} />
          </div>
        )}
        {(agent.tokens > 0 || agent.elapsedSeconds > 0) && (
          <footer>
            {agent.tokens > 0 && <span>{agent.tokens.toLocaleString()} tokens</span>}
            {agent.elapsedSeconds > 0 && <span>{formatAgentElapsed(agent.elapsedSeconds)}</span>}
          </footer>
        )}
      </div>
    </details>
  );
}

/** A paste this long is content, not a message: it becomes a chip. */
const PASTE_CHIP_MIN_LINES = 6;
const PASTE_CHIP_MIN_CHARS = 600;

function attachmentIcon(attachment: ComposerAttachment) {
  if (attachment.kind === "paste") return <ClipboardList size={12} />;
  const ext = (attachment.name.split(".").pop() || "").toLowerCase();
  if (["png", "jpg", "jpeg", "gif", "webp"].includes(ext)) return <ImageIcon size={12} />;
  if (ext === "pdf" || ext === "md" || ext === "txt") return <FileText size={12} />;
  return <FileCode2 size={12} />;
}

function AttachmentChips({
  attachments,
  onRemove,
}: {
  attachments: ComposerAttachment[];
  onRemove?: (id: string) => void;
}) {
  if (attachments.length === 0) return null;
  return (
    <div className="attachment-chips" aria-label="Attachments">
      {attachments.map((attachment) => (
        <span
          key={attachment.id}
          className={`attachment-chip ${attachment.ok === false ? "failed" : ""}`}
          title={attachment.detail || attachment.path || attachment.name}
        >
          {attachmentIcon(attachment)}
          <span>{attachment.name}</span>
          {attachment.detail && attachment.kind !== "paste" && <small>{attachment.detail}</small>}
          {onRemove && (
            <button type="button" onClick={() => onRemove(attachment.id)} aria-label={`Remove ${attachment.name}`}>
              <X size={11} />
            </button>
          )}
        </span>
      ))}
    </div>
  );
}

function Composer({
  value,
  setValue,
  commands,
  disabled,
  directionPrompt,
  focusRequest,
  send,
  attachments,
  onAttachFiles,
  onPasteText,
  onRemoveAttachment,
}: {
  value: string;
  setValue: (value: string) => void;
  commands: SlashCommandOption[];
  disabled: boolean;
  directionPrompt: string;
  focusRequest: number;
  send: () => void;
  attachments: ComposerAttachment[];
  onAttachFiles: () => void;
  onPasteText: (text: string) => void;
  onRemoveAttachment: (id: string) => void;
}) {
  const [slashMenuOpen, setSlashMenuOpen] = useState(false);
  const [cursorPosition, setCursorPosition] = useState(value.length);
  const textareaRef = useRef<HTMLTextAreaElement>(null);
  const commandHighlightRef = useRef<HTMLDivElement>(null);
  const slashContext = useMemo(
    () => slashCommandContextAt(value, cursorPosition, commands),
    [commands, cursorPosition, value],
  );
  const slashQuery = slashContext?.query.toLowerCase() ?? "";
  const filteredCommands = useMemo(
    () => commands.filter((item) => {
      if (slashQuery === "/" && item.command.includes(" ")) return false;
      return !slashQuery || item.command.toLowerCase().startsWith(slashQuery);
    }),
    [commands, slashQuery],
  );
  const commandRanges = useMemo(
    () => composerCommandRanges(value, commands, slashContext),
    [commands, slashContext, value],
  );
  const commandActive = commandRanges.length > 0;
  const showSlashMenu = !disabled && slashMenuOpen && slashContext !== null
    && filteredCommands.length > 0;

  useEffect(() => {
    if (disabled) setSlashMenuOpen(false);
  }, [disabled]);

  useEffect(() => {
    if (!focusRequest || disabled) return;
    const frame = window.requestAnimationFrame(() => textareaRef.current?.focus());
    return () => window.cancelAnimationFrame(frame);
  }, [disabled, focusRequest]);

  const chooseCommand = (command: string) => {
    if (!slashContext) return;
    const next = value.slice(0, slashContext.start) + command + value.slice(slashContext.end);
    const nextCursor = slashContext.start + command.length;
    setValue(next);
    setCursorPosition(nextCursor);
    setSlashMenuOpen(false);
    window.requestAnimationFrame(() => {
      textareaRef.current?.focus();
      textareaRef.current?.setSelectionRange(nextCursor, nextCursor);
    });
  };

  const toggleCommandMenu = () => {
    if (showSlashMenu) {
      setSlashMenuOpen(false);
      return;
    }

    const textarea = textareaRef.current;
    const selectionStart = textarea?.selectionStart ?? cursorPosition;
    const selectionEnd = textarea?.selectionEnd ?? selectionStart;
    const existingContext = slashCommandContextAt(value, selectionStart, commands);
    if (existingContext) {
      setCursorPosition(selectionStart);
      setSlashMenuOpen(true);
      window.requestAnimationFrame(() => textareaRef.current?.focus());
      return;
    }

    const left = value.slice(0, selectionStart);
    const right = value.slice(selectionEnd);
    const leading = left && !/\s$/.test(left) ? " /" : "/";
    const trailing = right && !/^\s/.test(right) ? " " : "";
    const next = left + leading + trailing + right;
    const nextCursor = left.length + leading.length;
    setValue(next);
    setCursorPosition(nextCursor);
    setSlashMenuOpen(true);
    window.requestAnimationFrame(() => {
      textareaRef.current?.focus();
      textareaRef.current?.setSelectionRange(nextCursor, nextCursor);
    });
  };

  let highlightedUntil = 0;

  return (
    <div className="composer-wrap">
      {directionPrompt && (
        <div className="direction-required" role="status">
          <CircleHelp size={14} />
          <span>{directionPrompt}</span>
        </div>
      )}
      {showSlashMenu && (
        <div className="slash-menu" role="listbox" aria-label="Tether commands">
          <header><TerminalSquare size={13} /><span>Commands</span><small>Choose one, then send</small></header>
          {filteredCommands.map((item) => (
            <button
              key={item.command}
              type="button"
              className={slashQuery === item.command.toLowerCase() ? "selected" : ""}
              onClick={() => chooseCommand(item.command)}
            >
              <code>{item.command}</code>
              <span>{item.description}</span>
              {item.category && <small>{item.category}</small>}
            </button>
          ))}
        </div>
      )}
      <div className={`composer ${showSlashMenu ? "slash-open" : ""} ${attachments.length > 0 ? "has-attachments" : ""}`}>
        <div className="composer-side">
          <button
            type="button"
            className="slash-button"
            onClick={toggleCommandMenu}
            disabled={disabled}
            aria-label="Show Tether commands"
            title="Show commands"
          >
            <TerminalSquare size={15} />
          </button>
          <button
            type="button"
            className="slash-button attach-button"
            onClick={onAttachFiles}
            disabled={disabled}
            aria-label="Attach files"
            title="Attach files: code, text, markdown, PDF, images"
          >
            <Paperclip size={15} />
          </button>
        </div>
        <div className="command-input-wrap">
          <AttachmentChips attachments={attachments} onRemove={onRemoveAttachment} />
          {commandActive && (
            <div ref={commandHighlightRef} className="command-highlight" aria-hidden="true">
              {commandRanges.flatMap((range, index) => {
                const plain = value.slice(highlightedUntil, range.start);
                const command = value.slice(range.start, range.end);
                highlightedUntil = range.end;
                return [
                  <span key={`plain-${index}`}>{plain}</span>,
                  <strong key={`command-${index}`}>{command}</strong>,
                ];
              })}
              <span>{value.slice(highlightedUntil)}</span>
            </div>
          )}
          <textarea
            ref={textareaRef}
            className={commandActive ? "command-token-active" : ""}
            value={value}
            onChange={(event) => {
              const next = event.target.value;
              const nextCursor = event.target.selectionStart;
              setValue(next);
              setCursorPosition(nextCursor);
              setSlashMenuOpen(slashCommandContextAt(next, nextCursor, commands) !== null);
            }}
            onPaste={(event) => {
              const text = event.clipboardData.getData("text/plain");
              const lines = text.split(/\r?\n/).length;
              if (lines >= PASTE_CHIP_MIN_LINES || text.length >= PASTE_CHIP_MIN_CHARS) {
                event.preventDefault();
                onPasteText(text);
              }
            }}
            onSelect={(event) => setCursorPosition(event.currentTarget.selectionStart)}
            onScroll={(event) => {
              if (!commandHighlightRef.current) return;
              commandHighlightRef.current.scrollTop = event.currentTarget.scrollTop;
              commandHighlightRef.current.scrollLeft = event.currentTarget.scrollLeft;
            }}
            onKeyDown={(event) => {
              if (event.key === "Escape") {
                setSlashMenuOpen(false);
                return;
              }
              if (event.key === "Enter" && (event.metaKey || event.ctrlKey)) {
                event.preventDefault();
                setSlashMenuOpen(false);
                send();
              }
            }}
            placeholder={attachments.length > 0 ? "Say what to do with the attachments…" : "Ask Tether anything — or choose a project to work on a codebase…"}
            disabled={disabled}
            rows={3}
          />
        </div>
        <button onClick={send} disabled={disabled || (!value.trim() && attachments.length === 0)} aria-label="Send message">
          <ArrowUp size={18} />
        </button>
      </div>
      <div className="composer-note">
        <span>⌘↩ to send · type / anywhere for commands</span>
        <span><LockKeyhole size={12} /> Restricted actions require approval</span>
      </div>
    </div>
  );
}

function ApprovalDialog({
  request,
  pending,
  onDecision,
}: {
  request: ApprovalRequest;
  pending: boolean;
  onDecision: (decision: "allow_once" | "allow_session" | "deny") => void;
}) {
  return (
    <div className="modal-backdrop">
      <section
        className="approval-dialog"
        role="dialog"
        aria-modal="true"
        aria-labelledby="approval-title"
        aria-busy={pending}
      >
        <header>
          <div className="approval-icon"><LockKeyhole size={23} /></div>
          <div>
            <h2 id="approval-title">Approval required</h2>
            <p>Tether wants to run <code>{request.tool}</code>.</p>
          </div>
        </header>
        <pre>{JSON.stringify(request.arguments, null, 2)}</pre>
        <div className="approval-note">
          <ShieldCheck size={15} /> Review the target and arguments before continuing.
        </div>
        <footer>
          <button className="deny" disabled={pending} onClick={() => onDecision("deny")}><X size={15} /> No</button>
          <div>
            <button disabled={pending} onClick={() => onDecision("allow_session")}><RotateCcw size={15} /> Allow full session</button>
            <button className="allow" disabled={pending} onClick={() => onDecision("allow_once")}><Check size={15} /> Allow once</button>
          </div>
        </footer>
      </section>
    </div>
  );
}

function QuestionDialog({
  request,
  onAnswer,
}: {
  request: QuestionRequest;
  onAnswer: (answers: Array<{ question: string; answer: string }>) => void;
}) {
  const [answers, setAnswers] = useState<Record<string, string>>({});
  const complete = request.questions.every((question) => Boolean(answers[question.id]?.trim()));
  return (
    <div className="modal-backdrop">
      <form
        className="question-dialog"
        role="dialog"
        aria-modal="true"
        aria-labelledby="questions-title"
        onSubmit={(event) => {
          event.preventDefault();
          if (!complete) return;
          onAnswer(request.questions.map((question) => ({
            question: question.question,
            answer: answers[question.id].trim(),
          })));
        }}
      >
        <header>
          <div className="question-icon"><ListChecks size={23} /></div>
          <div>
            <h2 id="questions-title">A quick decision</h2>
            <p>Tether needs your input before it continues.</p>
          </div>
        </header>
        <div className="question-list">
          {request.questions.map((question, index) => (
            <fieldset key={question.id}>
              <legend>{index + 1}. {question.question}</legend>
              <div className="question-options">
                {question.options.map((option) => (
                  <label key={option}>
                    <input
                      type="radio"
                      name={question.id}
                      value={option}
                      checked={answers[question.id] === option}
                      onChange={() => setAnswers((current) => ({ ...current, [question.id]: option }))}
                    />
                    <span>{option}</span>
                  </label>
                ))}
              </div>
              <input
                className="custom-answer"
                value={question.options.includes(answers[question.id] ?? "") ? "" : (answers[question.id] ?? "")}
                onChange={(event) => setAnswers((current) => ({ ...current, [question.id]: event.target.value }))}
                placeholder="Or type a different answer"
              />
            </fieldset>
          ))}
        </div>
        <footer>
          <button type="button" className="deny" onClick={() => onAnswer([])}>Use defaults</button>
          <button type="submit" className="allow" disabled={!complete}>
            <Check size={15} /> Continue
          </button>
        </footer>
      </form>
    </div>
  );
}
