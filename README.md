# iDRAC JViewer KVM Client

This is a quick and hackish reimplementation of iDRAC "iDRACView"/JViewer.jar
KVM client with integrated VNC proxy.

## Usage

This project required python 3.6+ and a bunch of normal dependencies. To install
these use:

    pip install -r requirements.txt

To test example VNC proxy use you can use `vncproxy.py`, which accepts the same
command line arguments as JViewer.jar, ie.

    [IP] [video port] [authentication token] [video encryption flag] [?] [?] [?] [?] [keyboard&mouse port]

...and connect to `localhost:5902`. This is still pretty much all work in
progress, and only video & keyboard is supported, VNC server is approx. 21.37%
protocol specification compliant, but at least seems to work "good enough" with
NoVNC, Remmina and XVNCViewer (except from minor issues with full screen
refreshes in the last one)

`client.KVMClient` class is supposed to be more-or-less reusable, but the API is
far from stable.

## Missing features

 * Virtual Media redirection
 * Mouse redirection
