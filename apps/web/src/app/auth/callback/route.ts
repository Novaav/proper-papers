import type { NextRequest } from "next/server.js";

import { handleCallback } from "@/lib/auth/handlers.ts";
import { authDeps } from "@/lib/auth/server.ts";

export const dynamic = "force-dynamic";

export function GET(request: NextRequest) {
  return handleCallback(
    request,
    (name) => request.cookies.get(name)?.value,
    authDeps(),
  );
}
