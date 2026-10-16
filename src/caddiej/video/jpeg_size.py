"""Read a JPEG's size without decoding it."""


def jpeg_size(data: bytes) -> tuple[int, int] | None:
    """Width and height from a JPEG SOF marker, or None if it is not a JPEG.

    Enough to report what was extracted. Not a decoder, and it never keeps the
    bytes it is shown.
    """

    if len(data) < 4 or data[0:2] != b"\xff\xd8":
        return None
    index = 2
    while index + 8 < len(data):
        if data[index] != 0xFF:
            return None
        marker = data[index + 1]
        if marker in {0xC0, 0xC1, 0xC2}:
            height = int.from_bytes(data[index + 5 : index + 7], "big")
            width = int.from_bytes(data[index + 7 : index + 9], "big")
            return width, height
        if marker in {0xD8, 0xD9}:
            index += 2
            continue
        length = int.from_bytes(data[index + 2 : index + 4], "big")
        if length < 2:
            return None
        index += 2 + length
    return None
