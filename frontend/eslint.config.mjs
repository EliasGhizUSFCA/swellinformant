import nextVitals from "eslint-config-next/core-web-vitals";
import nextTs from "eslint-config-next/typescript";

const config = [
  ...nextVitals,
  ...nextTs,
  {
    ignores: [".next/**", ".next-*/**", "node_modules/**", "playwright-report/**", "test-results/**", "types/openapi.d.ts", "next-env.d.ts"],
  },
];

export default config;
