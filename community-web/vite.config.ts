import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";
import tailwindcss from "@tailwindcss/vite";
import thirdPartyLicenses from "../app-web-kit/third-party-licenses.mjs";
import { fileURLToPath, URL } from "node:url";

// The app is served at /system-apps/community/ by server/applications/proxy.py
// (serve_app -> _serve_static). ABSOLUTE base — same reasoning as workbench-web:
// hashed assets resolve regardless of dir-vs-index.html opens.
export default defineConfig({
  base: "/system-apps/community/",
  // thirdPartyLicenses: assets/THIRD-PARTY-LICENSES.txt, the licence texts of
  // every npm package compiled in (it only adds that file; chunks are unchanged).
  plugins: [react(), tailwindcss(), thirdPartyLicenses()],
  resolve: {
    alias: {
      "@": fileURLToPath(new URL("./src", import.meta.url)),
      "@kit": fileURLToPath(new URL("../app-web-kit", import.meta.url)),
    },
    // Kit files live outside this app's node_modules chain; dedupe pins
    // their bare imports (react, radix, …) to THIS app's single copy —
    // two React instances in one bundle = broken hooks. (Shiki entries:
    // same dedupe as workbench-web — streamdown's code plugin.)
    dedupe: ["react", "react-dom", "radix-ui", "lucide-react", "class-variance-authority", "cmdk", "clsx", "tailwind-merge", "shiki", "@shikijs/core", "@shikijs/engine-oniguruma"],
  },
  build: {
    outDir: "dist",
    emptyOutDir: true,
    sourcemap: false,
  },
});
