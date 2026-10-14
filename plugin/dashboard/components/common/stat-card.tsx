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
        "min-w-0 px-5 py-5 flex items-start justify-between gap-3",
        className,
      )}
    >
      <div className="min-w-0">
        <div className="text-sm text-muted-foreground">
          {label}
        </div>
        <div className="mt-2 text-[1.75rem] leading-tight tracking-tight font-semibold tabular-nums text-foreground">
          {value}
        </div>
        {hint && (
          <div className="text-xs text-muted-foreground mt-1.5">{hint}</div>
        )}
      </div>
      {Icon && (
        <Icon className="mt-0.5 h-4 w-4 shrink-0 text-muted-foreground" aria-hidden="true" />
      )}
    </div>
  );
}
