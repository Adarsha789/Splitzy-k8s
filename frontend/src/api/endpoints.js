// Base URL for your backend API.
// Set by VITE_API_URL at build time. The /api default means same-origin
// requests so the Ingress can route them without CORS.
export const API_BASE_URL = import.meta.env.VITE_API_URL || "/api";
