import { act, renderHook } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { HEIGHT_UNIT_EVENT, HEIGHT_UNIT_KEY, isHeightUnit, resetHeightUnitMemory, storedHeightUnit, useHeightUnit, writeHeightUnit } from "@/lib/units";

afterEach(() => {
  vi.restoreAllMocks();
  resetHeightUnitMemory();
  try {
    window.localStorage.removeItem(HEIGHT_UNIT_KEY);
  } catch {
    // jsdom storage is always there; the try mirrors the code under test.
  }
});

describe("the site-wide height unit (ft / m)", () => {
  it("accepts only ft and m", () => {
    expect(isHeightUnit("ft")).toBe(true);
    expect(isHeightUnit("m")).toBe(true);
    expect(isHeightUnit("cm")).toBe(false);
    expect(isHeightUnit(null)).toBe(false);
  });

  it("is stored in the browser, so it works signed out, and the stored choice wins over the fallback", () => {
    const { result } = renderHook(() => useHeightUnit("ft"));
    expect(result.current[0]).toBe("ft");
    act(() => result.current[1]("m"));
    expect(window.localStorage.getItem(HEIGHT_UNIT_KEY)).toBe("m");
    expect(result.current[0]).toBe("m");
    // A signed-in account in feet does not override this browser's choice.
    const second = renderHook(() => useHeightUnit("ft"));
    expect(second.result.current[0]).toBe("m");
  });

  it("follows the fallback (the account's units) when nothing is stored", () => {
    expect(renderHook(() => useHeightUnit("m")).result.current[0]).toBe("m");
    window.localStorage.setItem(HEIGHT_UNIT_KEY, "yards");
    expect(storedHeightUnit()).toBeNull();
    expect(renderHook(() => useHeightUnit("m")).result.current[0]).toBe("m");
  });

  it("tells this page when it changes, once per change", () => {
    const seen = vi.fn();
    window.addEventListener(HEIGHT_UNIT_EVENT, seen);
    writeHeightUnit("m");
    writeHeightUnit("m");
    writeHeightUnit("ft");
    window.removeEventListener(HEIGHT_UNIT_EVENT, seen);
    expect(seen).toHaveBeenCalledTimes(2);
  });

  it("never throws when storage is blocked: the choice lasts for this page", () => {
    vi.spyOn(Storage.prototype, "setItem").mockImplementation(() => {
      throw new DOMException("blocked", "SecurityError");
    });
    vi.spyOn(Storage.prototype, "getItem").mockImplementation(() => {
      throw new DOMException("blocked", "SecurityError");
    });
    expect(storedHeightUnit()).toBeNull();
    const { result } = renderHook(() => useHeightUnit("ft"));
    act(() => result.current[1]("m"));
    expect(result.current[0]).toBe("m");
    expect(storedHeightUnit()).toBe("m");
  });
});
