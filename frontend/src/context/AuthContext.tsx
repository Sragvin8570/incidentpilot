import React, { createContext, useContext, useEffect, useState } from "react";
import { api, authStorage } from "../api/client";
import { User, UserRole } from "../types";

interface AuthContextType {
  user: User | null;
  isAuthenticated: boolean;
  role: UserRole | null;
  canApprove: boolean;
  login: (username: string, password: string) => Promise<void>;
  logout: () => void;
  isLoading: boolean;
}

const AuthContext = createContext<AuthContextType | undefined>(undefined);

export const AuthProvider: React.FC<{ children: React.ReactNode }> = ({
  children,
}) => {
  const [user, setUser] = useState<User | null>(null);
  const [isLoading, setIsLoading] = useState(true);

  useEffect(() => {
    const token = authStorage.getToken();
    const storedUser = authStorage.getUser();
    if (token && storedUser) {
      setUser({
        username: storedUser.username,
        role: storedUser.role as UserRole,
      });
    } else {
      authStorage.clear();
      setUser(null);
    }
    setIsLoading(false);
  }, []);

  const login = async (username: string, password: string) => {
    const res = await api.login(username, password);
    setUser({
      username,
      role: res.role,
    });
  };

  const logout = () => {
    api.logout();
    setUser(null);
  };

  const role = user?.role ?? null;
  const canApprove = role === "approver" || role === "admin";

  return (
    <AuthContext.Provider
      value={{
        user,
        isAuthenticated: !!user,
        role,
        canApprove,
        login,
        logout,
        isLoading,
      }}
    >
      {children}
    </AuthContext.Provider>
  );
};

export const useAuth = (): AuthContextType => {
  const context = useContext(AuthContext);
  if (!context) {
    throw new Error("useAuth must be used within an AuthProvider");
  }
  return context;
};
