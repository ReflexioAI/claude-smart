import { cn } from "@/lib/utils";
import type { LucideIcon } from "lucide-react";

export function StatCard({
  label,
  value,
  hint,
  icon: Icon,
  className,
}: {
  label: string;
  value: string | number;
  hint?: string;
  icon?: LucideIcon;
  className?: string;
}) {
  return (
    <div
      className={cn(
        "rounded-lg border border-border bg-card/92 px-5 py-4 flex items-start justify-between gap-4 shadow-sm",
        className,
      )}
    >
      <div className="min-w-0">
        <div className="text-xs uppercase text-muted-foreground font-semibold">
          {label}
        </div>
        <div className="mt-2 text-3xl font-semibold tabular-nums text-foreground">
          {value}
        </div>
        {hint && (
          <div className="text-xs text-muted-foreground mt-1.5">{hint}</div>
        )}
      </div>
      {Icon && (
        <div className="flex h-9 w-9 shrink-0 items-center justify-center rounded-lg border border-primary/15 bg-primary/10 text-primary">
          <Icon className="h-4 w-4" />
        </div>
      )}
    </div>
  );
}
