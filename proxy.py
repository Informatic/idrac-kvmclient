import socketserver
import struct
import threading
import asyncio
import csv
import logging

import websockets
import jwt

from client import rgb555_to_rgb888, grpc_connect, KVMClient
import proxy_pb2


config = {
    'jwt_secret': 'secret',
}


def build_keymap():
    keymap = {}

    with open('keymaps.csv') as fd:
        reader = csv.reader(fd)
        headers = next(reader)
        for row in reader:
            r = dict(zip(headers, row))
            if r['USB Keycodes'] and r['X11 keysym']:
                keymap[int(r['X11 keysym'][2:], 16)] = int(r['USB Keycodes'])

    return keymap


class VNCHandler(object):
    first_frame = None
    res_x = 0
    res_y = 0
    client = None
    keymap = build_keymap()

    def __init__(self, ws, path):
        self.ws = ws
        self.path = path
        self.recv_buffer = bytearray()
        self.logger = logging.getLogger('proxy.VNCHandler')

    async def on_frame(self, chunks, resx, resy):
        self.logger.debug('on_frame(%d, %d, %d)' % (len(chunks), resx, resy))

        frame = struct.pack('>BxH',
                            0,
                            len(chunks))

        for (x, y, w, h, chunk) in chunks:
            chunkdata = rgb555_to_rgb888(chunk)
            if y + h > resy:
                h = resy - y
                chunkdata = chunkdata[:w*h*4]
            frame += struct.pack('>HHHHi',
                                 x,
                                 y,
                                 w,
                                 h,
                                 0,
                                 ) + chunkdata

        if not self.connected.is_set():
            self.res_x = resx
            self.res_y = resy
            self.first_frame = frame
            self.connected.set()

        else:
            if (self.res_x, self.res_y) != (resx, resy):
                self.logger.debug('Resolution change detected')
                await self.send(struct.pack('>BxHHHHHi',
                                            0, 1,
                                            0, 0, resx, resy,
                                            -223))
                self.res_x = resx
                self.res_y = resy

            await self.send(frame)

    async def recv(self, num_bytes=None):
        if num_bytes is None:
            if len(self.recv_buffer) == 0:
                return await self.ws.recv()
            else:
                buf = self.recv_buffer
                self.recv_buffer = bytearray()
                return buf

        while len(self.recv_buffer) < num_bytes:
            self.recv_buffer += await self.ws.recv()

        chunk = self.recv_buffer[:num_bytes]
        self.recv_buffer = self.recv_buffer[num_bytes:]

        return chunk

    async def send(self, payload):
        await self.ws.send(payload)

    async def handle(self):
        self.logger.info('Incoming conection on %s' % (self.path))
        token = self.path.split('/')[-1]
        data = jwt.decode(token, config['jwt_secret'], algorithms=['HS256'])

        if 'blade' not in data or data['blade'] < 1 or data['blade'] > 16:
            self.logger.warning('Invalid data?')
            return

        # ProtocolVersion
        await self.send(b'RFB 003.008\n')
        client_version = (await self.recv()).strip()
        self.logger.debug('Client version: %s', client_version)

        # Security handshake
        await self.send(struct.pack('>BB', 1, 1))
        client_security = await self.recv(1)
        self.logger.debug('Security type: %02x', client_security)

        # SecurityResult
        await self.send(struct.pack('>I', 0))

        # ClientInit
        shared, = struct.unpack('>?', await self.recv(1))
        self.logger.debug('Shared: %r', shared)

        stub = grpc_connect()
        arguments = stub.GetKVMData(proxy_pb2.GetKVMDataRequest(
            blade_num=data['blade'])).arguments

        self.logger.debug('KVM arguments: %r', arguments)

        self.connected = asyncio.Event()
        self.client = KVMClient.from_arguments(arguments)
        loop = asyncio.get_event_loop()
        self.client.on_frame = lambda *args: asyncio.run_coroutine_threadsafe(
            self.on_frame(*args), loop)
        self.client_thread = threading.Thread(target=self.client.run)
        self.client_thread.start()

        self.logger.info('...waiting for connection')
        await self.connected.wait()
        self.logger.info('Connected! %d %d', self.res_x, self.res_y)

        # ServerInit
        server_name = b'Test RFB Server'
        server_init = struct.pack('>HHBBBBHHHBBBxxxI',
                                  self.res_x,  # width
                                  self.res_y,  # height
                                  24,  # bpp
                                  24,  # depth
                                  0,  # big endian
                                  1,  # true color
                                  0xff,  # max rgb
                                  0xff,
                                  0xff,
                                  16,  # shift rgb
                                  8,
                                  0,
                                  len(server_name)
                                  ) + server_name
        await self.send(server_init)

        while True:
            msg_type = ord(await self.recv(1))
            if msg_type in self.handlers:
                fmt, cb = self.handlers[msg_type]
                payload = await self.recv(struct.calcsize(fmt))
                data = struct.unpack(fmt, payload)
                await cb(self, *data)

    async def handle_SetPixelFormat(self, *pixel_format):
        # SetPixelFormat
        self.logger.info('Pixel format: %r', pixel_format)

    async def handle_SetEncodings(self, num_enc):
        # SetEncodings
        self.logger.info('Encodings: %d %r', num_enc,
                         await self.recv(4*num_enc))

    async def handle_UpdateRequest(self, incremental, x, y, w, h):
        # UpdateRequest
        if self.first_frame:
            await self.send(self.first_frame)
            self.first_frame = None

    modifiers = 0
    async def handle_KeyEvent(self, down, key):
        modifiers = {
            65507: 0x01,  # L CTRL
            65508: 0x10,  # R CTRL
            65505: 0x02,  # L SHIFT
            65506: 0x20,  # R SHIFT
            65513: 0x04,  # L ALT
            65027: 0x40,  # R ALT
        }

        self.logger.debug('Key:', down, key)

        if key in modifiers:
            keycode = 0

            if down:
                self.modifiers = self.modifiers | modifiers[key]
            else:
                self.modifiers = self.modifiers & ~modifiers[key]
        else:
            keycode = self.keymap.get(key)

        if keycode is not None:
            self.logger.debug('Sending %04x %02x %02x', keycode, self.modifiers, down)
            self.client.send_keyboard(keycode, self.modifiers, down)
        else:
            self.logger.warning('No keycode found for %r %r', down, key)

    async def handle_PointerEvent(self, mask, x, y):
        self.logger.debug('Pointer: %02x %d %d', mask, x, y)

    handlers = {
        0x00: ('>xxxBBBBHHHBBBxxx', handle_SetPixelFormat),
        0x02: ('>xH', handle_SetEncodings),
        0x03: ('>BHHHH', handle_UpdateRequest),
        0x04: ('>BxxI', handle_KeyEvent),
        0x05: ('>BHH', handle_PointerEvent),
    }

    def finish(self):
        if self.client:
            self.client.stop()
        self.logger.debug('cleanup finished')


if __name__ == "__main__":
    HOST, PORT = "0.0.0.0", 8081

    async def handler(websocket, path):
        handler = VNCHandler(websocket, path)
        try:
            await handler.handle()
        finally:
            handler.finish()

    start_server = websockets.serve(handler, HOST, PORT,
                                    subprotocols=['binary', ''])

    asyncio.get_event_loop().run_until_complete(start_server)
    asyncio.get_event_loop().run_forever()
