"use client";

import Link from "next/link";
import { usePathname, useRouter } from "next/navigation";
import { useEffect, useState } from "react";
import { api } from "@/lib/api";
import type { WorkspaceCounts, SessionInfo } from "@/lib/types";
import ThemeToggle from "@/components/ThemeToggle";

type NavItem = {
  href: string;
  label: string;
  count?: (c: WorkspaceCounts | null) => string;
  soon?: boolean;
};

const NAV: NavItem[] = [
  { href: "/dashboard/memories", label: "Memories", count: (c) => num(c?.memories) },
  { href: "/dashboard/skills", label: "Skills", count: (c) => num(c?.skills) },
  { href: "/dashboard/apis", label: "APIs", soon: true, count: () => "soon" },
  {
    href: "/dashboard/connections",
    label: "Connections",
    count: (c) => num(c?.connections),
  },
  { href: "/dashboard/tokens", label: "Tokens", count: () => "›" },
  { href: "/dashboard/audit", label: "Audit", count: () => "›" },
];

function num(n?: number): string {
  return typeof n === "number" ? String(n) : "·";
}

export default function AppShell({ children }: { children: React.ReactNode }) {
  const pathname = usePathname();
  const router = useRouter();
  const [counts, setCounts] = useState<WorkspaceCounts | null>(null);
  const [session, setSession] = useState<SessionInfo | null>(null);
  const [open, setOpen] = useState(false);
  const [signingOut, setSigningOut] = useState(false);

  useEffect(() => {
    let alive = true;
    api
      .workspace()
      .then((c) => alive && setCounts(c))
      .catch(() => {});
    api
      .session()
      .then((s) => alive && setSession(s))
      .catch((err) => {
        // Session invalid/expired — bounce to login.
        if (err?.status === 401) router.replace("/login");
      });
    return () => {
      alive = false;
    };
  }, [router]);

  // Close the mobile drawer on navigation.
  useEffect(() => {
    setOpen(false);
  }, [pathname]);

  async function signOut() {
    setSigningOut(true);
    try {
      await api.logout();
    } catch {
      // ignore — cookie clear is best-effort
    }
    router.replace("/login");
    router.refresh();
  }

  const wsName = session?.workspace.name ?? "Personal";

  return (
    <div className="app">
      {open && (
        <div
          className="scrim side-scrim"
          onClick={() => setOpen(false)}
          aria-hidden="true"
        />
      )}
      <aside className={`side${open ? " open" : ""}`} aria-label="Primary">
        <div className="brand">
          <span className="dot" />
          <b>purse</b>
        </div>
        <div className="ws" title="Workspace">
          <span>{wsName}</span>
          <span aria-hidden="true">▾</span>
        </div>
        <nav>
          {NAV.map((item) => {
            const active = pathname === item.href;
            return (
              <Link
                key={item.href}
                href={item.href}
                className={`${active ? "active" : ""}${item.soon ? " soon" : ""}`}
                aria-current={active ? "page" : undefined}
              >
                {item.label}
                <span className="c num">{item.count?.(counts)}</span>
              </Link>
            );
          })}
        </nav>

        <div style={{ marginTop: "auto", paddingTop: 16 }}>
          <a
            className="navlink"
            href="/api/export"
            download="purse-export.json"
            style={{
              display: "flex",
              justifyContent: "space-between",
              padding: "8px 10px",
              borderRadius: 7,
              color: "var(--muted)",
              fontSize: 13.5,
              textDecoration: "none",
            }}
          >
            Export <span className="c">↓</span>
          </a>
          <button
            type="button"
            className="navlink"
            onClick={signOut}
            disabled={signingOut}
          >
            {signingOut ? "Signing out…" : "Sign out"}
            <span className="c">⏻</span>
          </button>
          <div style={{ padding: "10px 8px 2px" }}>
            <ThemeToggle />
          </div>
        </div>
      </aside>

      <div className="main">
        <div className="mobilebar">
          <button
            type="button"
            className="iconbtn"
            onClick={() => setOpen(true)}
            aria-label="Open navigation"
          >
            ☰
          </button>
          <span className="clasp" style={{ fontSize: 13 }}>
            <span className="dot" />
            purse
          </span>
        </div>
        {children}
      </div>
    </div>
  );
}
