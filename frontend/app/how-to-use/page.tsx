import type { Metadata } from "next";

import { AppShell } from "@/components/layout/app-shell";
import { PageHeader } from "@/components/page-header";

import { HowToUseGuide } from "./guide";

export const metadata: Metadata = {
  title: "How to use",
  description: "How to read the surf map, ratings and forecasts, save a search, and get alerts with flights for the swells you want.",
};

export default function HowToUsePage() {
  return (
    <AppShell>
      <PageHeader
        eyebrow="Guide"
        title="How to use Swell Travel Agent"
        description="Find surf windows at 50 spots and the flights to reach them. Each section below covers one page or one task."
      />
      <HowToUseGuide />
    </AppShell>
  );
}
