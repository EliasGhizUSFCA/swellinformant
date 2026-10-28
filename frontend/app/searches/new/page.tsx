"use client";

import { AppShell } from "@/components/layout/app-shell";
import { PageHeader } from "@/components/page-header";
import { RequireAuth } from "@/components/require-auth";
import { SearchWizard } from "@/components/search/search-wizard";
import { useSearchDefaults } from "@/components/search/use-search-defaults";
import { Skeleton } from "@/components/ui/skeleton";
import { DEFAULT_ALERT_LEAD_DAYS, defaultWizardState } from "@/lib/validation";
import type { User } from "@/types/api";

function NewSearch({ user }: { user: User }) {
  // The deployment's default alert lead days (ALERT_LEAD_DEFAULT_*); the built-in 2–10 days
  // when they cannot be read.
  const defaults = useSearchDefaults();
  if (defaults.isPending) return <Skeleton className="h-96" />;
  const lead = defaults.data ? { min: defaults.data.alert_lead_min_days, max: defaults.data.alert_lead_max_days } : DEFAULT_ALERT_LEAD_DAYS;
  return (
    <SearchWizard user={user} initial={defaultWizardState(user.profile.home_airport, user.profile.units === "m" ? "m" : "ft", lead)} />
  );
}

export default function NewSearchPage() {
  return (
    <AppShell>
      <RequireAuth>
        {(user) => (
          <>
            <PageHeader eyebrow="New search" title="What waves are you chasing?" description="Six quick steps. You can change everything later." />
            <NewSearch user={user} />
          </>
        )}
      </RequireAuth>
    </AppShell>
  );
}
