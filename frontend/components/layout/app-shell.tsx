"use client";

import { useQueryClient } from "@tanstack/react-query";
import { Bell, LayoutDashboard, LogOut, Map as MapIcon, Menu, Search as SearchIcon, Settings, Waves, X } from "lucide-react";
import Link from "next/link";
import { usePathname, useRouter } from "next/navigation";
import { useState } from "react";
import { toast } from "sonner";

import { Logo } from "@/components/brand/logo";
import { Button } from "@/components/ui/button";
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuLabel,
  DropdownMenuSeparator,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu";
import { useMe, useSystemStatus } from "@/hooks/queries";
import { post } from "@/lib/api";
import { cn } from "@/lib/utils";

const NAV = [
  { href: "/dashboard", label: "Dashboard", icon: LayoutDashboard, auth: true },
  { href: "/searches", label: "Searches", icon: SearchIcon, auth: true },
  { href: "/map", label: "Surf map", icon: MapIcon, auth: false },
  { href: "/spots", label: "Spots", icon: Waves, auth: false },
  { href: "/notifications", label: "Alerts", icon: Bell, auth: true },
];

function DemoRibbon() {
  const { data } = useSystemStatus();
  if (!data) return null;
  const demoForecast = data.mode.forecast_providers.includes("demo");
  const demoFlights = data.mode.flight_provider === "demo";
  if (!demoForecast && !demoFlights) return null;
  return (
    <div className="bg-amber-100 px-4 py-1.5 text-center text-xs font-medium text-amber-900" role="status">
      Demo mode — {demoForecast ? "forecasts are synthetic" : "forecasts are live"}
      {" · "}
      {demoFlights ? "flight fares are mock data" : "flight fares are live quotes"}. Nothing here is a real booking.
    </div>
  );
}

export function AppShell({ children, wide = false }: { children: React.ReactNode; wide?: boolean }) {
  const { data: user } = useMe();
  const pathname = usePathname();
  const router = useRouter();
  const queryClient = useQueryClient();
  const [open, setOpen] = useState(false);
  const items = NAV.filter((n) => !n.auth || user);

  async function signOut() {
    try {
      await post("/api/auth/logout");
    } finally {
      queryClient.clear();
      toast.success("Signed out.");
      router.push("/");
    }
  }

  return (
    <div className="flex min-h-dvh flex-col">
      <DemoRibbon />
      <header className="sticky top-0 z-40 border-b bg-card/85 backdrop-blur supports-[backdrop-filter]:bg-card/70">
        <div className="mx-auto flex h-16 max-w-7xl items-center gap-6 px-4 sm:px-6">
          <Logo href={user ? "/dashboard" : "/"} />
          <nav className="hidden items-center gap-1 md:flex" aria-label="Main">
            {items.map(({ href, label, icon: Icon }) => {
              const active = pathname === href || pathname.startsWith(`${href}/`);
              return (
                <Link
                  key={href}
                  href={href}
                  className={cn(
                    "flex items-center gap-2 rounded-lg px-3 py-2 text-sm font-medium transition-colors",
                    active ? "bg-secondary text-ink" : "text-muted-foreground hover:bg-muted hover:text-ink",
                  )}
                >
                  <Icon className="size-4" />
                  {label}
                </Link>
              );
            })}
          </nav>
          <div className="ml-auto flex items-center gap-2">
            {user ? (
              <>
                <Button asChild size="sm" className="hidden sm:inline-flex">
                  <Link href="/searches/new">New search</Link>
                </Button>
                <DropdownMenu>
                  <DropdownMenuTrigger asChild>
                    <button
                      className="flex size-9 items-center justify-center rounded-full bg-ink text-sm font-semibold text-white outline-none focus-visible:ring-[3px] focus-visible:ring-ring/40"
                      aria-label="Account menu"
                    >
                      {user.full_name.trim().charAt(0).toUpperCase()}
                    </button>
                  </DropdownMenuTrigger>
                  <DropdownMenuContent align="end">
                    <DropdownMenuLabel>
                      <div className="font-medium text-foreground">{user.full_name}</div>
                      <div className="truncate">{user.email}</div>
                    </DropdownMenuLabel>
                    <DropdownMenuSeparator />
                    <DropdownMenuItem onSelect={() => router.push("/settings")}>
                      <Settings /> Settings
                    </DropdownMenuItem>
                    <DropdownMenuItem onSelect={() => router.push("/notifications")}>
                      <Bell /> Alert history
                    </DropdownMenuItem>
                    <DropdownMenuSeparator />
                    <DropdownMenuItem onSelect={signOut}>
                      <LogOut /> Sign out
                    </DropdownMenuItem>
                  </DropdownMenuContent>
                </DropdownMenu>
              </>
            ) : (
              <>
                <Button asChild variant="ghost" size="sm">
                  <Link href="/login">Sign in</Link>
                </Button>
                <Button asChild size="sm">
                  <Link href="/register">Get started</Link>
                </Button>
              </>
            )}
            <Button variant="ghost" size="icon" className="md:hidden" onClick={() => setOpen((v) => !v)} aria-label="Toggle menu">
              {open ? <X /> : <Menu />}
            </Button>
          </div>
        </div>
        {open && (
          <nav className="border-t bg-card px-4 py-2 md:hidden" aria-label="Mobile">
            {items.map(({ href, label, icon: Icon }) => (
              <Link key={href} href={href} onClick={() => setOpen(false)} className="flex items-center gap-3 rounded-lg px-3 py-2.5 text-sm font-medium hover:bg-muted">
                <Icon className="size-4 text-ocean" /> {label}
              </Link>
            ))}
            {user && (
              <Link href="/settings" onClick={() => setOpen(false)} className="flex items-center gap-3 rounded-lg px-3 py-2.5 text-sm font-medium hover:bg-muted">
                <Settings className="size-4 text-ocean" /> Settings
              </Link>
            )}
          </nav>
        )}
      </header>
      <main className={cn("mx-auto w-full flex-1 px-4 py-8 sm:px-6", wide ? "max-w-[1600px]" : "max-w-7xl")}>{children}</main>
      <SiteFooter />
    </div>
  );
}

export function SiteFooter({ dark = false }: { dark?: boolean }) {
  return (
    <footer className={cn("border-t", dark ? "border-white/10 bg-ink text-white/70" : "bg-card text-muted-foreground")}>
      <div className="mx-auto flex max-w-7xl flex-col gap-3 px-4 py-6 text-xs sm:flex-row sm:items-center sm:justify-between sm:px-6">
        <p>
          Forecasts are model estimates (NOAA GFS / GFS-Wave via Open-Meteo when live). Breaking heights and quality
          scores are approximate. Fares are quotes and can change.
        </p>
        <div className="flex gap-4">
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
