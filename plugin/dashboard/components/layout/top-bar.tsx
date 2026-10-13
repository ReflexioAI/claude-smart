"use client";

import { useTheme } from "next-themes";
import Image from "next/image";
import { Link2, Menu, Moon, Sun } from "lucide-react";
import { Button } from "@/components/ui/button";
import { useSettings } from "@/hooks/use-settings";
import { Sheet, SheetContent, SheetTrigger } from "@/components/ui/sheet";
import { Sidebar } from "./sidebar";

export function TopBar() {
  const { reflexioUrl, source, sources, changeSource } = useSettings();
  const { theme, setTheme } = useTheme();

  return (
    <header className="min-h-16 py-3 border-b border-border bg-card/92 backdrop-blur supports-[backdrop-filter]:bg-card/78 flex items-center px-4 gap-3 shrink-0 shadow-[0_1px_0_oklch(1_0_0/0.42)_inset] dark:shadow-none">
      <Sheet>
        <SheetTrigger
          render={<Button variant="ghost" size="icon" className="lg:hidden" />}
        >
          <Menu className="h-4 w-4" />
        </SheetTrigger>
        <SheetContent side="left" className="w-72 p-0">
          <Sidebar />
        </SheetContent>
      </Sheet>

      <div className="flex flex-wrap items-center gap-2 flex-1 min-w-0">
        <div className="flex h-9 w-9 shrink-0 items-center justify-center rounded-lg border border-primary/20 bg-primary/10">
          <Image
            src="/claude-smart-icon.png"
            alt="claude-smart"
            width={24}
            height={24}
            className="h-6 w-6"
            priority
          />
        </div>
        <div className="hidden min-w-0 sm:block">
          <div className="text-sm font-semibold leading-5">Claude-Smart</div>
        </div>
        <div className="mx-2 h-8 w-px bg-border hidden md:block" />
        <label className="flex max-w-full items-center gap-2 text-xs shrink-0">
          <span className="hidden md:inline">Data source</span>
          <select
            aria-label="Data source"
            className="h-9 min-w-0 max-w-full rounded-md border border-input bg-background px-2"
            value={source || "local"}
            onChange={(event) =>
              changeSource(event.target.value as "local" | "managed")
            }
          >
            <option value="managed">
              Managed Reflexio
              {sources?.managed.configured ? "" : " · not configured"}
            </option>
            <option value="local">Local Reflexio</option>
          </select>
        </label>
        <div
          className="relative min-w-0 max-w-md basis-full sm:basis-auto sm:flex-1 sm:w-[22rem]"
          suppressHydrationWarning
        >
          <Link2 className="pointer-events-none absolute left-2.5 top-1/2 h-3.5 w-3.5 -translate-y-1/2 text-muted-foreground" />
          <div
            id="reflexio-url"
            className="flex h-9 items-center rounded-md border border-input bg-background/80 pl-8 pr-3 text-xs font-mono text-muted-foreground"
            aria-label="Reflexio endpoint URL"
            title={reflexioUrl}
          >
            <span className="truncate">{reflexioUrl}</span>
          </div>
        </div>
      </div>

      <Button
        variant="ghost"
        size="icon"
        className="h-9 w-9"
        onClick={() => setTheme(theme === "dark" ? "light" : "dark")}
        title="Toggle theme"
        aria-label="Toggle theme"
      >
        <Sun className="h-4 w-4 rotate-0 scale-100 transition-all dark:-rotate-90 dark:scale-0" />
        <Moon className="absolute h-4 w-4 rotate-90 scale-0 transition-all dark:rotate-0 dark:scale-100" />
      </Button>
    </header>
  );
}
