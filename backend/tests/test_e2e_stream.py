"""
tests/test_e2e_stream.py

End-to-end integration test for the BPPIMT Campus Resource Assistant backend.

Tests the full authentication pipeline and streaming chat endpoint by making
real HTTP requests to a running backend. Bypasses the Next.js frontend entirely.

Usage:
  1. Start the backend:
       python -m uvicorn main:app --reload --port 8000

  2. Run WITHOUT a token (tests health + auth rejection):
       python tests/test_e2e_stream.py

  3. Run WITH a real Google ID token (tests full streaming):
       python tests/test_e2e_stream.py --token "<YOUR_GOOGLE_ID_TOKEN>"

How to get your Google ID token:
  Option A — From the browser (easiest):
    1. Start the frontend: cd frontend && npm run dev
    2. Login with your @bppimt.ac.in account
    3. Open browser DevTools → Application → Cookies
    4. Find the "next-auth.session-token" cookie
    5. OR: Open DevTools → Network tab → find any /api/chat/stream request
       → look at the Authorization header → copy the Bearer token

  Option B — From the running frontend (programmatic):
    1. After login, open browser Console and run:
         const session = await fetch('/api/auth/session').then(r => r.json());
         console.log(session.idToken);
    2. Copy the printed token
"""

from __future__ import annotations

import argparse
import json
import sys
import time

import httpx

cfg = {"base_url": "http://localhost:8000"}

# ─────────────────────────────────────────────────────────────────────────────
# ANSI colors for terminal output
# ─────────────────────────────────────────────────────────────────────────────

GREEN = "\033[92m"
RED = "\033[91m"
YELLOW = "\033[93m"
CYAN = "\033[96m"
DIM = "\033[2m"
RESET = "\033[0m"
BOLD = "\033[1m"


def ok(msg: str) -> None:
    print(f"  {GREEN}✓{RESET} {msg}")


def fail(msg: str) -> None:
    print(f"  {RED}✗{RESET} {msg}")


def info(msg: str) -> None:
    print(f"  {CYAN}ℹ{RESET} {msg}")


def warn(msg: str) -> None:
    print(f"  {YELLOW}⚠{RESET} {msg}")


def section(title: str) -> None:
    print(f"\n{BOLD}{'─' * 60}{RESET}")
    print(f"{BOLD}  {title}{RESET}")
    print(f"{BOLD}{'─' * 60}{RESET}")


# ─────────────────────────────────────────────────────────────────────────────
# Test 1: Health endpoint (no auth)
# ─────────────────────────────────────────────────────────────────────────────

def test_health() -> bool:
    section("Test 1: GET /health (no auth required)")
    try:
        r = httpx.get(f"{cfg['base_url']}/health", timeout=5)
        if r.status_code == 200:
            body = r.json()
            ok(f"Status {r.status_code} — {body}")
            return True
        else:
            fail(f"Expected 200, got {r.status_code}: {r.text}")
            return False
    except httpx.ConnectError:
        fail(f"Cannot connect to {cfg['base_url']}. Is the backend running?")
        return False


# ─────────────────────────────────────────────────────────────────────────────
# Test 2: Auth rejection — no token → 401
# ─────────────────────────────────────────────────────────────────────────────

def test_no_token() -> bool:
    section("Test 2: POST /api/v1/chat/stream — no Bearer token → expect 401")
    try:
        r = httpx.post(
            f"{cfg['base_url']}/api/v1/chat/stream",
            json={"query": "Hello"},
            timeout=5,
        )
        if r.status_code == 401:
            ok(f"Correctly rejected with 401: {r.json().get('detail', '')[:80]}")
            return True
        else:
            fail(f"Expected 401, got {r.status_code}: {r.text[:120]}")
            return False
    except Exception as e:
        fail(f"Request failed: {e}")
        return False


# ─────────────────────────────────────────────────────────────────────────────
# Test 3: Auth rejection — invalid token → 401
# ─────────────────────────────────────────────────────────────────────────────

def test_invalid_token() -> bool:
    section("Test 3: POST /api/v1/chat/stream — invalid Bearer token → expect 401")
    try:
        r = httpx.post(
            f"{cfg['base_url']}/api/v1/chat/stream",
            json={"query": "Hello"},
            headers={"Authorization": "Bearer totally.invalid.token"},
            timeout=5,
        )
        if r.status_code == 401:
            ok(f"Correctly rejected with 401: {r.json().get('detail', '')[:80]}")
            return True
        else:
            fail(f"Expected 401, got {r.status_code}: {r.text[:120]}")
            return False
    except Exception as e:
        fail(f"Request failed: {e}")
        return False


# ─────────────────────────────────────────────────────────────────────────────
# Test 4: Streaming chat with real token
# ─────────────────────────────────────────────────────────────────────────────

def test_stream_chat(token: str) -> bool:
    section("Test 4: POST /api/v1/chat/stream — valid token + streaming")

    query = "What is the fee structure for CSE students?"
    info(f"Query: {query}")
    print()

    try:
        with httpx.stream(
            "POST",
            f"{cfg['base_url']}/api/v1/chat/stream",
            json={
                "query": query,
                "conversation_id": None,
                "context": {"section": "CSE-B", "semester_end_date": None},
            },
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
            },
            timeout=60,
        ) as response:

            if response.status_code != 200:
                body = response.read().decode()
                fail(f"HTTP {response.status_code}: {body[:200]}")
                return False

            ok(f"HTTP {response.status_code} — SSE stream opened")
            print()

            token_count = 0
            tool_calls = []
            full_text = ""
            conversation_id = None
            stream_complete = False

            for line in response.iter_lines():
                line = line.strip()
                if not line:
                    continue

                try:
                    chunk = json.loads(line)
                except json.JSONDecodeError:
                    warn(f"Malformed line: {line[:60]}")
                    continue

                chunk_type = chunk.get("type")

                if chunk_type == "token":
                    delta = chunk.get("content", "")
                    full_text += delta
                    token_count += 1
                    # Print tokens in real-time
                    print(f"{DIM}{delta}{RESET}", end="", flush=True)

                elif chunk_type == "tool_call":
                    tool_name = chunk.get("tool", "?")
                    status = chunk.get("status", "?")
                    tool_calls.append(tool_name)
                    print(f"\n  {CYAN}🔧 Tool: {tool_name} [{status}]{RESET}")

                elif chunk_type == "tool_output":
                    tool_name = chunk.get("tool", "?")
                    print(f"  {CYAN}📦 Tool output: {tool_name}{RESET}")

                elif chunk_type == "done":
                    conversation_id = chunk.get("conversation_id")
                    stream_complete = True

                elif chunk_type == "error":
                    err_msg = chunk.get("message", "Unknown error")
                    fail(f"Stream error: {err_msg}")
                    return False

            print()  # Newline after streaming tokens
            print()

            # ── Summary ──────────────────────────────────────────────
            if stream_complete:
                ok(f"Stream completed successfully")
                info(f"Tokens received: {token_count}")
                info(f"Tools invoked: {tool_calls or 'none'}")
                info(f"Conversation ID: {conversation_id}")
                info(f"Response length: {len(full_text)} chars")
                if full_text:
                    preview = full_text[:150].replace("\n", " ")
                    info(f"Preview: {preview}...")
                return True
            else:
                fail("Stream ended without a 'done' chunk")
                return False

    except httpx.ReadTimeout:
        fail("Stream timed out after 60 seconds")
        return False
    except Exception as e:
        fail(f"Request failed: {e}")
        return False


# ─────────────────────────────────────────────────────────────────────────────
# Main
# ─────────────────────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(
        description="E2E test for BPPIMT Campus Resource Assistant backend",
    )
    parser.add_argument(
        "--token",
        type=str,
        default=None,
        help="Google ID token (from a @bppimt.ac.in account) for authenticated tests",
    )
    parser.add_argument(
        "--base-url",
        type=str,
        default=cfg["base_url"],
        help=f"Backend base URL (default: {cfg['base_url']})",
    )
    args = parser.parse_args()

    cfg["base_url"] = args.base_url

    print(f"\n{BOLD}BPPIMT Campus Resource Assistant — E2E Test Suite{RESET}")
    print(f"Backend: {cfg['base_url']}")
    print(f"Token:   {'provided' if args.token else 'not provided (auth tests only)'}")

    results: list[tuple[str, bool]] = []

    # Always run these
    results.append(("Health check", test_health()))
    if not results[-1][1]:
        fail("Backend is not reachable. Aborting remaining tests.")
        sys.exit(1)

    results.append(("No token → 401", test_no_token()))
    results.append(("Invalid token → 401", test_invalid_token()))

    # Only run with a real token
    if args.token:
        results.append(("Streaming chat", test_stream_chat(args.token)))
    else:
        section("Test 4: Streaming chat — SKIPPED (no --token provided)")
        warn("To test streaming, run again with:")
        warn(f"  python tests/test_e2e_stream.py --token \"<YOUR_ID_TOKEN>\"")
        print()
        warn("Get your token from the browser after logging into the frontend:")
        warn("  1. Login at http://localhost:3000")
        warn("  2. Open DevTools Console")
        warn("  3. Run: fetch('/api/auth/session').then(r=>r.json()).then(s=>console.log(s.idToken))")
        warn("  4. Copy the printed token")

    # ── Final report ──────────────────────────────────────────────────────
    section("Results Summary")
    passed = sum(1 for _, ok in results if ok)
    total = len(results)

    for name, ok in results:
        status = f"{GREEN}PASS{RESET}" if ok else f"{RED}FAIL{RESET}"
        print(f"  [{status}] {name}")

    print()
    if passed == total:
        print(f"  {GREEN}{BOLD}All {total} tests passed ✓{RESET}")
    else:
        print(f"  {RED}{BOLD}{total - passed}/{total} tests failed ✗{RESET}")

    sys.exit(0 if passed == total else 1)


if __name__ == "__main__":
    main()
