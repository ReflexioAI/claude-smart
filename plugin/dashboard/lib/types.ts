export type UserActionType =
  | "NONE"
  | "CORRECTIVE_PHRASE"
  | "CORRECTION"
  | "PRAISE"
  | "STOP";

export type PlaybookStatus = "PENDING" | "CURRENT" | "ARCHIVED";

export type ProfileStatus = "CURRENT" | "ARCHIVED" | "PENDING";

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
  status: PlaybookStatus | null;
  source: string | null;
  source_interaction_ids: number[];
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
  CLAUDE_SMART_USE_LOCAL_CLI: boolean;
  CLAUDE_SMART_USE_LOCAL_EMBEDDING: boolean;
  CLAUDE_SMART_CLI_PATH: string;
  CLAUDE_SMART_CLI_TIMEOUT: string;
  CLAUDE_SMART_STATE_DIR: string;
  [extra: string]: string | boolean;
}

export interface ReflexioExtractorConfig {
  extraction_definition_prompt?: string;
  [k: string]: unknown;
}

export interface ReflexioConfig {
  agent_context_prompt?: string | null;
  batch_size?: number;
  batch_interval?: number;
  profile_extractor_configs?: ReflexioExtractorConfig[] | null;
  user_playbook_extractor_configs?: ReflexioExtractorConfig[] | null;
  [k: string]: unknown;
}
