import axios from "axios";

// Base URL for the Kairos FastAPI backend.
// Override at build time via NEXT_PUBLIC_API_URL env var.
const API_BASE_URL =
  process.env.NEXT_PUBLIC_API_URL ?? "http://localhost:8000";

const apiClient = axios.create({
  baseURL: API_BASE_URL,
  headers: {
    "Content-Type": "application/json",
  },
  timeout: 15_000,
});

export default apiClient;
