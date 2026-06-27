export function isRecord(value) {
    return Boolean(value) && typeof value === "object" && !Array.isArray(value);
}
export function eventProperties(event) {
    return isRecord(event.properties) ? event.properties : event;
}
export function sessionIDFrom(value) {
    if (!isRecord(value))
        return "";
    const raw = value.sessionID ?? value.session_id;
    return typeof raw === "string" ? raw : "";
}
