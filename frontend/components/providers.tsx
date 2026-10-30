"use client";

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { useState } from "react";
import { Toaster } from "sonner";

import { ForecastVersionProvider } from "@/hooks/use-forecast-version";
import { ApiError } from "@/lib/api";

export function Providers({ children }: { children: React.ReactNode }) {
  const [client] = useState(
    () =>
      new QueryClient({
        defaultOptions: {
          queries: {
            staleTime: 30_000,
            refetchOnWindowFocus: false,
            retry: (count, error) => !(error instanceof ApiError && error.status < 500) && count < 2,
          },
        },
      }),
  );
  return (
    <QueryClientProvider client={client}>
      {/* Open pages follow new forecast versions without a reload (hooks/use-forecast-version.ts). */}
      <ForecastVersionProvider>{children}</ForecastVersionProvider>
      <Toaster position="top-right" richColors closeButton />
    </QueryClientProvider>
  );
}
