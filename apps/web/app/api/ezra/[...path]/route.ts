// Server-side proxy to the Ezra API. The API token (EZRA_API_TOKEN) stays on the
// server: the browser only ever talks to this same-origin route.
import type { NextRequest } from "next/server";

const API = (process.env.EZRA_API_URL ?? "http://localhost:8000").replace(/\/$/, "");

async function forward(req: NextRequest, ctx: { params: Promise<{ path: string[] }> }) {
  const { path } = await ctx.params;
  if (path.some((p) => p === ".." || p.includes("\\"))) {
    return Response.json({ detail: "invalid path" }, { status: 400 });
  }
  const url = `${API}/api/${path.map(encodeURIComponent).join("/")}${req.nextUrl.search}`;
  const headers = new Headers();
  for (const h of ["content-type", "range", "accept"]) {
    const v = req.headers.get(h);
    if (v) headers.set(h, v);
  }
  if (process.env.EZRA_API_TOKEN) headers.set("authorization", `Bearer ${process.env.EZRA_API_TOKEN}`);
  const init: RequestInit & { duplex?: "half" } = { method: req.method, headers, redirect: "manual" };
  if (!["GET", "HEAD"].includes(req.method)) {
    init.body = req.body;
    init.duplex = "half";
  }
  let res: Response;
  try {
    res = await fetch(url, init);
  } catch {
    return Response.json({ detail: `cannot reach the Ezra API at ${API}` }, { status: 502 });
  }
  const out = new Headers();
  for (const h of ["content-type", "content-length", "content-range", "accept-ranges", "location"]) {
    const v = res.headers.get(h);
    if (v) out.set(h, v);
  }
  return new Response(res.body, { status: res.status, headers: out });
}

export const GET = forward;
export const POST = forward;
export const PATCH = forward;
export const DELETE = forward;
export const dynamic = "force-dynamic";
