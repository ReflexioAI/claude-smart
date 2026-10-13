"use client";

import NextLink from "next/link";
import type { ComponentProps } from "react";
import { useSettings } from "@/hooks/use-settings";

/** Preserve the viewed backend in deep links, refreshes, and new tabs. */
export default function SourceLink(props: ComponentProps<typeof NextLink>) {
  const { source } = useSettings();
  let href = props.href;
  if (
    source &&
    typeof href === "string" &&
    href.startsWith("/") &&
    !href.startsWith("//")
  ) {
    const url = new URL(href, "http://dashboard");
    url.searchParams.set("source", source);
    href = `${url.pathname}${url.search}${url.hash}`;
  }
  return <NextLink {...props} href={href} />;
}
