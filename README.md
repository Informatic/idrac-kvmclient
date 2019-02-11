# iDRAC JViewer KVM Client

This is a quick and hackish reimplementation of iDRAC "iDRACView"/JViewer.jar
KVM client with integrated VNC proxy.

## Usage

This project required python 3.6+ and a bunch of normal dependencies. To install
these use:

    pip install -r requirements.txt

To test example VNC proxy use you can use `vncproxy.py`, which accepts the same
command line arguments as JViewer.jar, ie. (these can be extracted from
`<argument>` fields of `jviewer.jnlp` file downloaded from iDRAC webpage)

    [IP] [video port] [authentication token] [video encryption flag] [?] [?] [?] [?] [keyboard&mouse port]

    ...like:

    1.2.3.4 5901 abcdefABCDEF1234 1 0 3668 3669 511 5900 1 EN

...and connect to `localhost:5902`. This is still pretty much all work in
progress, and only video & keyboard is supported, VNC server is approx. 21.37%
protocol specification compliant, but at least seems to work "good enough" with
NoVNC, Remmina and XVNCViewer (except from minor issues with full screen
refreshes in the last one)

`client.KVMClient` class is supposed to be more-or-less reusable, but the API is
far from stable.

Another interesting PoC available is `cmcvncproxy.py`, which is a NoVNC
websockets-vnc proxy similar based `vncproxy.py`, that automatically generates
all required authentication arguments using [Dell M1000e CMC
proxy](https://code.hackerspace.pl/hscloud/tree/go/svc/cmc-proxy) that has been
developed for Warsaw Hackerspace internal use. Authentication is carried out
using JWT tokens passed in websocket URL.

## Protocol
Communication with iDRAC KVM is carried out using two TCP connections on port
5900 ("Keyboard & mouse port") and 5901 ("Video port"). Communication over 5901
can be optionally (default) wrapped in SSL (selected as "Video Port Encryption"
in iDRAC, though most probably actual keypair is hardcoded in iDRAC firmware,
and certificate is not even verified in any way in JViewer client).

User is authorized using 16-character a one-time use token generated by iDRAC
in `.jnlp` file. This token is sent only over port 5901.

Internally all frames on both sockets are more-or-less represented by a
following (packed) struct:

    struct kvm_frame {
        uint8_t msg_type;
        uint32_t msg_len;
        uint16_t status;
        uint8_t payload[msg_len];
    };

Message type names have been extracted from original `.jar` file. Status field
seems to be only used during multiple connection authorization. (first client
properly authorized to KVM can decide whether to allow another one to either
have full control over the virtual console, have only video preview, all deny
all access altogether)

Video frames (sent over port 5901) consist of multiple update rectangles
(similarly to VNC). Image data can be encoded using either 15-bit color
(RGB555) or 7-bit color, and then (optionally) compressed using simple RLE
algorithm (in either byte, 16-bit or 32-bit mode). Currently only 15-bit color
with 16-bit compression is supported. (the only mode that I managed to extract
frames in out of our test hardware) Interestingly, KVM sometimes seems to
report parts of rectangles out of actual reported framebuffer resolution (at
least in `y` axis), so this needs to be accounted for in user code.

Internally "Keyboard & Mouse" (5900) socket frames are using
[AMI iUSB protocol](https://github.com/samozy/iusb) wrapped in custom framing
described above. (though extended with support for mouse & keyboard, obviously)

## Missing features

 * Virtual Media redirection
 * Mouse redirection
 * Power control
 * 7-bit color mode
 * Alternative compression modes
 * Proper frame refreshes in VNC proxy
 * Shared KVM client instance in VNC proxy
