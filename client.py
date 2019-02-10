#!/usr/bin/env python3
import socket
import ssl
import sys
import select
import struct
import logging
import time
import binascii
import errno
from functools import reduce

from PIL import Image

import grpc
import proxy_pb2
import proxy_pb2_grpc

logging.basicConfig(level=logging.DEBUG)

frame_types = {
    'ADVISER_LOGIN': 1,
    'DUMMY_LOGIN': 2,
    'ADVISER_VIDEO_FRAGMENT': 3,
    'ADVISER_HID_PKT': 4,
    'ADVISER_REFRESH_VIDEO_SCREEN': 5,
    'ADVISER_PAUSE_REDIRECTION': 6,
    'ADVISER_RESUME_REDIRECTION': 7,
    'ADVISER_BLANK_SCREEN': 8,
    'ADVISER_STOP_SESSION_IMMEDIATE': 9,
    'ADVISER_GET_USB_MOUSE_MODE': 10,
    'ADVISER_SET_COLOR_MODE': 11,
    'ADVISER_USER_AUTH': 12,
    'ADVISER_SESS_REQ': 13,
    'ADVISER_SESS_APPROVAL': 14,
    'ADVISER_SYNC_KEYBD_LED': 15,
    'ADVISER_KVM_PRIV': 16,
    'ADVISER_SOCKET_STATUS': 17,
    'ADVISER_KEEPALIVE_REQ': 18,
    'ADVISER_KEEPALIVE_RES': 19,
    'ADVISER_SERVER_POWER': 20,
    'ADVISER_RET_VAL_SERVER_POWER': 21,
    'ADVISER_SLOT_INFO': 22,
    'ADVISER_SEND_SLOT_INFO': 23,
    'ADVISER_KEEPALIVE_REQ_HID': 24,
    'ADVISER_KEEPALIVE_RES_HID': 25,

    #'ADVISER_SERVER_POWER_SUCCESS': 1,
    #'ADVISER_SERVER_POWER_FAILURE': 2,
    #'ADVISER_SERVER_POWER_ON': 1,
    #'ADVISER_SERVER_POWER_OFF': 2,
    #'ADVISER_SERVER_NMI': 3,
    #'ADVISER_SERVER_GRACEFUL_SHUTDOWN': 4,
    #'ADVISER_SERVER_RESET': 5,
    #'ADVISER_SERVER_POWER_CYCLE': 6,
}

rev_types = {v: k for k, v in frame_types.items()}


# Convert RGB555 to RGB888
def rgb555_to_rgb888(data):
    out = bytearray()

    for b in struct.unpack('<%dH' % (len(data)/2), data):
        out.append(((b) & 0b11111) << 3)
        out.append(((b >> 5) & 0b11111) << 3)
        out.append(((b >> 10) & 0b11111) << 3)
        out.append(0)

    return bytes(out)


class KVMClient:
    on_chunk = None
    on_frame = None

    def __init__(self, address, token, video_port=5901,
                 video_ssl=True, kvm_port=5900):
        self.address = address
        self.token = token
        self.video_port = video_port
        self.video_ssl = video_ssl
        self.kvm_port = kvm_port

        self.fb = None
        self.running = True

    @classmethod
    def from_arguments(cls, arguments):
        return cls(
            address=arguments[0].partition(':')[0],
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

        self.video_socket = socket.create_connection(
            (self.address, self.video_port))

        if self.video_ssl:
            self.video_socket = self.ssl_context.wrap_socket(self.video_socket)

        self.kvm_socket = None

    def run(self):
        self.connect()

        while self.running:
            r, _, _ = select.select([self.video_socket] + ([self.kvm_socket] if self.kvm_socket else []), [], [], 1.0)
            for s in r:
                try:
                    self.process_socket(s)
                except OSError:
                    raise
                except:
                    logging.exception('Oops?')

    def stop(self):
        self.running = False
        self.video_socket.close()
        self.kvm_socket.close()

    def process_socket(self, sock):
        hdr = sock.recv(7)
        if not hdr:
            raise OSError(errno.ECONNRESET, '%r disconnected' % sock)

        msg_type, msg_len, status = struct.unpack('<BIH', hdr)

        print('[%02x %30s / %7d / %08x] %r' % (
            msg_type, rev_types.get(msg_type, None), msg_len, status,
            sock.getpeername()))

        payload = b''
        while len(payload) < msg_len:
            fragment = sock.recv(msg_len - len(payload))
            if not len(fragment):
                raise OSError(errno.ECONNRESET, '%r disconnected' % sock)

            payload += fragment

        if msg_type == 0x0e:
            # Authentication/handshake
            if not self.kvm_socket:
                self.kvm_socket = self.ssl_context.wrap_socket(
                    socket.create_connection((self.address, self.kvm_port)))
            elif sock == self.kvm_socket:
                self.authenticate()

        elif msg_type == 0x12:
            # Keepalive
            self.send_frame(sock, 0x13, b'')

        elif msg_type == 0x03:
            # Video frame fragment
            self.process_video(payload)

    def process_video(self, payload):
        hdrsize = 2 + 4 + 2 + 2 + 1
        fragnum, framesize, resx, resy, colormode = struct.unpack(
            '<HIHHB', payload[:hdrsize])

        if not self.fb or (resx, resy) != self.fb.size:
            self.fb = Image.new('RGB', (resx, resy), color='red')

        framedata = payload[hdrsize:]
        print('%04x %08x %04x %04x %02x' % (fragnum, framesize, resx, resy,
                                            colormode))

        pos = 0
        chunks = []
        while pos < len(framedata):
            x, y, w, h, compression_mode, compressed_length = \
                struct.unpack('<HHHHII', framedata[pos:pos+16])
            print('  %dx%d+%d+%d @ %d' % (w, h, x, y, compression_mode))
            compressed = framedata[pos+16:pos+16+compressed_length]
            pos += 16 + compressed_length

            chunk = None

            if compression_mode == 2 and colormode == 8:
                chunk = self.decompress(compressed)
            elif compression_mode == 0 and colormode == 8:
                chunk = compressed
            else:
                print('** unknown compression **')
                continue

            if self.on_chunk:
                self.on_chunk(x, y, w, h, chunk)

            chunks.append((x, y, w, h, chunk))

            #chunk_image = Image.frombytes('RGB', (w, h),
            #                              rgb555_to_rgb888(chunk))
            #self.fb.paste(chunk_image, (x, y))

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
            chunk_len = (data[pos-1] << 8) | (data[pos-2])
            # chunk_len, = struct.unpack('<H', data[pos-2:pos])

            if chunk_len & 0x8000:
                # fill
                fill_data = data[pos-4:pos-2]
                chunk_len = chunk_len & 0x7fff

                out[out_pos-chunk_len*2:out_pos] = fill_data * chunk_len
                out_pos -= chunk_len*2
                pos -= 4
            else:
                # copy
                out[out_pos-chunk_len*2:out_pos] = \
                    data[pos-chunk_len*2-2:pos-2]
                out_pos -= chunk_len * 2
                pos -= 2 + (chunk_len * 2)

        if out_pos <= 0:
            raise Exception('Buffer too small')

        return bytes(out[out_pos:])

    def authenticate(self):
        print('...authenticating')
        self.send_frame(self.video_socket, 0x0c, struct.pack(
            '<B98s47s', 0, self.token.encode(), self.address.encode()))

    def send_frame(self, sock, msg_type, data, status=0):
        sock.send(struct.pack('<BIH', msg_type, len(data), status) + data)

    def send_keyboard(self, keycode, modifiers, down):
        if down == 0:
            keycode = 0

        keyinfo = bytearray([
            modifiers,  # modifiers
            down,  # down
            keycode,  # keycode
            0, 0, 0, 0, 0])
        i = 9
        seq = 0x3d

        payload = bytearray([
            73, 85, 83, 66, 32, 32, 32, 32,  # signature
            0x1, 0x0, 0x20, 0
        ]) + struct.pack('<I', i) + bytearray([
            0, 0x30, 0x10, 0x80, 2, 0, 0, 0
        ]) + struct.pack('<I', seq) + bytearray([
            0, 0, 0, 0,
            8,
        ]) + keyinfo

        checksum = ((reduce(
            lambda a, b: (a + b) & 0xff, payload[:32], 0) ^ 0xff) + 1) & 0xff
        payload[11] = checksum

        self.send_frame(self.kvm_socket, 0x04, payload)


def read_key(name):
    with open('/home/informatic/gopath/src/code.hackerspace.pl/hscloud/go/svc/cmc-proxy/pki/%s' % name, 'rb') as fd:
        return fd.read()

def grpc_connect():
    credentials = grpc.ssl_channel_credentials(
        root_certificates=read_key('ca.pem'),
        private_key=read_key('service-key.pem'),
        certificate_chain=read_key('service.pem'))
    channel = grpc.secure_channel('cmc-proxy.dev.svc.cluster.local:4200', credentials)
    stub = proxy_pb2_grpc.CMCProxyStub(channel)
    return stub


if __name__ == '__main__':
    if len(sys.argv) == 2:
        stub = grpc_connect()
        arguments = stub.GetKVMData(proxy_pb2.GetKVMDataRequest(
            blade_num=int(sys.argv[1]))).arguments
        print(arguments)
    else:
        arguments = sys.argv[1:]

    client = KVMClient.from_arguments(arguments)
    client.run()
