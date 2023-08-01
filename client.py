#!/usr/bin/env python3
import socket
import ssl
import sys
import select
import struct
import logging
import errno
from functools import reduce

from PIL import Image


logging.basicConfig(level=logging.INFO)

FRAME_TYPES = {
    "ADVISER_LOGIN": 1,
    "DUMMY_LOGIN": 2,
    "ADVISER_VIDEO_FRAGMENT": 3,
    "ADVISER_HID_PKT": 4,
    "ADVISER_REFRESH_VIDEO_SCREEN": 5,
    "ADVISER_PAUSE_REDIRECTION": 6,
    "ADVISER_RESUME_REDIRECTION": 7,
    "ADVISER_BLANK_SCREEN": 8,
    "ADVISER_STOP_SESSION_IMMEDIATE": 9,
    "ADVISER_GET_USB_MOUSE_MODE": 10,
    "ADVISER_SET_COLOR_MODE": 11,
    "ADVISER_USER_AUTH": 12,
    "ADVISER_SESS_REQ": 13,
    "ADVISER_SESS_APPROVAL": 14,
    "ADVISER_SYNC_KEYBD_LED": 15,
    "ADVISER_KVM_PRIV": 16,
    "ADVISER_SOCKET_STATUS": 17,
    "ADVISER_KEEPALIVE_REQ": 18,
    "ADVISER_KEEPALIVE_RES": 19,
    "ADVISER_SERVER_POWER": 20,
    "ADVISER_RET_VAL_SERVER_POWER": 21,
    "ADVISER_SLOT_INFO": 22,
    "ADVISER_SEND_SLOT_INFO": 23,
    "ADVISER_KEEPALIVE_REQ_HID": 24,
    "ADVISER_KEEPALIVE_RES_HID": 25,
    # 'ADVISER_SERVER_POWER_SUCCESS': 1,
    # 'ADVISER_SERVER_POWER_FAILURE': 2,
    # 'ADVISER_SERVER_POWER_ON': 1,
    # 'ADVISER_SERVER_POWER_OFF': 2,
    # 'ADVISER_SERVER_NMI': 3,
    # 'ADVISER_SERVER_GRACEFUL_SHUTDOWN': 4,
    # 'ADVISER_SERVER_RESET': 5,
    # 'ADVISER_SERVER_POWER_CYCLE': 6,
}

REV_FRAME_TYPES = {v: k for k, v in FRAME_TYPES.items()}


# Convert RGB555 to RGB888
def rgb555_to_rgb888(data):
    """
    Converts RGB555 to (vnc-compatible) RGB888
    """
    out = bytearray()

    for b in struct.unpack("<%dH" % (len(data) / 2), data):
        out.append(((b) & 0b11111) << 3)
        out.append(((b >> 5) & 0b11111) << 3)
        out.append(((b >> 10) & 0b11111) << 3)
        out.append(0)

    return bytes(out)


class KVMClient:
    """
    JViewer.jar-compatible iDRAC/AMI KVM client implementation
    """

    on_chunk = None
    on_frame = None

    def __init__(self, address, token, video_port=5901, video_ssl=True, kvm_port=5900):
        self.address = address
        self.token = token
        self.video_port = video_port
        self.video_ssl = video_ssl
        self.kvm_port = kvm_port

        self.fb = None
        self.running = True

        self.logger = logging.getLogger("client.KVMClient")

    @classmethod
    def from_arguments(cls, arguments):
        return cls(
            address=arguments[0].partition(":")[0],
            video_port=int(arguments[1]),
            token=arguments[2],
            video_ssl=bool(arguments[3]),
            # arguments[4]
            # arguments[5]
            # arguments[6]
            # arguments[7]
            kvm_port=int(arguments[8]),
        )

    def connect(self):
        self.ssl_context = ssl._create_unverified_context()

        self.video_socket = socket.create_connection((self.address, self.video_port))

        if self.video_ssl:
            self.video_socket = self.ssl_context.wrap_socket(self.video_socket)

        self.kvm_socket = None

    def run(self):
        self.connect()

        while self.running:
            r, _, _ = select.select(
                [self.video_socket] + ([self.kvm_socket] if self.kvm_socket else []),
                [],
                [],
                1.0,
            )
            for s in r:
                try:
                    self.process_socket(s)
                except OSError:
                    raise
                except:
                    logging.exception("Oops?")

    def stop(self):
        self.running = False

        try:
            self.video_socket.close()
        except:
            pass

        try:
            self.kvm_socket.close()
        except:
            pass

    def process_socket(self, sock):
        hdr = sock.recv(7)
        if not hdr:
            raise OSError(errno.ECONNRESET, "%r disconnected" % sock)

        msg_type, msg_len, status = struct.unpack("<BIH", hdr)

        self.logger.debug(
            "[%02x %30s / %7d / %08x] %r",
            msg_type,
            REV_FRAME_TYPES.get(msg_type, None),
            msg_len,
            status,
            sock.getpeername(),
        )

        payload = b""
        while len(payload) < msg_len:
            fragment = sock.recv(msg_len - len(payload))
            if not len(fragment):
                raise OSError(errno.ECONNRESET, "%r disconnected" % sock)

            payload += fragment

        if msg_type == 0x0E:
            # Authentication/handshake
            if not self.kvm_socket:
                self.kvm_socket = self.ssl_context.wrap_socket(
                    socket.create_connection((self.address, self.kvm_port))
                )
            elif sock == self.kvm_socket:
                self.authenticate()

        elif msg_type == 0x10:
            if status == 0x002:
                self.logger.info("Waiting for authorization")

            elif status == 0x0001:
                self.logger.info("Authorization request, approving")
                # 0x0201 video only
                # 0x0101 deny
                # 0x0001 allow
                self.send_frame(self.video_socket, 0x10, b"", 0x0201)

            elif status == 0x004:
                self.logger.info("Authorization approved")
            elif status == 0x104:
                self.logger.info("Authorization denied")

        elif msg_type == 0x12:
            # Keepalive
            self.send_frame(sock, 0x13, b"")

        elif msg_type == 0x03:
            # Video frame fragment
            self.process_video(payload)

        else:
            self.logger.warning(
                "Unhandled frame %d (%r) on %r",
                msg_type,
                REV_FRAME_TYPES.get(msg_type, None),
                sock.getpeername(),
            )

    def process_video(self, payload):
        hdrsize = 2 + 4 + 2 + 2 + 1
        fragnum, framesize, resx, resy, colormode = struct.unpack(
            "<HIHHB", payload[:hdrsize]
        )

        if not self.fb or (resx, resy) != self.fb.size:
            self.fb = Image.new("RGB", (resx, resy), color="red")

        framedata = payload[hdrsize:]
        self.logger.debug(
            "Video frame: %04x %08x %04x %04x %02x",
            fragnum,
            framesize,
            resx,
            resy,
            colormode,
        )

        pos = 0
        chunks = []
        while pos < len(framedata):
            x, y, w, h, compression_mode, compressed_length = struct.unpack(
                "<HHHHII", framedata[pos : pos + 16]
            )
            self.logger.debug("  %dx%d+%d+%d @ %d", w, h, x, y, compression_mode)
            compressed = framedata[pos + 16 : pos + 16 + compressed_length]
            pos += 16 + compressed_length

            chunk = None

            if compression_mode == 2 and colormode == 8:
                chunk = self.decompress(compressed)
            elif compression_mode == 0 and colormode == 8:
                chunk = compressed
            else:
                self.logger.warning(
                    "Unknown compression: %02x %02x", compression_mode, colormode
                )
                continue

            if self.on_chunk:
                self.on_chunk(x, y, w, h, chunk)

            chunks.append((x, y, w, h, chunk))

            # chunk_image = Image.frombytes('RGB', (w, h),
            #                              rgb555_to_rgb888(chunk))
            # self.fb.paste(chunk_image, (x, y))

        if self.on_frame:
            self.on_frame(chunks, resx, resy)

        self.frame_number += 1
        # self.fb.save('/tmp/vnc/frame_%04d.png' % (self.frame_number))

    frame_number = 0
    chunk_number = 0

    def decompress(self, data):
        out = bytearray(10240000)
        out_pos = len(out)

        data = bytearray(data)
        pos = len(data)

        while pos > 0 and out_pos > 0:
            chunk_len = (data[pos - 1] << 8) | (data[pos - 2])
            # chunk_len, = struct.unpack('<H', data[pos-2:pos])

            if chunk_len & 0x8000:
                # fill
                fill_data = data[pos - 4 : pos - 2]
                chunk_len = chunk_len & 0x7FFF

                out[out_pos - chunk_len * 2 : out_pos] = fill_data * chunk_len
                out_pos -= chunk_len * 2
                pos -= 4
            else:
                # copy
                out[out_pos - chunk_len * 2 : out_pos] = data[
                    pos - chunk_len * 2 - 2 : pos - 2
                ]
                out_pos -= chunk_len * 2
                pos -= 2 + (chunk_len * 2)

        if out_pos <= 0:
            raise Exception("Buffer too small")

        return bytes(out[out_pos:])

    def authenticate(self):
        self.logger.debug("...authenticating")
        self.send_frame(
            self.video_socket,
            0x0C,
            struct.pack("<B98s47s", 0, self.token.encode(), self.address.encode()),
        )

    def send_frame(self, sock, msg_type, data, status=0):
        sock.send(struct.pack("<BIH", msg_type, len(data), status) + data)

    def send_keyboard(self, keycode, modifiers, down):
        if down == 0:
            keycode = 0

        keyinfo = bytearray(
            [modifiers, down, keycode, 0, 0, 0, 0, 0]  # modifiers  # down  # keycode
        )
        i = 9
        seq = 0x3D

        payload = (
            bytearray([73, 85, 83, 66, 32, 32, 32, 32, 0x1, 0x0, 0x20, 0])  # signature
            + struct.pack("<I", i)
            + bytearray([0, 0x30, 0x10, 0x80, 2, 0, 0, 0])
            + struct.pack("<I", seq)
            + bytearray(
                [
                    0,
                    0,
                    0,
                    0,
                    8,
                ]
            )
            + keyinfo
        )

        checksum = (
            (reduce(lambda a, b: (a + b) & 0xFF, payload[:32], 0) ^ 0xFF) + 1
        ) & 0xFF
        payload[11] = checksum

        self.send_frame(self.kvm_socket, 0x04, payload)


if __name__ == "__main__":
    arguments = sys.argv[1:]

    client = KVMClient.from_arguments(arguments)
    client.run()
