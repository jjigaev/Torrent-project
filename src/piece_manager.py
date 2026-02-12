
import hashlib
from pathlib import Path


class PieceManager:
    BLOCK_SIZE = 16384
    def __init__(self, torrent, download_dir="downloads"): #PATH TO DOWNLOAD
        self.torrent = torrent
        self.download_dir = Path(download_dir)
        self.download_dir.mkdir(exist_ok=True)

        self.have_pieces = [False] * len(torrent.pieces)
        self.piece_data = {} #bytes of pieces
        self.pending_blocks = {}
    
    def get_next_piece_to_download(self, peer): #checks
        for piece_index in range(len(self.torrent.pieces)):
            if self.have_pieces[piece_index]:
                continue
            if not peer.has_piece(piece_index):
                continue
            if piece_index in self.pending_blocks:
                continue
            return piece_index
        return None
    
    def init_piece_download(self, piece_index):
        piece_length = self.get_piece_length(piece_index) #real length of piece
        blocks = []
        offset = 0
        
        while offset < piece_length: #creates blocks
            block_length = min(self.BLOCK_SIZE, piece_length - offset) #if less 16KB, take the less
            blocks.append((offset, block_length))
            offset += block_length #offset, length
        
        self.pending_blocks[piece_index] = {offset: None for offset, _ in blocks} #dict for downloaded blocks
        return blocks
    
    def get_piece_length(self, piece_index):
        if piece_index == len(self.torrent.pieces) - 1: #last piece
            return self.torrent.total_size % self.torrent.piece_length or self.torrent.piece_length
        else:
            return self.torrent.piece_length
    
    def add_block(self, piece_index, block_offset, block_data):
        if piece_index not in self.pending_blocks:
            return False
        
        self.pending_blocks[piece_index][block_offset] = block_data
        
        if all(data is not None for data in self.pending_blocks[piece_index].values()):
            return self._complete_piece(piece_index)
        
        return False
    
    def _complete_piece(self, piece_index):
        blocks = self.pending_blocks[piece_index] #dict
        sorted_offsets = sorted(blocks.keys()) #sort
        piece_data = b''.join(blocks[offset] for offset in sorted_offsets) #assemble

        #check hash
        piece_hash = hashlib.sha1(piece_data).digest()
        expected_hash = self.torrent.pieces[piece_index]
        
        if piece_hash != expected_hash:
            print(f"Piece {piece_index} HASH MISMATCH")
            del self.pending_blocks[piece_index]
            return False

        self.piece_data[piece_index] = piece_data
        self.have_pieces[piece_index] = True
        del self.pending_blocks[piece_index]
        
        print(f"Piece {piece_index} completed and verified.")
        return True
    
    def get_progress(self):
        completed = sum(self.have_pieces)
        total = len(self.have_pieces)
        percentage = (completed / total) * 100 if total > 0 else 0
        
        return {
            'completed_pieces': completed,
            'total_pieces': total,
            'percentage': percentage,
            'downloaded_bytes': sum(len(self.piece_data[i]) for i in self.piece_data),
            'total_bytes': self.torrent.total_size
        }
    
    def save_to_disk(self):
        if not any(self.have_pieces):
            print("No pieces to save")
            return

        if self.torrent.is_multi_file:
            self._save_multi_file()
        else:
            self._save_single_file()
    
    def _save_single_file(self):
        output_path = self.download_dir / self.torrent.name

        print(f"\nSaving to: {output_path}")
        
        with open(output_path, 'wb') as f:
            for piece_index in range(len(self.torrent.pieces)):
                if piece_index in self.piece_data:
                    f.write(self.piece_data[piece_index])
        
        print(f"Saved {sum(self.have_pieces)} pieces to disk")
    
    def _save_multi_file(self):
        base_dir = self.download_dir / self.torrent.name
        base_dir.mkdir(exist_ok=True)
        
        print(f"\nSaving multi-file torrent to: {base_dir}")

        all_data = bytearray() #assemble all data
        for piece_index in range(len(self.torrent.pieces)):
            if piece_index in self.piece_data:
                all_data.extend(self.piece_data[piece_index])
        
        #get file list from torrent info
        files = self.torrent.data['info'].get('files', [])
        
        if not files:
            print("Warning: Multi-file torrent but no files list found")
            return

        offset = 0
        for file_info in files:
            path_parts = file_info['path']

            if isinstance(path_parts[0], bytes): #if path in bytes
                path_parts = [p.decode('utf-8') for p in path_parts] #decode the path into string
            file_path = base_dir / Path(*path_parts) #full path
            file_path.parent.mkdir(parents=True, exist_ok=True)
            length = file_info['length']
            file_data = all_data[offset:offset + length] #Berem нужную часть общего массива данных

            with open(file_path, 'wb') as f:
                f.write(file_data)
            print(f"Saved: {file_path.name} ({length} bytes)")
            offset += length

