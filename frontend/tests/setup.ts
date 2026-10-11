import "@testing-library/jest-dom/vitest";

import { cleanup } from "@testing-library/react";
import { afterEach } from "vitest";

// Vitest runs without globals, so Testing Library cannot register its own cleanup: unmount
// rendered components after every test so tests never see each other's DOM.
afterEach(() => cleanup());
