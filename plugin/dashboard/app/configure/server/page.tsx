"use client";

import { PageFrame } from "@/components/common/page-frame";

import { useCallback, useEffect, useState } from "react";
import { Save, CheckCircle2 } from "lucide-react";
import { reflexio } from "@/lib/reflexio-client";
import { PageHeader } from "@/components/common/page-header";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import type { ReflexioConfig } from "@/lib/types";

type ExtractorField =
  | "profile_extractor_config"
  | "user_playbook_extractor_config";

export default function ConfigureServerPage() {
  const [srvConfig, setSrvConfig] = useState<ReflexioConfig | null>(null);
  const [srvError, setSrvError] = useState<string | null>(null);
  const [srvSaved, setSrvSaved] = useState(false);
  const [srvSaving, setSrvSaving] = useState(false);
  const [srvLoading, setSrvLoading] = useState(true);
  const [candidateBudgetDraft, setCandidateBudgetDraft] = useState("");
  const supportsCandidateBudget = !!srvConfig?.retrieval_floor &&
    "candidate_pool_size" in srvConfig.retrieval_floor;
  const invalidCandidateBudget = supportsCandidateBudget &&
    (!/^\d+$/.test(candidateBudgetDraft) ||
      !Number.isSafeInteger(Number(candidateBudgetDraft)));

  const fetchSrvConfig = useCallback(async (): Promise<ReflexioConfig> => {
    return reflexio.getServerConfiguration();
  }, []);

  useEffect(() => {
    let alive = true;
    async function load() {
      setSrvLoading(true);
      setSrvError(null);
      try {
        const data = await fetchSrvConfig();
        if (alive) {
          setSrvConfig(data);
          setCandidateBudgetDraft(data.retrieval_floor?.candidate_pool_size?.toString() ?? "");
        }
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
      const extractor = prev[field];
      if (!extractor) return prev;
      return {
        ...prev,
        [field]: { ...extractor, extraction_definition_prompt: v },
      };
    });
    setSrvSaved(false);
  };

  const saveSrv = async () => {
    if (!srvConfig || invalidCandidateBudget) return;
    setSrvSaving(true);
    setSrvError(null);
    try {
      const publication = supportsCandidateBudget
        ? {
            ...srvConfig,
            retrieval_floor: {
              ...srvConfig.retrieval_floor,
              candidate_pool_size: Number(candidateBudgetDraft),
            },
          }
        : srvConfig;
      await reflexio.setServerConfiguration(publication);
      const fresh = await fetchSrvConfig();
      setSrvConfig(fresh);
      setCandidateBudgetDraft(fresh.retrieval_floor?.candidate_pool_size?.toString() ?? "");
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
        description="Configuration for the selected Reflexio backend."
        actions={
          <Button
            onClick={saveSrv}
            disabled={!srvConfig || srvSaving || invalidCandidateBudget}
            size="sm"
          >
            <Save className="h-3.5 w-3.5" />
            {srvSaving ? "Saving…" : "Save"}
          </Button>
        }
      />

      <PageFrame className="space-y-5">
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
          <section className="space-y-5 border-t border-border py-5">
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

            <div className="flex items-start justify-between gap-4 rounded-md border border-border bg-background/60 p-3">
              <div className="min-w-0">
                <Label htmlFor="search-candidate-budget">Search candidate budget</Label>
                <p id="search-candidate-budget-help" className="text-xs text-muted-foreground mt-0.5">
                  Zero disables cross-encoder flooring. A positive budget sets
                  the candidate pool per search arm when reranking is available;
                  top-k or recency may require a larger pool. Existing score
                  thresholds are preserved.
                  {(!srvConfig.retrieval_floor || !("candidate_pool_size" in srvConfig.retrieval_floor)) && " Update the connected server to configure this budget."}
                </p>
              </div>
              <Input
                id="search-candidate-budget"
                aria-describedby={invalidCandidateBudget ? "search-candidate-budget-help search-candidate-budget-error" : "search-candidate-budget-help"}
                aria-invalid={invalidCandidateBudget}
                type="number"
                disabled={!supportsCandidateBudget}
                min={0}
                step={1}
                value={candidateBudgetDraft}
                onChange={(event) => {
                  setCandidateBudgetDraft(event.target.value);
                  setSrvSaved(false);
                }}
                className="w-24 shrink-0 font-mono text-xs"
              />
            </div>

            {invalidCandidateBudget && (
              <p id="search-candidate-budget-error" role="alert" className="text-xs text-destructive">
                Enter a whole number zero or greater before saving.
              </p>
            )}

            <div className="space-y-2">
              <Label htmlFor="preference-extraction-prompt">Preference focus area</Label>
              <p className="text-xs text-muted-foreground">
                What the preference extractor should focus on.
              </p>
              {srvConfig.profile_extractor_config ? (
                <textarea
                  id="preference-extraction-prompt"
                  value={srvConfig.profile_extractor_config.extraction_definition_prompt ?? ""}
                  onChange={(e) =>
                    updateExtractorPrompt(
                      "profile_extractor_config",
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
              <Label htmlFor="skill-extraction-prompt">Project-specific skill focus area</Label>
              <p className="text-xs text-muted-foreground">
                What the project-specific skill extractor should focus on.
              </p>
              {srvConfig.user_playbook_extractor_config ? (
                <textarea
                  id="skill-extraction-prompt"
                  value={srvConfig.user_playbook_extractor_config.extraction_definition_prompt ?? ""}
                  onChange={(e) =>
                    updateExtractorPrompt(
                      "user_playbook_extractor_config",
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
      </PageFrame>
    </>
  );
}
