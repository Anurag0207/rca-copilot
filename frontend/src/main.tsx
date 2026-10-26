/**
 * Application entry point.
 *
 * Mounts the React application into the #root element and wraps it with:
 *   - React.StrictMode for development warnings
 *   - BrowserRouter for client-side routing
 *   - AuthProvider for JWT token context
 *
 * Routes:
 *   /login      → Login page (unauthenticated)
 *   /           → Dashboard (authenticated; redirects to /login if no token)
 */

import React from "react";
import { createRoot } from "react-dom/client";
import { BrowserRouter } from "react-router-dom";

import App from "./App";
import "./styles/tokens.css";

const rootElement = document.getElementById("root");
if (!rootElement) {
  throw new Error("Root element #root not found in index.html");
}

createRoot(rootElement).render(
  <React.StrictMode>
    <BrowserRouter>
      <App />
    </BrowserRouter>
  </React.StrictMode>
);
