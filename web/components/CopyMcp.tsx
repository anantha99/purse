"use client";

import { useState } from "react";

// The "MCP copy url" pill from the dashboard topbar (boards 03/04).
export default function CopyMcp({ url }: { url: string }) {
  const [copied, setCopied] = useState(false);

  async function copy() {
    try {
      await navigator.clipboard.writeText(url);
    } catch {
      // ignore
    }
    setCopied(true);
    setTimeout(() => setCopied(false), 1600);
  }

  return (
    <button
      type="button"
      className="copy-url"
      onClick={copy}
      title={url}
      aria-label={`Copy MCP URL ${url}`}
    >
      MCP&nbsp; <b>{copied ? "copied" : "copy url"}</b>
    </button>
  );
}
