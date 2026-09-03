import type { Metadata } from "next";
import "./globals.css";

export const metadata: Metadata = {
  title: "Purse — one purse, every agent opens it",
  description:
    "A portable, self-hostable vault for agent memory, skills, and API keys, exposed through a single MCP URL. Open source, Apache 2.0.",
};

// Apply the saved theme before first paint to avoid a flash. Dark is default.
const themeInit = `(function(){try{var t=localStorage.getItem("purse-theme");if(t==="light"||t==="dark"){document.documentElement.setAttribute("data-theme",t);}}catch(e){}})();`;

export default function RootLayout({
  children,
}: Readonly<{ children: React.ReactNode }>) {
  return (
    <html lang="en" data-theme="dark" suppressHydrationWarning>
      <head>
        <script dangerouslySetInnerHTML={{ __html: themeInit }} />
      </head>
      <body>{children}</body>
    </html>
  );
}
