import struct
from .piece_manager import PieceManager


class Downloader:
    MAX_INFLIGHT_PER_PEER = 10
    
    def __init__(self, torrent, peers):
        self.torrent = torrent
        self.peers = peers
        self.piece_manager = PieceManager(torrent)
        self.peer_inflight = {peer: 0 for peer in peers} #dict for peer requests
    
    def download_piece(self, peer, piece_index):
        blocks = self.piece_manager.init_piece_download(piece_index) #16KB
        total_blocks = len(blocks)
        requested = 0
        received = 0
        block_queue = list(blocks)
        
        while received < total_blocks:
            while requested < total_blocks and self.peer_inflight[peer] < self.MAX_INFLIGHT_PER_PEER:
                block_offset, block_length = block_queue[requested]
                peer.request_piece(piece_index, block_offset, block_length) #sending request
                self.peer_inflight[peer] += 1
                requested += 1
            
            msg = peer.receive_message(timeout=15) #vazno
            
            if msg is None:
                return False
            
            msg_type, payload = msg
            
            if msg_type == 7: #PIECE
                index = struct.unpack(">I", payload[0:4])[0] #index(4)
                begin = struct.unpack(">I", payload[4:8])[0] #begin(4)
                block_data = payload[8:] #variable
                
                if index != piece_index:
                    continue
                
                self.piece_manager.add_block(piece_index, begin, block_data) #add to piecemanager
                self.peer_inflight[peer] -= 1
                received += 1
                
            elif msg_type == 0:
                return False
        
        return self.piece_manager.have_pieces[piece_index]
    
    def download_pieces(self, num_pieces=5):
        pieces_downloaded = 0
        
        for peer in self.peers:
            if pieces_downloaded >= num_pieces:
                break
            
            piece_index = self.piece_manager.get_next_piece_to_download(peer)
            
            if piece_index is None:
                continue
            
            if self.download_piece(peer, piece_index):
                pieces_downloaded += 1
                progress = self.piece_manager.get_progress()
        
        return pieces_downloaded
