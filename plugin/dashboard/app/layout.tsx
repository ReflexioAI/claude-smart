import type { Metadata } from "next";
import { Inter } from "next/font/google";
import "./globals.css";
import { Providers } from "./providers";
import { Sidebar } from "@/components/layout/sidebar";
import { TopBar } from "@/components/layout/top-bar";

const inter = Inter({
  variable: "--font-sans",
  subsets: ["latin"],
});

export const metadata: Metadata = {
  title: "Claude-Smart Dashboard",
  description: "Manage sessions, profiles, playbooks, and configuration",
};

export default function RootLayout({
  children,
}: Readonly<{
  children: React.ReactNode;
}>) {
  return (
    <html lang="en" className={`${inter.variable} h-full`} suppressHydrationWarning>
      <body className="h-full flex flex-col antialiased font-sans">
        <Providers>
          <TopBar />
          <div className="flex flex-1 min-h-0">
            <aside className="hidden lg:block w-60 border-r border-border shrink-0">
              <Sidebar />
            </aside>
            <main className="flex-1 min-w-0 flex flex-col">{children}</main>
          </div>
        </Providers>
      </body>
    </html>
  );
}
