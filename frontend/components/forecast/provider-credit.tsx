"use client";

import { AttributionLinks } from "@/components/forecast-provenance";
import type { AttributionLink } from "@/types/api";

/** The provider's credit next to its data: the API's attribution links (Open-Meteo with its
 * CC BY 4.0 link, ECMWF, NOAA) and, for Windy data, "Contains data from the Windy database"
 * with a link to windy.com (lib/provider creditLinks). */
export function ProviderCredit({
  sourceCode,
  links,
  dark = false,
  className,
}: {
  sourceCode?: string | null;
  links: readonly AttributionLink[] | null | undefined;
  dark?: boolean;
  className?: string;
}) {
  return <AttributionLinks links={links} sourceCode={sourceCode} dark={dark} className={className} />;
}
