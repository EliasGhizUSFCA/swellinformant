"use client";

import { usePathname, useRouter } from "next/navigation";
import { useEffect } from "react";

import { Skeleton } from "@/components/ui/skeleton";
import { useMe } from "@/hooks/queries";
import type { User } from "@/types/api";

/** Client-side guard (the proxy and the backend also enforce authentication). */
export function RequireAuth({ children }: { children: (user: User) => React.ReactNode }) {
  const { data: user, isLoading } = useMe();
  const router = useRouter();
  const pathname = usePathname();
  useEffect(() => {
    if (!isLoading && user === null) router.replace(`/login?next=${encodeURIComponent(pathname)}`);
  }, [isLoading, user, router, pathname]);
  if (isLoading || !user) {
    return (
      <div className="space-y-4" aria-busy="true">
        <Skeleton className="h-10 w-64" />
        <div className="grid gap-4 md:grid-cols-3">
          <Skeleton className="h-28" />
          <Skeleton className="h-28" />
          <Skeleton className="h-28" />
        </div>
        <Skeleton className="h-64" />
      </div>
    );
  }
  return <>{children(user)}</>;
}
