import type { NextRequest } from "next/server.js";

import { handleLogout } from "@/lib/auth/handlers.ts";
import { authDeps } from "@/lib/auth/server.ts";

export const dynamic = "force-dynamic";

// POST only: a GET (or any other method) gets 405 from Next.js.
export function POST(request: NextRequest) {
  return handleLogout(request, authDeps());
}
