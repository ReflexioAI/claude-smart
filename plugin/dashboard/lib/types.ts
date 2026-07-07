export type UserActionType =
  | "NONE"
  | "CORRECTIVE_PHRASE"
  | "CORRECTION"
  | "PRAISE"
  | "STOP";

// Wire format from the reflexio API. CURRENT rows arrive as `status: null`
// (response_model_exclude_none strips it from the JSON entirely), so the type
// only enumerates the non-null values. The values are lowercase because they
// come from Python's Status StrEnum.
export type LifecycleStatus = "pending" | "archived" | "merged" | "superseded";

export type AgentPlaybookStatus = "pending" | "approved" | "rejected";

export type ProfileStatus = "pending" | "archived" | "merged" | "superseded";

export interface ToolUsed {
  tool_name: string;
  status: string;
  tool_data?: { input?: Record<string, unknown>; output?: string };
}

export interface CitedItem {
  id: string;
  kind: "playbook" | "profile";
  title: string;
  real_id?: string;
  source_kind?: "user_playbook" | "agent_playbook" | "profile";
}

export interface Interaction {
  interaction_id: number;
  user_id: string;
  request_id: string;
  created_at: number;
  role: string;
  content: string;
  user_action: UserActionType;
  user_action_description?: string;
  tools_used: ToolUsed[];
}

export interface UserPlaybook {
  user_playbook_id: number;
  user_id: string | null;
  agent_version: string;
  request_id: string;
  playbook_name: string;
  created_at: number;
  content: string;
  trigger: string | null;
  rationale: string | null;
  status: LifecycleStatus | null;
  source: string | null;
  source_interaction_ids: number[];
}

export interface AgentPlaybook {
  agent_playbook_id: number;
  playbook_name: string;
  agent_version: string;
  created_at: number;
  content: string;
  trigger: string | null;
  rationale: string | null;
  playbook_status: AgentPlaybookStatus;
  playbook_metadata: string;
  status: LifecycleStatus | null;
}

export interface UserProfile {
  profile_id: string;
  user_id: string;
  content: string;
  last_modified_timestamp: number;
  generated_from_request_id: string;
  profile_time_to_live?: string;
  expiration_timestamp?: number;
  custom_features?: Record<string, unknown> | null;
  extractor_names?: string[] | null;
  status: ProfileStatus | null;
  source: string | null;
}

export interface SessionTurn {
  role: "User" | "Assistant";
  content: string;
  ts?: number;
  user_id?: string;
  tools_used?: ToolUsed[];
  cited_items?: CitedItem[];
  user_action?: UserActionType;
  user_action_description?: string;
}

export interface SessionSummary {
  session_id: string;
  turn_count: number;
  learning_interaction_count: number;
  last_activity: number | null;
  first_activity: number | null;
  published_up_to: number;
  preview: string | null;
  source: "local";
}

export interface SessionDetail {
  session_id: string;
  turns: SessionTurn[];
  published_up_to: number;
}

export interface ClaudeSmartConfig {
  REFLEXIO_URL: string;
  REFLEXIO_API_KEY: string;
  REFLEXIO_API_KEY_SET?: boolean;
  CLAUDE_SMART_USE_LOCAL_CLI: boolean;
  CLAUDE_SMART_USE_LOCAL_EMBEDDING: boolean;
  CLAUDE_SMART_READ_ONLY: boolean;
  CLAUDE_SMART_CLI_PATH: string;
  CLAUDE_SMART_CLI_TIMEOUT: string;
  CLAUDE_SMART_STATE_DIR: string;
  [extra: string]: string | boolean | undefined;
}

export type OptimizerMode = "auto" | "enabled" | "disabled";

export interface ClaudeCodeHookConfig {
  CLAUDE_SMART_ENABLE_OPTIMIZER: OptimizerMode;
  effectiveValue: OptimizerMode;
  localValue: OptimizerMode | null;
  userValue: OptimizerMode | null;
  settingsPath: string;
  userSettingsPath: string;
}

export interface ReflexioExtractorConfig {
  extraction_definition_prompt?: string;
  [k: string]: unknown;
}

export interface ReflexioRetrievalFloorConfig {
  enabled?: boolean;
  pool_size?: number;
  profile_floor?: number;
  user_playbook_floor?: number;
  agent_playbook_floor?: number;
  [k: string]: unknown;
}

export interface ReflexioConfig {
  agent_context_prompt?: string | null;
  window_size?: number;
  stride_size?: number;
  retrieval_floor?: ReflexioRetrievalFloorConfig | null;
  profile_extractor_configs?: ReflexioExtractorConfig[] | null;
  user_playbook_extractor_configs?: ReflexioExtractorConfig[] | null;
  [k: string]: unknown;
}

/**
 * Per-rule citation counts aggregated from local session cited_items. Timestamps
 * are unix epoch seconds, matching the int-epoch convention used elsewhere in
 * the dashboard.
 */
export interface PlaybookApplicationStat {
  real_id: string;
  citation_id?: string;
  kind: "playbook" | "profile" | "user_playbook" | "agent_playbook";
  source_kind?: "user_playbook" | "agent_playbook" | "profile";
  title: string;
  href?: string;
  applied_count: number;
  last_applied_at: number | null;
  last_interaction_id: number | null;
}
