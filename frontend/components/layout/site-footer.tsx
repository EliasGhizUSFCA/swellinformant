import Link from "next/link";

import { cn } from "@/lib/utils";

// Not a client module, so the server-rendered landing page shows it without loading AppShell.
export function SiteFooter({ dark = false }: { dark?: boolean }) {
  return (
    <footer className={cn("border-t", dark ? "border-white/10 bg-ink text-white/70" : "bg-card text-muted-foreground")}>
      <div className="mx-auto flex max-w-7xl flex-col gap-3 px-4 py-6 text-xs sm:flex-row sm:items-center sm:justify-between sm:px-6">
        <p>
          Forecasts are model estimates (NOAA GFS and GFS-Wave by default; other models only where this site is set up for them; synthetic data in
          demo mode), not observations. Breaking heights and quality scores are approximate. Fares are quotes and can change.
        </p>
        <div className="flex gap-4">
          <Link href="/how-to-use" className="hover:underline">
            How to use
          </Link>
          <Link href="/status" className="hover:underline">
            System status
          </Link>
          <Link href="/spots" className="hover:underline">
            Spots
          </Link>
        </div>
      </div>
    </footer>
  );
}
