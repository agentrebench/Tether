export type ConnectionState = "no-project" | "connecting" | "ready" | "failed";

export type ChatRole = "user" | "assistant";

/** One chronological slice of an assistant turn: prose the model wrote, or
 * the run of tool calls it made before writing more. Rendered in order so
 * the final answer lands *below* the tool cards it followed. */
export type TurnSegment =
  | { kind: "text"; text: string }
  | { kind: "tools"; activities: ToolActivity[] };

export interface ChatMessage {
  id: string;
  turnId?: string;
  role: ChatRole;
  text: string;
  /** Ordered text/tool segments for assistant turns (absent for user turns
   * and for turns restored without streaming detail; falls back to `text`). */
  segments?: TurnSegment[];
  toolCalls?: string[];
  toolActivities?: ToolActivity[];
  agents?: AgentSnapshot[];
  queueState?: "submitting" | "queued" | "active" | "cleared";
  queuePosition?: number;
  queueReason?: string;
  tokenCount?: number;
  streaming?: boolean;
  stopReason?: string;
}

export interface ToolActivity {
  id: string;
  name: string;
  status: "running" | "completed" | "failed";
  argsPreview: string;
  outputPreview: string;
  displayKind: string;
  errorCode: string;
  metadata: Record<string, unknown>;
}

export type AgentStatus = "queued" | "running" | "completed" | "failed" | "cancelled";

export interface AgentToolCall {
  id: string;
  name: string;
  status: "queued" | "running" | "completed" | "failed" | "denied";
  argsPreview: string;
  isError: boolean;
}

export interface AgentSnapshot {
  turnId: string;
  agentId: string;
  agentNumber: number;
  revision: number;
  label: string;
  agentType: string;
  task: string;
  status: AgentStatus;
  activity: string;
  toolCalls: AgentToolCall[];
  output: string;
  tokens: number;
  elapsedSeconds: number;
}

export interface TodoItem {
  content: string;
  status: "pending" | "in_progress" | "completed";
  active_form?: string;
}

export interface ClarifyingQuestion {
  id: string;
  question: string;
  options: string[];
}

export interface QuestionRequest {
  id: string;
  questions: ClarifyingQuestion[];
}

export interface ProviderModel {
  id: string;
  label: string;
  description: string;
  context_size?: number | null;
  reasoning_efforts?: string[];
  default_reasoning_effort?: string;
  thinking_modes?: string[];
  default_thinking_mode?: string;
  thinking_mode?: string;
}

export interface ProviderOption {
  id: string;
  label: string;
  description: string;
  requires_api_key: boolean;
  api_key_configured: boolean;
  api_key_env: string;
  models: ProviderModel[];
}

export interface ApprovalRequest {
  id: string;
  tool: string;
  arguments: Record<string, unknown>;
}

export interface BridgePayload {
  type: string;
  [key: string]: unknown;
}

export interface BridgeEnvelope {
  bridgeId: string;
  payload: BridgePayload;
}

export interface WorkspaceEntry {
  name: string;
  path: string;
  isDirectory: boolean;
  sizeBytes: number;
}

export interface WorkspacePreview {
  path: string;
  kind: "file" | "directory";
  content: string;
  entries: WorkspaceEntry[];
  sizeBytes: number;
  truncated: boolean;
}
