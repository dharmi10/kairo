import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// The dashboard talks to the FastAPI backend (default
// http://127.0.0.1:8000, see backend/README run instructions) over
// plain fetch(), not a dev-server proxy -- see src/api.js. Nothing here
// needs to know the backend's address; VITE_API_BASE_URL (.env) does.
export default defineConfig({
  plugins: [react()],
  server: {
    port: 5173,
  },
});
