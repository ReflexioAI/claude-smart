import Link from "next/link";
import { redirect } from "next/navigation";
import { AlertTriangle, ArrowLeft } from "lucide-react";
import { PageHeader } from "@/components/common/page-header";
import { EmptyState } from "@/components/common/empty-state";
import { Button } from "@/components/ui/button";
import { resolveRuleLink } from "@/lib/session-reader";

export const dynamic = "force-dynamic";

export default async function RuleResolverPage({
  params,
}: {
  params: Promise<{ id: string }>;
}) {
  const { id: rawId } = await params;
  let id: string;
  try {
    id = decodeURIComponent(rawId);
  } catch {
    id = rawId;
  }
  const resolved = await resolveRuleLink(id);
  if (resolved) {
    redirect(resolved.href);
  }

  return (
    <div className="flex-1 overflow-auto">
      <PageHeader
        title="Rule link not found"
        description="This short claude-smart link could not be resolved from the local session registry."
      />
      <div className="p-6 max-w-2xl mx-auto">
        <EmptyState
          icon={AlertTriangle}
          title="Rule link not found"
          description="The session may have been cleared, or the short link may come from a different machine."
          action={
            <Link href="/sessions">
              <Button variant="outline" size="sm">
                <ArrowLeft className="h-3.5 w-3.5" />
                Back to sessions
              </Button>
            </Link>
          }
        />
      </div>
    </div>
  );
}
