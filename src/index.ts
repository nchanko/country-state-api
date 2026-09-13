/**
 * Handles only what static assets cannot: the search endpoints, and requests
 * whose path needs normalizing before it can hit a precomputed file.
 *
 * Everything else in the API is a file under dist/ and is served by Cloudflare
 * before this code runs, which is why it costs nothing.
 */

import COUNTRIES from "./data/countries.json";
import STATES from "./data/states.json";
import COUNTRY_NAMES from "./data/country_names.json";
import SPLIT_KEYS from "./data/split_keys.json";

interface Env {
  ASSETS: Fetcher;
}

type CityRow = [string, string, string, string, number | null, number | null];

const SPLIT = new Set<string>(SPLIT_KEYS);
const NAMES = COUNTRY_NAMES as Record<string, string>;

const JSON_HEADERS = {
  "Content-Type": "application/json; charset=utf-8",
  "Access-Control-Allow-Origin": "*",
  "Cache-Control": "public, max-age=3600",
};

const json = (body: unknown, status = 200) =>
  new Response(JSON.stringify(body), { status, headers: JSON_HEADERS });

const notFound = (detail: string) => json({ detail }, 404);

/** Collapse whitespace and lowercase, matching the indexer's normalization. */
const norm = (s: string) => s.trim().replace(/\s+/g, " ").toLowerCase();

/**
 * Word-prefix match: the full name starts with q, or any word does.
 * Must stay in sync with keys_for() in scripts/build_search.py — that function
 * decides which shard a city lands in, and this one decides whether it matches.
 */
function matchesPrefix(name: string, q: string): boolean {
  const n = norm(name);
  if (!n) return false;
  if (n.startsWith(q)) return true;
  for (const word of n.split(" ")) if (word.startsWith(q)) return true;
  return false;
}

async function loadShard(env: Env, base: URL, key: string): Promise<CityRow[]> {
  const hex = [...new TextEncoder().encode(key)]
    .map((b) => b.toString(16).padStart(2, "0"))
    .join("");
  const res = await env.ASSETS.fetch(new URL(`/_idx/cities/${hex}`, base));
  if (!res.ok) return [];
  return res.json();
}

async function searchCities(env: Env, base: URL, q: string): Promise<Response> {
  if (!q) return json([]);

  // Route to a shard that is guaranteed complete for this query. A 2-char key
  // that was split is capped, so anything longer must use its 3-char child.
  const key = q.length >= 3 && SPLIT.has(q.slice(0, 2)) ? q.slice(0, 3) : q.slice(0, 2);
  const rows = await loadShard(env, base, key.length < 2 ? q.slice(0, 1) : key);

  const out = [];
  for (const [n, nl, s, c, lat, lon] of rows) {
    if (matchesPrefix(n, q) || (nl && matchesPrefix(nl, q))) {
      out.push({
        name: n,
        name_local: nl,
        state_name: s,
        country_code: c,
        country_name: NAMES[c] ?? c,
        latitude: lat,
        longitude: lon,
      });
      if (out.length >= 50) break;
    }
  }
  return json(out);
}

/**
 * Mirrors find_state_relaxedly() in main.py: exact, then starts-with, then
 * substring in either direction.
 */
function findStateRelaxed(cc: string, stateName: string) {
  const target = stateName.trim().toLowerCase();
  const inCountry = STATES.filter((s) => s.country_code === cc);
  if (!inCountry.length) return null;

  return (
    inCountry.find((s) => s.name.toLowerCase() === target) ??
    inCountry.find((s) => s.name.toLowerCase().startsWith(target)) ??
    inCountry.find((s) => {
      const n = s.name.toLowerCase();
      return n.includes(target) || target.includes(n);
    }) ??
    null
  );
}

/** Re-fetch a canonical asset path on behalf of a non-canonical request. */
async function serveAsset(env: Env, base: URL, path: string): Promise<Response | null> {
  const res = await env.ASSETS.fetch(new URL(path, base));
  if (!res.ok) return null;
  return new Response(res.body, { status: 200, headers: JSON_HEADERS });
}

/**
 * Build the canonical path segment for a state name: collapse whitespace to
 * match export_static.py's naming, but preserve case — asset lookup is
 * case-sensitive and the files are written with the original casing.
 */
const seg = (s: string) => encodeURIComponent(s.trim().replace(/\s+/g, " "));

export default {
  async fetch(request: Request, env: Env): Promise<Response> {
    const url = new URL(request.url);
    const base = url;
    let path: string;
    try {
      path = decodeURIComponent(url.pathname).replace(/\/+$/, "");
    } catch {
      return json({ detail: "Malformed URL" }, 400);
    }

    if (request.method === "OPTIONS") {
      return new Response(null, {
        headers: {
          "Access-Control-Allow-Origin": "*",
          "Access-Control-Allow-Methods": "*",
          "Access-Control-Allow-Headers": "*",
        },
      });
    }
    if (request.method !== "GET" && request.method !== "HEAD") {
      return json({ detail: "Method Not Allowed" }, 405);
    }

    const q = norm(url.searchParams.get("q") ?? "");

    if (path === "/v1/search/cities") return searchCities(env, base, q);

    if (path === "/v1/search/states") {
      if (!q) return json([]);
      return json(STATES.filter((s) => s.name.toLowerCase().includes(q)).slice(0, 20));
    }

    if (path === "/v1/search/countries") {
      if (!q) return json([]);
      return json(COUNTRIES.filter((c) => c.name.toLowerCase().includes(q)).slice(0, 20));
    }

    const phone = path.match(/^\/v1\/search\/phone-code\/(.+)$/);
    if (phone) {
      let code = phone[1].trim();
      if (!code.startsWith("+")) code = "+" + code;
      return json(
        COUNTRIES.filter((c) => c.phone_code === code || c.phone_code.startsWith(code)).slice(0, 10),
      );
    }

    // Reaching here means no precomputed file matched, so the path is
    // non-canonical: wrong case, stray whitespace, or an inexact state name.
    const cities = path.match(/^\/v1\/countries\/([^/]+)\/states\/(.+)\/cities$/);
    if (cities) {
      const cc = cities[1].toUpperCase();
      if (!(cc in NAMES)) return notFound("Country not found");
      const state = findStateRelaxed(cc, cities[2]);
      if (!state) return notFound("State not found");
      const hit = await serveAsset(env, base, `/v1/countries/${cc}/states/${seg(state.name)}/cities`);
      return hit ?? json([]);
    }

    const detail = path.match(/^\/v1\/countries\/([^/]+)(\/states)?$/);
    if (detail) {
      const cc = detail[1].toUpperCase();
      if (!(cc in NAMES)) return notFound("Country not found");
      const hit = await serveAsset(env, base, `/v1/countries/${cc}${detail[2] ?? ""}`);
      if (hit) return hit;
    }

    const reg = path.match(/^\/v1\/regions\/([^/]+)\/countries$/);
    if (reg) {
      // path is already decoded; decoding again would throw on a literal '%'
      const regName = reg[1].trim().toLowerCase();
      const knownRegions: Record<string, string> = {
        africa: "Africa",
        americas: "Americas",
        antarctic: "Antarctic",
        asia: "Asia",
        europe: "Europe",
        oceania: "Oceania",
      };
      if (regName in knownRegions) {
        const hit = await serveAsset(env, base, `/v1/regions/${knownRegions[regName]}/countries`);
        if (hit) return hit;
      }
      return notFound("Region not found");
    }

    return notFound("Not Found");
  },
};
