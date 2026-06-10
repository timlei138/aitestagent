import { defineConfig } from "vite";
import vue from "@vitejs/plugin-vue";

export default defineConfig({
  plugins: [vue()],
  base: "/static/dist/",
  build: {
    outDir: "../dist",
    emptyOutDir: true
  }
});

