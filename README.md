# Mini-Torrent Client

A functional BitTorrent client with web interface built with Python and FastAPI. Student project. 

## Features

- Full BitTorrent protocol implementation
- Modern web interface with real-time updates
- Multi-file torrent support
- Parallel downloads from multiple peers
- Dark/Light theme support
- Search and filtering capabilities
- Live download statistics

## Tech Stack

**Backend:**
- Python 3.11+
- FastAPI (async web framework)
- WebSockets (real-time updates)
- Threading (parallel downloads)

**Frontend:**
- Vanilla JavaScript
- CSS3 (dark/light themes)
- WebSocket API

**Protocol:**
- BitTorrent Peer Wire Protocol
- HTTP Tracker Protocol
- SHA-1 piece verification

## Project Structure

```
my_torrent/
├── src/                    # Core BitTorrent implementation
│   ├── bencode.py         # Bencode encoder/decoder
│   ├── torrent.py         # .torrent file parser
│   ├── tracker.py         # Tracker communication
│   ├── peer.py            # Peer Wire Protocol
│   ├── piece_manager.py   # Piece/block management
│   └── downloader.py      # Download coordinator
├── frontend/              # Web interface
│   ├── index.html        # Main UI
│   ├── css/style.css     # Styling
│   └── jss/app.js        # Frontend logic
├── web_server.py         # FastAPI server
├── torrents/             # .torrent files directory
├── downloads/            # Downloaded files
└── requirements.txt      # Python dependencies
```

## Installation

### Prerequisites

- Python 3.11 or higher
- pip (Python package manager)

### Step 1: Clone Repository

```bash
git clone https://github.com/yourusername/mini-torrent.git
cd mini-torrent
```

### Step 2: Create Virtual Environment (Recommended)

```bash
python -m venv venv

# Windows
venv\Scripts\activate

# Linux/Mac
source venv/bin/activate
```

### Step 3: Install Dependencies

```bash
pip install -r requirements.txt
```

### Step 4: Create Required Directories

```bash
mkdir torrents downloads
```

## Usage

### Starting the Server

```bash
python web_server.py
```

The server will start on `http://localhost:8000`

The application is intended for one local user. HTTP and WebSocket access is
restricted to loopback clients and local hostnames. Opening the home page creates
a process-specific session token; the browser supplies it automatically for API
requests. WebSockets require the matching HttpOnly, SameSite cookie and origin.
API clients must send `Authorization: Bearer <token>` using the token in the home
page's `api-token` meta element. Restarting the server invalidates the session.

Uploads are limited to 4 MiB and validated before storage. Torrent path components
must be portable file names; absolute paths, traversal, Windows device names, and
alternate data streams are rejected. Piece lengths above 64 MiB are rejected.

### Adding a Torrent

1. Click "Add Torrent" button
2. Drag & drop a .torrent file or click "Select"
3. Click "Add a torrent"

### Managing Downloads

- **Start Download:** Click ▶ button
- **Pause Download:** Click ⏸ button
- **Delete Torrent:** Click ✕ button
- **View Details:** Click on torrent item

### Search and Filter

- **Search:** Type in search bar (live filtering)
- **Filter by Status:** Use "Show" dropdown
- **Sort:** Use "Sort" dropdown (Newest/Name/Size)

### Theme Toggle

Click the settings icon (⚙️) in top-right corner to switch themes.

## API Documentation

Once the server is running, visit:
- Swagger UI: `http://localhost:8000/docs`
- ReDoc: `http://localhost:8000/redoc`

### Main Endpoints

```
GET  /api/torrents              # List all torrents
POST /api/torrents/add          # Add new torrent
POST /api/torrents/{hash}/start # Start download
POST /api/torrents/{hash}/pause # Pause download
DELETE /api/torrents/{hash}     # Remove torrent
WS   /ws                        # WebSocket connection
```

## Configuration

### Server Settings

Edit `web_server.py`:

```python
# Change port
uvicorn.run(app, host="localhost", port=8000)  
# Change download directory
PieceManager(torrent, download_dir="downloads")  # Change "downloads" path
```

### Download Settings

Edit `src/downloader.py`:

```python
MAX_INFLIGHT_PER_PEER = 10  # Requests per peer (higher = faster)
```

Edit `src/piece_manager.py`:

```python
BLOCK_SIZE = 16384  # Block size in bytes (16KB)
```

## Troubleshooting

### Error: "No module named 'fastapi'"

```bash
pip install -r requirements.txt
```

### Error: "Address already in use"

Port 8000 is occupied. Change port in `web_server.py` or kill process:

```bash
# Windows
netstat -ano | findstr :8000
taskkill /PID <PID> /F

# Linux/Mac
lsof -i :8000
kill -9 <PID>
```

### Slow Download Speed

1. Check number of connected peers (shown in UI)
2. Try different torrents (more seeders = faster)
3. Increase `MAX_INFLIGHT_PER_PEER` in `downloader.py`
4. Connect to more peers by editing `web_server.py`:

```python
active_peers = [p for p in results if p is not None][:20]  # Increase from [:10]
```

### WebSocket Connection Failed

1. Check server is running
2. Try `http://127.0.0.1:8000` instead of `localhost`
3. Check browser console for errors (F12)

## Development

Install test dependencies and run the regression suites from the project root:

```bash
python -m pip install -r requirements-dev.txt
python -m unittest discover -s tests -v
node --test tests/frontend.test.cjs
```

The Python suite exercises the real HTTP/WebSocket application and file output
using temporary directories. Trackers and peers are mocked, so tests do not join
public swarms. The JavaScript suite uses Node's built-in test runner; no npm
dependencies are required.

For an opt-in check against a real swarm, obtain an Ubuntu `.torrent` from
[the official release directory](https://releases.ubuntu.com/) and run:

```bash
python -m tests.live_download --torrent /absolute/path/to/ubuntu.torrent --minimum-mib 1 --stage-timeout 180
```

This starts a temporary local server and uses the real API, tracker and peers.
It verifies at least 1 MiB of SHA-1 checked pieces before pausing, checks that
traffic stops, resumes until at least another 1 MiB is verified, then removes
the torrent and checks that workers and sockets have stopped. All test storage
is isolated in an automatically cleaned temporary directory. It samples the
ISO; it does not download or assemble the entire image. Swarm availability and
network access affect this check, so it is excluded from the regular suite.


### Project Architecture

```
User Request → FastAPI → Tracker → Peers → Download → Verify → Save
                ↓
            WebSocket → Frontend (real-time updates)
```

**Download Flow:**
1. Parse .torrent file (bencode)
2. Contact tracker (get peer list)
3. Connect to peers (parallel connection)
4. Download pieces (parallel from multiple peers)
5. Verify pieces (SHA-1 hash)
6. Save to disk (assemble pieces)

## Performance

**Typical Speeds:**
- 1-3 peers: 60-120 KB/s
- 5-10 peers: 200-500 KB/s
- 10+ peers: 500 KB/s - 2 MB/s

## Limitations

- UDP trackers not supported (HTTP/HTTPS only)
- DHT not implemented
- Verified pieces survive pause/resume in memory; restarting the process loses download state

## Future Improvements

- [ ] DHT support
- [ ] UDP tracker support
- [ ] Persist download state across restarts
- [ ] Magnet link support
- [ ] Download queue management

## License

This project is for educational purposes.

## Acknowledgments

- BitTorrent Protocol Specification (BEP 0003)
- FastAPI Documentation
- WebSocket Protocol RFC 6455

---

**Note:** This client is designed for educational purposes to understand BitTorrent protocol implementation.
