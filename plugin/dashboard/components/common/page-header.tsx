import { cn } from "@/lib/utils";

export function PageHeader({
  title,
  description,
  actions,
  className,
}: {
  title: string;
  description?: React.ReactNode;
  actions?: React.ReactNode;
  className?: string;
}) {
  return (
    <header
      className={cn(
        "page-frame pt-7 pb-5 lg:pt-9 flex flex-wrap items-start justify-between gap-4",
        className,
      )}
    >
      <div className="min-w-[min(18rem,100%)] flex-1">
        <h1 className="text-[1.65rem] leading-tight tracking-[-0.02em] font-semibold">{title}</h1>
        {description && (
          <div className="text-sm text-muted-foreground mt-1 max-w-3xl">
            {description}
          </div>
        )}
      </div>
      {actions && (
        <div className="flex max-w-full flex-wrap items-center justify-end gap-2">
          {actions}
        </div>
      )}
    </header>
  );
}
