import http from "node:http";
import fs from "node:fs/promises";
import path from "node:path";
import worker from "./bundle_for_test.mjs";

const ROOT = path.resolve(import.meta.dirname, "..");
const DIST = path.join(ROOT, "dist");

const env = {
  ASSETS: {
    async fetch(url) {
      const u = typeof url === "string" ? new URL(url) : url instanceof Request ? new URL(url.url) : url;
      let pathname = decodeURIComponent(u.pathname);

      const candidates = [
        path.join(DIST, pathname),
        path.join(DIST, pathname + ".html"),
        path.join(DIST, pathname, "index.html"),
      ];

      for (const filepath of candidates) {
        try {
          const stat = await fs.stat(filepath);
          if (stat.isFile()) {
            const content = await fs.readFile(filepath, "utf-8");
            return new Response(content, {
              status: 200,
              headers: { "Content-Type": "application/json; charset=utf-8" },
            });
          }
        } catch {}
      }
      return new Response(JSON.stringify({ detail: "Not Found" }), { status: 404 });
    },
  },
};

const server = http.createServer(async (req, res) => {
  try {
    const fullUrl = new URL(req.url, `http://${req.headers.host || "localhost:8799"}`);
    
    // Cloudflare Assets matching logic:
    // If exact asset file exists in dist, serve it directly (like Cloudflare does)
    let pathname = decodeURIComponent(fullUrl.pathname).replace(/\/+$/, "");
    if (!pathname) pathname = "/index";

    const assetCandidates = [
      path.join(DIST, pathname + ".html"),
      path.join(DIST, pathname),
      path.join(DIST, pathname, "index.html"),
    ];

    let assetFound = null;
    for (const p of assetCandidates) {
      try {
        const stat = await fs.stat(p);
        if (stat.isFile()) {
          assetFound = await fs.readFile(p, "utf-8");
          break;
        }
      } catch {}
    }

    if (assetFound !== null) {
      const isHtml = pathname === "/index";
      res.writeHead(200, {
        "Content-Type": isHtml ? "text/html; charset=utf-8" : "application/json; charset=utf-8",
        "Access-Control-Allow-Origin": "*",
      });
      res.end(assetFound);
      return;
    }

    // Fallback to Worker fetch handler
    const webReq = new Request(fullUrl.toString(), {
      method: req.method,
      headers: req.headers,
    });

    const webRes = await worker.fetch(webReq, env);
    res.writeHead(webRes.status, Object.fromEntries(webRes.headers.entries()));
    const bodyText = await webRes.text();
    res.end(bodyText);
  } catch (err) {
    res.writeHead(500, { "Content-Type": "application/json" });
    res.end(JSON.stringify({ error: err.message }));
  }
});

server.listen(8799, "127.0.0.1", () => {
  console.log("Worker local server listening on http://127.0.0.1:8799");
});
