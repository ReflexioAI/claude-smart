/**
 * Client-side wrapper for talking to reflexio through the Next.js proxy at
 * /api/reflexio/*. The proxy forwards to the reflexio URL the user picked in
 * Settings (see hooks/use-settings.tsx). Endpoint paths and request bodies
 * mirror reflexio/reflexio/server/api.py — every call below corresponds to a
 * FastAPI route mounted under the /api prefix.
 */

import type {
  AgentPlaybook,
  AgentPlaybookStatus,
  Interaction,
  UserPlaybook,
  UserProfile,
} from "./types";

type Json = Record<string, unknown>;

interface Opts {
  reflexioUrl?: string;
}

async function request<T>(
  path: string,
  init: RequestInit,
  reflexioUrl: string | undefined,
): Promise<T> {
  const headers = new Headers(init.headers);
  headers.set("content-type", "application/json");
  if (reflexioUrl) headers.set("x-reflexio-url", reflexioUrl);

  const res = await fetch(`/api/reflexio/api/${path.replace(/^\/+/, "")}`, {
    ...init,
    headers,
    cache: "no-store",
  });
  if (!res.ok) {
    const body = await res.text().catch(() => "");
    throw new Error(`reflexio ${res.status}: ${body.slice(0, 300)}`);
  }
  return res.json() as Promise<T>;
}

function post<T>(path: string, body: Json, reflexioUrl?: string): Promise<T> {
  return request<T>(path, { method: "POST", body: JSON.stringify(body) }, reflexioUrl);
}

function get<T>(path: string, reflexioUrl?: string): Promise<T> {
  return request<T>(path, { method: "GET" }, reflexioUrl);
}

export const reflexio = {
  /** POST /api/get_user_playbooks — all filters are optional. */
  async getUserPlaybooks(
    opts: Opts & {
      userId?: string;
      agentVersion?: string;
      playbookName?: string;
      statusFilter?: (string | null)[];
      limit?: number;
    },
  ): Promise<{ user_playbooks: UserPlaybook[] }> {
    const body: Json = { limit: opts.limit ?? 100 };
    if (opts.userId) body.user_id = opts.userId;
    if (opts.agentVersion) body.agent_version = opts.agentVersion;
    if (opts.playbookName) body.playbook_name = opts.playbookName;
    if (opts.statusFilter) body.status_filter = opts.statusFilter;
    return post("get_user_playbooks", body, opts.reflexioUrl);
  },

  /** POST /api/get_agent_playbooks — all filters are optional. */
  async getAgentPlaybooks(
    opts: Opts & {
      agentVersion?: string;
      playbookName?: string;
      statusFilter?: (string | null)[];
      playbookStatusFilter?: AgentPlaybookStatus;
      limit?: number;
    },
  ): Promise<{ agent_playbooks: AgentPlaybook[] }> {
    const body: Json = { limit: opts.limit ?? 100 };
    if (opts.agentVersion) body.agent_version = opts.agentVersion;
    if (opts.playbookName) body.playbook_name = opts.playbookName;
    if (opts.statusFilter) body.status_filter = opts.statusFilter;
    if (opts.playbookStatusFilter)
      body.playbook_status_filter = opts.playbookStatusFilter;
    return post("get_agent_playbooks", body, opts.reflexioUrl);
  },

  /**
   * GET /api/get_all_profiles — no per-user filter required. Returns all
   * preferences across sessions, which is what the dashboard wants.
   */
  async getAllProfiles(
    opts: Opts & { limit?: number; statusFilter?: string } = {},
  ): Promise<{ user_profiles: UserProfile[] }> {
    const qs = new URLSearchParams();
    qs.set("limit", String(opts.limit ?? 200));
    if (opts.statusFilter) qs.set("status_filter", opts.statusFilter);
    return get(`get_all_profiles?${qs.toString()}`, opts.reflexioUrl);
  },

  /** GET /api/get_all_interactions — global, unfiltered. */
  async getAllInteractions(
    opts: Opts & { limit?: number } = {},
  ): Promise<{ interactions: Interaction[] }> {
    const qs = new URLSearchParams();
    qs.set("limit", String(opts.limit ?? 100));
    return get(`get_all_interactions?${qs.toString()}`, opts.reflexioUrl);
  },

  /** PUT /api/update_user_playbook — partial update of one project-specific skill. */
  async updateUserPlaybook(
    update: {
      user_playbook_id: number;
      playbook_name?: string | null;
      content?: string | null;
      trigger?: string | null;
      rationale?: string | null;
    },
    reflexioUrl?: string,
  ): Promise<Json> {
    return request(
      "update_user_playbook",
      { method: "PUT", body: JSON.stringify(update) },
      reflexioUrl,
    );
  },

  /** PUT /api/update_agent_playbook — partial update of one shared skill. */
  async updateAgentPlaybook(
    update: {
      agent_playbook_id: number;
      playbook_name?: string | null;
      content?: string | null;
      trigger?: string | null;
      rationale?: string | null;
      playbook_status?: AgentPlaybookStatus | null;
    },
    reflexioUrl?: string,
  ): Promise<Json> {
    return request(
      "update_agent_playbook",
      { method: "PUT", body: JSON.stringify(update) },
      reflexioUrl,
    );
  },

  /** DELETE /api/delete_user_playbook — body carries the id. */
  async deleteUserPlaybook(
    userPlaybookId: number,
    reflexioUrl?: string,
  ): Promise<Json> {
    return request(
      "delete_user_playbook",
      {
        method: "DELETE",
        body: JSON.stringify({ user_playbook_id: userPlaybookId }),
      },
      reflexioUrl,
    );
  },

  /** DELETE /api/delete_agent_playbook — body carries the id. */
  async deleteAgentPlaybook(
    agentPlaybookId: number,
    reflexioUrl?: string,
  ): Promise<Json> {
    return request(
      "delete_agent_playbook",
      {
        method: "DELETE",
        body: JSON.stringify({ agent_playbook_id: agentPlaybookId }),
      },
      reflexioUrl,
    );
  },

  /** PUT /api/update_user_profile — partial update; content is the common field. */
  async updateUserProfile(
    update: {
      user_id: string;
      profile_id: string;
      content?: string | null;
    },
    reflexioUrl?: string,
  ): Promise<Json> {
    return request(
      "update_user_profile",
      { method: "PUT", body: JSON.stringify(update) },
      reflexioUrl,
    );
  },

  /** DELETE /api/delete_profile — needs both user_id and profile_id. */
  async deleteUserProfile(
    params: { user_id: string; profile_id: string },
    reflexioUrl?: string,
  ): Promise<Json> {
    return request(
      "delete_profile",
      { method: "DELETE", body: JSON.stringify(params) },
      reflexioUrl,
    );
  },

  /** DELETE /api/delete_all_interactions — org-wide purge. */
  async deleteAllInteractions(reflexioUrl?: string): Promise<Json> {
    return request(
      "delete_all_interactions",
      { method: "DELETE" },
      reflexioUrl,
    );
  },

  /** DELETE /api/delete_all_profiles — org-wide purge. */
  async deleteAllProfiles(reflexioUrl?: string): Promise<Json> {
    return request("delete_all_profiles", { method: "DELETE" }, reflexioUrl);
  },

  /** DELETE /api/delete_all_user_playbooks — org-wide purge. */
  async deleteAllUserPlaybooks(reflexioUrl?: string): Promise<Json> {
    return request(
      "delete_all_user_playbooks",
      { method: "DELETE" },
      reflexioUrl,
    );
  },

  /** DELETE /api/delete_all_agent_playbooks — org-wide shared skill purge. */
  async deleteAllAgentPlaybooks(reflexioUrl?: string): Promise<Json> {
    return request(
      "delete_all_agent_playbooks",
      { method: "DELETE" },
      reflexioUrl,
    );
  },
};
