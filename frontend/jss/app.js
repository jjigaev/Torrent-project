
class TorrentClient {
    constructor() {
        this.apiBase = 'http://localhost:8000/api';
        this.ws = null;
        this.torrents = new Map();
        this.filteredTorrents = [];
        this.selectedTorrent = null;
        this.currentFilter = 'All';
        this.currentSort = 'Newest';
        this.init();
    }

    init() {
        this.setupWebSocket();
        this.setupEventListeners();
        this.loadTorrents();
        this.loadTheme();
    }

    // ========================================================================
    // WebSocket
    // ========================================================================

    setupWebSocket() {
        this.ws = new WebSocket('ws://localhost:8000/ws');
        
        this.ws.onopen = () => console.log('✅ WebSocket connected');
        this.ws.onmessage = (event) => this.handleWebSocketMessage(JSON.parse(event.data));
        this.ws.onerror = (error) => console.error('❌ WebSocket error:', error);
        this.ws.onclose = () => {
            console.log('⚠️ WebSocket disconnected. Reconnecting...');
            setTimeout(() => this.setupWebSocket(), 3000);
        };
    }

    handleWebSocketMessage(data) {
        console.log('WS:', data);
        
        switch (data.type) {
            case 'torrent_added':
                this.loadTorrents();
                break;
            case 'progress_update':
                this.updateTorrentFromWS(data);
                break;
            case 'status_update':
                const t = this.torrents.get(data.info_hash);
                if (t) {
                    t.status = data.status;
                    this.applyFilters();
                }
                break;
            case 'completed':
                const torrent = this.torrents.get(data.info_hash);
                if (torrent) {
                    torrent.status = 'completed';
                    torrent.progress = 100;
                    this.applyFilters();
                }
                alert(`✅ Download completed!`);
                break;
            case 'torrent_removed':
                this.torrents.delete(data.info_hash);
                this.applyFilters();
                break;
            case 'error':
                alert(`❌ Error: ${data.message || 'Unknown error'}`);
                break;
        }
    }

    updateTorrentFromWS(data) {
        const torrent = this.torrents.get(data.info_hash);
        if (!torrent) return;

        torrent.progress = data.progress || 0;
        torrent.downloaded_pieces = data.downloaded_pieces || 0;
        torrent.download_speed = data.download_speed || 0;
        torrent.upload_speed = data.upload_speed || 0;
        torrent.peers_connected = data.peers_connected || 0;
        if (data.status) torrent.status = data.status;

        const el = document.querySelector(`[data-hash="${data.info_hash}"]`);
        if (el) {
            const bar = el.querySelector('.progress-bar');
            const text = el.querySelector('.progress-text');
            const stats = el.querySelector('.torrent-stats');
            
            if (bar) {
                bar.style.width = `${torrent.progress}%`;
                if (torrent.progress >= 100) bar.classList.add('completed');
            }
            if (text) text.textContent = `${torrent.progress.toFixed(1)}%`;
            if (stats) {
                stats.innerHTML = `
                    <span>${this.formatSize(torrent.total_size)}</span>
                    <span>↓ ${this.formatSpeed(torrent.download_speed)}</span>
                    <span>↑ ${this.formatSpeed(torrent.upload_speed)}</span>
                    <span>${torrent.peers_connected} peers</span>
                `;
            }
        }
        
        this.updateGlobalStats();
    }

    // ========================================================================
    // Event Listeners - ВСЕ ФИЛЬТРЫ + ПОИСК + ТЕМА
    // ========================================================================

    setupEventListeners() {
        // Navigation
        document.querySelectorAll('.nav-item').forEach(btn => {
            btn.addEventListener('click', (e) => this.switchView(e.currentTarget.dataset.view));
        });

        // Modal
        const addBtn = document.getElementById('addTorrentBtn');
        const modal = document.getElementById('addTorrentModal');
        const modalClose = document.getElementById('modalClose');
        const modalOverlay = document.getElementById('modalOverlay');
        
        if (addBtn) addBtn.addEventListener('click', () => this.openModal());
        if (modalClose) modalClose.addEventListener('click', () => this.closeModal());
        if (modalOverlay) modalOverlay.addEventListener('click', () => this.closeModal());

        // File Upload
        const selectBtn = document.getElementById('selectFileBtn');
        const fileInput = document.getElementById('torrentFileInput');
        const uploadArea = document.getElementById('uploadArea');

        if (selectBtn) selectBtn.addEventListener('click', () => fileInput?.click());
        if (fileInput) fileInput.addEventListener('change', (e) => this.handleFileSelect(e));

        // Drag & Drop
        if (uploadArea) {
            uploadArea.addEventListener('dragover', (e) => {
                e.preventDefault();
                uploadArea.classList.add('dragover');
            });
            uploadArea.addEventListener('dragleave', () => {
                uploadArea.classList.remove('dragover');
            });
            uploadArea.addEventListener('drop', (e) => {
                e.preventDefault();
                uploadArea.classList.remove('dragover');
                const files = e.dataTransfer.files;
                if (files.length > 0) this.uploadTorrent(files[0]);
            });
        }

        // ✅ SEARCH - LIVE FILTERING
        const searchInput = document.getElementById('searchInput');
        if (searchInput) {
            searchInput.addEventListener('input', () => this.applyFilters());
        }

        // ✅ FILTERS - Show and Sort
        const filterSelects = document.querySelectorAll('.filter-select');
        if (filterSelects.length >= 2) {
            filterSelects[0].addEventListener('change', (e) => {
                this.currentFilter = e.target.value;
                this.applyFilters();
            });
            filterSelects[1].addEventListener('change', (e) => {
                this.currentSort = e.target.value;
                this.applyFilters();
            });
        }

        // ✅ THEME TOGGLE
        const themeBtn = document.querySelectorAll('.icon-btn')[1]; // Second icon button
        if (themeBtn) {
            themeBtn.addEventListener('click', () => this.toggleTheme());
        }
    }

    // ========================================================================
    // FILTERS + SEARCH + SORT - ПОЛНАЯ РЕАЛИЗАЦИЯ
    // ========================================================================

    applyFilters() {
        const searchInput = document.getElementById('searchInput');
        const query = searchInput ? searchInput.value.toLowerCase().trim() : '';
        
        // Start with all torrents
        let filtered = Array.from(this.torrents.values());
        
        // Apply search
        if (query) {
            filtered = filtered.filter(t => 
                t.name.toLowerCase().includes(query)
            );
        }
        
        // Apply status filter
        if (this.currentFilter === 'Downloading') {
            filtered = filtered.filter(t => t.status === 'downloading');
        } else if (this.currentFilter === 'Completed') {
            filtered = filtered.filter(t => t.status === 'completed' || t.progress >= 100);
        }
        
        // Apply sorting
        if (this.currentSort === 'Name') {
            filtered.sort((a, b) => a.name.localeCompare(b.name));
        } else if (this.currentSort === 'Size') {
            filtered.sort((a, b) => b.total_size - a.total_size);
        } else if (this.currentSort === 'Newest') {
            // Keep original order (most recent first)
        }
        
        this.filteredTorrents = filtered;
        this.renderTorrents();
        this.updateGlobalStats();
    }

    // ========================================================================
    // THEME TOGGLE
    // ========================================================================

    toggleTheme() {
        document.body.classList.toggle('light-theme');
        const isLight = document.body.classList.contains('light-theme');
        localStorage.setItem('theme', isLight ? 'light' : 'dark');
        console.log(`Theme switched to: ${isLight ? 'light' : 'dark'}`);
    }

    loadTheme() {
        const theme = localStorage.getItem('theme');
        if (theme === 'light') {
            document.body.classList.add('light-theme');
        }
    }

    // ========================================================================
    // API Calls
    // ========================================================================

    async loadTorrents() {
        try {
            const response = await fetch(`${this.apiBase}/torrents`);
            const data = await response.json();
            
            this.torrents.clear();
            data.torrents.forEach(t => this.torrents.set(t.info_hash, t));
            
            this.applyFilters();
        } catch (error) {
            console.error('Error loading torrents:', error);
        }
    }

    async handleFileSelect(event) {
        const file = event.target.files[0];
        if (file) await this.uploadTorrent(file);
    }

    async uploadTorrent(file) {
        const formData = new FormData();
        formData.append('file', file);

        try {
            const response = await fetch(`${this.apiBase}/torrents/add`, {
                method: 'POST',
                body: formData
            });

            const data = await response.json();
            
            if (data.success) {
                alert(`✅ Added: ${data.name}`);
                this.closeModal();
                await this.loadTorrents();
            }
        } catch (error) {
            console.error('Error:', error);
            alert('❌ Failed to add torrent');
        }
    }

    async startTorrent(infoHash) {
        try {
            await fetch(`${this.apiBase}/torrents/${infoHash}/start`, { method: 'POST' });
            const torrent = this.torrents.get(infoHash);
            if (torrent) {
                torrent.status = 'downloading';
                this.applyFilters();
            }
        } catch (error) {
            console.error('Error starting:', error);
        }
    }

    async pauseTorrent(infoHash) {
        try {
            await fetch(`${this.apiBase}/torrents/${infoHash}/pause`, { method: 'POST' });
            const torrent = this.torrents.get(infoHash);
            if (torrent) {
                torrent.status = 'paused';
                this.applyFilters();
            }
        } catch (error) {
            console.error('Error pausing:', error);
        }
    }

    async deleteTorrent(infoHash) {
        if (!confirm('Remove this torrent?')) return;
        
        try {
            await fetch(`${this.apiBase}/torrents/${infoHash}`, { method: 'DELETE' });
        } catch (error) {
            console.error('Error deleting:', error);
        }
    }

    // ========================================================================
    // UI Rendering
    // ========================================================================

    renderTorrents() {
        const container = document.getElementById('torrentsList');
        if (!container) return;

        if (this.filteredTorrents.length === 0) {
            container.innerHTML = '<div class="empty-state"><p>No torrents match your filter</p></div>';
            return;
        }

        container.innerHTML = '';
        this.filteredTorrents.forEach(torrent => {
            container.appendChild(this.createTorrentElement(torrent));
        });
    }

    createTorrentElement(t) {
        const div = document.createElement('div');
        div.className = 'torrent-item';
        div.dataset.hash = t.info_hash;

        // ✅ CLICK TO VIEW DETAILS
        div.addEventListener('click', () => {
            this.selectTorrent(t.info_hash);
        });

        const isDownloading = t.status === 'downloading';
        const icon = isDownloading ? '⏸' : '▶';
        const action = isDownloading ? 'pauseTorrent' : 'startTorrent';
        
        div.innerHTML = `
            <div class="torrent-header">
                <div class="torrent-info">
                    <div class="torrent-name">${t.name}</div>
                    <div class="torrent-stats">
                        <span>${this.formatSize(t.total_size)}</span>
                        <span>↓ ${this.formatSpeed(t.download_speed || 0)}</span>
                        <span>↑ ${this.formatSpeed(t.upload_speed || 0)}</span>
                        <span>${t.peers_connected || 0} peers</span>
                    </div>
                </div>
                <div class="torrent-actions">
                    <button class="action-btn" onclick="event.stopPropagation(); client.${action}('${t.info_hash}')" title="${isDownloading ? 'Pause' : 'Start'}">${icon}</button>
                    <button class="action-btn" onclick="event.stopPropagation(); client.deleteTorrent('${t.info_hash}')" title="Delete">✕</button>
                </div>
            </div>
            <div class="progress-container">
                <div class="progress-bar-wrapper">
                    <div class="progress-bar ${t.progress >= 100 ? 'completed' : ''}" style="width: ${t.progress || 0}%"></div>
                    <div class="progress-text">${(t.progress || 0).toFixed(1)}%</div>
                </div>
            </div>
        `;

        return div;
    }

    // ========================================================================
    // CONTROL PANEL - SELECT TORRENT
    // ========================================================================

    selectTorrent(infoHash) {
        this.selectedTorrent = this.torrents.get(infoHash);
        if (!this.selectedTorrent) return;
        
        this.renderControlPanel();
        this.switchView('control-panel');
    }

    renderControlPanel() {
        const container = document.getElementById('control-panel-view');
        if (!container || !this.selectedTorrent) return;

        const t = this.selectedTorrent;
        
        container.innerHTML = `
            <div class="panel-container">
                <h2>${t.name}</h2>
                <div style="margin-top: 20px; color: var(--text-secondary);">
                    <p><strong>Size:</strong> ${this.formatSize(t.total_size)}</p>
                    <p><strong>Progress:</strong> ${t.progress.toFixed(1)}%</p>
                    <p><strong>Status:</strong> ${t.status}</p>
                    <p><strong>Download Speed:</strong> ${this.formatSpeed(t.download_speed || 0)}</p>
                    <p><strong>Upload Speed:</strong> ${this.formatSpeed(t.upload_speed || 0)}</p>
                    <p><strong>Peers Connected:</strong> ${t.peers_connected || 0}</p>
                    <p><strong>Pieces:</strong> ${t.downloaded_pieces || 0} / ${t.piece_count || '?'}</p>
                </div>
            </div>
        `;
    }

    updateGlobalStats() {
        let totalDown = 0;
        let totalUp = 0;
        
        this.torrents.forEach(t => {
            if (t.status === 'downloading') {
                totalDown += t.download_speed || 0;
                totalUp += t.upload_speed || 0;
            }
        });
        
        const downEl = document.getElementById('globalDownSpeed');
        const upEl = document.getElementById('globalUpSpeed');
        
        if (downEl) downEl.textContent = this.formatSpeed(totalDown);
        if (upEl) upEl.textContent = this.formatSpeed(totalUp);
    }

    // ========================================================================
    // View Management
    // ========================================================================

    switchView(viewName) {
        document.querySelectorAll('.nav-item').forEach(btn => {
            btn.classList.remove('active');
            if (btn.dataset.view === viewName) btn.classList.add('active');
        });

        document.querySelectorAll('.view-container').forEach(view => {
            view.style.display = 'none';
        });

        const targetView = document.getElementById(`${viewName}-view`);
        if (targetView) targetView.style.display = 'block';
    }

    openModal() {
        document.getElementById('addTorrentModal')?.classList.add('active');
    }

    closeModal() {
        document.getElementById('addTorrentModal')?.classList.remove('active');
    }

    // ========================================================================
    // Utilities
    // ========================================================================

    formatSize(bytes) {
        if (bytes === 0) return '0 B';
        const k = 1024;
        const sizes = ['B', 'KB', 'MB', 'GB'];
        const i = Math.floor(Math.log(bytes) / Math.log(k));
        return parseFloat((bytes / Math.pow(k, i)).toFixed(2)) + ' ' + sizes[i];
    }

    formatSpeed(bytesPerSec) {
        return this.formatSize(bytesPerSec) + '/s';
    }
}

// Initialize
const client = new TorrentClient();
