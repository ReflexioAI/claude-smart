import { redirect } from "next/navigation";

export default async function RootPage({
  searchParams,
}: {
  searchParams: Promise<{ source?: string }>;
}) {
  const { source } = await searchParams;
  redirect(
    source === "local" || source === "managed"
      ? `/dashboard?source=${source}`
      : "/dashboard",
  );
}
