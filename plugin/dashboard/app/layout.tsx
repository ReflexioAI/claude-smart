import type { Metadata } from "next";
import "./globals.css";
import { Providers } from "./providers";
import { Sidebar } from "@/components/layout/sidebar";
import { TopBar } from "@/components/layout/top-bar";
import { StallBanner } from "@/components/stall-banner";

export const metadata: Metadata = {
  title: "Claude-Smart Dashboard",
  description: "Manage sessions, preferences, skills, and configuration",
};

export default function RootLayout({
  children,
}: Readonly<{
  children: React.ReactNode;
}>) {
  return (
    <html
      lang="en"
      className="h-full"
      suppressHydrationWarning
    >
      <body
        className="h-full flex flex-col antialiased font-sans"
        suppressHydrationWarning
      >
        <Providers>
          <StallBanner />
          <TopBar />
          <div className="flex flex-1 min-h-0">
            <aside className="hidden lg:block w-56 border-r border-sidebar-border bg-sidebar shrink-0">
              <Sidebar />
            </aside>
            <main className="flex-1 min-w-0 flex flex-col bg-background">
              {children}
            </main>
          </div>
          <footer className="shrink-0 border-t border-border bg-background px-4 py-2 text-xs text-muted-foreground flex items-center justify-center gap-1.5">
            <span>Powered by</span>
            <a
              href="https://github.com/ReflexioAI/reflexio"
              target="_blank"
              rel="noopener noreferrer"
              className="font-medium hover:text-foreground underline-offset-2 hover:underline"
            >
              reflexio
            </a>
            <span aria-hidden>·</span>
            <a
              href="https://github.com/ReflexioAI/reflexio"
              target="_blank"
              rel="noopener noreferrer"
              className="hover:text-foreground underline-offset-2 hover:underline"
            >
              ⭐ Star on GitHub
            </a>
          </footer>
        </Providers>
      </body>
    </html>
  );
}
