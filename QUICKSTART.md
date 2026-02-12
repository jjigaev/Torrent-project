# Quick Start Guide

Get Mini-Torrent running in 5 minutes.

## Step 1: Install Python

Download Python 3.11+: https://www.python.org/downloads/

## Step 2: Clone Repository

```bash
git clone https://github.com/yourusername/mini-torrent.git
cd mini-torrent
```

## Step 3: Install Dependencies

```bash
pip install -r requirements.txt
```

## Step 4: Run

```bash
python web_server.py
```

## Step 5: Open Browser

Go to: http://localhost:8000

## Step 6: Add a Torrent

1. Click "Add Torrent"
2. Select a .torrent file
3. Click "Add"
4. Click ▶ to start download

Done!

---

## Troubleshooting

**Port 8000 in use?**
```bash
# Change port in web_server.py line 297:
uvicorn.run(app, host="0.0.0.0", port=8001)
```

**Module not found?**
```bash
pip install fastapi uvicorn[standard] python-multipart
```
