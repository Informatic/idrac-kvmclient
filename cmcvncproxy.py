#
# Work-in-progress jwt-authz/authn'ed websocket VNC server integrated with
# hscloud/cmc-proxy (Warsaw Hackerspace Dell M1000e management)
#

import os
import mimetypes
from http.server import HTTPStatus
import urllib.parse
import logging

import websockets
import asyncio
import jwt
import grpc

from client import KVMClient
from vncproxy import VNCHandler

import proxy_pb2
import proxy_pb2_grpc


config = {
    'jwt_secret': 'secret',
}


def serve_static(path):
    """
    Returns a static files server for specified path to be used as
    `process_request` handler with websockets
    """

    base = os.path.abspath(path)
    mimetypes.init()

    async def handler(path, headers):
        path = urllib.parse.urlparse(path).path
        if path == '/':
            path = 'index.html'
        else:
            path = path[1:]

        target = os.path.abspath(os.path.join(base, path))
        if not target.startswith(base) or not os.path.exists(target):
            return None

        with open(target, 'rb') as fd:
            return HTTPStatus.OK, {
                'content-type': mimetypes.guess_type(path)[0],
            }, fd.read()

    return handler


def read_key(name):
    with open('/home/informatic/gopath/src/code.hackerspace.pl/hscloud/go/svc/cmc-proxy/pki/%s' % name, 'rb') as fd:
        return fd.read()


def grpc_connect():
    credentials = grpc.ssl_channel_credentials(
        root_certificates=read_key('ca.pem'),
        private_key=read_key('service-key.pem'),
        certificate_chain=read_key('service.pem'))
    channel = grpc.secure_channel('cmc-proxy.dev.svc.cluster.local:4200',
                                  credentials)
    stub = proxy_pb2_grpc.CMCProxyStub(channel)
    return stub


if __name__ == "__main__":
    HOST, PORT = "0.0.0.0", 8081
    logger = logging.getLogger('proxy')
    loop = asyncio.get_event_loop()

    async def handler(websocket, path):
        logger.info('Incoming conection on %s' % path)
        token = path.split('/')[-1]
        data = jwt.decode(token, config['jwt_secret'], algorithms=['HS256'])

        if 'blade' not in data or data['blade'] < 1 or data['blade'] > 16:
            logger.warning('Invalid data?')
            return

        stub = grpc_connect()
        arguments = stub.GetKVMData(proxy_pb2.GetKVMDataRequest(
            blade_num=data['blade'])).arguments

        logger.debug('KVM arguments: %r', arguments)

        client = KVMClient.from_arguments(arguments)

        handler = VNCHandler(websocket, client, loop)
        try:
            await handler.handle()
        finally:
            handler.finish()

    start_server = websockets.serve(
        handler, HOST, PORT, subprotocols=['binary'],
        process_request=serve_static('./noVNC-1.0.0/'))

    loop.run_until_complete(start_server)
    loop.run_forever()
