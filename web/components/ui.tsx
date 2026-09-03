"use client";

export function Loading({ label = "Loading…" }: { label?: string }) {
  return (
    <div className="state" role="status" aria-live="polite">
      {label}
    </div>
  );
}

export function EmptyState({
  title,
  hint,
}: {
  title: string;
  hint?: string;
}) {
  return (
    <div className="state">
      {title}
      {hint && <span className="mono">{hint}</span>}
    </div>
  );
}

export function ErrorBanner({
  message,
  onRetry,
}: {
  message: string;
  onRetry?: () => void;
}) {
  return (
    <div className="errbar" role="alert">
      <span>{message}</span>
      {onRetry && (
        <button type="button" className="linkbtn" onClick={onRetry}>
          retry
        </button>
      )}
    </div>
  );
}
