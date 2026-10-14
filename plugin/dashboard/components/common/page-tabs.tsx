"use client";

import Link from "@/components/common/source-link";
import type { LucideIcon } from "lucide-react";
import { cn } from "@/lib/utils";

export interface PageTabItem {
  id: string;
  label: string;
  description?: string;
  count?: number;
  href?: string;
  icon?: LucideIcon;
}

interface PageTabsProps {
  items: PageTabItem[];
  activeId: string;
  onSelect?: (id: string) => void;
  className?: string;
}

export function PageTabs({
  items,
  activeId,
  onSelect,
  className,
}: PageTabsProps) {
  return (
    <div
      className={cn(
        "flex flex-wrap gap-x-6 gap-y-2",
        className,
      )}
      role="tablist"
    >
      {items.map((item) => {
        const active = item.id === activeId;
        const Icon = item.icon;
        const content = (
          <>
            <span className="flex items-start justify-between gap-3">
              <span className="flex min-w-0 items-center gap-2">
                {Icon && (
                  <Icon
                    className={cn(
                      "mt-0.5 h-4 w-4 shrink-0",
                      active ? "text-foreground" : "text-muted-foreground",
                    )}
                  />
                )}
                <span className="truncate text-sm font-semibold">
                  {item.label}
                </span>
              </span>
              {item.count !== undefined && (
                <span
                  className={cn(
                    "rounded-md border px-1.5 py-0.5 font-mono text-[10px]",
                    active
                      ? "border-foreground/20 bg-background text-foreground"
                      : "border-border bg-muted/40 text-muted-foreground",
                  )}
                >
                  {item.count}
                </span>
              )}
            </span>
            {item.description && (
              <span className="mt-1 block text-xs leading-relaxed text-muted-foreground">
                {item.description}
              </span>
            )}
          </>
        );

        const className = cn(
          "relative border-b-2 px-0 py-3 text-left transition-colors",
          "hover:text-foreground focus-visible:outline-2 focus-visible:outline-ring focus-visible:outline-offset-2",
          active
            ? "border-primary text-foreground"
            : "border-transparent text-muted-foreground",
        );

        if (item.href) {
          return (
            <Link
              key={item.id}
              href={item.href}
              className={className}
              role="tab"
              aria-selected={active}
              aria-current={active ? "page" : undefined}
            >
              {content}
            </Link>
          );
        }

        return (
          <button
            key={item.id}
            type="button"
            className={className}
            role="tab"
            aria-selected={active}
            onClick={() => onSelect?.(item.id)}
          >
            {content}
          </button>
        );
      })}
    </div>
  );
}
