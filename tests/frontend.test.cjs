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
        this.value = '';
        this.style = {};
        this.nodes = [];
        const classes = new Set();
        this.classList = { toggle(name, enabled) { enabled ? classes.add(name) : classes.delete(name); },
            contains: name => classes.has(name) };
    }
    querySelector(selector) {
        if (!this.children.has(selector)) this.children.set(selector, new Element());
        return this.children.get(selector);
    }
    addEventListener(name, callback) { this.handlers.set(name, callback); }
    appendChild(child) { this.nodes.push(child); }
}

function fixture(href = 'http://localhost:8001/') {
    const panel = new Element();
    const sockets = [];
    const calls = [];
    const elements = new Map([['control-panel-view', panel]]);
    const cards = new Map();
    const connections = [];
    const document = {
        querySelector: selector => selector === 'meta[name="api-token"]' ? { content: 'test-session' } : cards.get(selector) || null,
        createElement: () => new Element(),
        getElementById: id => {
            if (!elements.has(id)) elements.set(id, new Element());
            return elements.get(id);
        },
    };
    const context = vm.createContext({
        document, URL, Headers, FormData,
        window: { location: new URL(href) },
        WebSocket: class { constructor(url) { sockets.push(url); connections.push(this); } },
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
    return { client: new Client(), context, panel, sockets, calls, elements, cards, connections };
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

test('Completed excludes failed, paused, and saving torrents even at 100 percent', () => {
    const { client } = fixture();
    client.currentFilter = 'Completed';
    client.renderTorrents = () => {};
    for (const status of ['error', 'paused', 'downloading', 'completed']) {
        client.torrents.set(torrent.info_hash, { ...torrent, status, progress: 100 });
        client.applyFilters();
        assert.equal(client.filteredTorrents.length, status === 'completed' ? 1 : 0);
        const card = client.createTorrentElement({ ...torrent, status, progress: 100 });
        assert.equal(card.innerHTML.includes('progress-bar completed'), status === 'completed');
    }
});

test('progress updates the existing card and open details without rebuilding the list', () => {
    const { client, panel, cards } = fixture();
    const data = { ...torrent, status: 'downloading', progress: 25 };
    client.torrents.set(data.info_hash, data);
    client.selectedTorrent = data;
    const card = client.createTorrentElement(data);
    cards.set(`[data-hash="${data.info_hash}"]`, card);
    client.renderTorrents = () => { throw new Error('Ordinary progress must preserve card focus'); };
    client.renderControlPanel();
    client.handleWebSocketMessage({ type: 'progress_update', info_hash: data.info_hash,
        progress: 75, downloaded_pieces: 3, piece_count: 4, download_speed: 128, peers_connected: 2 });
    assert.equal(card.querySelector('.progress-bar').style.width, '75%');
    assert.ok(panel.innerHTML.includes('75.0%'));
    assert.ok(panel.innerHTML.includes('3 / 4'));
});

test('completed applies the full final snapshot to model, card, details, and totals', () => {
    const { client, panel, elements } = fixture();
    const data = { ...torrent, status: 'downloading', progress: 25, download_speed: 100,
        upload_speed: 20, peers_connected: 1, downloaded_pieces: 1, piece_count: 4 };
    client.torrents.set(data.info_hash, data);
    client.selectedTorrent = data;
    client.handleWebSocketMessage({ ...data, type: 'completed', status: 'completed', progress: 100,
        download_speed: 0, upload_speed: 0, peers_connected: 0, downloaded_pieces: 4 });
    assert.equal(data.download_speed, 0);
    assert.equal(data.peers_connected, 0);
    assert.equal(data.downloaded_pieces, 4);
    assert.ok(panel.innerHTML.includes('4 / 4'));
    assert.ok(panel.innerHTML.includes('0 B/s'));
    const card = elements.get('torrentsList').nodes.at(-1);
    assert.ok(card.innerHTML.includes('0 peers'));
    assert.equal(elements.get('globalDownSpeed').textContent, '0 B/s');
});

test('socket reconnect reloads the authoritative snapshot and clears removed selection', async () => {
    const { client, context, connections, panel } = fixture();
    client.torrents.set(torrent.info_hash, { ...torrent });
    client.selectedTorrent = client.torrents.get(torrent.info_hash);
    let refreshes = 0;
    context.fetch = async () => { refreshes++; return { ok: true, json: async () => ({ torrents: [] }) }; };
    client.setupWebSocket();
    await connections.at(-1).onopen();
    assert.equal(refreshes, 1);
    client.torrents.set(torrent.info_hash, { ...torrent });
    client.selectedTorrent = client.torrents.get(torrent.info_hash);
    client.setupWebSocket();
    await connections.at(-1).onopen();
    assert.equal(refreshes, 2);
    assert.equal(client.torrents.size, 0);
    assert.equal(client.selectedTorrent, null);
    assert.ok(panel.innerHTML.includes('Select a torrent'));
});

test('refresh rebinds selected details and removal clears their stale contents', async () => {
    const { client, context, panel } = fixture();
    client.torrents.set(torrent.info_hash, { ...torrent });
    client.selectedTorrent = client.torrents.get(torrent.info_hash);
    context.fetch = async () => ({ ok: true, json: async () => ({ torrents: [{ ...torrent, progress: 50 }] }) });
    await client.loadTorrents();
    assert.equal(client.selectedTorrent, client.torrents.get(torrent.info_hash));
    assert.ok(panel.innerHTML.includes('50.0%'));
    client.handleWebSocketMessage({ type: 'torrent_removed', info_hash: torrent.info_hash });
    assert.equal(client.selectedTorrent, null);
    assert.ok(!panel.innerHTML.includes('50.0%'));
});
