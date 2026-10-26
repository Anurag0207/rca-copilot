/**
 * Root application component.
 *
 * Provides the AuthContext (JWT token state) and defines client-side routes.
 * Protected routes redirect to /login when no token is present.
 *
 * Route map:
 *   /login  → <Login />
 *   /       → <Dashboard />  (requires auth)
 *   /rca    → <Rca />        (requires auth)
 *   *       → redirect to /
 */

import React, { createContext, useCallback, useContext, useState } from "react";
import { Navigate, Route, Routes } from "react-router-dom";

import { setAuthToken } from "./api/client";
import Dashboard from "./pages/Dashboard";
import Login from "./pages/Login";
import Rca from "./pages/Rca";
import { ThemeProvider } from "./theme";

// ─────────────────────────────────────────────────────────────────────────────
// Auth context
// ─────────────────────────────────────────────────────────────────────────────

interface AuthContextValue {
  token: string | null;
  login: (token: string) => void;
  logout: () => void;
}

export const AuthContext = createContext<AuthContextValue>({
  token: null,
  login: () => {},
  logout: () => {},
});

export function useAuth(): AuthContextValue {
  return useContext(AuthContext);
}

function AuthProvider({ children }: { children: React.ReactNode }): React.JSX.Element {
  const [token, setToken] = useState<string | null>(null);

  const login = useCallback((newToken: string) => {
    setToken(newToken);
    setAuthToken(newToken);
  }, []);

  const logout = useCallback(() => {
    setToken(null);
    setAuthToken(null);
    // TODO: optionally call DELETE /auth/token or clear server-side session
  }, []);

  return (
    <AuthContext.Provider value={{ token, login, logout }}>
      {children}
    </AuthContext.Provider>
  );
}

// ─────────────────────────────────────────────────────────────────────────────
// Protected route wrapper
// ─────────────────────────────────────────────────────────────────────────────

function ProtectedRoute({ children }: { children: React.JSX.Element }): React.JSX.Element {
  const { token } = useAuth();
  if (!token) {
    return <Navigate to="/login" replace />;
  }
  return children;
}

// ─────────────────────────────────────────────────────────────────────────────
// App
// ─────────────────────────────────────────────────────────────────────────────

export default function App(): React.JSX.Element {
  return (
    <ThemeProvider>
      <AuthProvider>
        <Routes>
          <Route path="/login" element={<Login />} />
          <Route
            path="/"
            element={
              <ProtectedRoute>
                <Dashboard />
              </ProtectedRoute>
            }
          />
          <Route
            path="/rca"
            element={
              <ProtectedRoute>
                <Rca />
              </ProtectedRoute>
            }
          />
          <Route path="*" element={<Navigate to="/" replace />} />
        </Routes>
      </AuthProvider>
    </ThemeProvider>
  );
}
