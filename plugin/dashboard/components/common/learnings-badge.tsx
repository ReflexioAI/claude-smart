import { Sparkles } from "lucide-react";
import { Badge } from "@/components/ui/badge";
import { cn } from "@/lib/utils";

export function LearningsBadge({
  count,
  size = "md",
  className,
}: {
  count: number;
  size?: "md" | "sm";
  className?: string;
}) {
  if (count <= 0) return null;
  const isSmall = size === "sm";
  return (
    <Badge
      variant="outline"
      className={cn(
        "border-amber-500/40 gap-1",
        isSmall ? "h-4 px-1.5 text-[10px] shrink-0" : "h-5",
        className,
      )}
    >
      <Sparkles
        className={cn(
          "text-amber-500",
          isSmall ? "h-2.5 w-2.5" : "h-3 w-3",
        )}
      />
      {count} learning applied
    </Badge>
  );
}
