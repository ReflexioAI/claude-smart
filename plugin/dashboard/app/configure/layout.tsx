"use client";

import { usePathname } from "next/navigation";
import { Server, SlidersHorizontal } from "lucide-react";
import { PageTabs } from "@/components/common/page-tabs";
import { PageFrame } from "@/components/common/page-frame";

const tabs = [
  {
    id: "env",
    href: "/configure/env",
    label: "Environment",
    description: "Local plugin paths and runtime flags",
    icon: SlidersHorizontal,
  },
  {
    id: "server",
    href: "/configure/server",
    label: "Reflexio server",
    description: "Extraction settings from the backend",
    icon: Server,
  },
];

export default function ConfigureLayout({
  children,
}: {
  children: React.ReactNode;
}) {
  const pathname = usePathname();

  return (
    <div className="flex-1 flex flex-col overflow-hidden">
      <div className="border-b border-border bg-background">
        <PageFrame className="py-3">
          <PageTabs
            items={tabs}
            activeId={
              tabs.find(
                (tab) =>
                  pathname === tab.href || pathname.startsWith(`${tab.href}/`),
              )?.id ?? "env"
            }
          />
        </PageFrame>
      </div>
      <div className="flex-1 overflow-auto">{children}</div>
    </div>
  );
}
