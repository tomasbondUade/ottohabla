from __future__ import annotations

import argparse
import signal
import socket
import struct
import wave


MCAST_GRP = "239.168.123.161"
MCAST_PORT = 5555
LOCAL_IP = "192.168.123.164"
SAMPLE_RATE = 16000

running = True


def stop(_signum, _frame) -> None:
    global running
    running = False


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Record Unitree G1 microphone UDP PCM to WAV.")
    parser.add_argument("out_wav")
    parser.add_argument("--group", default=MCAST_GRP)
    parser.add_argument("--port", type=int, default=MCAST_PORT)
    parser.add_argument("--local-ip", default=LOCAL_IP)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)

    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.settimeout(0.5)
    sock.bind(("", args.port))
    membership = socket.inet_aton(args.group) + socket.inet_aton(args.local_ip)
    sock.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP, membership)

    frames = 0
    with wave.open(args.out_wav, "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(SAMPLE_RATE)

        while running:
            try:
                data, _addr = sock.recvfrom(65535)
            except socket.timeout:
                continue
            if len(data) % 2:
                data = data[:-1]
            wav.writeframes(data)
            frames += len(data) // 2

    sock.close()
    print(f"recorded_frames={frames}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
