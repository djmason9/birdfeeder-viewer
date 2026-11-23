"""mitmproxy addon: log VicoHome live-view activity to data/live_watch.log (no blocking).

Run with xlink (port 52000) passed through untouched (replace YOUR_MAC_LAN_IP with this
Mac's address, dots escaped, e.g. 192\\.168\\.1\\.10):
  mitmweb --mode wireguard --mode regular@8888 \
    --ignore-hosts '(^|\\.)(icloud|apple|mzstatic)\\.com(:\\d+)?$|^YOUR_MAC_LAN_IP(:\\d+)?$|:52000$' \
    -s postman/live_watch.py -w data/vicohome-live2.flows
"""
import os
import time

LOG = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "data", "live_watch.log")
WATCH = ("getWebrtcTicket", "wakeupDevice", "getAccessEndpoint", "Live", "live", "p2p", "stream")


def _log(msg):
    with open(LOG, "a") as f:
        f.write("%s %s\n" % (time.strftime("%H:%M:%S"), msg))


def running():
    _log("---- live_watch started ----")


def response(flow):
    path = flow.request.path.split("?", 1)[0]
    if any(w in path for w in WATCH):
        _log("HTTP %s %s%s -> %s" % (flow.request.method, flow.request.pretty_host, path, flow.response.status_code))


def websocket_start(flow):
    _log("WS OPEN %s%s" % (flow.request.pretty_host, flow.request.path.split("?", 1)[0]))


def websocket_message(flow):
    m = flow.websocket.messages[-1]
    _log("WS %s %s %d bytes" % ("->" if m.from_client else "<-", "text" if m.is_text else "binary", len(m.content)))


def websocket_end(flow):
    _log("WS CLOSE %s (%d messages)" % (flow.request.pretty_host, len(flow.websocket.messages)))


def tls_failed_client(data):
    sni = data.context.client.sni or "?"
    if "smartvideogo" in sni or "vicoo" in sni:
        _log("TLS FAIL (client rejected our cert) for %s" % sni)


def tcp_start(flow):
    _log("TCP %s" % (flow.server_conn.address,))


def udp_start(flow):
    addr = flow.server_conn.address
    if addr and addr[1] != 53:
        _log("UDP %s" % (addr,))
