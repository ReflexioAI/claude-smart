import { eventProperties, sessionIDFrom } from "./internal.js";
function textFromPart(part) {
    if (!part || typeof part !== "object")
        return undefined;
    const item = part;
    if (item.type !== "text")
        return undefined;
    if (typeof item.text !== "string")
        return undefined;
    return {
        id: item.id || `${item.messageID || "message"}:${item.type}`,
        text: item.text,
        messageID: item.messageID,
    };
}
function partIDFrom(part) {
    if (!part || typeof part !== "object")
        return undefined;
    const item = part;
    return item.id || (item.messageID && item.type ? `${item.messageID}:${item.type}` : undefined);
}
function emptySession(messageID) {
    return { messageID, parts: new Map(), textPartIDs: new Set(), ignoredPartIDs: new Set() };
}
export class AssistantBuffer {
    sessions = new Map();
    update(event) {
        const type = event.type;
        const properties = eventProperties(event);
        const sessionID = sessionIDFrom(properties);
        if (!sessionID || !type)
            return;
        if (type === "message.updated") {
            const info = properties.info;
            if (!info || typeof info !== "object")
                return;
            const message = info;
            if (message.role !== "assistant")
                return;
            const current = this.sessions.get(sessionID);
            if (!current || current.messageID !== message.id) {
                this.sessions.set(sessionID, emptySession(message.id));
            }
            return;
        }
        if (type === "message.part.updated") {
            const hit = textFromPart(properties.part);
            if (!hit) {
                const ignoredID = partIDFrom(properties.part);
                if (!ignoredID)
                    return;
                const current = this.sessions.get(sessionID) ?? emptySession();
                current.ignoredPartIDs.add(ignoredID);
                current.textPartIDs.delete(ignoredID);
                current.parts.delete(ignoredID);
                this.sessions.set(sessionID, current);
                return;
            }
            const current = this.sessions.get(sessionID) ?? emptySession();
            if (hit.messageID && current.messageID && hit.messageID !== current.messageID) {
                current.messageID = hit.messageID;
                current.parts.clear();
                current.textPartIDs.clear();
                current.ignoredPartIDs.clear();
            }
            else if (hit.messageID) {
                current.messageID = hit.messageID;
            }
            current.ignoredPartIDs.delete(hit.id);
            current.textPartIDs.add(hit.id);
            current.parts.set(hit.id, hit.text);
            this.sessions.set(sessionID, current);
            return;
        }
        if (type === "message.part.delta") {
            const partID = typeof properties.partID === "string" ? properties.partID : undefined;
            const delta = typeof properties.delta === "string" ? properties.delta : undefined;
            if (!partID || !delta)
                return;
            const current = this.sessions.get(sessionID) ?? emptySession();
            if (current.ignoredPartIDs.has(partID) || !current.textPartIDs.has(partID))
                return;
            current.parts.set(partID, `${current.parts.get(partID) || ""}${delta}`);
            this.sessions.set(sessionID, current);
        }
    }
    text(sessionID) {
        const current = this.sessions.get(sessionID);
        if (!current)
            return "";
        return Array.from(current.parts.values()).filter(Boolean).join("\n\n");
    }
    clear(sessionID) {
        this.sessions.delete(sessionID);
    }
}
