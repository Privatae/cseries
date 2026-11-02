"""The git credential helper of the Host's own coding forge.

git runs this (credential.helper in ~/.gitconfig, and the -c the Host's own
git calls pass: server/git_server.helper_command) each time it needs to sign
in, as `<python> -I git_credential_helper.py <socket> get`, with the request
on stdin (protocol=https, host=...). It hands the request to the running Host
over the Unix socket that server/git_server.serve_credentials listens on, which
reads the token from Keys for that one answer, and prints what comes back. It
never writes anything, so the token is never on the disk: it lives in Keys and
in the memory of the git process that asked.

`store` and `erase` (git telling helpers a sign-in worked or failed) are read
and ignored: there is nothing here to keep or forget.

Standard library only, and nothing from the Host imported, so a git call costs
one short Python start. Every failure is an empty answer, which git takes as
"this helper has nothing", and one line on stderr saying why.
"""
import socket
import sys

READ_MAX = 64 * 1024
TIMEOUT_S = 30


def main(argv: list) -> int:
    request = sys.stdin.buffer.read(READ_MAX)
    if len(argv) < 3 or argv[2] != "get":
        return 0
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
            s.settimeout(TIMEOUT_S)
            s.connect(argv[1])
            s.sendall(request)
            s.shutdown(socket.SHUT_WR)
            reply = b""
            while len(reply) < READ_MAX:
                got = s.recv(READ_MAX)
                if not got:
                    break
                reply += got
    except OSError as e:
        print(f"cseries git credential: the Host did not answer ({e.__class__.__name__})",
              file=sys.stderr)
        return 0
    sys.stdout.buffer.write(reply[:READ_MAX])
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
