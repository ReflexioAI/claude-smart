"use client";

import { PageFrame } from "@/components/common/page-frame";

import { useEffect, useState } from "react";
import { Save, CheckCircle2 } from "lucide-react";
import { PageHeader } from "@/components/common/page-header";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Switch } from "@/components/ui/switch";
import { SETTINGS_CHANGED_EVENT } from "@/hooks/use-settings";
import type { ClaudeSmartConfig } from "@/lib/types";

export default function ConfigureEnvPage() {
  const [config, setConfig] = useState<ClaudeSmartConfig | null>(null);
  const [apiKeyDirty, setApiKeyDirty] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [saved, setSaved] = useState(false);
  const [saving, setSaving] = useState(false);

  useEffect(() => {
    fetch("/api/config", { cache: "no-store" })
      .then((r) => {
        if (!r.ok) throw new Error(`environment load failed: ${r.status}`);
        return r.json();
      })
      .then((envConfig) => {
        setConfig(envConfig);
        setApiKeyDirty(false);
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

  const save = async () => {
    if (!config) return;
    setSaving(true);
    setError(null);
    try {
      const envUpdate: Partial<ClaudeSmartConfig> = { ...config };
      if (!apiKeyDirty) delete envUpdate.REFLEXIO_API_KEY;
      delete envUpdate.REFLEXIO_API_KEY_SET;
      const envRes = await fetch("/api/config", {
        method: "PUT",
        headers: { "content-type": "application/json" },
        body: JSON.stringify(envUpdate),
      });
      if (!envRes.ok)
        throw new Error(`environment save failed: ${envRes.status}`);
      const updated: ClaudeSmartConfig = await envRes.json();
      setConfig(updated);
      window.dispatchEvent(new Event(SETTINGS_CHANGED_EVENT));
      setApiKeyDirty(false);
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
        description="Configure the active learning runtime. The dashboard data-source selector only changes what you view."
        actions={
          <Button
            onClick={save}
            disabled={!config || saving}
            size="sm"
          >
            <Save className="h-3.5 w-3.5" />
            {saving ? "Saving…" : "Save"}
          </Button>
        }
      />

      <PageFrame className="space-y-5">
        {error && (
          <div className="rounded-lg border border-destructive/30 bg-destructive/5 text-destructive px-4 py-3 text-sm">
            {error}
          </div>
        )}
        {saved && (
          <div className="rounded-lg border border-border bg-accent/40 px-4 py-2.5 text-sm flex items-center gap-2">
            <CheckCircle2 className="h-4 w-4 text-muted-foreground" />
            Saved to ~/.claude-smart/.env and Claude Code settings
          </div>
        )}

        <section className="space-y-4 border-t border-border py-5">
          <div>
            <h2 className="text-sm font-semibold">claude-smart environment</h2>
            <p className="text-xs text-muted-foreground">
              Writes to <code className="font-mono">~/.claude-smart/.env</code>.
              Unknown keys are preserved. These values are read by the Reflexio
              backend.
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
                  className="font-mono text-xs bg-background/80"
                  placeholder="http://localhost:8071/"
                />
              </div>

              <div className="space-y-2">
                <Label>REFLEXIO_API_KEY</Label>
                <Input
                  type="password"
                  value={String(config.REFLEXIO_API_KEY ?? "")}
                  onChange={(e) => {
                    setApiKeyDirty(true);
                    update("REFLEXIO_API_KEY", e.target.value);
                  }}
                  className="font-mono text-xs bg-background/80"
                  placeholder={
                    config.REFLEXIO_API_KEY_SET
                      ? "(configured - leave blank to keep existing key)"
                      : "(empty - local mode)"
                  }
                  autoComplete="off"
                />
              </div>

              <div className="flex items-start justify-between gap-4">
                <div className="min-w-0">
                  <Label htmlFor="use-local-cli">
                    CLAUDE_SMART_USE_LOCAL_CLI
                  </Label>
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
                  <Label htmlFor="read-only-mode">CLAUDE_SMART_READ_ONLY</Label>
                  <p className="text-xs text-muted-foreground mt-0.5">
                    Keep session context available but skip publishing
                    interactions to Reflexio.
                  </p>
                </div>
                <Switch
                  id="read-only-mode"
                  checked={!!config.CLAUDE_SMART_READ_ONLY}
                  onCheckedChange={(v) => update("CLAUDE_SMART_READ_ONLY", v)}
                />
              </div>

              <div className="space-y-2">
                <Label>CLAUDE_SMART_CLI_PATH</Label>
                <Input
                  value={String(config.CLAUDE_SMART_CLI_PATH ?? "")}
                  onChange={(e) =>
                    update("CLAUDE_SMART_CLI_PATH", e.target.value)
                  }
                  className="font-mono text-xs bg-background/80"
                  placeholder="(empty — auto-detect via $PATH)"
                />
              </div>

              <div className="space-y-2">
                <Label htmlFor="learning-model">Local learning model</Label>
                <Input
                  id="learning-model"
                  value={String(config.CLAUDE_SMART_LEARNING_MODEL ?? "")}
                  onChange={(event) =>
                    update("CLAUDE_SMART_LEARNING_MODEL", event.target.value)
                  }
                  placeholder="Provider default"
                />
                <p className="text-xs text-muted-foreground">
                  Model ID supported by your local learning agent. Apply changes
                  with claude-smart update, which refreshes the backend. Managed
                  Reflexio uses its server configuration.
                </p>
              </div>

              <div className="space-y-2">
                <Label>CLAUDE_SMART_CLI_TIMEOUT</Label>
                <Input
                  value={String(config.CLAUDE_SMART_CLI_TIMEOUT ?? "")}
                  onChange={(e) =>
                    update("CLAUDE_SMART_CLI_TIMEOUT", e.target.value)
                  }
                  className="font-mono text-xs bg-background/80"
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
                  className="font-mono text-xs bg-background/80"
                  placeholder="(empty — default ~/.claude-smart/sessions)"
                />
              </div>
            </div>
          ) : null}
        </section>

        <section className="space-y-2">
          <h2 className="text-sm font-semibold">Optimizer configuration</h2>
          <p className="text-xs text-muted-foreground">
            Configure optimization on the Reflexio server. Starting an editor
            session preserves the server’s enabled state, budgets and assistant backend.
          </p>
        </section>
      </PageFrame>
    </>
  );
}
