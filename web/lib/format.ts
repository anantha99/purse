export function relativeTime(iso: string): string {
  const then = new Date(iso).getTime();
  if (Number.isNaN(then)) return "";
  const secs = Math.max(1, Math.floor((Date.now() - then) / 1000));
  const mins = Math.floor(secs / 60);
  const hours = Math.floor(mins / 60);
  const days = Math.floor(hours / 24);
  const weeks = Math.floor(days / 7);
  if (secs < 60) return "just now";
  if (mins < 60) return `${mins}m ago`;
  if (hours < 24) return `${hours}h ago`;
  if (days < 7) return `${days}d ago`;
  if (weeks < 5) return `${weeks}w ago`;
  return new Date(iso).toLocaleDateString();
}

export function initiatedLabel(by: string): string {
  return by === "agent" ? "agent-initiated" : "user-initiated";
}

// Turn the backend's raw auth_mode enum into the mockup's readable form.
export function formatAuthMode(mode: string): string {
  switch (mode) {
    case "pat":
      return "token";
    case "oauth_cimd":
      return "oauth · cimd";
    case "oauth_dcr":
      return "oauth · dcr";
    case "oauth_static":
      return "oauth · static";
    default:
      return mode;
  }
}
