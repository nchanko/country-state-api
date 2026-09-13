import fs from "node:fs/promises";
import path from "node:path";
import assert from "node:assert";
import worker from "./bundle_for_test.mjs";

const ROOT = path.resolve(import.meta.dirname, "..");
const DIST = path.join(ROOT, "dist");

// Mock Cloudflare env.ASSETS fetcher
const env = {
  ASSETS: {
    async fetch(url) {
      const u = typeof url === "string" ? new URL(url) : url instanceof Request ? new URL(url.url) : url;
      let pathname = decodeURIComponent(u.pathname);

      // Try exact file, .html, or index.html
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
        } catch {
          // Continue to next candidate
        }
      }
      return new Response(JSON.stringify({ detail: "Not Found" }), { status: 404 });
    },
  },
};

async function runTests() {
  console.log("🧪 Running Cloudflare Worker verification tests...\n");

  // Test 1: City search for Yangon
  console.log("Test 1: GET /v1/search/cities?q=Yangon");
  let res = await worker.fetch(new Request("https://worker.local/v1/search/cities?q=Yangon"), env);
  assert.strictEqual(res.status, 200);
  let data = await res.json();
  assert(Array.isArray(data) && data.length > 0, "Expected cities returned for Yangon");
  console.log(`  ✓ Found ${data.length} results. First match: ${data[0].name}, ${data[0].country_name}`);

  // Test 2: City search for Tokyo
  console.log("\nTest 2: GET /v1/search/cities?q=Tokyo");
  res = await worker.fetch(new Request("https://worker.local/v1/search/cities?q=Tokyo"), env);
  assert.strictEqual(res.status, 200);
  data = await res.json();
  assert(Array.isArray(data) && data.length > 0, "Expected cities returned for Tokyo");
  console.log(`  ✓ Found ${data.length} results. First match: ${data[0].name}, ${data[0].country_name}`);

  // Test 3: State search for California
  console.log("\nTest 3: GET /v1/search/states?q=California");
  res = await worker.fetch(new Request("https://worker.local/v1/search/states?q=California"), env);
  assert.strictEqual(res.status, 200);
  data = await res.json();
  assert(Array.isArray(data) && data.length > 0, "Expected states returned for California");
  console.log(`  ✓ Found ${data.length} states. First match: ${data[0].name} (${data[0].country_code})`);

  // Test 4: Country search for Myanmar
  console.log("\nTest 4: GET /v1/search/countries?q=Myanmar");
  res = await worker.fetch(new Request("https://worker.local/v1/search/countries?q=Myanmar"), env);
  assert.strictEqual(res.status, 200);
  data = await res.json();
  assert(Array.isArray(data) && data.length > 0, "Expected country returned for Myanmar");
  console.log(`  ✓ Found ${data.length} countries. First match: ${data[0].name} (${data[0].code})`);

  // Test 5: Phone code search
  console.log("\nTest 5: GET /v1/search/phone-code/+95");
  res = await worker.fetch(new Request("https://worker.local/v1/search/phone-code/+95"), env);
  assert.strictEqual(res.status, 200);
  data = await res.json();
  assert(Array.isArray(data) && data.length > 0, "Expected country for +95");
  console.log(`  ✓ Found ${data.length} country. First match: ${data[0].name} (${data[0].phone_code})`);

  // Test 6: Non-canonical relaxed path lookup
  console.log("\nTest 6: GET /v1/countries/us/states/california/cities (relaxed route)");
  res = await worker.fetch(new Request("https://worker.local/v1/countries/us/states/california/cities"), env);
  assert.strictEqual(res.status, 200);
  data = await res.json();
  assert(Array.isArray(data) && data.length > 0, "Expected cities in California");
  console.log(`  ✓ Found ${data.length} cities in California via relaxed route.`);

  // Test 7: Direct static asset check (e.g. MM country data)
  console.log("\nTest 7: Direct static asset fetch for /v1/countries/MM");
  const assetRes = await env.ASSETS.fetch(new URL("https://worker.local/v1/countries/MM"));
  assert.strictEqual(assetRes.status, 200);
  const mmData = await assetRes.json();
  assert.strictEqual(mmData.code, "MM");
  console.log(`  ✓ Country: ${mmData.name}, Region: ${mmData.region}, Currency: ${mmData.currency}`);

  // Test 8: Direct static asset check for Myanmar states
  console.log("\nTest 8: Direct static asset fetch for /v1/countries/MM/states");
  const statesRes = await env.ASSETS.fetch(new URL("https://worker.local/v1/countries/MM/states"));
  assert.strictEqual(statesRes.status, 200);
  const mmStates = await statesRes.json();
  assert(Array.isArray(mmStates) && mmStates.length > 0);
  console.log(`  ✓ Myanmar has ${mmStates.length} states/regions pre-rendered.`);

  // Test 9: Regions check
  console.log("\nTest 9: Direct static asset fetch for /v1/regions");
  const regionsRes = await env.ASSETS.fetch(new URL("https://worker.local/v1/regions"));
  assert.strictEqual(regionsRes.status, 200);
  const regions = await regionsRes.json();
  assert(Array.isArray(regions) && regions.length > 0);
  console.log(`  ✓ Found ${regions.length} global regions.`);

  // Test 10: Malformed percent-encoding must be a clean 400, not a thrown URIError
  console.log("\nTest 10: GET /v1/countries/%E0%A4 (malformed URL)");
  res = await worker.fetch(new Request("https://worker.local/v1/countries/%E0%A4"), env);
  assert.strictEqual(res.status, 400);
  res = await worker.fetch(new Request("https://worker.local/v1/regions/%25E0/countries"), env);
  assert.strictEqual(res.status, 404);
  console.log("  ✓ Malformed URL returns 400; literal '%' in a region returns 404.");

  console.log("\n🎉 ALL 10 VERIFICATION TESTS PASSED SUCCESSFULLY!");
}

runTests().catch((err) => {
  console.error("\n❌ Test failed:", err);
  process.exit(1);
});
