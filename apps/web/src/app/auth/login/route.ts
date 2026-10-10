import type { NextRequest } from "next/server.js";

import { handleLogin } from "@/lib/auth/handlers.ts";
import { authDeps } from "@/lib/auth/server.ts";

export const dynamic = "force-dynamic";

export function GET(request: NextRequest) {
  return handleLogin(request, authDeps());
}
