"use client";

import Link from "next/link";
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
        "grid gap-2 sm:grid-cols-[repeat(auto-fit,minmax(220px,1fr))]",
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
          "relative rounded-lg border px-3 py-2.5 text-left transition-colors",
          "hover:border-foreground/25 hover:bg-accent/40 focus-visible:outline-none focus-visible:ring-3 focus-visible:ring-ring/50",
          active
            ? "border-foreground/25 bg-card text-foreground shadow-sm before:absolute before:inset-x-3 before:top-0 before:h-0.5 before:rounded-full before:bg-foreground"
            : "border-border bg-background/60 text-muted-foreground",
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
