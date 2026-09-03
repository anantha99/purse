import Link from "next/link";
import CopyButton from "@/components/CopyButton";
import ThemeToggle from "@/components/ThemeToggle";

export default function LandingPage() {
  const mcpUrl = process.env.PURSE_MCP_URL || "https://your-vault.dev/mcp";

  return (
    <main>
      <header className="doc-head-bar">
        <div
          className="wrap"
          style={{
            display: "flex",
            justifyContent: "space-between",
            alignItems: "center",
            paddingTop: 22,
            paddingBottom: 22,
          }}
        >
          <span className="clasp">
            <span className="dot" />
            PURSE
          </span>
          <div style={{ display: "flex", gap: 12, alignItems: "center" }}>
            <ThemeToggle />
            <Link className="btn btn-ghost" href="/login">
              Sign in
            </Link>
          </div>
        </div>
      </header>

      <section className="landing">
        <div className="wrap">
          <div className="eyebrow" style={{ marginBottom: 22 }}>
            Open-source · Apache&nbsp;2.0 · self-host in one command
          </div>
          <h2>
            One purse.
            <br />
            <span className="thin">Every agent opens it.</span>
          </h2>
          <p className="lede">
            A portable vault for agent <b>memory</b>, <b>skills</b>, and{" "}
            <b>API keys</b> — exposed through a single MCP URL. Chrome
            doesn&apos;t sync passwords with Safari; both ask the same vault.
            Purse does that for your agents. <b>Keys never enter model context.</b>
          </p>

          <div className="url-hero">
            <span className="label">MCP</span>
            <span className="val">{mcpUrl}</span>
            <CopyButton value={mcpUrl} className="copy" />
          </div>

          <div className="cta-row">
            <a
              className="btn btn-primary"
              href="https://github.com/purse-dev/purse"
              target="_blank"
              rel="noreferrer noopener"
            >
              Self-host it <span className="g">docker compose up</span>
            </a>
            <a
              className="btn btn-ghost"
              href="https://github.com/purse-dev/purse"
              target="_blank"
              rel="noreferrer noopener"
            >
              Star on GitHub <span className="g num">★ 1.2k</span>
            </a>
          </div>

          <div className="trust">
            <span>Verbatim canonical store</span>
            <span>Provenance on every write</span>
            <span>Proxy-only secrets</span>
            <span>Full JSON export, always</span>
          </div>
        </div>
      </section>

      <footer className="foot">
        <div className="wrap">
          <div className="eyebrow" style={{ marginBottom: 10 }}>
            Notes
          </div>
          <p className="mono" style={{ color: "var(--faint)", fontSize: 12 }}>
            Open source under Apache 2.0. Self-host the whole vault with{" "}
            <b style={{ color: "var(--muted)" }}>docker compose up</b>. Your keys
            stay in your instance and never enter model context.
          </p>
        </div>
      </footer>
    </main>
  );
}
