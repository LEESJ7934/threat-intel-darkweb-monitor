/* ES5 for Monstache's Otto VM. No I/O, risk classification or Mongo writes.
 * ObjectID references arrive as hex strings in 6.7.7. Original _id remains
 * the Elasticsearch document ID; do not include it in _source.
 */
var metadata = ["company_name", "company_url", "country", "data_contents",
                "data_size", "publication_date", "description", "source_url"];
var material = ["company_name", "company_url", "country", "data_contents", "data_size", "description"];
function clean(value) {
    if (typeof value !== "string") { return null; }
    return value.replace(/-----BEGIN (?:[A-Z0-9]+ )?PRIVATE KEY-----[\s\S]*?(?:-----END (?:[A-Z0-9]+ )?PRIVATE KEY-----|$)/gi, "[redacted]")
        .replace(/mongodb(?:\+srv)?:\/\/[^\s<>"']+/gi, "[redacted]")
        .replace(/\b\d{8,12}:[A-Za-z0-9_-]{30,}\b/g, "[redacted]")
        .replace(/\b[a-z][a-z0-9+.-]*:\/\/[^\s\/@<>]+@[^\s<>"']+/gi, "[redacted]")
        .replace(/\b(?:password|passwd|token|api[_-]?key|secret)\b["']?\s*[:=]\s*(?:"[^"]*"|'[^']*'|[^\s,;<>]+)/gi, "[redacted]")
        .replace(/^\s+|\s+$/g, "").slice(0, 8000);
}
function copyText(out, doc, fields) {
    fields.forEach(function (field) {
        var value = clean(doc[field]);
        if (value !== null) { out[field] = value; }
    });
}
function copyId(out, doc, field) {
    var value = doc[field];
    if (typeof value === "string") { out[field] = clean(value); }
    else if (typeof value === "number" && isFinite(value)) { out[field] = String(value); }
}
function fromMillis(value) {
    var millis = Number(value);
    if (!isFinite(millis)) { return null; }
    var stamp = new Date(millis);
    return isNaN(stamp.getTime()) ? null : stamp.toISOString();
}
function iso(value) {
    if (value === null || typeof value === "undefined" || value === "") { return null; }
    if (value instanceof Date) { return fromMillis(value.getTime()); }
    if (typeof value === "number") { return fromMillis(value); }
    if (typeof value === "string") {
        var parsed = new Date(value);
        return isNaN(parsed.getTime()) ? null : parsed.toISOString();
    }
    if (typeof value !== "object") { return null; }

    // Monstache/Otto may expose BSON datetimes as Go-backed values rather than
    // native JavaScript Date objects. Support primitive.DateTime-like wrappers
    // and time.Time-like objects without changing the Mongo source document.
    try {
        if (typeof value.Time === "function") {
            var goTime = value.Time();
            if (goTime !== value) {
                var viaTime = iso(goTime);
                if (viaTime !== null) { return viaTime; }
            }
        }
    } catch (ignoredTime) {}

    try {
        if (typeof value.UnixMilli === "function") {
            var viaMillis = fromMillis(value.UnixMilli());
            if (viaMillis !== null) { return viaMillis; }
        }
    } catch (ignoredUnixMilli) {}

    try {
        if (typeof value.Unix === "function") {
            var seconds = Number(value.Unix());
            var nanos = 0;
            if (typeof value.Nanosecond === "function") {
                nanos = Number(value.Nanosecond());
            }
            if (isFinite(seconds) && isFinite(nanos)) {
                var viaUnix = fromMillis(seconds * 1000 + Math.floor(nanos / 1000000));
                if (viaUnix !== null) { return viaUnix; }
            }
        }
    } catch (ignoredUnix) {}

    // Also tolerate Extended-JSON-like wrappers if one is supplied by a
    // different Mongo/Monstache path.
    try {
        if (typeof value.$date !== "undefined") {
            var viaExtendedJson = iso(value.$date);
            if (viaExtendedJson !== null) { return viaExtendedJson; }
        }
    } catch (ignoredExtendedJson) {}

    try {
        if (typeof value.valueOf === "function") {
            var primitive = value.valueOf();
            if (primitive !== value && (typeof primitive === "number" || typeof primitive === "string")) {
                return iso(primitive);
            }
        }
    } catch (ignoredValueOf) {}
    return null;
}
function dates(out, doc, fields) {
    fields.forEach(function (field) {
        var value = iso(doc[field]);
        if (value !== null) { out[field] = value; }
    });
}
function integers(out, doc, fields) {
    fields.forEach(function (field) {
        var value = doc[field];
        if (typeof value === "number" && isFinite(value) && Math.floor(value) === value
                && value >= 0 && value <= 2147483647) { out[field] = value; }
    });
}
module.exports = function (doc, namespace) {
    var collection = namespace.slice(namespace.lastIndexOf(".") + 1);
    var out = {};
    if (collection === "leaked_data") {
        copyText(out, doc, metadata.concat(["source", "event_key", "identity_basis", "content_fingerprint"]));
        dates(out, doc, ["first_seen", "last_seen", "scraped_time"]);
        // ES-only fallback for legacy documents; Mongo stays unchanged.
        if (!out.first_seen && out.scraped_time) { out.first_seen = out.scraped_time; }
        if (!out.last_seen && out.scraped_time) { out.last_seen = out.scraped_time; }
        integers(out, doc, ["observation_count", "schema_version"]);
    } else if (collection === "leak_history") {
        copyText(out, doc, ["event_key", "source"]);
        copyId(out, doc, "document_id");
        dates(out, doc, ["changed_at"]);
        integers(out, doc, ["schema_version"]);
        out.changes = {};
        metadata.forEach(function (field) {
            var delta = doc.changes && doc.changes[field];
            if (delta && typeof delta === "object" && !Array.isArray(delta)) {
                out.changes[field] = {before: clean(delta.before), after: clean(delta.after)};
            }
        });
    } else if (collection === "alert_log") {
        copyText(out, doc, ["event_type", "event_key", "source", "risk_level", "risk_reason",
                           "actor", "status", "suppression_reason"]);
        copyId(out, doc, "document_id");
        copyId(out, doc, "history_id");
        dates(out, doc, ["created_at", "updated_at", "sent_at"]);
        integers(out, doc, ["attempt_count", "schema_version"]);
        out.changed_fields = material.filter(function (field) {
            return Array.isArray(doc.changed_fields) && doc.changed_fields.indexOf(field) !== -1;
        });
    } else {
        // false schedules a delete in Monstache. Fail instead.
        throw new Error("unsupported namespace");
    }
    return out;
};
