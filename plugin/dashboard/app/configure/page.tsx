import { redirect } from "next/navigation";

export default async function ConfigurePage({
  searchParams,
}: {
  searchParams: Promise<{ source?: string }>;
}) {
  const { source } = await searchParams;
  redirect(
    source === "local" || source === "managed"
      ? `/configure/env?source=${source}`
      : "/configure/env",
  );
}
