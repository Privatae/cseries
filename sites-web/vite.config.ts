import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";
import tailwindcss from "@tailwindcss/vite";
import thirdPartyLicenses from "../app-web-kit/third-party-licenses.mjs";
import { fileURLToPath, URL } from "node:url";

// The app is served at /system-apps/sites/ by server/applications/proxy.py
// (serve_app -> _serve_static). An ABSOLUTE base removes the trailing-slash
// ambiguity: hashed assets always resolve to /system-apps/sites/assets/*
// regardless of whether the window opens the dir or index.html directly.
export default defineConfig({
  base: "/system-apps/sites/",
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
    // two React instances in one bundle = broken hooks.
    dedupe: ["react", "react-dom", "radix-ui", "lucide-react", "class-variance-authority", "cmdk", "clsx", "tailwind-merge"],
  },
  build: {
    outDir: "dist",
    emptyOutDir: true,
    sourcemap: false,
  },
});
