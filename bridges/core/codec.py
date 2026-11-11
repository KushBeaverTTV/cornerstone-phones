"""Checksums and byte helpers the brand protocols share.

Each protocol picked one of a few well-known checks; they are written once
here, table-driven, and named by their catalogue names so a reader can look
them up. tests/codec_test.py pins every one against its standard check value
(the checksum of b"123456789").
"""
import zlib


def _table16(poly, reflected):
    table = []
    for byte in range(256):
        if reflected:
            crc = byte
            for _ in range(8):
                crc = (crc >> 1) ^ poly if crc & 1 else crc >> 1
        else:
            crc = byte << 8
            for _ in range(8):
                crc = ((crc << 1) ^ poly) if crc & 0x8000 else crc << 1
        table.append(crc & 0xFFFF)
    return table


_XMODEM = _table16(0x1021, reflected=False)
_MODBUS = _table16(0xA001, reflected=True)


def crc16_xmodem(data, crc=0x0000):
    """CRC-16/XMODEM: poly 0x1021, init 0, not reflected (Samsung, Huawei)."""
    for byte in bytes(data):
        crc = ((crc << 8) & 0xFFFF) ^ _XMODEM[((crc >> 8) ^ byte) & 0xFF]
    return crc


def crc16_modbus(data, crc=0xFFFF):
    """CRC-16/MODBUS: poly 0x8005 reflected, init 0xFFFF (Nothing)."""
    for byte in bytes(data):
        crc = (crc >> 8) ^ _MODBUS[(crc ^ byte) & 0xFF]
    return crc


def crc32(data):
    """CRC-32 (ISO-HDLC), the zlib one (Motorola)."""
    return zlib.crc32(bytes(data)) & 0xFFFFFFFF


def sum8(data):
    """The low byte of the plain sum (Sony, Soundcore, TOZO)."""
    return sum(bytes(data)) & 0xFF


def hexstr(data):
    """b'\\xaa\\x01' -> 'aa 01', the way captures and pins write bytes."""
    return " ".join("%02x" % byte for byte in bytes(data))


def unhex(text):
    """'aa 01' or 'aa01' -> b'\\xaa\\x01'."""
    return bytes.fromhex(str(text).replace(" ", ""))
