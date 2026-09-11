/* Optional developer check: node --test tests/test_elk_projection.js.
 * The sandbox has no require/fetch/process globals or real Mongo/HTTP objects.
 */
const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const vm = require("node:vm");
const path = require("node:path");
const code = fs.readFileSync(path.join(__dirname, "../monstache/project_document.js"), "utf8");
function project(doc, collection) {
    const box = {module: {exports: {}}};
    vm.runInNewContext(code, box, {timeout: 1000});
    return JSON.parse(JSON.stringify(box.module.exports(doc, "day12_elk_e2e." + collection)));
}
const stamp = 1789041600000;

test("events allowlist, original ID and input immutability", () => {
    const doc = {_id: "original", company_name: " Example ", source: "bitlock", data_size: "10 GB",
        risk_level: "CRITICAL", raw_html: "<html>private</html>", unknown_field: "private"};
    const before = JSON.stringify(doc);
    assert.deepEqual(project(doc, "leaked_data"), {company_name: "Example", source: "bitlock", data_size: "10 GB"});
    assert.equal(JSON.stringify(doc), before);
});
test("BSON epoch milliseconds and legacy dates become UTC ISO", () => {
    const out = project({scraped_time: stamp}, "leaked_data");
    assert.equal(out.scraped_time, new Date(stamp).toISOString());
    assert.equal(out.first_seen, out.scraped_time);
    assert.equal(out.last_seen, out.scraped_time);
});
test("explicit observation dates preserve their instant", () => {
    const out = project({last_seen: "2026-09-10T21:00:00+09:00", scraped_time: stamp}, "leaked_data");
    assert.equal(out.last_seen, "2026-09-10T12:00:00.000Z");
});
test("Go time.Time-like values become UTC ISO", () => {
    const goTime = {Unix: () => 1789041600, Nanosecond: () => 123000000};
    const out = project({last_seen: goTime}, "leaked_data");
    assert.equal(out.last_seen, "2026-09-10T12:00:00.123Z");
});
test("primitive DateTime-like Time wrappers become UTC ISO", () => {
    const wrapped = {Time: () => ({Unix: () => 1789041600, Nanosecond: () => 456000000})};
    const out = project({changed_at: wrapped}, "leak_history");
    assert.equal(out.changed_at, "2026-09-10T12:00:00.456Z");
});
test("alerts omit claim/resume/message/exception and nested private objects", () => {
    const out = project({event_type: "UPDATED", risk_level: "HIGH", status: "SENT",
        claim_token: "private", resume_token: {private: true}, message: "private",
        raw_exception: "private", credential: "private", source: "bitlock",
        risk_reason: {nested_secret: "private"}, changed_fields: ["description", "claim_token"]}, "alert_log");
    assert.deepEqual(out, {event_type: "UPDATED", source: "bitlock", risk_level: "HIGH",
        status: "SENT", changed_fields: ["description"]});
});
test("URI, URL credentials, token-shaped values and secret assignments are redacted", () => {
    const secret = "123456789:" + "A".repeat(36);
    const out = project({risk_reason: "mongodb://db.invalid/path " + secret + " password=private",
        source: "https://user:pass@host/path"}, "alert_log");
    assert.ok(!JSON.stringify(out).includes(secret));
    assert.ok(!JSON.stringify(out).includes("mongodb://"));
    assert.ok(!JSON.stringify(out).includes("user:pass"));
    assert.ok(!JSON.stringify(out).includes("private"));
});
test("history changes use Day9 metadata fields and before/after only", () => {
    const doc = {document_id: "abc", changes: {data_size: {before: "10 GB", after: "15 GB", token: "private"},
        claim_token: {before: "private", after: "private"}, description: {before: null, after: "enriched"}}};
    const before = JSON.stringify(doc);
    assert.deepEqual(project(doc, "leak_history"), {document_id: "abc", changes: {
        data_size: {before: "10 GB", after: "15 GB"}, description: {before: null, after: "enriched"}}});
    assert.equal(JSON.stringify(doc), before);
});
test("Monstache-converted ObjectID hex and numeric references are keyword compatible", () => {
    assert.equal(project({document_id: "0123456789abcdef01234567"}, "leak_history").document_id,
        "0123456789abcdef01234567");
    assert.equal(project({document_id: 42}, "alert_log").document_id, "42");
});
test("invalid timestamps, booleans and non-integer counts are excluded", () => {
    const out = project({scraped_time: "unknown", observation_count: true, schema_version: 1.5}, "leaked_data");
    assert.deepEqual(out, {});
    assert.deepEqual(project({observation_count: 2147483648}, "leaked_data"), {});
});
test("supported integers and alerts persisted risk pass through without reclassification", () => {
    const out = project({risk_level: "LOW", attempt_count: 0, schema_version: 1}, "alert_log");
    assert.deepEqual(out, {risk_level: "LOW", attempt_count: 0, schema_version: 1, changed_fields: []});
});
test("unsupported collection fails rather than returning false to schedule a deletion", () => {
    assert.throws(() => project({_id: "x"}, "alert_state"), /unsupported namespace/);
});
