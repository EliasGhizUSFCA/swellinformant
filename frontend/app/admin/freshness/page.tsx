"use client";

import { FreshnessDashboard, NotAdmin } from "@/components/admin/freshness-dashboard";
import { AppShell } from "@/components/layout/app-shell";
import { PageHeader } from "@/components/page-header";
import { RequireAuth } from "@/components/require-auth";

/** Admin freshness dashboard (spec section 16): signed-out visitors are sent to the sign-in
 * page, other users see why they cannot open it, and the API refuses them as well (403). */
export default function AdminFreshnessPage() {
  return (
    <AppShell wide>
      <RequireAuth>
        {(user) => (
          <>
            <PageHeader
              eyebrow="Admin"
              title="Forecast freshness"
              description="Model runs, refreshes, publication, API usage and latencies. Admin alerts for these go by email to verified admins only."
            />
            {user.is_admin ? <FreshnessDashboard /> : <NotAdmin />}
          </>
        )}
      </RequireAuth>
    </AppShell>
  );
}
