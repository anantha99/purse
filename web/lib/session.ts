// Central place for the BFF session cookie contract.

export const SESSION_COOKIE = "purse_session";

export function cookieSecure(): boolean {
  return (
    process.env.NODE_ENV === "production" ||
    process.env.PURSE_COOKIE_SECURE === "1"
  );
}

/** Options for the httpOnly, SameSite=Lax session cookie on the frontend origin. */
export function sessionCookieOptions(maxAgeSeconds: number) {
  return {
    httpOnly: true,
    sameSite: "lax" as const,
    secure: cookieSecure(),
    path: "/",
    maxAge: maxAgeSeconds,
  };
}

// ~12h to match the backend session token expiry.
export const SESSION_MAX_AGE = 12 * 60 * 60;
