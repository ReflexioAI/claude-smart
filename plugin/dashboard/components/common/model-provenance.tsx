import { AlertTriangle, Cpu } from "lucide-react";
import { cn } from "@/lib/utils";
import type { LearningModelProvenance } from "@/lib/model-lineage";

function formatProviderModel(
  provider: string | null,
  modelName: string | null,
): string | null {
  if (provider && modelName) return `${provider}/${modelName}`;
  if (modelName) return modelName;
  if (provider) return provider;
  return null;
}

function normalizeModelKey(value: string | null | undefined): string | null {
  if (!value) return null;
  return value.trim().toLowerCase().replace(/\s+/g, "");
}

function modelsConflict(
  observed: string | null,
  requested: string | null,
): boolean {
  const o = normalizeModelKey(observed);
  const r = normalizeModelKey(requested);
  if (!o || !r) return false;
  if (o === r) return false;
  // Treat provider/model and bare model as matching when one ends with the other.
  if (o.endsWith(r) || r.endsWith(o)) return false;
  const oTail = o.includes("/") ? o.split("/").pop()! : o;
  const rTail = r.includes("/") ? r.split("/").pop()! : r;
  if (oTail && rTail && oTail === rTail) return false;
  return true;
}

export function LearningModelProvenanceView({
  provenance,
  className,
}: {
  provenance: LearningModelProvenance | null;
  className?: string;
}) {
  if (!provenance) {
    return <span className={cn("text-muted-foreground", className)}>Loading…</span>;
  }

  if (provenance.unavailable) {
    return (
      <span
        className={cn("text-muted-foreground", className)}
        title={provenance.reason || "Model provenance unavailable"}
      >
        Unavailable
      </span>
    );
  }

  const observed = formatProviderModel(provenance.provider, provenance.modelName);
  const requested = provenance.requestedModel?.trim() || null;
  const mismatch = modelsConflict(observed, requested);
  const label = observed || requested || "Not recorded";
  const recorded = Boolean(observed || requested);

  const titleParts = [
    observed ? `observed=${observed}` : null,
    requested ? `requested=${requested}` : null,
    provenance.credentialLabel
      ? `credential=${provenance.credentialLabel}`
      : null,
    provenance.op ? `op=${provenance.op}` : null,
    provenance.actor ? `actor=${provenance.actor}` : null,
    mismatch
      ? "Configuration mismatch: requested model differs from observed model used to generate this learning."
      : null,
    provenance.reason || null,
  ].filter(Boolean);

  return (
    <span
      className={cn(
        "inline-flex max-w-full flex-col items-end gap-0.5 text-right",
        className,
      )}
      title={titleParts.join(" · ")}
    >
      <span className="inline-flex max-w-full items-center justify-end gap-1.5">
        {mismatch ? (
          <AlertTriangle className="h-3 w-3 shrink-0 text-amber-600 dark:text-amber-400" />
        ) : (
          <Cpu
            className={cn(
              "h-3 w-3 shrink-0",
              recorded ? "text-foreground/70" : "text-muted-foreground",
            )}
          />
        )}
        <span
          className={cn(
            "min-w-0 break-words font-mono text-[11px]",
            mismatch
              ? "text-amber-700 dark:text-amber-300"
              : recorded
                ? "text-foreground"
                : "text-muted-foreground italic",
          )}
        >
          {label}
        </span>
      </span>
      {mismatch && requested && (
        <span className="max-w-full break-words text-[10px] text-amber-700/90 dark:text-amber-300/90">
          requested {requested}
        </span>
      )}
      {!observed && requested && !mismatch && (
        <span className="max-w-full break-words text-[10px] text-muted-foreground">
          requested only
        </span>
      )}
    </span>
  );
}
