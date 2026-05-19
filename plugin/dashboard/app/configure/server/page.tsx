"use client";

import { useCallback, useEffect, useState } from "react";
import { Save, CheckCircle2 } from "lucide-react";
import { PageHeader } from "@/components/common/page-header";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { useSettings } from "@/hooks/use-settings";
import type { ReflexioConfig } from "@/lib/types";

type ExtractorField =
  | "profile_extractor_configs"
  | "user_playbook_extractor_configs";

export default function ConfigureServerPage() {
  const { reflexioUrl } = useSettings();
  const [srvConfig, setSrvConfig] = useState<ReflexioConfig | null>(null);
  const [srvError, setSrvError] = useState<string | null>(null);
  const [srvSaved, setSrvSaved] = useState(false);
  const [srvSaving, setSrvSaving] = useState(false);
  const [srvLoading, setSrvLoading] = useState(true);

  const fetchSrvConfig = useCallback(async (): Promise<ReflexioConfig> => {
    const headers: HeadersInit = {};
    if (reflexioUrl) headers["x-reflexio-url"] = reflexioUrl;
    const res = await fetch("/api/reflexio/api/get_config", {
      cache: "no-store",
      headers,
    });
    if (!res.ok) {
      const body = await res.json().catch(() => null);
      const detail = body?.error
        ? `${body.error}${body.detail ? `: ${body.detail}` : ""}`
        : `HTTP ${res.status}`;
      throw new Error(detail);
    }
    return (await res.json()) as ReflexioConfig;
  }, [reflexioUrl]);

  useEffect(() => {
    let alive = true;
    async function load() {
      setSrvLoading(true);
      setSrvError(null);
      try {
        const data = await fetchSrvConfig();
        if (alive) setSrvConfig(data);
      } catch (e) {
        if (alive) setSrvError(e instanceof Error ? e.message : String(e));
      } finally {
        if (alive) setSrvLoading(false);
      }
    }
    void load();
    return () => {
      alive = false;
    };
  }, [fetchSrvConfig]);

  const updateSrv = <K extends keyof ReflexioConfig>(
    key: K,
    value: ReflexioConfig[K],
  ) => {
    setSrvConfig((prev) => (prev ? { ...prev, [key]: value } : prev));
    setSrvSaved(false);
  };

  const updateExtractorPrompt = (field: ExtractorField, v: string) => {
    setSrvConfig((prev) => {
      if (!prev) return prev;
      const list = prev[field];
      if (!list || list.length === 0) return prev;
      const next = [...list];
      next[0] = { ...next[0], extraction_definition_prompt: v };
      return { ...prev, [field]: next };
    });
    setSrvSaved(false);
  };

  const saveSrv = async () => {
    if (!srvConfig) return;
    setSrvSaving(true);
    setSrvError(null);
    try {
      const headers: HeadersInit = { "content-type": "application/json" };
      if (reflexioUrl) headers["x-reflexio-url"] = reflexioUrl;
      const res = await fetch("/api/reflexio/api/set_config", {
        method: "POST",
        headers,
        body: JSON.stringify(srvConfig),
      });
      if (!res.ok) {
        const body = await res.json().catch(() => null);
        const detail = body?.error
          ? `${body.error}${body.detail ? `: ${body.detail}` : ""}`
          : `HTTP ${res.status}`;
        throw new Error(detail);
      }
      const fresh = await fetchSrvConfig();
      setSrvConfig(fresh);
      setSrvSaved(true);
    } catch (e) {
      setSrvError(e instanceof Error ? e.message : String(e));
    } finally {
      setSrvSaving(false);
    }
  };

  return (
    <>
      <PageHeader
        title="Reflexio server"
        description="Fetched live from the reflexio backend via /api/get_config. Requires reflexio to be running."
        actions={
          <Button onClick={saveSrv} disabled={!srvConfig || srvSaving} size="sm">
            <Save className="h-3.5 w-3.5" />
            {srvSaving ? "Saving…" : "Save"}
          </Button>
        }
      />

      <div className="p-6 max-w-3xl mx-auto space-y-5">
        {srvError && (
          <div className="rounded-lg border border-destructive/30 bg-destructive/5 text-destructive px-4 py-3 text-sm">
            {srvError}
          </div>
        )}
        {srvSaved && (
          <div className="rounded-lg border border-border bg-accent/40 px-4 py-2.5 text-sm flex items-center gap-2">
            <CheckCircle2 className="h-4 w-4 text-muted-foreground" />
            Saved to reflexio
          </div>
        )}

        {srvLoading ? (
          <div className="text-sm text-muted-foreground">Loading…</div>
        ) : srvConfig ? (
          <section className="space-y-5 rounded-lg border border-border bg-card/92 p-5 shadow-sm">
            <div className="space-y-2">
              <Label>Additional agent description</Label>
              <p className="text-xs text-muted-foreground">
                Free-form context about the agent&apos;s working environment.
                Prepended to extractor prompts.
              </p>
              <textarea
                value={srvConfig.agent_context_prompt ?? ""}
                onChange={(e) =>
                  updateSrv("agent_context_prompt", e.target.value || null)
                }
                className="w-full font-mono text-xs min-h-[80px] rounded-md border border-input bg-background/80 px-3 py-2 focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-ring"
                placeholder="(empty)"
              />
            </div>

            <div className="space-y-2">
              <Label>Extraction window size</Label>
              <p className="text-xs text-muted-foreground">
                How many recent interactions are fed into each extraction run
                (past K interactions).
              </p>
              <Input
                type="number"
                min={1}
                value={srvConfig.window_size ?? ""}
                onChange={(e) => {
                  const n = Number(e.target.value);
                  updateSrv(
                    "window_size",
                    Number.isFinite(n) && n > 0 ? n : undefined,
                  );
                }}
                className="font-mono text-xs bg-background/80"
                placeholder="10"
              />
            </div>

            <div className="space-y-2">
              <Label>Extraction stride</Label>
              <p className="text-xs text-muted-foreground">
                How many new interactions must accumulate before the next
                extraction run.
              </p>
              <Input
                type="number"
                min={1}
                value={srvConfig.stride_size ?? ""}
                onChange={(e) => {
                  const n = Number(e.target.value);
                  updateSrv(
                    "stride_size",
                    Number.isFinite(n) && n > 0 ? n : undefined,
                  );
                }}
                className="font-mono text-xs bg-background/80"
                placeholder="5"
              />
            </div>

            <div className="space-y-2">
              <Label>Preference focus area</Label>
              <p className="text-xs text-muted-foreground">
                What the preference extractor should focus on.
              </p>
              {srvConfig.profile_extractor_configs &&
              srvConfig.profile_extractor_configs.length > 0 ? (
                <textarea
                  value={
                    srvConfig.profile_extractor_configs[0]
                      .extraction_definition_prompt ?? ""
                  }
                  onChange={(e) =>
                    updateExtractorPrompt(
                      "profile_extractor_configs",
                      e.target.value,
                    )
                  }
                  className="w-full font-mono text-xs min-h-[120px] rounded-md border border-input bg-background/80 px-3 py-2 focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-ring"
                />
              ) : (
                <div className="text-xs text-muted-foreground italic">
                  No preference extractor configured on the server.
                </div>
              )}
            </div>

            <div className="space-y-2">
              <Label>Project-specific skill focus area</Label>
              <p className="text-xs text-muted-foreground">
                What the project-specific skill extractor should focus on.
              </p>
              {srvConfig.user_playbook_extractor_configs &&
              srvConfig.user_playbook_extractor_configs.length > 0 ? (
                <textarea
                  value={
                    srvConfig.user_playbook_extractor_configs[0]
                      .extraction_definition_prompt ?? ""
                  }
                  onChange={(e) =>
                    updateExtractorPrompt(
                      "user_playbook_extractor_configs",
                      e.target.value,
                    )
                  }
                  className="w-full font-mono text-xs min-h-[120px] rounded-md border border-input bg-background/80 px-3 py-2 focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-ring"
                />
              ) : (
                <div className="text-xs text-muted-foreground italic">
                  No project-specific skill extractor configured on the server.
                </div>
              )}
            </div>
          </section>
        ) : null}
      </div>
    </>
  );
}
