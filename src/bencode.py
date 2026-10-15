'''
turning torrent bytes python dictionary,
then we slice some info from dictionary with bencode encoder to make it 20byte infohash
'''

class BencodeDecoder:
    MAX_SIZE = 4 * 1024 * 1024
    MAX_DEPTH = 64
    MAX_ITEMS = 100_000

    def __init__(self, data):
        if not isinstance(data, bytes) or len(data) > self.MAX_SIZE:
            raise ValueError("Invalid bencode input size")
        self.data = data
        self.index = 0
        self.items = 0
        self.info_span = None

    def decode(self):
        result = self._decode_next()
        if self.index != len(self.data):
            raise ValueError("Trailing bencode data")
        return result

    def _peek(self):
        if self.index >= len(self.data):
            raise ValueError("Unexpected end of data")
        return self.data[self.index]

    def _decode_next(self, depth=0):
        self.items += 1
        if depth > self.MAX_DEPTH or self.items > self.MAX_ITEMS:
            raise ValueError("Bencode structure is too complex")
        current = self._peek()
        if current == ord('i'):
            return self._decode_int()
        if current == ord('l'):
            return self._decode_list(depth)
        if current == ord('d'):
            return self._decode_dict(depth)
        if ord('0') <= current <= ord('9'):
            return self._decode_string()
        raise ValueError("Unknown bencode type")

    def _decode_int(self):
        self.index += 1
        end = self.data.index(b'e', self.index)
        number = self.data[self.index:end]
        digits = number[1:] if number.startswith(b'-') else number
        if not digits or not digits.isdigit() or (len(digits) > 1 and digits.startswith(b'0')) or number == b'-0':
            raise ValueError("Invalid bencode integer")
        self.index = end + 1
        return int(number)

    def _decode_string(self):
        colon = self.data.index(b':', self.index)
        digits = self.data[self.index:colon]
        if not digits or not digits.isdigit() or (len(digits) > 1 and digits.startswith(b'0')):
            raise ValueError("Invalid bencode string length")
        length = int(digits)
        start = colon + 1
        end = start + length
        if end > len(self.data):
            raise ValueError("Truncated bencode string")
        self.index = end
        return self.data[start:end]

    def _decode_list(self, depth):
        self.index += 1
        items = []
        while self._peek() != ord('e'):
            items.append(self._decode_next(depth + 1))
        self.index += 1
        return items

    def _decode_dict(self, depth):
        self.index += 1
        dictionary = {}
        previous = None
        while self._peek() != ord('e'):
            if not ord('0') <= self._peek() <= ord('9'):
                raise ValueError("Bencode dictionary keys must be strings")
            key = self._decode_string()
            if previous is not None and key <= previous:
                raise ValueError("Bencode keys must be unique and sorted")
            previous = key
            start = self.index
            value = self._decode_next(depth + 1)
            if depth == 0 and key == b'info':
                self.info_span = (start, self.index)
            try:
                decoded_key = key.decode('utf-8')
            except UnicodeDecodeError:
                decoded_key = key
            dictionary[decoded_key] = value
        self.index += 1
        return dictionary


class BencodeEncoder:
    @staticmethod
    def encode(data):
        if isinstance(data, int):
            return f"i{data}e".encode()
        elif isinstance(data, bytes):
            return f"{len(data)}:".encode() + data
        elif isinstance(data, str):
            return BencodeEncoder.encode(data.encode())
        elif isinstance(data, list):
            result = b'l'
            for item in data:
                result += BencodeEncoder.encode(item)
            result += b'e'
            return result
        elif isinstance(data, dict):
            result = b'd'


            encoded_keys = {}
            for key, value in data.items():
                if isinstance(key, str):
                    key = key.encode('utf-8')
                if not isinstance(key, bytes):
                    raise TypeError("Dictionary keys must be strings")
                if key in encoded_keys:
                    raise ValueError("Duplicate encoded dictionary key")
                encoded_keys[key] = value
            for key in sorted(encoded_keys):
                result += BencodeEncoder.encode(key)
                result += BencodeEncoder.encode(encoded_keys[key])
            result += b'e'
            return result
        else:
            raise TypeError(f"Unsupported type: {type(data)}")
