import { Terminal } from "lucide-react";
import { Badge } from "@/components/ui/badge";
import { cn } from "@/lib/utils";
import type { Host } from "@/lib/types";

// Exact product colors from the current official app assets. Label colors meet
// WCAG AA for the badge's small text: Claude 5.65:1, Codex 5.39:1, OpenCode 18.93:1.
const HOST_META: Record<
  Exclude<Host, "unknown">,
  { label: string; badgeClass: string; dotClass: string }
> = {
  "claude-code": {
    label: "Claude Code",
    badgeClass: "border-[#D97757] bg-[#D97757] text-[#2A120B]",
    dotClass: "bg-[#D97757]",
  },
  codex: {
    label: "Codex",
    badgeClass: "border-[#0169CC] bg-[#0169CC] text-white",
    dotClass: "bg-[#0169CC]",
  },
  opencode: {
    label: "OpenCode",
    badgeClass:
      "border-[#131010] bg-[#131010] text-white dark:border-white/30",
    dotClass: "bg-[#131010] dark:bg-white",
  },
};

export function HostBadge({
  host,
  display = "badge",
  size = "md",
  className,
}: {
  host: Host | null;
  display?: "badge" | "provenance";
  size?: "md" | "sm";
  className?: string;
}) {
  const knownHost = host && host !== "unknown" ? host : null;
  const meta = knownHost ? HOST_META[knownHost] : null;
  const label = meta?.label ?? "unknown host";

  if (display === "provenance") {
    return (
      <span
        className={cn(
          "inline-flex items-center gap-1 text-[11px] text-muted-foreground",
          className,
        )}
        title={
          knownHost
            ? `This skill was learned from a ${label} session; it is not limited to that host.`
            : "The session host that produced this skill was not recorded."
        }
      >
        <span>Learned via</span>
        <span
          aria-hidden="true"
          className={cn(
            "h-1.5 w-1.5 shrink-0 rounded-full",
            meta
              ? meta.dotClass
              : "border border-dashed border-muted-foreground/70",
          )}
        />
        <span className="font-medium text-foreground/75">{label}</span>
      </span>
    );
  }

  const compact = size === "sm";
  const sizeClass = compact ? "h-4 px-1.5 text-[10px]" : "h-5";
  const badgeLabel = knownHost ? label : "Host unknown";
  return (
    <Badge
      variant="outline"
      className={cn(
        "gap-1",
        meta
          ? meta.badgeClass
          : "border-dashed text-muted-foreground",
        sizeClass,
        className,
      )}
      title={knownHost ? `Session host: ${label}` : "Session host was not recorded"}
    >
      <Terminal
        className={cn(
          knownHost ? "text-current" : "text-muted-foreground",
          compact ? "h-2.5 w-2.5" : "h-3 w-3",
        )}
      />
      {badgeLabel}
    </Badge>
  );
}
