"use client";

import { useTheme } from "next-themes";
import Image from "next/image";
import { ExternalLink, Menu, Moon, Sun } from "lucide-react";
import { Button } from "@/components/ui/button";
import { useSettings } from "@/hooks/use-settings";
import { Sheet, SheetContent, SheetTitle, SheetTrigger } from "@/components/ui/sheet";
import { Sidebar } from "./sidebar";

export function TopBar() {
  const { reflexioUrl, source, sources, changeSource } = useSettings();
  const { theme, setTheme } = useTheme();
  let endpoint: URL | null = null;
  try {
    const parsed = new URL(reflexioUrl);
    if (["http:", "https:"].includes(parsed.protocol)) endpoint = parsed;
  } catch {
    // Connection metadata may still be loading or not configured.
  }

  return (
    <header className="min-h-14 py-2 border-b border-border bg-background flex items-center px-4 gap-3 shrink-0">
      <Sheet>
        <SheetTrigger
          render={<Button variant="ghost" size="icon" className="lg:hidden" aria-label="Open navigation" />}
        >
          <Menu className="h-4 w-4" />
        </SheetTrigger>
        <SheetContent side="left" className="w-72 p-0">
          <SheetTitle className="sr-only">Navigation</SheetTitle>
          <Sidebar />
        </SheetContent>
      </Sheet>

      <div className="flex flex-wrap items-center gap-2 flex-1 min-w-0">
        <div className="flex h-9 w-9 shrink-0 items-center justify-center">
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
            className="h-8 min-w-0 max-w-full rounded-md border border-input bg-background px-2"
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
        <div className="flex min-w-0 basis-full flex-wrap items-center gap-x-4 gap-y-2 text-xs sm:basis-auto">
          <span
            id="reflexio-url"
            className="min-w-0 truncate text-muted-foreground"
            title={reflexioUrl}
          >
            {endpoint
              ? `Backend: ${endpoint.host.replace(/^www\./, "")}`
              : "Connection not configured"}
          </span>
          {source === "managed" && sources?.managed.configured && endpoint && (
            <a
              href={new URL("/dashboard", endpoint).href}
              target="_blank"
              rel="noopener noreferrer"
              className="inline-flex items-center gap-1 text-primary underline-offset-4 hover:underline focus-visible:underline"
            >
              Open cloud dashboard
              <ExternalLink className="h-3 w-3" aria-hidden="true" />
            </a>
          )}
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
