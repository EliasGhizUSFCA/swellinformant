/**
 * Natural Earth 1:50m land and country boundaries (world-atlas 2.0.2, ISC licence; Natural
 * Earth data is public domain), loaded as lazy chunks the first time the map opens and
 * prepared once for drawing: land rings and interior borders in Mercator units, plus a label
 * position per country.
 */
import type { FeatureCollection, Geometry } from "geojson";
import type { GeometryCollection, Topology } from "topojson-specification";

import { countryLabels, lineStrings, polygonRings } from "@/lib/map-geo";

import type { BasemapGeometry } from "./basemap-draw";

export const NATURAL_EARTH_ATTRIBUTION = '<a href="https://www.naturalearthdata.com/" target="_blank" rel="noreferrer">Natural Earth</a>';

let pending: Promise<BasemapGeometry> | null = null;

export function loadBasemap(): Promise<BasemapGeometry> {
  pending ??= Promise.all([import("world-atlas/land-50m.json"), import("world-atlas/countries-50m.json"), import("topojson-client")])
    .then(([landMod, countriesMod, topo]) => {
      const land = landMod.default as unknown as Topology<{ land: GeometryCollection }>;
      const countries = countriesMod.default as unknown as Topology<{ countries: GeometryCollection<{ name: string }> }>;
      const landGeo = topo.feature(land, land.objects.land);
      const countryGeo = topo.feature(countries, countries.objects.countries) as FeatureCollection<Geometry, { name?: string }>;
      // Interior boundaries only (shared arcs), so coastlines are not stroked twice.
      const borders = topo.mesh(countries, countries.objects.countries, (a, b) => a !== b);
      return { land: polygonRings(landGeo), borders: lineStrings(borders), countries: countryLabels(countryGeo) };
    })
    .catch((e: unknown) => {
      pending = null;
      throw e;
    });
  return pending;
}
