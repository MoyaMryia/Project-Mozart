"""读取固定大小的 PCM 块，管道积压保留在内核中。"""


def read_block(stream, size):
    payload = bytearray()
    while len(payload) < size:
        chunk = stream.read(size - len(payload))
        if not chunk:
            break
        payload.extend(chunk)
    return bytes(payload)
