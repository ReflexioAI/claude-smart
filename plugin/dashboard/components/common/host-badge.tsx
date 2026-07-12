import { Terminal } from "lucide-react";
import { Badge } from "@/components/ui/badge";
import { cn } from "@/lib/utils";
import type { Host } from "@/lib/types";

const HOST_LABELS: Record<Exclude<Host, "unknown">, string> = {
  "claude-code": "Claude Code",
  codex: "Codex",
  opencode: "OpenCode",
};

export function HostBadge({
  host,
  unavailable = false,
  size = "md",
  className,
}: {
  host: Host | null;
  unavailable?: boolean;
  size?: "md" | "sm";
  className?: string;
}) {
  const compact = size === "sm";
  const sizeClass = compact ? "h-4 px-1.5 text-[10px]" : "h-5";
  if (unavailable) {
    return (
      <Badge
        variant="outline"
        className={cn(
          "border-dashed text-muted-foreground",
          sizeClass,
          className,
        )}
        title="Host unavailable: shared skill aggregation lineage is not recorded"
      >
        —
      </Badge>
    );
  }

  const knownHost = host && host !== "unknown" ? host : null;
  const label = knownHost ? HOST_LABELS[knownHost] : "Host unknown";
  return (
    <Badge
      variant="outline"
      className={cn(
        "gap-1",
        knownHost
          ? "border-blue-500/45 bg-blue-500/10 text-blue-700 dark:text-blue-300"
          : "border-dashed text-muted-foreground",
        sizeClass,
        className,
      )}
      title={knownHost ? `Produced by ${label}` : "Host was not recorded"}
    >
      <Terminal
        className={cn(
          knownHost ? "text-blue-500" : "text-muted-foreground",
          compact ? "h-2.5 w-2.5" : "h-3 w-3",
        )}
      />
      {label}
    </Badge>
  );
}
