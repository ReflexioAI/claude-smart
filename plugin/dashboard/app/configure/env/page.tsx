"use client";

import { useEffect, useState } from "react";
import { Save, CheckCircle2 } from "lucide-react";
import { PageHeader } from "@/components/common/page-header";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Switch } from "@/components/ui/switch";
import { Separator } from "@/components/ui/separator";
import { useSettings } from "@/hooks/use-settings";
import type { ClaudeCodeHookConfig, ClaudeSmartConfig } from "@/lib/types";

export default function ConfigureEnvPage() {
  const { reflexioUrl, setReflexioUrl } = useSettings();
  const [config, setConfig] = useState<ClaudeSmartConfig | null>(null);
  const [hookConfig, setHookConfig] = useState<ClaudeCodeHookConfig | null>(null);
  const [hookDirty, setHookDirty] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [saved, setSaved] = useState(false);
  const [saving, setSaving] = useState(false);

  useEffect(() => {
    Promise.all([
      fetch("/api/config", { cache: "no-store" }).then((r) => r.json()),
      fetch("/api/claude-settings", { cache: "no-store" }).then((r) =>
        r.json(),
      ),
    ])
      .then(([envConfig, claudeConfig]) => {
        setConfig(envConfig);
        setHookConfig(claudeConfig);
        setHookDirty(false);
      })
      .catch((e) => setError(e instanceof Error ? e.message : String(e)));
  }, []);

  const update = <K extends keyof ClaudeSmartConfig>(
    key: K,
    value: ClaudeSmartConfig[K],
  ) => {
    setConfig((prev) => (prev ? { ...prev, [key]: value } : prev));
    setSaved(false);
  };

  const updateOptimizer = (enabled: boolean) => {
    setHookConfig((prev) =>
      prev
        ? {
            ...prev,
            CLAUDE_SMART_ENABLE_OPTIMIZER: enabled,
            effectiveValue: enabled,
            localValue: enabled,
          }
        : prev,
    );
    setHookDirty(true);
    setSaved(false);
  };

  const save = async () => {
    if (!config || !hookConfig) return;
    setSaving(true);
    setError(null);
    try {
      const [envRes, hookRes] = await Promise.all([
        fetch("/api/config", {
          method: "PUT",
          headers: { "content-type": "application/json" },
          body: JSON.stringify(config),
        }),
        fetch("/api/claude-settings", {
          method: "PUT",
          headers: { "content-type": "application/json" },
          body: JSON.stringify(
            hookDirty
              ? {
                  CLAUDE_SMART_ENABLE_OPTIMIZER:
                    hookConfig.CLAUDE_SMART_ENABLE_OPTIMIZER,
                }
              : {},
          ),
        }),
      ]);
      if (!envRes.ok) throw new Error(`environment save failed: ${envRes.status}`);
      if (!hookRes.ok)
        throw new Error(`Claude Code settings save failed: ${hookRes.status}`);
      const updated: ClaudeSmartConfig = await envRes.json();
      const updatedHook: ClaudeCodeHookConfig = await hookRes.json();
      setConfig(updated);
      setHookConfig(updatedHook);
      setHookDirty(false);
      setSaved(true);
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setSaving(false);
    }
  };

  return (
    <>
      <PageHeader
        title="Environment"
        description="Dashboard settings, Reflexio backend environment, and Claude Code hook environment."
        actions={
          <Button onClick={save} disabled={!config || !hookConfig || saving} size="sm">
            <Save className="h-3.5 w-3.5" />
            {saving ? "Saving…" : "Save"}
          </Button>
        }
      />

      <div className="p-6 max-w-2xl mx-auto space-y-6">
        {error && (
          <div className="rounded-lg border border-destructive/30 bg-destructive/5 text-destructive px-4 py-3 text-sm">
            {error}
          </div>
        )}
        {saved && (
          <div className="rounded-lg border border-border bg-accent/40 px-4 py-2.5 text-sm flex items-center gap-2">
            <CheckCircle2 className="h-4 w-4 text-muted-foreground" />
            Saved to ~/.reflexio/.env and Claude Code settings
          </div>
        )}

        <section className="space-y-4">
          <div>
            <h2 className="text-sm font-semibold">Dashboard</h2>
            <p className="text-xs text-muted-foreground">
              Stored in browser localStorage — only affects this UI.
            </p>
          </div>
          <div className="space-y-2">
            <Label>Reflexio endpoint (dashboard)</Label>
            <Input
              value={reflexioUrl}
              onChange={(e) => setReflexioUrl(e.target.value)}
              className="font-mono text-xs"
              placeholder="http://localhost:8071"
            />
          </div>
        </section>

        <Separator />

        <section className="space-y-4">
          <div>
            <h2 className="text-sm font-semibold">claude-smart environment</h2>
            <p className="text-xs text-muted-foreground">
              Writes to <code className="font-mono">~/.reflexio/.env</code>. Unknown
              keys are preserved. These values are read by the Reflexio backend.
            </p>
          </div>

          {config === null && !error ? (
            <div className="text-sm text-muted-foreground">Loading…</div>
          ) : config ? (
            <div className="space-y-5">
              <div className="space-y-2">
                <Label>REFLEXIO_URL</Label>
                <Input
                  value={config.REFLEXIO_URL}
                  onChange={(e) => update("REFLEXIO_URL", e.target.value)}
                  className="font-mono text-xs"
                  placeholder="http://localhost:8071/"
                />
              </div>

              <div className="flex items-start justify-between gap-4">
                <div className="min-w-0">
                  <Label htmlFor="use-local-cli">CLAUDE_SMART_USE_LOCAL_CLI</Label>
                  <p className="text-xs text-muted-foreground mt-0.5">
                    Route generation through the local <code>claude</code> CLI.
                  </p>
                </div>
                <Switch
                  id="use-local-cli"
                  checked={!!config.CLAUDE_SMART_USE_LOCAL_CLI}
                  onCheckedChange={(v) =>
                    update("CLAUDE_SMART_USE_LOCAL_CLI", v)
                  }
                />
              </div>

              <div className="flex items-start justify-between gap-4">
                <div className="min-w-0">
                  <Label htmlFor="use-local-embed">
                    CLAUDE_SMART_USE_LOCAL_EMBEDDING
                  </Label>
                  <p className="text-xs text-muted-foreground mt-0.5">
                    Use in-process ONNX embedder (offline-friendly).
                  </p>
                </div>
                <Switch
                  id="use-local-embed"
                  checked={!!config.CLAUDE_SMART_USE_LOCAL_EMBEDDING}
                  onCheckedChange={(v) =>
                    update("CLAUDE_SMART_USE_LOCAL_EMBEDDING", v)
                  }
                />
              </div>

              <div className="space-y-2">
                <Label>CLAUDE_SMART_CLI_PATH</Label>
                <Input
                  value={String(config.CLAUDE_SMART_CLI_PATH ?? "")}
                  onChange={(e) => update("CLAUDE_SMART_CLI_PATH", e.target.value)}
                  className="font-mono text-xs"
                  placeholder="(empty — auto-detect via $PATH)"
                />
              </div>

              <div className="space-y-2">
                <Label>CLAUDE_SMART_CLI_TIMEOUT</Label>
                <Input
                  value={String(config.CLAUDE_SMART_CLI_TIMEOUT ?? "")}
                  onChange={(e) =>
                    update("CLAUDE_SMART_CLI_TIMEOUT", e.target.value)
                  }
                  className="font-mono text-xs"
                  placeholder="120"
                />
              </div>

              <div className="space-y-2">
                <Label>CLAUDE_SMART_STATE_DIR</Label>
                <Input
                  value={String(config.CLAUDE_SMART_STATE_DIR ?? "")}
                  onChange={(e) =>
                    update("CLAUDE_SMART_STATE_DIR", e.target.value)
                  }
                  className="font-mono text-xs"
                  placeholder="(empty — default ~/.claude-smart/sessions)"
                />
              </div>
            </div>
          ) : null}
        </section>

        <Separator />

        <section className="space-y-4">
          <div>
            <h2 className="text-sm font-semibold">Claude Code hook environment</h2>
            <p className="text-xs text-muted-foreground">
              Writes to{" "}
              <code className="font-mono">.claude/settings.local.json</code>.
              Hook-side env changes apply to new Claude Code sessions.
            </p>
          </div>

          {hookConfig === null && !error ? (
            <div className="text-sm text-muted-foreground">Loading…</div>
          ) : hookConfig ? (
            <div className="space-y-5">
              <div className="flex items-start justify-between gap-4">
                <div className="min-w-0">
                  <Label htmlFor="enable-optimizer">
                    CLAUDE_SMART_ENABLE_OPTIMIZER
                  </Label>
                  <p className="text-xs text-muted-foreground mt-0.5">
                    Enable shared skill optimization and rollups during
                    SessionStart.
                  </p>
                </div>
                <Switch
                  id="enable-optimizer"
                  checked={!!hookConfig.CLAUDE_SMART_ENABLE_OPTIMIZER}
                  onCheckedChange={updateOptimizer}
                />
              </div>
              <div className="rounded-md border border-border bg-muted/20 p-3 text-xs text-muted-foreground space-y-2">
                <div className="flex items-center justify-between gap-4">
                  <span>Effective value</span>
                  <span className="font-medium text-foreground">
                    {formatSettingValue(hookConfig.effectiveValue)}
                  </span>
                </div>
                <div className="flex items-center justify-between gap-4">
                  <span>Project override</span>
                  <span className="font-medium text-foreground">
                    {formatSettingValue(hookConfig.localValue)}
                  </span>
                </div>
                <div className="flex items-center justify-between gap-4">
                  <span>User setting</span>
                  <span className="font-medium text-foreground">
                    {formatSettingValue(hookConfig.userValue)}
                  </span>
                </div>
                <div className="pt-1 space-y-1">
                  <p>
                    Project file:{" "}
                    <code className="font-mono break-all">
                      {hookConfig.settingsPath}
                    </code>
                  </p>
                  <p>
                    User file:{" "}
                    <code className="font-mono break-all">
                      {hookConfig.userSettingsPath}
                    </code>
                  </p>
                </div>
              </div>
            </div>
          ) : null}
        </section>
      </div>
    </>
  );
}

function formatSettingValue(value: boolean | null): string {
  if (value === null) return "Not set";
  return value ? "Enabled" : "Disabled";
}
