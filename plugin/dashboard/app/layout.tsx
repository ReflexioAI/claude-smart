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
            <aside className="hidden lg:block w-64 border-r border-sidebar-border bg-sidebar/95 shrink-0">
              <Sidebar />
            </aside>
            <main className="flex-1 min-w-0 flex flex-col bg-background/88">
              {children}
            </main>
          </div>
        </Providers>
      </body>
    </html>
  );
}
