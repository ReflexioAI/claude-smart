import { Sparkles } from "lucide-react";
import { Badge } from "@/components/ui/badge";
import { formatRelative } from "@/lib/format";
import type { PlaybookApplicationStat } from "@/lib/types";

export function LearningApplicationBadge({
  stat,
}: {
  stat: PlaybookApplicationStat | undefined;
}) {
  if (!stat || stat.applied_count === 0) {
    return (
      <Badge
        variant="outline"
        className="h-5 text-[10px] text-muted-foreground"
        title="No citations recorded yet for this learning. It will count once an assistant reply cites it."
      >
        Never applied
      </Badge>
    );
  }

  const last = formatRelative(stat.last_applied_at);
  return (
    <Badge
      variant="outline"
      className="h-5 gap-1 border-amber-500/45 bg-amber-500/10 text-[10px] text-amber-700 dark:text-amber-300"
      title={`Last applied ${last}`}
    >
      <Sparkles className="h-2.5 w-2.5 text-amber-500" />
      Applied {stat.applied_count}×{stat.last_applied_at ? ` · ${last}` : ""}
    </Badge>
  );
}
