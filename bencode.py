'''
turning torrent bytes python dictionary,
then we slice some info from dictionary with bencode encoder to make it 20byte infohash
'''

class BencodeDecoder:
    def __init__(self, data):
        self.data = data
        self.index = 0

    def decode(self):
        return self._decode_next()

    def _decode_next(self): #decoding next elements
        if self.index >= len(self.data):
            raise ValueError("Unexpected end of data")

        current = chr(self.data[self.index])

        if current == 'i':
            return self._decode_int()
        elif current == 'l':
            return self._decode_list()
        elif current == 'd':
            return self._decode_dict()
        elif current.isdigit():
            return self._decode_string()
        else:
            raise ValueError(f"Unknown bencode type: {current}")

    def _decode_int(self):
        self.index += 1  #skip i i<>e
        end = self.data.index(b'e', self.index)
        number = int(self.data[self.index:end])
        self.index = end + 1
        return number

    def _decode_string(self): # n:<>
        colon = self.data.index(b':', self.index)
        length = int(self.data[self.index:colon])
        self.index = colon + 1
        string = self.data[self.index:self.index + length]
        self.index += length
        return string

    def _decode_list(self): #<>e
        self.index += 1  #skip l
        items = []
        while chr(self.data[self.index]) != 'e':
            items.append(self._decode_next())
        self.index += 1  #skip e
        return items

    def _decode_dict(self): #d<>e
        self.index += 1  #skip d
        dictionary = {}
        while chr(self.data[self.index]) != 'e':
            key = self._decode_next()
            value = self._decode_next() #keys = bytes in bencode
            if isinstance(key, bytes):
                key = key.decode('utf-8', errors='ignore')
            dictionary[key] = value
        self.index += 1  #skip e
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


            for key in sorted(data.keys()): #sorting
                result += BencodeEncoder.encode(key)
                result += BencodeEncoder.encode(data[key])
            result += b'e'
            return result
        else:
            raise TypeError(f"Unsupported type: {type(data)}")
