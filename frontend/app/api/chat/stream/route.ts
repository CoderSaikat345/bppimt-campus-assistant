import { getServerSession } from "next-auth";
import { type NextRequest, NextResponse } from "next/server";
import { authOptions } from "@/lib/auth-options";

const BACKEND_URL = process.env.BACKEND_URL ?? "http://localhost:8000";

export async function POST(request: NextRequest): Promise<Response> {
  const session = await getServerSession(authOptions);

  if (!session?.user?.email) {
    return NextResponse.json({ error: "Unauthorized" }, { status: 401 });
  }

  let body: unknown;
  try {
    body = await request.json();
  } catch {
    return NextResponse.json({ error: "Invalid request body" }, { status: 400 });
  }

  // Forward the request to the FastAPI backend with the user's ID token
  // The ID token is stored in the session by NextAuth
  const idToken = (session as { idToken?: string }).idToken;

  if (!idToken) {
    return NextResponse.json(
      { error: "No ID token available. Please sign in again." },
      { status: 401 }
    );
  }

  const backendResponse = await fetch(
    `${BACKEND_URL}/api/v1/chat/stream`,
    {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        "Authorization": `Bearer ${idToken}`,
      },
      body: JSON.stringify(body),
      // @ts-expect-error -- Node 18 fetch duplex option
      duplex: "half",
    }
  );

  if (!backendResponse.ok) {
    const errText = await backendResponse.text();
    return NextResponse.json(
      { error: `Backend error: ${errText}` },
      { status: backendResponse.status }
    );
  }

  // Stream the SSE response directly to the client
  return new Response(backendResponse.body, {
    status: 200,
    headers: {
      "Content-Type": "text/event-stream",
      "Cache-Control": "no-cache",
      "X-Accel-Buffering": "no",
      "Transfer-Encoding": "chunked",
    },
  });
}
