#!/usr/bin/env node
/**
 * Lobster WhatsApp Bridge (Baileys)
 *
 * Standalone Node.js service that:
 *   1. Authenticates to WhatsApp via QR code (first run only)
 *   2. Persists session as multi-file auth state (JSON files, no browser)
 *   3. Writes incoming message events as JSON files to WA_EVENTS_DIR
 *   4. Watches WA_COMMANDS_DIR for outgoing message commands
 *
 * Uses @whiskeysockets/baileys — direct WebSocket to WhatsApp servers,
 * no Puppeteer/Chromium dependency.
 *
 * Stdout: NDJSON message events only (no logs)
 * Stderr: all logs, status messages, QR code display
 *
 * Environment variables (all optional with sensible defaults):
 *   WHATSAPP_SESSION_PATH    Where to store auth state files
 *                            (default: ~/.config/lobster/whatsapp-session)
 *   WHATSAPP_LOBSTER_JID     Lobster's own WhatsApp JID, e.g. 15551234567@c.us
 *                            Auto-detected after first connection; set this after first run.
 *   WA_COMMANDS_DIR          Directory to watch for outgoing message commands
 *                            (default: ~/messages/wa-commands)
 *   WA_EVENTS_DIR            Directory to write message events as individual JSON files
 *                            (default: ~/messages/wa-events)
 *   WA_HEARTBEAT_FILE        File to touch on each received message
 *                            (default: ~/lobster-workspace/logs/whatsapp-heartbeat)
 *   WHATSAPP_ALLOWED_JIDS    Comma-separated whitelist of JIDs allowed to message Lobster.
 *                            If empty, all DMs are accepted.
 *   NODE_ENV                 Set to "production" for production deployments
 */

'use strict';

const path = require('path');
const fs = require('fs');
const os = require('os');

// ---------------------------------------------------------------------------
// Configuration from environment
// ---------------------------------------------------------------------------

const HOME = process.env.HOME || os.homedir();
const XDG_CONFIG = process.env.XDG_CONFIG_HOME || path.join(HOME, '.config');

const SESSION_PATH = process.env.WHATSAPP_SESSION_PATH
    || path.join(XDG_CONFIG, 'lobster', 'whatsapp-session');

const COMMANDS_DIR = process.env.WA_COMMANDS_DIR
    || path.join(HOME, 'messages', 'wa-commands');

const EVENTS_DIR = process.env.WA_EVENTS_DIR
    || path.join(HOME, 'messages', 'wa-events');

const HEARTBEAT_FILE = process.env.WA_HEARTBEAT_FILE
    || path.join(HOME, 'lobster-workspace', 'logs', 'whatsapp-heartbeat');

// Comma-separated list of allowed sender JIDs (phone@c.us). Empty = allow all.
const ALLOWED_JIDS = (process.env.WHATSAPP_ALLOWED_JIDS || '')
    .split(',')
    .map(s => s.trim())
    .filter(Boolean);

// ---------------------------------------------------------------------------
// Ensure directories exist
// ---------------------------------------------------------------------------

const ensureDir = (dir) => fs.mkdirSync(dir, { recursive: true });

ensureDir(SESSION_PATH);
ensureDir(COMMANDS_DIR);
ensureDir(EVENTS_DIR);
ensureDir(path.dirname(HEARTBEAT_FILE));

// ---------------------------------------------------------------------------
// Core data functions (pure, exported for testing)
// ---------------------------------------------------------------------------

/**
 * Build a normalized message event from a Baileys message object.
 * Returns a plain object suitable for NDJSON serialization.
 *
 * @param {object} msg - Baileys proto.IWebMessageInfo
 * @param {string|null} myJid - Lobster's own JID for mention detection
 * @param {string} chatName - display name of the chat/group
 * @returns {object} normalized event
 */
const buildMessageEvent = (msg, myJid, chatName) => {
    const key = msg.key || {};
    const remoteJid = key.remoteJid || '';
    const isGroup = remoteJid.endsWith('@g.us');
    const fromMe = Boolean(key.fromMe);

    // Extract text body from various message types
    const msgContent = msg.message || {};
    const body = msgContent.conversation
        || (msgContent.extendedTextMessage && msgContent.extendedTextMessage.text)
        || (msgContent.imageMessage && msgContent.imageMessage.caption)
        || (msgContent.videoMessage && msgContent.videoMessage.caption)
        || (msgContent.buttonsResponseMessage && msgContent.buttonsResponseMessage.selectedDisplayText)
        || (msgContent.listResponseMessage && msgContent.listResponseMessage.title)
        || '';

    // In groups, participant is the individual sender; remoteJid is the group
    const author = (isGroup ? key.participant : remoteJid) || remoteJid;

    // Extract mentioned JIDs from extended text message
    const mentionedIds = (
        (msgContent.extendedTextMessage && msgContent.extendedTextMessage.contextInfo
            && msgContent.extendedTextMessage.contextInfo.mentionedJid)
        || []
    );

    const mentionsLobster = myJid
        ? mentionedIds.some(jid => jid === myJid || jid.split('@')[0] === myJid.split('@')[0])
        : false;

    const msgId = key.id || `baileys_${Date.now()}`;
    const timestamp = typeof msg.messageTimestamp === 'object'
        ? Number(msg.messageTimestamp)
        : (msg.messageTimestamp || Math.floor(Date.now() / 1000));

    return {
        id: `${remoteJid}_${msgId}`,
        body,
        from: remoteJid,
        fromMe,
        isGroup,
        author,
        timestamp,
        mentionedIds,
        mentions_lobster: mentionsLobster,
        chatName: chatName || '',
    };
};

/**
 * Parse a command file written by whatsapp_bridge_adapter.py.
 * Returns null if the file is invalid.
 *
 * Expected format: {"action": "send", "to": "<jid>", "text": "..."}
 *
 * @param {string} filePath - path to the JSON command file
 * @returns {object|null} parsed command or null on error
 */
const parseCommandFile = (filePath) => {
    try {
        const raw = fs.readFileSync(filePath, 'utf8');
        const cmd = JSON.parse(raw);
        if (!cmd.action || !cmd.to || !cmd.text) {
            process.stderr.write(`[CMD] Invalid command file (missing action/to/text): ${filePath}\n`);
            return null;
        }
        return cmd;
    } catch (e) {
        process.stderr.write(`[CMD] Failed to parse command file: ${filePath} — ${e.message}\n`);
        return null;
    }
};

/**
 * Emit a message event to stdout as NDJSON and write to EVENTS_DIR.
 *
 * @param {object} event - normalized message event
 */
const emitEvent = (event) => {
    // Stdout: NDJSON (no logs ever go here)
    process.stdout.write(JSON.stringify(event) + '\n');

    // Also write to events directory for file-based IPC
    const safeId = event.id.replace(/[^a-zA-Z0-9_-]/g, '_');
    const filename = `${safeId}_${Date.now()}.json`;
    const filePath = path.join(EVENTS_DIR, filename);
    try {
        fs.writeFileSync(filePath, JSON.stringify(event));
    } catch (e) {
        process.stderr.write(`[EVENT] Failed to write event file: ${e.message}\n`);
    }
};

/**
 * Write a system event (e.g. session expired) to the events directory and stdout.
 *
 * @param {string} subtype - e.g. 'session_expired', 'connected', 'disconnected'
 * @param {string} message - human-readable message text
 */
const emitSystemEvent = (subtype, message) => {
    const event = {
        id: `sys_${Date.now()}`,
        type: 'system',
        subtype,
        body: `[WhatsApp bridge] ${message}`,
        from: 'system',
        fromMe: false,
        isGroup: false,
        author: 'system',
        timestamp: Math.floor(Date.now() / 1000),
        mentionedIds: [],
        mentions_lobster: false,
        chatName: '',
    };
    emitEvent(event);
};

/**
 * Touch the heartbeat file to signal that the bridge is alive and processing.
 */
const touchHeartbeat = () => {
    try {
        fs.writeFileSync(HEARTBEAT_FILE, new Date().toISOString());
    } catch (e) {
        // Non-fatal
    }
};

/**
 * Determine whether a message should be processed.
 * Drops: fromMe, non-text (no body), non-whitelisted senders.
 *
 * @param {object} event - normalized event
 * @returns {boolean}
 */
const isAllowed = (event) => {
    if (event.fromMe) return false;
    if (!event.body) return false;
    if (ALLOWED_JIDS.length > 0 && !ALLOWED_JIDS.includes(event.author)) return false;
    return true;
};

// ---------------------------------------------------------------------------
// Export for testing
// ---------------------------------------------------------------------------

module.exports = { buildMessageEvent, parseCommandFile, emitEvent, emitSystemEvent, isAllowed };

if (require.main === module) {
    startBridge();
}

// ---------------------------------------------------------------------------
// Bridge startup
// ---------------------------------------------------------------------------

async function startBridge() {
    // Baileys and chokidar must be available at runtime
    let makeWASocket, DisconnectReason, useMultiFileAuthState, fetchLatestBaileysVersion, chokidar, pino, qrcode;

    try {
        ({ makeWASocket, DisconnectReason, useMultiFileAuthState, fetchLatestBaileysVersion }
            = require('@whiskeysockets/baileys'));
        chokidar = require('chokidar');
        pino = require('pino');
        qrcode = require('qrcode-terminal');
    } catch (e) {
        process.stderr.write('[FATAL] Missing dependencies. Run: npm install\n');
        process.stderr.write(e.message + '\n');
        process.exit(1);
    }

    process.stderr.write('[INIT] Starting Lobster WhatsApp Bridge (Baileys)\n');
    process.stderr.write(`[INIT] Session path: ${SESSION_PATH}\n`);
    process.stderr.write(`[INIT] Commands dir: ${COMMANDS_DIR}\n`);
    process.stderr.write(`[INIT] Events dir:   ${EVENTS_DIR}\n`);

    // Suppress Baileys verbose internal logs — route only to stderr
    const logger = pino({ level: 'warn' }, process.stderr);

    let myJid = process.env.WHATSAPP_LOBSTER_JID || null;
    let sock = null;
    let reconnectTimer = null;
    let reconnectAttempts = 0;
    const MAX_RECONNECT_ATTEMPTS = 10;
    let shuttingDown = false;

    // Command file watcher (chokidar) — set up once, persists across reconnects
    const watcher = chokidar.watch(path.join(COMMANDS_DIR, '*.json'), {
        persistent: true,
        ignoreInitial: false,
        awaitWriteFinish: { stabilityThreshold: 200, pollInterval: 100 },
    });

    watcher.on('add', async (filePath) => {
        const cmd = parseCommandFile(filePath);
        if (!cmd) {
            try { fs.unlinkSync(filePath); } catch (e) {}
            return;
        }

        if (!sock) {
            process.stderr.write(`[SEND] Not connected — dropping command to ${cmd.to}\n`);
            try { fs.unlinkSync(filePath); } catch (e) {}
            return;
        }

        try {
            // Ensure JID is properly formatted
            const jid = cmd.to.includes('@') ? cmd.to : `${cmd.to}@c.us`;
            await sock.sendMessage(jid, { text: cmd.text });
            process.stderr.write(`[SEND] Sent reply to ${jid} — ${cmd.text.substring(0, 60)}\n`);
        } catch (e) {
            process.stderr.write(`[SEND] Failed to send to ${cmd.to}: ${e.message}\n`);
        }

        try { fs.unlinkSync(filePath); } catch (e) {}
    });

    watcher.on('error', (err) => {
        process.stderr.write(`[WATCH] Watcher error: ${err.message}\n`);
    });

    // ---------------------------------------------------------------------------
    // Graceful shutdown
    // ---------------------------------------------------------------------------

    const shutdown = async (signal) => {
        if (shuttingDown) return;
        shuttingDown = true;
        process.stderr.write(`[SHUTDOWN] Received ${signal} — shutting down gracefully\n`);
        if (reconnectTimer) clearTimeout(reconnectTimer);
        try { watcher.close(); } catch (e) {}
        try { if (sock) await sock.logout(); } catch (e) {}
        process.exit(0);
    };

    process.on('SIGINT', () => shutdown('SIGINT'));
    process.on('SIGTERM', () => shutdown('SIGTERM'));

    // ---------------------------------------------------------------------------
    // Connect function — called on startup and each reconnect
    // ---------------------------------------------------------------------------

    const connect = async () => {
        const { state, saveCreds } = await useMultiFileAuthState(SESSION_PATH);
        const { version } = await fetchLatestBaileysVersion();
        process.stderr.write(`[INIT] Using WA version: ${version.join('.')}\n`);

        sock = makeWASocket({
            version,
            auth: state,
            logger,
            // Suppress browser console spam
            browser: ['Lobster', 'Chrome', '131.0.0'],
            // Prefer latest message format
            getMessage: async () => undefined,
        });

        // Persist auth state changes
        sock.ev.on('creds.update', saveCreds);

        // ---------------------------------------------------------------------------
        // Connection state changes
        // ---------------------------------------------------------------------------

        sock.ev.on('connection.update', async (update) => {
            const { connection, lastDisconnect, qr } = update;

            if (qr) {
                // Render QR code in the terminal
                qrcode.generate(qr, { small: true }, (code) => {
                    process.stderr.write(code + '\n');
                });
                process.stderr.write('[QR] Scan the QR code above in WhatsApp:\n');
                process.stderr.write('[QR]   Settings > Linked Devices > Link a Device\n');
                process.stderr.write('[QR] After scanning, the bridge will print [READY] and your JID.\n');
                process.stderr.write('[QR] Add WHATSAPP_LOBSTER_JID=<jid> to your config file.\n');
            }

            if (connection === 'open') {
                reconnectAttempts = 0;

                // Auto-detect own JID
                if (sock.user) {
                    const detectedJid = sock.user.id.replace(/:\d+@/, '@');
                    if (!myJid) {
                        myJid = detectedJid;
                        process.stderr.write(`[READY] Detected Lobster JID: ${myJid}\n`);
                        process.stderr.write(`[READY] Add this to your config: WHATSAPP_LOBSTER_JID=${myJid}\n`);
                    } else {
                        process.stderr.write(`[READY] Using JID from env: ${myJid}\n`);
                    }
                }

                touchHeartbeat();
                process.stderr.write('[READY] WhatsApp bridge connected and listening\n');
            }

            if (connection === 'close') {
                const statusCode = lastDisconnect && lastDisconnect.error
                    ? lastDisconnect.error.output && lastDisconnect.error.output.statusCode
                    : null;

                const isLogout = statusCode === DisconnectReason.loggedOut;

                process.stderr.write(`[DISCONNECTED] Reason: ${statusCode || 'unknown'} logout=${isLogout}\n`);

                if (isLogout) {
                    // Session invalidated — delete and emit session_expired so Lobster notifies Drew
                    process.stderr.write('[SESSION] Logged out by WhatsApp — deleting session\n');
                    try {
                        fs.rmSync(SESSION_PATH, { recursive: true, force: true });
                        process.stderr.write(`[SESSION] Deleted session at ${SESSION_PATH}\n`);
                    } catch (e) {
                        process.stderr.write(`[SESSION] Could not delete session: ${e.message}\n`);
                    }
                    emitSystemEvent(
                        'session_expired',
                        'WhatsApp session logged out — QR re-scan required. Run: lobster-whatsapp-qr'
                    );
                    process.exit(1);
                }

                if (!shuttingDown && reconnectAttempts < MAX_RECONNECT_ATTEMPTS) {
                    reconnectAttempts++;
                    const delay = Math.min(5000 * reconnectAttempts, 60000);
                    process.stderr.write(
                        `[RECONNECT] Attempt ${reconnectAttempts}/${MAX_RECONNECT_ATTEMPTS} in ${delay}ms\n`
                    );
                    reconnectTimer = setTimeout(connect, delay);
                } else if (reconnectAttempts >= MAX_RECONNECT_ATTEMPTS) {
                    process.stderr.write('[RECONNECT] Max attempts reached — exiting for systemd restart\n');
                    process.exit(1);
                }
            }
        });

        // ---------------------------------------------------------------------------
        // Incoming messages
        // ---------------------------------------------------------------------------

        sock.ev.on('messages.upsert', async ({ messages, type }) => {
            // 'notify' = new incoming messages; 'append' = history sync — skip appends
            if (type !== 'notify') return;

            for (const msg of messages) {
                // Resolve chat name for groups
                let chatName = '';
                const remoteJid = msg.key && msg.key.remoteJid;
                const isGroup = remoteJid && remoteJid.endsWith('@g.us');

                if (isGroup) {
                    try {
                        const meta = await sock.groupMetadata(remoteJid);
                        chatName = (meta && meta.subject) ? meta.subject : '';
                    } catch (e) {
                        // Non-fatal — group name is cosmetic
                    }
                }

                const event = buildMessageEvent(msg, myJid, chatName);

                if (!isAllowed(event)) {
                    if (event.fromMe) {
                        // Silently skip own messages
                    } else {
                        process.stderr.write(
                            `[FILTER] Dropping message from ${event.author} (body empty or not whitelisted)\n`
                        );
                    }
                    continue;
                }

                // Group filter: only route if Lobster is @mentioned
                if (isGroup && !event.mentions_lobster) {
                    process.stderr.write(`[FILTER] Group msg from ${event.author} — no @mention, skipping\n`);
                    continue;
                }

                emitEvent(event);
                touchHeartbeat();
                process.stderr.write(
                    `[MSG] from=${event.author} group=${event.isGroup} text="${event.body.substring(0, 60)}"\n`
                );
            }
        });
    };

    // Initial connection
    await connect();
}
