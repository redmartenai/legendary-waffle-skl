"""
Watch a bus live, exactly like the parent app does (sign in -> realtime token ->
subscribe to trip:{id} over WebSocket). Useful for testing live tracking.

    python scripts/watch_trip.py                      # Meera Iyer (Aarav & Diya), morning trip
    python scripts/watch_trip.py --seconds 120 --min-updates 5

Run the backend (runserver) and Centrifugo first; start `manage.py simulate_trip` to move the bus.
"""

import argparse
import asyncio
import json
import sys

import httpx
import websockets


async def main(args) -> int:
    async with httpx.AsyncClient(timeout=10) as http:
        requested = (await http.post(f"{args.api}/auth/otp/request", json={"school_code": args.school, "phone": args.phone})).json()
        if "dev_code" not in requested:
            print("No dev_code returned: set OTP_DEV_ECHO=1 on the backend.")
            return 2
        verified = (
            await http.post(
                f"{args.api}/auth/otp/verify",
                json={"challenge_id": requested["challenge_id"], "code": requested["dev_code"]},
            )
        ).json()
        headers = {"Authorization": f"Bearer {verified['access']}"}
        connection = (await http.get(f"{args.api}/realtime/connection", headers=headers)).json()
        if not connection.get("enabled"):
            print("Realtime is disabled on the backend (CENTRIFUGO_* not set); the app would poll instead.")
            return 2
        child = (await http.get(f"{args.api}/parent/children", headers=headers)).json()["children"][0]
        transport = (await http.get(f"{args.api}/students/{child['id']}/transport", headers=headers)).json()
        trip = next(t for t in transport["today"] if t["direction"] == args.direction)
        subscription = (
            await http.get(f"{args.api}/transport/trips/{trip['trip_id']}/subscription", headers=headers)
        ).json()

    print(f"Signed in as {verified['user']['full_name']}; watching {child['name']}'s {args.direction} trip {trip['trip_id']}")
    updates = 0
    loop = asyncio.get_running_loop()
    deadline = loop.time() + args.seconds
    async with websockets.connect(connection["url"]) as ws:
        await ws.send(json.dumps({"id": 1, "connect": {"token": connection["token"], "name": "watch_trip"}}))
        await ws.send(json.dumps({"id": 2, "subscribe": {"channel": subscription["channel"], "token": subscription["token"]}}))
        await ws.send(json.dumps({"id": 3, "subscribe": {"channel": connection["personal_channel"]}}))
        while loop.time() < deadline:
            try:
                frame = await asyncio.wait_for(ws.recv(), timeout=max(0.1, deadline - loop.time()))
            except TimeoutError:
                break
            for line in str(frame).strip().splitlines():
                message = json.loads(line) if line else {}
                if message == {}:
                    await ws.send("{}")  # answer the server's ping
                    continue
                if "error" in message:
                    print("Centrifugo error:", message["error"])
                    return 1
                if message.get("id") in {1, 2, 3}:
                    print({1: "connected", 2: "subscribed to trip", 3: "subscribed to personal channel"}[message["id"]])
                    continue
                pub = (message.get("push") or {}).get("pub")
                if not pub:
                    continue
                data = pub.get("data", {})
                if data.get("type") == "trip.live":
                    updates += 1
                    live = data["trip"]
                    nxt = live.get("next_stop") or {}
                    eta = nxt.get("eta_seconds")
                    eta_text = f"{eta // 60}m{eta % 60:02d}s" if isinstance(eta, int) else "—"
                    position = live.get("position") or {}
                    print(
                        f"[{updates:3d}] {live['status']:<9} signal={live['signal']:<5} "
                        f"next={nxt.get('name', '—'):<20} eta={eta_text:<7} "
                        f"at=({position.get('lat', 0):.5f},{position.get('lng', 0):.5f})"
                    )
                elif data.get("type") == "chat.message":
                    print("chat:", data["message"]["body"])
    print(f"Received {updates} live updates.")
    return 0 if updates >= args.min_updates else 1


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--api", default="http://127.0.0.1:8000/api/v1")
    parser.add_argument("--school", default="GHIS")
    parser.add_argument("--phone", default="+919900000001")
    parser.add_argument("--direction", default="pickup", choices=["pickup", "drop"])
    parser.add_argument("--seconds", type=float, default=60)
    parser.add_argument("--min-updates", type=int, default=1)
    sys.exit(asyncio.run(main(parser.parse_args())))
