# A model on the bastion's machine, and a Host that connects to a server: runs, 2026-11-03

Two runs of this branch's Host code against the real bastion binaries, each in
private PID, network and mount namespaces on one Linux machine. Every result
below is output captured during the run. The scripts are in
[scripts/verification_runs/serve_lane_and_host_dials_2026_11_03/](../../scripts/verification_runs/serve_lane_and_host_dials_2026_11_03/).

How the Host ran: `python3 -m server.launch` from this tree, with the image's
`/app` layout bind-mounted read-only from the tree (Dockerfile.linux's COPY
lines), the app signing keys under `/etc/cseries/keys`, a Python with
`websockets` 16.0 (requirements.txt), and the kernel's own schema in a throwaway
cseries-postgres reached over its unix socket (`db_hold.py`,
scripts/kernel_schema.py). Inside the namespaces `/run`, `/tmp`,
`/var/lib/privatae` and `/var/lib/cseries` are fresh tmpfs mounts, and `/etc` is
an overlay whose writes stay there, so no path, socket, lock or firewall rule of
the machine's own is touched. No provider key or model address is set anywhere.

The person's side is driven over HTTP the way the apps drive it: the first
sign-in (`/api/owner/setup` with the code from the Host's log), an app launch
for its token, then the app's routes.

## 1. Chat on the model served on the machine the Host runs on (no key)

`inner.sh` + `drive.py`. The machine runs bastion `main` 46c14c8 and a stand-in
OpenAI-compatible server on ITS 127.0.0.1:8080. The Host runs in a network
namespace of its own (its VM) that can't reach the machine's loopback; its
enroll and lane ports are forwarded to the machine's 127.0.0.1:8810/8811, as
`npx cseries`'s QEMU forwards publish them. Pairing is `npx cseries`'s own two
steps: `python3 -m cli.cseries bastion pair-token` in the Host, then
`privatae-bastion pair --redeem-url <enroll> --host-ca <ca> -` on the machine.

```
from the Host VM: 10.77.0.1:8080 -> exit 7 (refused)
from the Host VM: its 127.0.0.1:8080 -> exit 7 (refused)
Paired with home4dc0f5c7.cseries.localhost over TCP.
[home4dc0f5c7.cseries.localhost] serve workers up — the model servers set on this machine, by name, via wss://home4dc0f5c7.cseries.localhost:8443/bastion-serve on 127.0.0.1:8811
[llm] found an OpenAI-compatible model server on 127.0.0.1:8080, llama.cpp's port: standin-llama-1b
serve B1b bind-enforce: token_id=1 ... cn='operator' cn_trusted=True peer=127.0.0.1 ok=True
first sign-in (owner setup): 200
Getting Started's offer, GET /api/inference/local-default: 200 {"candidate": {"model": "standin-llama-1b", "machine": "privatae-mgmt-2", "kind": "llm-openai"}, "chat_routed": false, "holds_key": false, "by_default": true}
POST /api/circles/messages (self): 200 in 1.0s
0:"Hello from the stand-in model server on the bastion's machine."
```

The check-in row: `transport tcp | llm_models ["standin-llama-1b"] | llm_port NULL | reported_ip NULL`.
The stand-in's own log and the bastion's audit of the stream:

```
07:06:08 standin POST /v1/chat/completions from 127.0.0.1 model='standin-llama-1b' stream=True last_message='Say hello in one short sentence.' authorization=none
{"bytes_from_host":18539,"bytes_to_host":1219,"decision":"served","idle_closed":false,"op":"serve","service":"llm","target":"127.0.0.1:8080","ts":"2026-11-03T07:06:23Z"}
```

An earlier run of the same script showed the turn's own accounting:
`"model": "standin-llama-1b", "provider": "local"`.

## 2. A Host at home connects to a server on the internet (bastion branch full-compat-s2)

`inner2.sh` + `drive2.py`. Three namespaces: the server, 203.0.113.1, running
bastion `full-compat-s2` 40dcf50 and a stand-in model server on its own
127.0.0.1:8080; a router, 203.0.113.2, masquerading the home LAN and forwarding
nothing inbound; home, 192.168.1.10, where the Host runs.

```
ok: the Host is up at home, and the server cannot reach it (192.168.1.10:8811 behind the NAT)
on the server: privatae-bastion pair --open
  code:     (20 characters, shown once)
```

Network app's route, `POST /api/network/servers/pair`, with the Network app's token:

```
Add a server with a wrong code -> 403 The server didn't take that code. Check it against the one privatae-bastion pair --open shows there (each wrong try makes it wait longer), or open a new window.
Add a server, 203.0.113.1 and the code -> 200
 "state": "active", "server": "203.0.113.1:8812", "machine": "privatae-mgmt-2", "token_id": 1,
 "message": "Your Host is connected to privatae-mgmt-2. It connects to it by itself from now on, whenever it runs."
```

Lanes, check-in, a grant and an op, all over the connections the Host opened:

```
the server in Network: {"id": 1, "online": true, "last_seen_at": "2026-11-03T07:07:27...", "capabilities": ["llm-openai", ...]}
authenticator enrolled: 200
grant proposed: 200 {"grant_id": 1, "server": "privatae-mgmt-2", "ops": ["uptime"], ...}
grant approved with the authenticator code: 200 {"grant_id": 1, "approved": true, ..., "stepped_up": true, "approved_by": "user"}
uptime on the server under that grant, over the Host's connections: 200 {"op": "uptime", "exit": 0, "stdout": " 07:08:01 up 62 days, ..."}
chat, no key: 200 reply: ["Hello from the stand-in model server on the bastion's machine."] model/provider: [('standin-llama-1b', 'local')]
```

The chat used the server's model through its serve lane, over the Host's
connections. The person chose it for the thread (`model` on the message): the
server is not one this Host is in charge of (it has no anchor), so by the
own-machine rule it is not picked by default.

On the server, every connection arrives from the router's address, and none
goes the other way (`ss` on the server, in the first run of this script, 06:36Z):

```
server side, established on :8812: 20, each from [::ffff:203.0.113.2]
server-originated connections to 192.168.1.0/24: 0
```

Asked in chat. The person tells their Host the server's address and a fresh
window's code. The Host's model (here a stand-in that answers as a model would,
in the Host's XML tool format: `find_tools`, then `operate(tool='manage_bastion',
args={op:'add_server', ...})`) files the card; the person presses Connect
through `POST /api/tickets/{id}/decide`, the route Circles uses.

While the Host was still paired there, the server refused, and the card said so:

```
the person pressed Connect: 409 {"detail": "203.0.113.1:8812 didn't pair with your Host: this Host is already paired with this server as home201721e4.cseries.localhost; nothing was paired. Nothing was kept."}
```

After `privatae-bastion unpair` on the server, with the Host's model a server
the person named on the home network (`PUT /api/inference/local-endpoints`
http://192.168.1.1:8080; no key, no model chosen):

```
chat (no key, no model chosen): 200 the Host said: I've asked you on a card to connect to that server. model/provider: [('standin-llama-1b', 'local')]
the card over the chat: {"id": 3, "sitrep": "Connect your Host to the server at 203.0.113.1:8812?\n...", "options": ["Connect", "Don't connect"]}
the person pressed Connect: 200 {"decision": {"option_id": "connect", "note": "Your Host is connected to privatae-mgmt-2. ..."}, "server": {"connected": true, "state": "active", "token_id": 2, ...}, "answer_turn_started": true}
server 203.0.113.1:8812: pairing token_id=1 replaced by token_id=2
lane port: closed 1 connection(s) of replaced bastion token_id=1
connected to server 203.0.113.1:8812 (token_id=2)
```

The Host's turn then received the person's answer with the server's words
("Ticket #3 — I chose “Connect”: Your Host is connected to privatae-mgmt-2 ...").
The scripted stand-in model, reading its first message again, started the same
calls once more in that answer turn; a real model reads the answer.

Restarts, by the check-in's time:

```
before:                      07:10:43
after the bastion restarted: 07:11:02
after the Host restarted:    07:11:09   (connected to server 203.0.113.1:8812 (token_id=2), 19 connections from 203.0.113.2)
```

The server's pairing audit for the run (`pairing_audit.jsonl`): the window
opened, the wrong code (1 of 5), paired from 203.0.113.2, connected, the
"already paired" refusals, disconnected and connected again across the
restarts. A second wrong code at 07:10:27 was the driver pressing an older card
whose code had already been used.
