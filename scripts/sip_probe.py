#!/usr/bin/env python3
"""
Send one authenticated SIP INVITE to the LiveKit Cloud SIP URI and report the outcome.

Verifies a DID is routable without placing a carrier call: the expected sequence is
100 -> 407 (digest challenge) -> 180 Ringing -> 200 OK, after which we send BYE. Optionally
exchanges RTP for a few seconds to prove the media path, which also confirms NAT latching.

    python3 scripts/sip_probe.py +14793482732          # signalling only
    python3 scripts/sip_probe.py +14793482732 --rtp    # signalling + 8s of RTP

Reads LIVEKIT_SIP_DOMAIN / LIVEKIT_SIP_USERNAME / LIVEKIT_SIP_PASSWORD from .env.
See docs/ADD_PHONE_NUMBER.md for what each failure means.
"""

import hashlib
import os
import random
import re
import socket
import struct
import sys
import time

from dotenv import load_dotenv

ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
load_dotenv(os.path.join(ROOT, ".env"))

HOST = os.getenv("LIVEKIT_SIP_DOMAIN", "3k0byilfyuy.sip.livekit.cloud")
PORT = 5060
USER = os.getenv("LIVEKIT_SIP_USERNAME", "")
PASS = os.getenv("LIVEKIT_SIP_PASSWORD", "")
FROM = "+15551234567"


def md5(text: str) -> str:
    return hashlib.md5(text.encode()).hexdigest()


def main() -> int:
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    if not args:
        print(__doc__)
        return 2
    to = args[0]
    want_rtp = "--rtp" in sys.argv
    uri = f"sip:{to}@{HOST}"

    ip = socket.gethostbyname(HOST)
    sig = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sig.settimeout(10)
    sig.bind(("0.0.0.0", 0))
    rtp = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    rtp.settimeout(0.2)
    rtp.bind(("0.0.0.0", 0))
    probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    probe.connect((ip, PORT))
    local_ip = probe.getsockname()[0]
    probe.close()
    lport = sig.getsockname()[1]
    rport = rtp.getsockname()[1]

    tag = "%08x" % random.getrandbits(32)
    call_id = "%016x" % random.getrandbits(64)
    sdp = (
        "v=0\r\n"
        f"o=- 0 0 IN IP4 {local_ip}\r\n"
        "s=probe\r\n"
        f"c=IN IP4 {local_ip}\r\n"
        "t=0 0\r\n"
        f"m=audio {rport} RTP/AVP 0 101\r\n"
        "a=rtpmap:0 PCMU/8000\r\n"
        "a=rtpmap:101 telephone-event/8000\r\n"
        "a=sendrecv\r\n"
    )

    def invite(cseq: int, auth: str = ""):
        branch = "z9hG4bK" + "%08x" % random.getrandbits(32)
        msg = (
            f"INVITE {uri} SIP/2.0\r\n"
            f"Via: SIP/2.0/UDP {local_ip}:{lport};branch={branch};rport\r\n"
            "Max-Forwards: 70\r\n"
            f"From: <sip:{FROM}@{local_ip}>;tag={tag}\r\n"
            f"To: <{uri}>\r\n"
            f"Call-ID: {call_id}\r\n"
            f"CSeq: {cseq} INVITE\r\n"
            f"Contact: <sip:{FROM}@{local_ip}:{lport}>\r\n"
            "User-Agent: voice-agent-sip-probe\r\n"
        )
        if auth:
            msg += auth + "\r\n"
        msg += f"Content-Type: application/sdp\r\nContent-Length: {len(sdp)}\r\n\r\n{sdp}"
        return msg, branch

    def ack(branch: str, cseq: int, to_tag: str = ""):
        sig.sendto((
            f"ACK {uri} SIP/2.0\r\n"
            f"Via: SIP/2.0/UDP {local_ip}:{lport};branch={branch}\r\n"
            "Max-Forwards: 70\r\n"
            f"From: <sip:{FROM}@{local_ip}>;tag={tag}\r\n"
            f"To: <{uri}>{to_tag}\r\n"
            f"Call-ID: {call_id}\r\nCSeq: {cseq} ACK\r\nContent-Length: 0\r\n\r\n"
        ).encode(), (ip, PORT))

    def wait_final(branch: str, cseq: int):
        deadline = time.time() + 12
        while time.time() < deadline:
            try:
                data, _ = sig.recvfrom(65535)
            except socket.timeout:
                return None, ""
            text = data.decode("utf-8", "replace")
            line = text.split("\r\n")[0]
            if not line.startswith("SIP/2.0"):
                continue
            print("   <--", line)
            code = int(line.split(" ")[1])
            if code >= 300:
                ack(branch, cseq)
            if code >= 200 or code in (401, 407):
                return code, text
        return None, ""

    print(f"--> INVITE {uri}")
    msg, branch = invite(1)
    sig.sendto(msg.encode(), (ip, PORT))
    code, text = wait_final(branch, 1)

    if code in (401, 407):
        if not USER:
            print("\nTrunk requires auth but LIVEKIT_SIP_USERNAME is not set in .env")
            return 1
        header = "WWW-Authenticate" if code == 401 else "Proxy-Authenticate"
        challenge = next(
            l for l in text.split("\r\n") if l.lower().startswith(header.lower())
        ).split(":", 1)[1].strip()
        params = dict(re.findall(r'(\w+)="?([^",]+)"?', challenge))
        ha1 = md5(f"{USER}:{params['realm']}:{PASS}")
        ha2 = md5(f"INVITE:{uri}")
        response = md5(f"{ha1}:{params['nonce']}:{ha2}")
        auth = (
            f'Authorization: Digest username="{USER}", realm="{params["realm"]}", '
            f'nonce="{params["nonce"]}", uri="{uri}", response="{response}"'
        )
        if code == 407:
            auth = auth.replace("Authorization:", "Proxy-Authorization:")
        print("--> INVITE (authenticated)")
        msg, branch = invite(2, auth)
        sig.sendto(msg.encode(), (ip, PORT))
        code, text = wait_final(branch, 2)

    if not code:
        print("\nRESULT: no final response — nothing answered on that host/port")
        return 1
    if code == 404:
        print("\nRESULT: 404 No trunk found — number is not on the inbound trunk, or the SIP host is wrong")
        return 1
    if code in (401, 407):
        print("\nRESULT: still challenged after authenticating — credentials do not match the trunk")
        return 1
    if not 200 <= code < 300:
        print(f"\nRESULT: rejected with {code}")
        return 1

    m = re.search(r"^To:.*;tag=([^\r\n;]+)", text, re.M)
    to_tag = ";tag=" + m.group(1) if m else ""
    ack(branch, 2, to_tag)
    print("\nRESULT: accepted — the DID routes and an agent was dispatched")

    if want_rtp:
        remote_ip = re.search(r"c=IN IP4 ([\d.]+)", text).group(1)
        remote_port = int(re.search(r"m=audio (\d+)", text).group(1))
        print(f"    sending PCMU silence to {remote_ip}:{remote_port} for 8s")
        seq, ts, ssrc, got = random.randint(0, 30000), 0, random.getrandbits(32), 0
        end = time.time() + 8
        while time.time() < end:
            rtp.sendto(struct.pack("!BBHII", 0x80, 0, seq & 0xFFFF, ts, ssrc) + b"\xff" * 160,
                       (remote_ip, remote_port))
            seq += 1
            ts += 160
            try:
                data, _ = rtp.recvfrom(4096)
                if len(data) > 12:
                    got += 1
            except socket.timeout:
                pass
            time.sleep(0.02)
        verdict = "media path OK" if got > 100 else "NO return media — NAT/latching problem"
        print(f"    inbound RTP packets: {got}  ->  {verdict}")

    time.sleep(0.3)
    bye_branch = "z9hG4bK" + "%08x" % random.getrandbits(32)
    sig.sendto((
        f"BYE {uri} SIP/2.0\r\n"
        f"Via: SIP/2.0/UDP {local_ip}:{lport};branch={bye_branch};rport\r\n"
        "Max-Forwards: 70\r\n"
        f"From: <sip:{FROM}@{local_ip}>;tag={tag}\r\n"
        f"To: <{uri}>{to_tag}\r\n"
        f"Call-ID: {call_id}\r\nCSeq: 3 BYE\r\nContent-Length: 0\r\n\r\n"
    ).encode(), (ip, PORT))
    return 0


if __name__ == "__main__":
    sys.exit(main())
