import type { HTMLAttributes } from "react";
import { cn } from "@/lib/utils";

/** Shared page gutters and width, matching the managed Reflexio workspace. */
export function PageFrame({ className, ...props }: HTMLAttributes<HTMLDivElement>) {
  return <div className={cn("page-frame pb-10", className)} {...props} />;
}
