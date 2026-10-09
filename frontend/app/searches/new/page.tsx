"use client";

import { AppShell } from "@/components/layout/app-shell";
import { PageHeader } from "@/components/page-header";
import { RequireAuth } from "@/components/require-auth";
import { SearchWizard } from "@/components/search/search-wizard";
import { defaultWizardState } from "@/lib/validation";

export default function NewSearchPage() {
  return (
    <AppShell>
      <RequireAuth>
        {(user) => (
          <>
            <PageHeader eyebrow="New search" title="What waves are you chasing?" description="Six quick steps. You can change everything later." />
            <SearchWizard
              user={user}
              initial={defaultWizardState(user.profile.home_airport, user.profile.units === "m" ? "m" : "ft")}
            />
          </>
        )}
      </RequireAuth>
    </AppShell>
  );
}
