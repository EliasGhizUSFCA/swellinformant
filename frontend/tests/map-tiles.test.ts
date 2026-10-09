import { describe, expect, it } from "vitest";

import { CARTO_VOYAGER_URL, DEFAULT_TILE_URL, cartoKeyMissing, tileConfig, tileCspSources, tileHostSource } from "@/lib/map-tiles";

describe("tile config", () => {
  it("defaults to key-less OpenStreetMap tiles with OSM attribution", () => {
    for (const unset of [undefined, "", "   "]) {
      const t = tileConfig(unset, unset);
      expect(t.url).toBe(DEFAULT_TILE_URL);
      expect(t.attribution).toContain("OpenStreetMap");
      expect(t.attribution).not.toContain("CARTO");
      expect(t.maxZoom).toBe(19);
    }
  });

  it("adds CARTO attribution for CARTO URLs and honours an explicit attribution", () => {
    const carto = "https://{s}.basemaps.cartocdn.com/rastertiles/voyager/{z}/{x}/{y}{r}.png?key=abc";
    expect(tileConfig(carto).attribution).toMatch(/OpenStreetMap.*CARTO/);
    expect(tileConfig(carto, "Tiles &copy; Someone").attribution).toBe("Tiles &copy; Someone");
  });
});

describe("CARTO key", () => {
  it("switches to CARTO Voyager with the key, URL-encoded, and CARTO attribution", () => {
    const t = tileConfig(undefined, undefined, " my key&x ");
    expect(t.url).toBe(`${CARTO_VOYAGER_URL}?key=my%20key%26x`);
    expect(t.attribution).toContain("CARTO");
    expect(tileCspSources(undefined, undefined, "k")).toEqual(["https://*.basemaps.cartocdn.com"]);
  });

  it("an explicit tile URL wins over the key", () => {
    expect(tileConfig("https://tiles.example.com/{z}/{x}/{y}.png", undefined, "k").url).toBe("https://tiles.example.com/{z}/{x}/{y}.png");
  });

  it("flags CARTO URLs without a key (CARTO then serves 'API KEY REQUIRED' tiles)", () => {
    expect(cartoKeyMissing(CARTO_VOYAGER_URL)).toBe(true);
    expect(cartoKeyMissing(`${CARTO_VOYAGER_URL}?key=`)).toBe(true);
    expect(cartoKeyMissing(`${CARTO_VOYAGER_URL}?key=abc`)).toBe(false);
    expect(cartoKeyMissing(DEFAULT_TILE_URL)).toBe(false);
  });
});

describe("CSP sources for tile URLs", () => {
  it.each([
    [DEFAULT_TILE_URL, "https://tile.openstreetmap.org"],
    ["https://{s}.basemaps.cartocdn.com/rastertiles/voyager/{z}/{x}/{y}{r}.png?key=k", "https://*.basemaps.cartocdn.com"],
    ["https://tiles-{s}.example.com/{z}/{x}/{y}.png", "https://*.example.com"],
    ["http://localhost:8080/{z}/{x}/{y}.png", "http://localhost:8080"],
    ["HTTPS://Tiles.Example.COM/{z}/{x}/{y}.png", "https://tiles.example.com"],
  ])("%s → %s", (url, source) => {
    expect(tileHostSource(url)).toBe(source);
  });

  it("rejects relative or malformed templates", () => {
    expect(tileHostSource("/tiles/{z}/{x}/{y}.png")).toBeNull();
    expect(tileHostSource("https://{s}/{z}/{x}/{y}.png")).toBeNull();
    expect(tileHostSource("javascript:alert(1)")).toBeNull();
  });

  it("always allows the configured tile host, plus extra hosts, without duplicates", () => {
    expect(tileCspSources(undefined, undefined)).toEqual(["https://tile.openstreetmap.org"]);
    expect(tileCspSources("", "https://a.example.com  https://tile.openstreetmap.org")).toEqual([
      "https://tile.openstreetmap.org",
      "https://a.example.com",
    ]);
  });
});
