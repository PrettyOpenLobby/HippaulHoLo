"""The standalone TCP listener (POL_JAN_PORT, 51272): one thread per
connection, one line at a time.
"""
import os
import socket
import socketserver
from . import dispatch, wirelog

PORT = int(os.environ.get("POL_JAN_PORT", "51272"))
HOST = os.environ.get("POL_JAN_HOST", "0.0.0.0")


class Handler(socketserver.StreamRequestHandler):
    def handle(self):
        peer = "%s:%d" % self.client_address
        wirelog.log("%s connected" % peer)
        buf = b""
        try:
            while True:
                chunk = self.request.recv(4096)
                if not chunk:
                    break
                buf += chunk
                while True:
                    i = min((buf.find(t) for t in (b"\r\n", b"\n") if t in buf),
                            default=-1)
                    if i < 0:
                        break
                    line, buf = buf[:i], buf[i:].lstrip(b"\r\n")
                    if not line:
                        continue
                    reply = dispatch.handle_line(line, peer)
                    for r in ([reply] if isinstance(reply, bytes) else (reply or [])):
                        self.request.sendall(r + b"\r\n")
                if len(buf) > 65536:
                    wirelog.log("%s   %d unterminated bytes -- not line framing?\n%s"
                        % (peer, len(buf), wirelog.hexdump(buf)))
                    buf = b""
        except (ConnectionError, socket.timeout) as e:
            wirelog.log("%s connection ended: %s" % (peer, e))
        wirelog.log("%s disconnected" % peer)


class Server(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True


def serve(host=HOST, port=PORT):
    """The standalone 51272 listener -- a CAPTURE HARNESS, not where games run.

    Every hanchan is played on the AUTH band inside `responders` (the client
    never dials this port for play; see jan-ingame-handler-is-authsess). The
    deadline sweeper that used to run here ticked a Manager that never held a
    table and logged "no peer to send it to" for records nobody was waiting
    for -- audit finding 8/33. Removed 2026-09-04: `jangame.Manager.tick` is
    swept by `Manager.handle` on the band the games are on.
    """
    srv = Server((host, port), Handler)
    wirelog.log("listening on %s:%d (content id 3, gi003.pol.com) -- capture harness; "
        "games play on the auth band" % (host, port))
    srv.serve_forever()
