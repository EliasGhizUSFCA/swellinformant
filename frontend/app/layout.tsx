import "@fontsource-variable/inter";
import "@fontsource-variable/fraunces";
import "./globals.css";

import type { Metadata, Viewport } from "next";

import { Providers } from "@/components/providers";

// The deployment's public URL (NEXT_PUBLIC_APP_URL, the backend's APP_BASE_URL): the base of
// absolute metadata URLs. Unset or malformed = Next.js's own default.
function appUrl(): URL | null {
  const raw = process.env.NEXT_PUBLIC_APP_URL?.trim();
  if (!raw) return null;
  try {
    return new URL(raw);
  } catch {
    return null;
  }
}

export const metadata: Metadata = {
  metadataBase: appUrl(),
  title: { default: "Swell Travel Agent — Find the swell, not just the destination", template: "%s · Swell Travel Agent" },
  description:
    "Swell Travel Agent watches 50 world-class surf breaks, detects promising swells about a week ahead and matches them with flights that fit your budget.",
  icons: { icon: "/favicon.svg" },
};

export const viewport: Viewport = { themeColor: "#06263a" };

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="en">
      <body className="min-h-dvh">
        <Providers>{children}</Providers>
      </body>
    </html>
  );
}
