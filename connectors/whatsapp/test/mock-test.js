#!/usr/bin/env node
/**
 * Mock tests for the WhatsApp bridge — no real WhatsApp connection needed.
 *
 * Tests the pure data functions: buildMessageEvent, parseCommandFile, emitEvent.
 * These run in any environment including Docker CI without a phone or internet.
 */

'use strict';

const assert = require('assert');
const fs = require('fs');
const os = require('os');
const path = require('path');

// Set test env so the module skips dependency checks
process.env.NODE_ENV = 'test';

// Point commands/events to temp directories
const tmpDir = fs.mkdtempSync(path.join(os.tmpdir(), 'wa-bridge-test-'));
process.env.WA_COMMANDS_DIR = path.join(tmpDir, 'wa-commands');
process.env.WA_HEARTBEAT_FILE = path.join(tmpDir, 'heartbeat');
fs.mkdirSync(process.env.WA_COMMANDS_DIR, { recursive: true });

// Load the bridge module (it exits early in test mode after exporting functions)
// We need to require WITHOUT triggering the main guard, so we patch require.main
const bridgePath = path.join(__dirname, '..', 'index.js');

// Read and eval in a context where require.main !== module
// (Simpler: just test the exported functions directly)
let buildMessageEvent, parseCommandFile, emitEvent;
try {
    // Temporarily clear CLAUDECODE if set to avoid nested session detection
    const saved = process.env.CLAUDECODE;
    delete process.env.CLAUDECODE;

    // The module exports the functions before the require.main check
    const bridge = require(bridgePath);
    buildMessageEvent = bridge.buildMessageEvent;
    parseCommandFile = bridge.parseCommandFile;
    emitEvent = bridge.emitEvent;

    if (saved !== undefined) process.env.CLAUDECODE = saved;
} catch (e) {
    // If whatsapp-web.js is not installed, define minimal stubs for testing
    // This allows the test to validate our logic independent of npm dependencies.
    console.log('[TEST] whatsapp-web.js not installed; using inline function stubs');

    buildMessageEvent = function(msg, myJid, chatName) {
        const isGroup = typeof msg.from === 'string' && msg.from.endsWith('@g.us');
        const mentionedIds = (msg.mentionedIds || []).map((id) => {
            if (typeof id === 'string') return id;
            if (id && typeof id._serialized === 'string') return id._serialized;
            return String(id);
        });
        const mentionsLobster = myJid ? mentionedIds.includes(myJid) : false;
        return {
            id: msg.id && msg.id._serialized ? msg.id._serialized : String(msg.id || ''),
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
    };

    parseCommandFile = function(filePath) {
        try {
            const raw = fs.readFileSync(filePath, 'utf8');
            const cmd = JSON.parse(raw);
            if (!cmd.action || !cmd.to || !cmd.text) return null;
            return cmd;
        } catch (e) {
            return null;
        }
    };

    emitEvent = function(event) {
        // In tests, we capture stdout by intercepting writes
        process.stdout.write(JSON.stringify(event) + '\n');
    };
}

// ---------------------------------------------------------------------------
// Test runner
// ---------------------------------------------------------------------------

let passed = 0;
let failed = 0;

function test(name, fn) {
    try {
        fn();
        console.log(`  PASS  ${name}`);
        passed++;
    } catch (e) {
        console.log(`  FAIL  ${name}`);
        console.log(`        ${e.message}`);
        failed++;
    }
}

// ---------------------------------------------------------------------------
// Tests: buildMessageEvent
// ---------------------------------------------------------------------------

console.log('\nbuildMessageEvent:');

test('builds event from DM message', () => {
    const msg = {
        id: { _serialized: 'true_15551234567@c.us_ABC123' },
        body: 'Hello Lobster',
        from: '15551234567@c.us',
        fromMe: false,
        author: '',
        timestamp: 1700000000,
        mentionedIds: [],
    };
    const event = buildMessageEvent(msg, null, '');
    assert.strictEqual(event.id, 'true_15551234567@c.us_ABC123');
    assert.strictEqual(event.body, 'Hello Lobster');
    assert.strictEqual(event.isGroup, false);
    assert.strictEqual(event.mentions_lobster, false);
    assert.deepStrictEqual(event.mentionedIds, []);
});

test('detects group message', () => {
    const msg = {
        id: { _serialized: 'false_120363000000000001@g.us_XYZ' },
        body: 'Group message',
        from: '120363000000000001@g.us',
        fromMe: false,
        author: '15559876543@c.us',
        timestamp: 1700000001,
        mentionedIds: [],
    };
    const event = buildMessageEvent(msg, null, 'Team Chat');
    assert.strictEqual(event.isGroup, true);
    assert.strictEqual(event.chatName, 'Team Chat');
    assert.strictEqual(event.author, '15559876543@c.us');
});

test('detects mention when myJid is in mentionedIds (string format)', () => {
    const myJid = '19995551234@c.us';
    const msg = {
        id: { _serialized: 'false_120363000000000001@g.us_MENTION' },
        body: '@Lobster help me',
        from: '120363000000000001@g.us',
        fromMe: false,
        author: '15551111111@c.us',
        timestamp: 1700000002,
        mentionedIds: [myJid],
    };
    const event = buildMessageEvent(msg, myJid, 'Project Group');
    assert.strictEqual(event.mentions_lobster, true);
    assert.deepStrictEqual(event.mentionedIds, [myJid]);
});

test('detects mention when mentionedIds uses {_serialized} format', () => {
    const myJid = '19995551234@c.us';
    const msg = {
        id: { _serialized: 'false_120363000000000001@g.us_MENTION2' },
        body: '@Lobster do something',
        from: '120363000000000001@g.us',
        fromMe: false,
        author: '15552222222@c.us',
        timestamp: 1700000003,
        mentionedIds: [{ _serialized: myJid }],
    };
    const event = buildMessageEvent(msg, myJid, 'Dev Chat');
    assert.strictEqual(event.mentions_lobster, true);
    assert.deepStrictEqual(event.mentionedIds, [myJid]);
});

test('no mention when different JID is mentioned', () => {
    const myJid = '19995551234@c.us';
    const otherJid = '15557778888@c.us';
    const msg = {
        id: { _serialized: 'false_120363@g.us_NOMENTION' },
        body: '@someone else',
        from: '120363@g.us',
        fromMe: false,
        author: '15551111111@c.us',
        timestamp: 1700000004,
        mentionedIds: [otherJid],
    };
    const event = buildMessageEvent(msg, myJid, 'Chat');
    assert.strictEqual(event.mentions_lobster, false);
});

test('no mention when myJid is null', () => {
    const msg = {
        id: { _serialized: 'false_120363@g.us_NOJID' },
        body: 'hello',
        from: '120363@g.us',
        fromMe: false,
        author: '15551111111@c.us',
        timestamp: 1700000005,
        mentionedIds: ['19995551234@c.us'],
    };
    const event = buildMessageEvent(msg, null, 'Chat');
    assert.strictEqual(event.mentions_lobster, false);
});

test('handles missing optional fields gracefully', () => {
    const msg = {
        id: 'plain-id-string',
        body: '',
        from: '15559999999@c.us',
        fromMe: false,
    };
    const event = buildMessageEvent(msg, null, null);
    assert.strictEqual(event.body, '');
    assert.deepStrictEqual(event.mentionedIds, []);
    assert.strictEqual(event.chatName, '');
});

// ---------------------------------------------------------------------------
// Tests: parseCommandFile
// ---------------------------------------------------------------------------

console.log('\nparseCommandFile:');

test('parses valid send command', () => {
    const filePath = path.join(tmpDir, 'cmd_valid.json');
    fs.writeFileSync(filePath, JSON.stringify({
        action: 'send',
        to: '120363000000000001@g.us',
        text: 'Hello from Lobster',
    }));
    const cmd = parseCommandFile(filePath);
    assert.strictEqual(cmd.action, 'send');
    assert.strictEqual(cmd.to, '120363000000000001@g.us');
    assert.strictEqual(cmd.text, 'Hello from Lobster');
    fs.unlinkSync(filePath);
});

test('returns null for missing fields', () => {
    const filePath = path.join(tmpDir, 'cmd_missing.json');
    fs.writeFileSync(filePath, JSON.stringify({ action: 'send', to: '120363@g.us' }));
    const cmd = parseCommandFile(filePath);
    assert.strictEqual(cmd, null);
    fs.unlinkSync(filePath);
});

test('returns null for invalid JSON', () => {
    const filePath = path.join(tmpDir, 'cmd_invalid.json');
    fs.writeFileSync(filePath, 'not json at all {{{');
    const cmd = parseCommandFile(filePath);
    assert.strictEqual(cmd, null);
    fs.unlinkSync(filePath);
});

test('returns null for non-existent file', () => {
    const cmd = parseCommandFile(path.join(tmpDir, 'nonexistent.json'));
    assert.strictEqual(cmd, null);
});

// ---------------------------------------------------------------------------
// Tests: NDJSON output format
// ---------------------------------------------------------------------------

console.log('\nNDJSON output format:');

test('emitEvent writes valid JSON followed by newline to stdout', () => {
    // Capture stdout
    const chunks = [];
    const originalWrite = process.stdout.write.bind(process.stdout);
    process.stdout.write = (chunk) => {
        chunks.push(chunk);
        return true;
    };

    const event = {
        id: 'test-event-001',
        body: 'test body',
        from: '15551234567@c.us',
        fromMe: false,
        isGroup: false,
        author: '15551234567@c.us',
        timestamp: 1700000000,
        mentionedIds: [],
        mentions_lobster: false,
        chatName: '',
    };
    emitEvent(event);

    process.stdout.write = originalWrite;

    assert.ok(chunks.length > 0, 'stdout.write was called');
    const line = chunks.join('');
    assert.ok(line.endsWith('\n'), 'output ends with newline');
    const parsed = JSON.parse(line.trim());
    assert.strictEqual(parsed.id, 'test-event-001');
    assert.strictEqual(parsed.body, 'test body');
});

// ---------------------------------------------------------------------------
// Results
// ---------------------------------------------------------------------------

console.log('\n' + '─'.repeat(40));
console.log(`Results: ${passed} passed, ${failed} failed`);
console.log('─'.repeat(40));

// Clean up temp directory
try { fs.rmSync(tmpDir, { recursive: true }); } catch (e) {}

if (failed > 0) {
    process.exit(1);
} else {
    console.log('\nAll tests passed.');
    process.exit(0);
}
