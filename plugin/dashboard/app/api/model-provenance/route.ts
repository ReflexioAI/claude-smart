import { NextResponse } from "next/server";
import {
  getLearningModelProvenance,
  type LearningEntityType,
} from "@/lib/model-lineage";

const VALID_TYPES = new Set<LearningEntityType>([
  "profile",
  "user_playbook",
  "agent_playbook",
]);

// Local dashboard data route (same pattern as /api/sessions): server reads
// filesystem-local state and returns JSON to client components.
export async function GET(req: Request) {
  const url = new URL(req.url);
  const entityType = (url.searchParams.get("entityType") || "").trim();
  const entityId = (url.searchParams.get("entityId") || "").trim();

  if (!VALID_TYPES.has(entityType as LearningEntityType)) {
    return NextResponse.json(
      {
        error:
          "entityType must be one of profile, user_playbook, agent_playbook",
      },
      { status: 400 },
    );
  }
  if (!entityId) {
    return NextResponse.json({ error: "entityId is required" }, { status: 400 });
  }

  const provenance = getLearningModelProvenance(entityType, entityId);
  return NextResponse.json({ provenance });
}
