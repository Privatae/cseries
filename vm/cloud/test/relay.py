"""The network's relay for remote access, on the test machine, for the cloud
VM test's relay leg: the reference relay (server/network/remote_relay.py, the
one the network's edge relay is built from), run as it is.

  python3 relay.py <address> <dir> <log> <status>

<dir> is the member phase's network directory (member_door.py): root.pem, the
test root that issues passports, is the only CA a tunnel's client certificate
may chain to; relay.pem and relay.key are the relay's own certificate for
tunnel.remote.privatae.ai under that root (the Host checks it against the
network root its passport came with); ticket.pub is the public half of the
member door's ticket key, kept here as the edges keep theirs, by kid.

Tunnels come to <address>:443, the port a Host dials every relay address its
ticket names on; browsers to <address>:8444 as plain TCP carrying their TLS,
which the relay never opens. The relay's own log lines go to <log> (a
stream's line holds the address ID and the browser's /24, never its full
address); every second <status> is rewritten with the address IDs that have
a tunnel up, so the test can see a tunnel come and go.
"""

import asyncio
import json
import logging
import os
import sys

from cryptography.hazmat.primitives import serialization

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "..", ".."))
from server.network import remote_relay  # noqa: E402

ADDR, DIR, LOG, STATUS = sys.argv[1:5]
KID = "vmtest-1"            # member_door.py's
ZONE = "remote.privatae.ai"
TUNNEL_PORT = 443
BROWSER_PORT = 8444


def write_status(relay):
    tmp = STATUS + ".new"
    with open(tmp, "w") as f:
        json.dump({"tunnels": sorted(a for a, t in relay.tunnels.items() if not t.closing)}, f)
    os.replace(tmp, STATUS)


async def main():
    logging.basicConfig(filename=LOG, level=logging.INFO, format="%(asctime)s %(message)s")
    with open(os.path.join(DIR, "ticket.pub"), "rb") as f:
        pub = serialization.load_pem_public_key(f.read())
    ctx = remote_relay.server_context(cert=os.path.join(DIR, "relay.pem"), key=os.path.join(DIR, "relay.key"),
                                      client_ca=os.path.join(DIR, "root.pem"))
    relay = remote_relay.Relay(zone=ZONE, ticket_keys={KID: pub}, tunnel_ssl=ctx)
    await relay.start(ADDR, TUNNEL_PORT, BROWSER_PORT)
    logging.info("relay listening on %s: tunnels %d, browsers %d", ADDR, relay.tunnel_port, relay.browser_port)
    while True:
        write_status(relay)
        await asyncio.sleep(1)


asyncio.run(main())
