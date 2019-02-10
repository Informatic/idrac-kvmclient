import socketserver
import struct
import threading

from client import rgb555_to_rgb888, grpc_connect, KVMClient
import proxy_pb2

import csv

def build_keymap():
    keymap = {}

    with open('keymaps.csv') as fd:
        reader = csv.reader(fd)
        headers = next(reader)
        for row in reader:
            r = dict(zip(headers, row))
            if r['USB Keycodes'] and r['X11 keysym']:
                keymap[int(r['X11 keysym'][2:],16)] = int(r['USB Keycodes'])

    return keymap

class VNCHandler(socketserver.BaseRequestHandler):
    first_frame = None
    res_x = 0
    res_y = 0
    client = None
    keymap = build_keymap()

    def on_frame(self, chunks, resx, resy):
        print('on_frame(%d, %d, %d)' % (len(chunks), resx, resy))

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
                print('***Resolution change***')
                self.request.sendall(struct.pack('>BxHHHHHi',
                                                 0, 1,
                                                 0, 0, resx, resy,
                                                 -223))
                self.res_x = resx
                self.res_y = resy

            self.request.sendall(frame)

    def handle(self):
        print('Incoming conection from %r', self.client_address)
        # ProtocolVersion
        self.request.sendall(b'RFB 003.008\n')
        self.data = self.request.recv(1024).strip()
        print("{} wrote:".format(self.client_address[0]))
        print(self.data)

        # Security handshake
        self.request.send(struct.pack('>BB', 1, 1))
        print('Security type:', self.request.recv(1))

        # SecurityResult
        self.request.send(struct.pack('>I', 0))

        # ClientInit
        shared, = struct.unpack('>?', self.request.recv(1))
        print('Shared:', shared)

        stub = grpc_connect()
        arguments = stub.GetKVMData(proxy_pb2.GetKVMDataRequest(
            blade_num=10)).arguments
        print(arguments)

        self.connected = threading.Event()
        self.client = KVMClient.from_arguments(arguments)
        self.client.on_frame = self.on_frame
        self.client_thread = threading.Thread(target=self.client.run)
        self.client_thread.start()

        print('...waiting for connection')
        self.connected.wait()

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
                                  0,  # shift rgb
                                  0,
                                  0,
                                  len(server_name)
                                  ) + server_name
        self.request.sendall(server_init)
        print(server_init)

        while True:
            msg_type = ord(self.request.recv(1))

            if msg_type == 0x00:
                # SetPixelFormat
                self.request.recv(3)
                pixel_format = self.request.recv(16)
                print('Pixel format:', struct.unpack('>BBBBHHHBBBxxx', pixel_format))

            elif msg_type == 0x02:
                # SetEncodings
                num_enc, = struct.unpack('>xH', self.request.recv(3))
                print('Encodings:', num_enc, self.request.recv(4*num_enc))

            elif msg_type == 0x03:
                # UpdateRequest
                incremental, x, y, w, h = struct.unpack('>BHHHH', self.request.recv(9))
                print('Update request:', incremental, x, y, w, h)

                if self.first_frame:
                    self.request.sendall(self.first_frame)
                    self.first_frame = None

            elif msg_type == 0x04:
                modifiers = {
                    65507: 0x01,
                    65508: 0x10,
                    65505: 0x02,
                    65506: 0x20,
                    65513: 0x04,
                    65027: 0x40,
                }

                # KeyEvent
                # LCTRL 65507 = 0x01
                # RCTRL 65508 = 0x10
                # LALT 65513 = 0x04
                # RALT 65027 = 0x40 (not supported?)
                # LSHIFT 65505 = 0x02
                # RSHIFT 65506 = 0x20

                # 02 = shift
                # 01 = ctrl
                # 04 = alt
                # 10 = rctrl
                # 20 = rshift
                # 40 = ralt ← does not work on lunix?

                down, key = struct.unpack('>BxxI', self.request.recv(7))
                print('Key:', down, key)

                if key in modifiers:
                    keycode = 0

                    if down:
                        self.modifiers = self.modifiers | modifiers[key]
                    else:
                        self.modifiers = self.modifiers & ~modifiers[key]
                else:
                    keycode = self.keymap.get(key)

                if keycode is not None:
                    print('Sending %04x %02x %02x', keycode, self.modifiers, down)
                    self.client.send_keyboard(keycode, self.modifiers, down)
                else:
                    print('No keycode found :(')

            elif msg_type == 0x05:
                # PointerEvent
                mask, x, y = struct.unpack('>BHH', self.request.recv(5))
                print('Pointer:', mask, x, y)
            else:
                print('Unknown msg:', msg_type)

    modifiers = 0

    def finish(self):
        print('...cleanup')
        if self.client:
            self.client.stop()
        print('cleanup finished')

if __name__ == "__main__":
    HOST, PORT = "localhost", 5902

    # Create the server, binding to localhost on port 9999
    socketserver.TCPServer.allow_reuse_address = True
    server = socketserver.TCPServer((HOST, PORT), VNCHandler)

    # Activate the server; this will keep running until you
    # interrupt the program with Ctrl-C
    server.serve_forever()
