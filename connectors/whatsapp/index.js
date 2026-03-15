#!/usr/bin/env node
/**
 * Lobster WhatsApp Bridge
 *
 * Standalone Node.js service that:
 *   1. Authenticates to WhatsApp Web via QR code (first run only)
 *   2. Persists session across restarts via LocalAuth
 *   3. Emits message events as NDJSON to stdout (consumed by whatsapp_bridge_adapter.py)
 *   4. Accepts reply commands by watching WA_COMMANDS_DIR for JSON files
 *
 * Stdout: NDJSON message events only (no logs)
 * Stderr: all logs, status messages, QR code display
 *
 * Environment variables (all optional with sensible defaults):
 *   WHATSAPP_SESSION_PATH   - Where to store the LocalAuth session (default: ./session)
 *   WHATSAPP_LOBSTER_JID    - Lobster's own WhatsApp JID, e.g. 15551234567@c.us
 *                             Auto-detected after first connection; set this after first run.
 *   WA_COMMANDS_DIR         - Directory to watch for outgoing message commands
 *                             (default: ~/messages/wa-commands)
 *   WA_EVENTS_DIR           - Directory to write message events as individual JSON files
 *                             (alternative to stdout for inter-process resilience)
 *                             If set, events are also written here in addition to stdout.
 *   WA_HEARTBEAT_FILE       - File to touch on each received message
 *                             (default: ~/lobster-workspace/logs/whatsapp-heartbeat)
 *   NODE_ENV                - Set to "production" for production deployments
 */

'use strict';

const path = require('path');
const fs = require('fs');

// ---------------------------------------------------------------------------
// Configuration from environment
// ---------------------------------------------------------------------------

const HOME = process.env.HOME || '/home/admin';
const SESSION_PATH = process.env.WHATSAPP_SESSION_PATH || path.join(__dirname, 'session');
const COMMANDS_DIR = process.env.WA_COMMANDS_DIR || path.join(HOME, 'messages', 'wa-commands');
const EVENTS_DIR = process.env.WA_EVENTS_DIR || null;
const HEARTBEAT_FILE = process.env.WA_HEARTBEAT_FILE || path.join(HOME, 'lobster-workspace', 'logs', 'whatsapp-heartbeat');

// ---------------------------------------------------------------------------
// Ensure directories exist
// ---------------------------------------------------------------------------

function ensureDir(dir) {
    try {
        fs.mkdirSync(dir, { recursive: true });
    } catch (e) {
        // Ignore if already exists
    }
}

ensureDir(COMMANDS_DIR);
if (EVENTS_DIR) ensureDir(EVENTS_DIR);
ensureDir(path.dirname(HEARTBEAT_FILE));

// ---------------------------------------------------------------------------
// Load whatsapp-web.js (may not be installed in test environment)
// ---------------------------------------------------------------------------

let Client, LocalAuth, qrcode, chokidar;

try {
    ({ Client, LocalAuth } = require('whatsapp-web.js'));
    qrcode = require('qrcode-terminal');
    chokidar = require('chokidar');
} catch (e) {
    // Allow loading in test environments without full npm install
    if (process.env.NODE_ENV === 'test') {
        module.exports = { buildMessageEvent, parseCommandFile, emitEvent };
        process.exit(0);
    }
    console.error('[FATAL] Missing dependencies. Run: npm install');
    console.error(e.message);
    process.exit(1);
}

// ---------------------------------------------------------------------------
// Core data functions (exported for testing)
// ---------------------------------------------------------------------------

/**
 * Build a normalized message event from a whatsapp-web.js message object.
 * Returns a plain object suitable for NDJSON serialization.
 *
 * @param {object} msg - whatsapp-web.js Message object
 * @param {string|null} myJid - Lobster's own JID for mention detection
 * @param {string} chatName - display name of the chat/group
 * @returns {object} normalized event
 */
function buildMessageEvent(msg, myJid, chatName) {
    const isGroup = typeof msg.from === 'string' && msg.from.endsWith('@g.us');

    // Normalize mentionedIds: handle both string and {_serialized: ...} formats
    const mentionedIds = (msg.mentionedIds || []).map((id) => {
        if (typeof id === 'string') return id;
        if (id && typeof id._serialized === 'string') return id._serialized;
        return String(id);
    });

    const mentionsLobster = myJid ? mentionedIds.includes(myJid) : false;

    return {
        id: msg.id && msg.id._serialized ? msg.id._serialized : String(msg.id),
        body: msg.body || '',
        from: msg.from || '',
        fromMe: Boolean(msg.fromMe),
        isGroup,
        author: msg.author || msg.from || '',
        timestamp: msg.timestamp || Math.floor(Date.now() / 1000),
        mentionedIds,
        mentions_lobster: mentionsLobster,
        chatName: chatName || '',
    };
}

/**
 * Parse a command file written by whatsapp_bridge_adapter.py.
 * Returns null if the file is invalid.
 *
 * Expected format: {"action": "send", "to": "<jid>", "text": "..."}
 *
 * @param {string} filePath - path to the JSON command file
 * @returns {object|null} parsed command or null on error
 */
function parseCommandFile(filePath) {
    try {
        const raw = fs.readFileSync(filePath, 'utf8');
        const cmd = JSON.parse(raw);
        if (!cmd.action || !cmd.to || !cmd.text) {
            console.error('[CMD] Invalid command file (missing action/to/text):', filePath);
            return null;
        }
        return cmd;
    } catch (e) {
        console.error('[CMD] Failed to parse command file:', filePath, e.message);
        return null;
    }
}

/**
 * Emit a message event to stdout as NDJSON.
 * If EVENTS_DIR is set, also write to an individual JSON file there.
 *
 * @param {object} event - normalized message event
 */
function emitEvent(event) {
    // Stdout: NDJSON (only output on stdout — no logs ever go here)
    process.stdout.write(JSON.stringify(event) + '\n');

    // Optional: also write to events directory for file-based IPC
    if (EVENTS_DIR) {
        const filename = `${event.id.replace(/[^a-zA-Z0-9_-]/g, '_')}_${Date.now()}.json`;
        const filePath = path.join(EVENTS_DIR, filename);
        try {
            fs.writeFileSync(filePath, JSON.stringify(event));
        } catch (e) {
            console.error('[EVENT] Failed to write event file:', e.message);
        }
    }
}

/**
 * Write a system event (e.g. session expired) to the events directory or stdout.
 *
 * @param {string} subtype - e.g. 'session_expired', 'connected', 'disconnected'
 * @param {string} message - human-readable message text
 */
function emitSystemEvent(subtype, message) {
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
}

/**
 * Touch the heartbeat file to signal that the bridge is alive and processing.
 */
function touchHeartbeat() {
    try {
        const now = new Date().toISOString();
        fs.writeFileSync(HEARTBEAT_FILE, now);
    } catch (e) {
        // Non-fatal
    }
}

// ---------------------------------------------------------------------------
// Export for testing (mock-test.js)
// ---------------------------------------------------------------------------

module.exports = { buildMessageEvent, parseCommandFile, emitEvent, emitSystemEvent };

// If this file is run directly (not required), start the bridge
if (require.main === module) {
    startBridge();
}

// ---------------------------------------------------------------------------
// Bridge startup
// ---------------------------------------------------------------------------

function startBridge() {
    console.error('[INIT] Starting Lobster WhatsApp Bridge');
    console.error('[INIT] Session path:', SESSION_PATH);
    console.error('[INIT] Commands dir:', COMMANDS_DIR);
    console.error('[INIT] Heartbeat file:', HEARTBEAT_FILE);

    // ---------------------------------------------------------------------------
    // State
    // ---------------------------------------------------------------------------

    // Lobster's own WhatsApp JID — set from env or auto-detected after ready
    let myJid = process.env.WHATSAPP_LOBSTER_JID || null;

    // Reconnect state
    let isReconnecting = false;
    const MAX_RECONNECT_ATTEMPTS = 5;
    let reconnectAttempts = 0;

    // ---------------------------------------------------------------------------
    // Initialize client
    // ---------------------------------------------------------------------------

    const client = new Client({
        authStrategy: new LocalAuth({ dataPath: SESSION_PATH }),
        puppeteer: {
            args: [
                '--no-sandbox',
                '--disable-setuid-sandbox',
                '--disable-dev-shm-usage',
                '--disable-accelerated-2d-canvas',
                '--no-first-run',
                '--no-zygote',
                '--single-process',
                '--disable-gpu',
            ],
            headless: true,
        },
    });

    // ---------------------------------------------------------------------------
    // QR code — first-run authentication
    // ---------------------------------------------------------------------------

    client.on('qr', (qr) => {
        qrcode.generate(qr, { small: true });
        console.error('[QR] Scan this QR code in WhatsApp: Settings > Linked Devices > Link a Device');
        console.error('[QR] After scanning, the bridge will print READY and your JID');
        console.error('[QR] Set WHATSAPP_LOBSTER_JID in your environment file to that JID');
    });

    // ---------------------------------------------------------------------------
    // Ready — connected and authenticated
    // ---------------------------------------------------------------------------

    client.on('ready', () => {
        // Auto-detect our own JID if not set via env
        if (!myJid && client.info && client.info.wid) {
            myJid = client.info.wid._serialized;
            console.error('[READY] Detected Lobster JID:', myJid);
            console.error('[READY] Set WHATSAPP_LOBSTER_JID=' + myJid + ' in your config');
        } else if (myJid) {
            console.error('[READY] Using JID from env:', myJid);
        }

        // Reset reconnect state
        reconnectAttempts = 0;
        isReconnecting = false;

        // Touch heartbeat
        touchHeartbeat();

        console.error('[READY] WhatsApp bridge connected and listening');
    });

    // ---------------------------------------------------------------------------
    // Incoming messages
    // ---------------------------------------------------------------------------

    client.on('message', async (msg) => {
        // Skip messages sent by us
        if (msg.fromMe) return;

        const isGroup = typeof msg.from === 'string' && msg.from.endsWith('@g.us');

        // Normalize mentionedIds early for filtering
        const mentionedIds = (msg.mentionedIds || []).map((id) => {
            if (typeof id === 'string') return id;
            if (id && typeof id._serialized === 'string') return id._serialized;
            return String(id);
        });
        const mentionsLobster = myJid ? mentionedIds.includes(myJid) : false;

        // Filter: group messages only pass if they mention Lobster
        if (isGroup && !mentionsLobster) return;

        // Resolve group/chat name
        let chatName = '';
        try {
            const chat = await msg.getChat();
            chatName = (chat && chat.name) ? chat.name : '';
        } catch (e) {
            // Non-fatal
        }

        const event = buildMessageEvent(msg, myJid, chatName);
        emitEvent(event);
        touchHeartbeat();
    });

    // ---------------------------------------------------------------------------
    // Disconnect / reconnect handling
    // ---------------------------------------------------------------------------

    client.on('disconnected', async (reason) => {
        console.error('[DISCONNECTED]', reason);

        if (reason === 'LOGOUT') {
            // Session invalidated by WhatsApp — need fresh QR scan
            console.error('[SESSION] Session expired or logged out by WhatsApp');

            // Delete session directory so next startup prompts QR
            try {
                fs.rmSync(SESSION_PATH, { recursive: true, force: true });
                console.error('[SESSION] Deleted expired session at', SESSION_PATH);
            } catch (e) {
                console.error('[SESSION] Could not delete session:', e.message);
            }

            // Notify Drew via the event bus
            emitSystemEvent(
                'session_expired',
                'Session expired — QR scan required. Restart the service: sudo systemctl restart lobster-whatsapp-bridge'
            );

            // Exit cleanly — systemd will restart and trigger QR mode
            process.exit(1);
        } else {
            // Transient disconnect — attempt reconnect with exponential backoff
            if (!isReconnecting && reconnectAttempts < MAX_RECONNECT_ATTEMPTS) {
                isReconnecting = true;
                reconnectAttempts++;
                const delay = Math.min(5000 * reconnectAttempts, 60000);
                console.error(
                    `[RECONNECT] Attempt ${reconnectAttempts}/${MAX_RECONNECT_ATTEMPTS} in ${delay}ms (reason: ${reason})`
                );
                setTimeout(async () => {
                    try {
                        await client.initialize();
                        isReconnecting = false;
                        console.error('[RECONNECT] Re-initialization complete');
                    } catch (e) {
                        console.error('[RECONNECT] Failed:', e.message);
                        isReconnecting = false;
                    }
                }, delay);
            } else if (reconnectAttempts >= MAX_RECONNECT_ATTEMPTS) {
                console.error('[RECONNECT] Max attempts reached, exiting for systemd restart');
                process.exit(1);
            }
        }
    });

    // ---------------------------------------------------------------------------
    // Command file watcher — outgoing messages
    // ---------------------------------------------------------------------------

    const watcher = chokidar.watch(path.join(COMMANDS_DIR, '*.json'), {
        persistent: true,
        ignoreInitial: false,
        awaitWriteFinish: { stabilityThreshold: 200, pollInterval: 100 },
    });

    watcher.on('add', async (filePath) => {
        const cmd = parseCommandFile(filePath);
        if (!cmd) {
            // Remove invalid files to avoid retry loops
            try { fs.unlinkSync(filePath); } catch (e) {}
            return;
        }

        try {
            const chat = await client.getChatById(cmd.to);
            await chat.sendMessage(cmd.text);
            console.error('[SEND] Sent reply to', cmd.to, '-', cmd.text.substring(0, 50));
        } catch (e) {
            console.error('[SEND] Failed to send to', cmd.to, ':', e.message);
        }

        // Remove command file after processing (success or failure)
        try { fs.unlinkSync(filePath); } catch (e) {}
    });

    watcher.on('error', (err) => {
        console.error('[WATCH] Watcher error:', err.message);
    });

    // ---------------------------------------------------------------------------
    // Graceful shutdown
    // ---------------------------------------------------------------------------

    async function shutdown(signal) {
        console.error('[SHUTDOWN] Received', signal, '— shutting down gracefully');
        try {
            watcher.close();
            await client.destroy();
        } catch (e) {
            // Ignore cleanup errors
        }
        process.exit(0);
    }

    process.on('SIGINT', () => shutdown('SIGINT'));
    process.on('SIGTERM', () => shutdown('SIGTERM'));

    // ---------------------------------------------------------------------------
    // Start
    // ---------------------------------------------------------------------------

    console.error('[INIT] Initializing WhatsApp client...');
    client.initialize().catch((e) => {
        console.error('[FATAL] Failed to initialize client:', e.message);
        process.exit(1);
    });
}
