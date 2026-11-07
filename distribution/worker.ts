import { installer, validVersion } from "./installer";

type BucketObject = { body: ReadableStream; httpEtag?: string; size?: number };
export type Env = {
  RELEASES: { get(key: string): Promise<BucketObject | null> };
  RATE_LIMITER: { limit(options: { key: string }): Promise<{ success: boolean }> };
};
export type EdgeCache = { match(request: Request): Promise<Response | undefined>; put(request: Request, response: Response): Promise<void> };
type Context = { waitUntil(promise: Promise<unknown>): void };
const errorResponse = (message: string, status: number, headers: Record<string, string> = {}) => new Response(message, { status, headers: { "Cache-Control": "no-store", ...headers } });

export async function handleRequest(request: Request, env: Env, context?: Context, cache = (globalThis as typeof globalThis & { caches?: { default: EdgeCache } }).caches?.default): Promise<Response> {
  const url = new URL(request.url);
  if (request.method !== "GET" && request.method !== "HEAD") return errorResponse("Method not allowed", 405, { Allow: "GET, HEAD" });
  if (url.search) return errorResponse("Query strings are not supported", 400);
  const release = /^\/releases\/([^/]+)\/skills\.tar\.gz(\.sha256)?$/.exec(url.pathname);
  const stored = url.pathname === "/latest.json" || !!(release && validVersion(release[1]));
  if (!stored && !["/health", "/install"].includes(url.pathname)) return errorResponse("Not found", 404);
  // Anonymous users behind a NAT share this generous allowance. Native limits are
  // approximate and per Cloudflare location, not a global request or spending cap.
  try {
    const { success } = await env.RATE_LIMITER.limit({ key: `clzmj-skills:${request.headers.get("CF-Connecting-IP") || "unknown"}` });
    if (!success) return errorResponse("Too many requests; retry in one minute", 429, { "Retry-After": "60" });
  } catch {
    // A failed limiter must not turn an outage into unlimited R2 reads.
    return errorResponse("Request protection temporarily unavailable", 503, { "Retry-After": "60" });
  }
  const finish = (response: Response) => request.method === "HEAD" ? new Response(null, { status: response.status, headers: response.headers }) : response;
  if (url.pathname === "/health") return finish(new Response("ok\n", { headers: { "Content-Type": "text/plain; charset=utf-8", "Cache-Control": "no-store" } }));
  // Fixed host, no query, GET only: client headers and HEAD cannot vary cache keys.
  const cacheKey = new Request(`https://skills.clzmj.dev${url.pathname}`);
  if (cache) {
    try {
      const hit = await cache.match(cacheKey);
      if (hit) return finish(hit);
    } catch { /* Cache availability does not bypass the rate limiter. */ }
  }
  let response: Response;
  if (url.pathname === "/install") response = new Response(installer(), { headers: { "Content-Type": "text/x-shellscript; charset=utf-8", "Cache-Control": "public, max-age=60", "X-Content-Type-Options": "nosniff" } });
  else {
    const object = await env.RELEASES.get(url.pathname.slice(1));
    if (!object) return errorResponse("Not found", 404);
    const headers = new Headers({ "Content-Type": url.pathname.endsWith(".json") ? "application/json" : url.pathname.endsWith(".sha256") ? "text/plain" : "application/gzip", "Cache-Control": release ? "public, max-age=31536000, immutable" : "public, max-age=60", "X-Content-Type-Options": "nosniff" });
    if (object.httpEtag) headers.set("ETag", object.httpEtag);
    if (object.size !== undefined) headers.set("Content-Length", String(object.size));
    response = new Response(object.body, { headers });
  }
  if (cache) {
    const write = cache.put(cacheKey, response.clone()).catch(() => {});
    if (context) context.waitUntil(write);
    else await write;
  }
  return finish(response);
}

export default { fetch: handleRequest };
