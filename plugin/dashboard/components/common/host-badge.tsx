import { Terminal } from "lucide-react";
import { Badge } from "@/components/ui/badge";
import { cn } from "@/lib/utils";
import type { Host } from "@/lib/types";

const HOST_LABELS: Record<Exclude<Host, "unknown">, string> = {
  "claude-code": "Claude Code",
  codex: "Codex",
  opencode: "OpenCode",
};

// Exact product colors from the current official app assets. Label colors meet
// WCAG AA for the badge's small text: Claude 5.65:1, Codex 5.39:1, OpenCode 18.93:1.
const HOST_STYLES: Record<Exclude<Host, "unknown">, string> = {
  "claude-code": "border-[#D97757] bg-[#D97757] text-[#2A120B]",
  codex: "border-[#0169CC] bg-[#0169CC] text-white",
  opencode:
    "border-[#131010] bg-[#131010] text-white dark:border-white/30",
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
          ? HOST_STYLES[knownHost]
          : "border-dashed text-muted-foreground",
        sizeClass,
        className,
      )}
      title={knownHost ? `Produced by ${label}` : "Host was not recorded"}
    >
      <Terminal
        className={cn(
          knownHost ? "text-current" : "text-muted-foreground",
          compact ? "h-2.5 w-2.5" : "h-3 w-3",
        )}
      />
      {label}
    </Badge>
  );
}
