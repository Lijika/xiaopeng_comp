import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { StrictMode } from "react";
import { createRoot } from "react-dom/client";

import App from "./App";
import "./index.css";

const queryClient = new QueryClient({
  defaultOptions: {
    // Bounded, explicit projection-query retry: one immediate retry for
    // transient transport failures; protected mutations never retry.
    queries: { retry: 1, retryDelay: 0 },
    mutations: { retry: false },
  },
});

const rootElement = document.getElementById("root");
if (rootElement === null) {
  throw new Error("React root element is missing");
}

createRoot(rootElement).render(
  <StrictMode>
    <QueryClientProvider client={queryClient}>
      <App />
    </QueryClientProvider>
  </StrictMode>,
);
