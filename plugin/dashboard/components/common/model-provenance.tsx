import { Cpu } from "lucide-react";
import { cn } from "@/lib/utils";
import type { LearningModelProvenance } from "@/lib/model-lineage";

function label(provider: string | null, modelName: string | null): string | null {
  if (provider && modelName) return `${provider}/${modelName}`;
  return modelName || provider;
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

  const observed = label(provenance.provider, provenance.modelName);
  return (
    <span
      className={cn(
        "inline-flex max-w-full items-center justify-end gap-1.5 text-right",
        className,
      )}
      title={observed ?? provenance.reason ?? "Observed model not recorded"}
    >
      <Cpu
        className={cn(
          "h-3 w-3 shrink-0",
          observed ? "text-foreground/70" : "text-muted-foreground",
        )}
      />
      <span
        className={cn(
          "min-w-0 break-words font-mono text-[11px]",
          observed ? "text-foreground" : "text-muted-foreground italic",
        )}
      >
        {observed ?? "Not recorded"}
      </span>
    </span>
  );
}
