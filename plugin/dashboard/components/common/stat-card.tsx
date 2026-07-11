import { cn } from "@/lib/utils";
import type { LucideIcon } from "lucide-react";

export function StatCard({
  label,
  value,
  hint,
  icon: Icon,
  className,
  tone = "default",
}: {
  label: string;
  value: string | number;
  hint?: string;
  icon?: LucideIcon;
  className?: string;
  tone?: "default" | "warning" | "danger";
}) {
  return (
    <div
      className={cn(
        "rounded-lg border border-border bg-card/92 px-5 py-4 flex items-start justify-between gap-4 shadow-sm",
        tone === "warning" && "border-amber-500/70 bg-amber-500/10",
        tone === "danger" && "border-destructive/70 bg-destructive/10",
        className,
      )}
    >
      <div className="min-w-0">
        <div
          className={cn(
            "text-xs uppercase text-muted-foreground font-semibold",
            tone === "warning" && "text-amber-700 dark:text-amber-400",
            tone === "danger" && "text-destructive",
          )}
        >
          {label}
        </div>
        <div
          className={cn(
            "mt-2 text-3xl font-semibold tabular-nums text-foreground",
            tone === "warning" && "text-amber-700 dark:text-amber-400",
            tone === "danger" && "text-destructive",
          )}
        >
          {value}
        </div>
        {hint && (
          <div
            className={cn(
              "text-xs text-muted-foreground mt-1.5",
              tone === "warning" && "text-amber-700/80 dark:text-amber-400/80",
              tone === "danger" && "text-destructive/80",
            )}
          >
            {hint}
          </div>
        )}
      </div>
      {Icon && (
        <div
          className={cn(
            "flex h-9 w-9 shrink-0 items-center justify-center rounded-lg border border-primary/15 bg-primary/10 text-primary",
            tone === "warning" &&
              "border-amber-500/25 bg-amber-500/15 text-amber-700 dark:text-amber-400",
            tone === "danger" &&
              "border-destructive/25 bg-destructive/15 text-destructive",
          )}
        >
          <Icon className="h-4 w-4" />
        </div>
      )}
    </div>
  );
}
