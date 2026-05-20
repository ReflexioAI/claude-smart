"use client";

import { useEffect, useId, useState } from "react";
import { AlertTriangle, Trash2, X } from "lucide-react";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";

type DeleteLearningDangerZoneProps = {
  learningName: string;
  description: string;
  consequences: string[];
  deleting: boolean;
  disabled?: boolean;
  onDelete: () => void | Promise<void>;
};

const REQUIRED_CONFIRMATION = "DELETE";

export function DeleteLearningDangerZone({
  learningName,
  description,
  consequences,
  deleting,
  disabled = false,
  onDelete,
}: DeleteLearningDangerZoneProps) {
  const [open, setOpen] = useState(false);
  const [confirmation, setConfirmation] = useState("");
  const titleId = useId();
  const descriptionId = useId();
  const canDelete = confirmation === REQUIRED_CONFIRMATION && !deleting;

  useEffect(() => {
    if (!open) return;

    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key === "Escape" && !deleting) {
        setOpen(false);
        setConfirmation("");
      }
    };

    window.addEventListener("keydown", onKeyDown);
    return () => window.removeEventListener("keydown", onKeyDown);
  }, [deleting, open]);

  const close = () => {
    if (deleting) return;
    setOpen(false);
    setConfirmation("");
  };

  const confirmDelete = async () => {
    if (!canDelete) return;
    await onDelete();
  };

  return (
    <>
      <section className="rounded-xl border border-destructive/30 bg-destructive/5 p-4 flex items-start justify-between gap-4">
        <div className="min-w-0">
          <h3 className="text-sm font-semibold text-destructive">
            Danger zone
          </h3>
          <p className="text-xs text-muted-foreground mt-0.5">
            {description}
          </p>
        </div>
        <Button
          variant="destructive"
          size="sm"
          onClick={() => setOpen(true)}
          disabled={deleting || disabled}
        >
          <Trash2 className="h-3.5 w-3.5" />
          Delete
        </Button>
      </section>

      {open && (
        <div
          className="fixed inset-0 z-50 flex items-center justify-center bg-background/80 p-4 backdrop-blur-sm"
          role="dialog"
          aria-modal="true"
          aria-labelledby={titleId}
          aria-describedby={descriptionId}
        >
          <div className="w-full max-w-md rounded-xl border border-border bg-card shadow-lg">
            <div className="flex items-start justify-between gap-4 border-b border-border px-5 py-4">
              <div className="flex items-start gap-3">
                <div className="mt-0.5 rounded-full bg-destructive/10 p-2 text-destructive">
                  <AlertTriangle className="h-4 w-4" />
                </div>
                <div>
                  <h2 id={titleId} className="text-base font-semibold">
                    Delete learning?
                  </h2>
                  <p
                    id={descriptionId}
                    className="mt-1 text-sm text-muted-foreground"
                  >
                    This permanently deletes {learningName}.
                  </p>
                </div>
              </div>
              <Button
                type="button"
                variant="ghost"
                size="sm"
                onClick={close}
                disabled={deleting}
                aria-label="Close"
              >
                <X className="h-4 w-4" />
              </Button>
            </div>

            <div className="space-y-4 px-5 py-4">
              <ul className="list-disc space-y-1.5 pl-5 text-sm text-muted-foreground">
                {consequences.map((item) => (
                  <li key={item}>{item}</li>
                ))}
                <li>This cannot be undone.</li>
              </ul>

              <label className="block space-y-2">
                <span className="text-sm font-medium">
                  Type DELETE to confirm
                </span>
                <Input
                  value={confirmation}
                  onChange={(event) => setConfirmation(event.target.value)}
                  placeholder="DELETE"
                  autoFocus
                  disabled={deleting}
                />
              </label>
            </div>

            <div className="flex items-center justify-end gap-2 border-t border-border px-5 py-4">
              <Button
                type="button"
                variant="outline"
                size="sm"
                onClick={close}
                disabled={deleting}
              >
                Cancel
              </Button>
              <Button
                type="button"
                variant="destructive"
                size="sm"
                onClick={confirmDelete}
                disabled={!canDelete || disabled}
              >
                <Trash2 className="h-3.5 w-3.5" />
                {deleting ? "Deleting..." : "Delete learning"}
              </Button>
            </div>
          </div>
        </div>
      )}
    </>
  );
}
