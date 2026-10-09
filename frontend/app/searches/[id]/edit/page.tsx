"use client";

import { useParams } from "next/navigation";

import { AppShell } from "@/components/layout/app-shell";
import { PageHeader } from "@/components/page-header";
import { RequireAuth } from "@/components/require-auth";
import { SearchWizard } from "@/components/search/search-wizard";
import { Skeleton } from "@/components/ui/skeleton";
import { useSearch } from "@/hooks/queries";
import { fromSearch } from "@/lib/validation";
import type { User } from "@/types/api";

function Edit({ user, id }: { user: User; id: string }) {
  const { data, isLoading, error } = useSearch(id);
  if (isLoading) return <Skeleton className="h-96" />;
  if (error || !data) return <p className="text-muted-foreground">Search not found.</p>;
  return (
    <>
      <PageHeader eyebrow="Edit search" title={data.name} description="Changes apply from the next evaluation (within minutes)." />
      <SearchWizard user={user} initial={fromSearch(data)} searchId={id} />
    </>
  );
}

export default function EditSearchPage() {
  const { id } = useParams<{ id: string }>();
  return (
    <AppShell>
      <RequireAuth>{(user) => <Edit user={user} id={id} />}</RequireAuth>
    </AppShell>
  );
}
