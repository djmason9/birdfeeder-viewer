"""mitmproxy addon: block VicoHome's xlink live-view path so the app falls back to WebRTC.

Run:  mitmweb --mode wireguard -s postman/force_webrtc.py -w data/vicohome-webrtc.flows
"""
import logging

XLINK_PORT = 52000


def request(flow):
    # The app asks for xlink relay endpoints here; refuse so it has none to use.
    if flow.request.path.split("?", 1)[0].endswith("/device/getAccessEndpoint"):
        logging.warning("force_webrtc: blocked getAccessEndpoint")
        flow.kill()


def tcp_start(flow):
    addr = flow.server_conn.address
    if addr and addr[1] == XLINK_PORT:
        logging.warning("force_webrtc: killed xlink TCP to %s:%s", addr[0], addr[1])
        flow.kill()


def websocket_start(flow):
    logging.warning("force_webrtc: WebSocket opened to %s%s",
                    flow.request.pretty_host, flow.request.path.split("?", 1)[0])
