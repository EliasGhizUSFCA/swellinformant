import "@fontsource-variable/inter";
import "@fontsource-variable/fraunces";
import "./globals.css";

import type { Metadata, Viewport } from "next";

import { Providers } from "@/components/providers";

export const metadata: Metadata = {
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
