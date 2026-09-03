"use client";

import { Suspense, useState } from "react";
import { useRouter, useSearchParams } from "next/navigation";
import { api, ApiClientError } from "@/lib/api";
import ThemeToggle from "@/components/ThemeToggle";

function LoginForm() {
  const router = useRouter();
  const params = useSearchParams();
  const next = params.get("next") || "/dashboard/memories";

  const [password, setPassword] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  async function onSubmit(e: React.FormEvent) {
    e.preventDefault();
    setError(null);
    setBusy(true);
    try {
      await api.login(password);
      router.replace(next.startsWith("/dashboard") ? next : "/dashboard/memories");
      router.refresh();
    } catch (err) {
      if (err instanceof ApiClientError && err.code === "INVALID_CREDENTIALS") {
        setError("That password didn't match. Try again.");
      } else if (err instanceof ApiClientError) {
        setError(err.message);
      } else {
        setError("Couldn't sign in. Try again in a moment.");
      }
      setBusy(false);
    }
  }

  return (
    <div className="login-stage">
      <div style={{ position: "absolute", top: 18, right: 20 }}>
        <ThemeToggle />
      </div>
      <form className="login-card" onSubmit={onSubmit}>
        <div className="mark">
          <span className="dot" />
          <b>purse</b>
        </div>
        <h3>Open your purse</h3>
        <p className="sub">Sign in to the vault running on this instance.</p>

        <div className="field">
          <label htmlFor="email">Email</label>
          <div className="input filled" id="email" aria-readonly="true">
            owner@localhost
          </div>
        </div>

        <div className="field">
          <label htmlFor="password">Password</label>
          <input
            id="password"
            className="input"
            type="password"
            autoComplete="current-password"
            value={password}
            onChange={(e) => setPassword(e.target.value)}
            placeholder="••••••••••••"
            required
            autoFocus
          />
        </div>

        {error && (
          <div className="errbar" role="alert" style={{ marginBottom: 12 }}>
            <span>{error}</span>
          </div>
        )}

        <button className="btn btn-primary" type="submit" disabled={busy}>
          {busy ? "Opening…" : "Sign in"}
        </button>

        <div className="login-note">
          Single-operator instance. Set the password on first boot with{" "}
          <b>PURSE_OWNER_PASSWORD</b>, or from the printed bootstrap credentials.
          Agents authenticate separately — with tokens, never this password.
        </div>
      </form>
    </div>
  );
}

export default function LoginPage() {
  return (
    <Suspense fallback={<div className="login-stage" />}>
      <LoginForm />
    </Suspense>
  );
}
