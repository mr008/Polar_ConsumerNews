// Serves the archive's pictures and videos from the R2 bucket bound as MEDIA.
// Only keys under media/ are reachable. Supports Range so videos can seek.
export async function onRequestGet(ctx) {
  try {
    return await serve(ctx);
  } catch (err) {
    console.error("media error:", err);   // details go to the Pages log, not the visitor
    return new Response("Not found", { status: 404 });
  }
}

async function serve({ request, env, params }) {
  const key = "media/" + [].concat(params.path || []).join("/");
  const range = parseRange(request.headers.get("range"));
  const obj = await env.MEDIA.get(key, range ? { range } : undefined);
  if (!obj) return new Response("Not found", { status: 404 });

  const headers = new Headers();
  obj.writeHttpMetadata(headers);
  if (!headers.has("content-type")) headers.set("content-type", guessType(key));
  headers.set("etag", obj.httpEtag);
  headers.set("accept-ranges", "bytes");
  headers.set("cache-control", "public, max-age=31536000, immutable");
  if (range && obj.range) {
    const start = obj.range.offset ?? 0;
    const length = obj.range.length ?? obj.size - start;
    headers.set("content-range", `bytes ${start}-${start + length - 1}/${obj.size}`);
    headers.set("content-length", String(length));
    return new Response(obj.body, { status: 206, headers });
  }
  headers.set("content-length", String(obj.size));
  return new Response(obj.body, { headers });
}

function parseRange(value) {
  const m = /^bytes=(\d*)-(\d*)$/.exec(value || "");
  if (!m || (m[1] === "" && m[2] === "")) return null;
  if (m[1] === "") return { suffix: Number(m[2]) };
  if (m[2] === "") return { offset: Number(m[1]) };
  return { offset: Number(m[1]), length: Number(m[2]) - Number(m[1]) + 1 };
}

function guessType(key) {
  const ext = key.split(".").pop().toLowerCase();
  return { jpg: "image/jpeg", jpeg: "image/jpeg", png: "image/png", gif: "image/gif",
           webp: "image/webp", avif: "image/avif", svg: "image/svg+xml",
           mp4: "video/mp4" }[ext] || "application/octet-stream";
}
