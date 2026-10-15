const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const test = require('node:test');
const vm = require('node:vm');

// This small DOM fixture records HTML and text sinks separately. Real assets
// and session bootstrap are exercised by the ASGI tests in test_server.py.
class Element {
    constructor() {
        this.dataset = {};
        this.innerHTML = '';
        this.textContent = '';
        this.children = new Map();
        this.handlers = new Map();
    }
    querySelector(selector) {
        if (!this.children.has(selector)) this.children.set(selector, new Element());
        return this.children.get(selector);
    }
    addEventListener(name, callback) { this.handlers.set(name, callback); }
}

function fixture(href = 'http://localhost:8001/') {
    const panel = new Element();
    const sockets = [];
    const calls = [];
    const document = {
        querySelector: () => ({ content: 'test-session' }),
        createElement: () => new Element(),
        getElementById: () => panel,
    };
    const context = vm.createContext({
        document, URL, Headers, FormData,
        window: { location: new URL(href) },
        WebSocket: class { constructor(url) { sockets.push(url); } },
        console: { log() {}, error() {} }, setTimeout() {}, alert() {}, confirm: () => true,
        fetch: async (url, options) => {
            calls.push({ url, options });
            return { ok: true, json: async () => ({ success: true, status: 'completed' }) };
        },
    });
    const source = fs.readFileSync(path.join(__dirname, '../frontend/jss/app.js'), 'utf8');
    vm.runInContext(source.replace('const client = new TorrentClient();',
        'globalThis.ExportedClient = TorrentClient;'), context);
    class Client extends context.ExportedClient { init() {} }
    return { client: new Client(), context, panel, sockets, calls };
}

const torrent = {
    info_hash: 'a'.repeat(40), name: 'ordinary file', total_size: 4,
    progress: 0, status: 'paused', peers_connected: 0,
};

test('torrent names are text in both list and detail views', () => {
    const { client, panel } = fixture();
    const payload = '<img src=x onerror="globalThis.injected=true"> & "quotes"';
    const data = { ...torrent, name: payload, status: payload };
    const item = client.createTorrentElement(data);
    assert.equal(item.querySelector('.torrent-name').textContent, payload);
    assert.ok(!item.innerHTML.includes(payload));
    client.selectedTorrent = data;
    client.renderControlPanel();
    assert.equal(panel.querySelector('h2').textContent, payload);
    assert.equal(panel.querySelector('.panel-status').textContent, payload);
    assert.ok(!panel.innerHTML.includes(payload));
});

test('action buttons use event listeners without interpolating hashes into HTML', () => {
    const { client } = fixture();
    const hash = 'untrusted\'"<img src=x>';
    const calls = [];
    client.startTorrent = value => calls.push(['start', value]);
    client.deleteTorrent = value => calls.push(['delete', value]);
    const item = client.createTorrentElement({ ...torrent, info_hash: hash });
    assert.ok(!item.innerHTML.includes('onclick='));
    assert.ok(!item.innerHTML.includes(hash));
    const event = { stopPropagation() {} };
    item.querySelector('[data-action="toggle"]').handlers.get('click')(event);
    item.querySelector('[data-action="delete"]').handlers.get('click')(event);
    assert.deepEqual(calls, [['start', hash], ['delete', hash]]);
});

test('API requests use the current origin and session token', async () => {
    const { client, calls } = fixture();
    await client.apiRequest('/torrents', { method: 'GET' });
    assert.equal(calls[0].url, '/api/torrents');
    assert.equal(calls[0].options.headers.get('Authorization'), 'Bearer test-session');
});

test('WebSocket follows the page host, port and HTTPS scheme', () => {
    for (const [href, expected] of [
        ['http://127.0.0.1:9000/', 'ws://127.0.0.1:9000/ws'],
        ['https://localhost:9443/', 'wss://localhost:9443/ws'],
    ]) {
        const { client, sockets } = fixture(href);
        client.setupWebSocket();
        assert.equal(sockets[0], expected);
    }
});

test('failed HTTP responses are errors and never update torrent state', async () => {
    const { client, context } = fixture();
    client.torrents.set(torrent.info_hash, { ...torrent });
    context.fetch = async () => ({ ok: false, json: async () => ({ detail: 'Authentication required' }) });
    await assert.rejects(client.apiRequest('/torrents'), /Authentication required/);
    await client.startTorrent(torrent.info_hash);
    assert.equal(client.torrents.get(torrent.info_hash).status, 'paused');
});

test('start and pause preserve the authoritative server status', async () => {
    const { client } = fixture();
    client.torrents.set(torrent.info_hash, { ...torrent });
    client.applyFilters = () => {};
    await client.startTorrent(torrent.info_hash);
    assert.equal(client.torrents.get(torrent.info_hash).status, 'completed');
    await client.pauseTorrent(torrent.info_hash);
    assert.equal(client.torrents.get(torrent.info_hash).status, 'completed');
});
