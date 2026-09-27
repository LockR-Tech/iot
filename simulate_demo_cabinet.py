"""Temporary cabinet simulator for demoing the mobile <-> IoT unlock loop
without real Raspberry Pi/Arduino hardware.

This is intentionally separate from `main.py` (the real hardware-track
runtime maintained for the physical cabinet) and does not touch
`infracstructure/serial_manager.py`, `services/setup_handler.py`, or any
other file on that track. `main.py` only starts responding to open
commands after it receives a SETUP_LOCKERS handshake on
`iot/{macAddress}/command/setup` -- nothing in the backend sends that
handshake today, and even with SIMULATION=true it would still wait for it.
This script skips that handshake entirely: it subscribes to every
cabinet's open command directly and replies as if a cabinet were wired up.

What it talks to: `iot-service` (`LockerMqttService.sendUnlockCommandAsync`)
publishes to `cabinet/{lockerId}/command/open` with body
`{"commandId": "...", "boxId": <id>, "box_id": <id>, "slotIndex": <n>, "action": "OPEN", "timeout": 15}`
and waits for a reply on `cabinet/{lockerId}/command/open/result`. This script
answers that reply. Contract: docs/01-overview/mqtt-contract.md (ADR-0008).

It also mirrors the **booking -> IoT sync** (GAP 1): whenever an order
reserves/occupies/releases a cell, locker-service -> iot-service publishes the
box's new state to `cabinet/{lockerId}/command/sync` with body
`{"boxId": <id>, "state": "RESERVED|OCCUPIED|AVAILABLE|FAULT", "orderId": <id>?}`.
That message is fire-and-forget (no reply expected); this script just logs it,
standing in for the cabinet updating its on-screen cell map.

After a successful open it also reports the box's **physical** door state (GAP 2)
on `cabinet/{lockerId}/locker/{slotIndex}/status` (`{"slotIndex": <n>, "boxId": <id>,
"hwState": "OPEN"|"CLOSED", "doorOpen": bool}`). iot-service persists that hardware
truth separately from the order-driven box status, so ops can spot mismatches.
An older backend that sends no `slotIndex` gets the old shape (`slotIndex` = box id).

Usage:
    uv run python simulate_demo_cabinet.py
    SIM_FORCE_FAIL=true uv run python simulate_demo_cabinet.py   # test the failure path
    SIM_DELAY_SECONDS=3 uv run python simulate_demo_cabinet.py   # slower "door" for demos

Env (only used by this script, independent of config/settings.py so it
defaults to the SAME broker iot-service defaults to when nothing is
configured):
    MQTT_BROKER_URL   e.g. tcp://broker.hivemq.com:1883 (matches iot-service's own env var),
                      or wss://api.locker-drone.tech/mqtt for the private broker
    MQTT_BROKER / MQTT_PORT   alternative host/port pair if you'd rather set those
    MQTT_USERNAME / MQTT_PASSWORD   account on the private broker (never sent without TLS)
    SIM_DELAY_SECONDS  simulated door latency before replying (default 1.5)
    SIM_FORCE_FAIL     "true" to always reply FAILED, for testing the error path
    SIM_HEARTBEAT_SECONDS    how often to heartbeat known cabinets (default 30)
    SIM_HEARTBEAT_CABINETS   comma-separated cabinet/locker ids to mark ONLINE
                             up-front, e.g. "2,3" (otherwise learned from traffic)

Device health (GAP 3): iot-service records `cabinet/{id}/heartbeat` for the
device-health dashboard (`GET /api/manage/iot/device-status`). Nothing was
publishing one, so this sim now heartbeats every cabinet it knows about (seeded
via SIM_HEARTBEAT_CABINETS and/or learned from open/sync traffic).
"""

import json
import os
import threading
import time
from datetime import datetime, timezone

import paho.mqtt.client as mqtt

OPEN_COMMAND_TOPIC = "cabinet/+/command/open"
# Booking -> IoT sync (GAP 1): locker-service publishes a box's new lifecycle
# state (RESERVED/OCCUPIED/AVAILABLE/FAULT) here whenever an order reserves/
# occupies/releases a cell, so the cabinet can mirror the booking. Fire-and-
# forget on the backend side (no reply expected) -- we just log it as if the
# cabinet were updating its on-screen cell map.
SYNC_COMMAND_TOPIC = "cabinet/+/command/sync"
SIM_DELAY_SECONDS = float(os.getenv("SIM_DELAY_SECONDS", "1.5"))
SIM_FORCE_FAIL = os.getenv("SIM_FORCE_FAIL", "false").lower() == "true"
# Door auto-close (GAP 2): after a successful open, the cabinet reports the box
# door OPEN then, a few seconds later, CLOSED on the box-status channel that
# iot-service persists (`cabinet/{lockerId}/locker/{boxId}/status`). This is the
# hardware truth, kept separate from the order-driven status.
SIM_DOOR_CLOSE_SECONDS = float(os.getenv("SIM_DOOR_CLOSE_SECONDS", "4"))

# Device health (GAP 3): iot-service's `LockerMqttService` already subscribes to
# `cabinet/{id}/heartbeat` and records last-seen for the device-health dashboard
# (`GET /api/manage/iot/device-status`), but nothing was ever publishing one, so
# the dashboard stayed empty. This sim now periodically heartbeats every cabinet
# it knows about. Cabinets are learned from any traffic (open/sync command for
# `cabinet/{id}/...`) and/or seeded up-front via SIM_HEARTBEAT_CABINETS so a
# device can show ONLINE before the first command arrives.
SIM_HEARTBEAT_SECONDS = float(os.getenv("SIM_HEARTBEAT_SECONDS", "30"))
SIM_HEARTBEAT_CABINETS = [
    c.strip() for c in os.getenv("SIM_HEARTBEAT_CABINETS", "").split(",") if c.strip()
]
_known_cabinets: set[str] = set()
_known_lock = threading.Lock()


_DEFAULT_PORTS = {"tcp": 1883, "mqtt": 1883, "ssl": 8883, "mqtts": 8883, "ws": 80, "wss": 443}


def _resolve_broker() -> dict:
    """Same default as iot-service's `mqtt.broker-url` (tcp://broker.hivemq.com:1883)
    so this script works out of the box without any local config, but still
    honours MQTT_BROKER_URL / MQTT_BROKER+MQTT_PORT if someone pointed both
    sides at a different broker (a local Mosquitto, or wss://…/mqtt in production)."""
    url = os.getenv("MQTT_BROKER_URL")
    if url:
        scheme, _, rest = url.partition("://") if "://" in url else ("tcp", "", url)
        hostport, slash, path = rest.partition("/")
        host, _, port = hostport.partition(":")
        scheme = scheme.lower()
        return {
            "host": host,
            "port": int(port) if port else _DEFAULT_PORTS.get(scheme, 1883),
            "tls": scheme in ("ssl", "mqtts", "wss"),
            "transport": "websockets" if scheme in ("ws", "wss") else "tcp",
            "path": f"/{path}" if slash else "/mqtt",
        }
    return {
        "host": os.getenv("MQTT_BROKER", "broker.hivemq.com"),
        "port": int(os.getenv("MQTT_PORT", "1883")),
        "tls": False, "transport": "tcp", "path": "/mqtt",
    }


def _publish_heartbeat(client: mqtt.Client, cabinet_id: str):
    """Publish one ONLINE heartbeat for a cabinet. iot-service maps the topic
    segment to the device id and stamps last-seen, so the device shows ONLINE."""
    if not client.is_connected():
        return
    payload = {"status": "ONLINE", "timestamp": datetime.now(timezone.utc).isoformat()}
    client.publish(f"cabinet/{cabinet_id}/heartbeat", json.dumps(payload), qos=1)


def _learn_cabinet(client: mqtt.Client, cabinet_id: str):
    """Remember a cabinet seen in traffic and heartbeat it immediately so it
    appears ONLINE right away instead of waiting for the next interval tick."""
    with _known_lock:
        new = cabinet_id not in _known_cabinets
        _known_cabinets.add(cabinet_id)
    if new:
        print(f"[SIM] Learned cabinet {cabinet_id} -> heartbeating it")
        _publish_heartbeat(client, cabinet_id)


def _heartbeat_loop(client: mqtt.Client):
    """Background thread: periodically heartbeat every known cabinet."""
    while True:
        time.sleep(SIM_HEARTBEAT_SECONDS)
        with _known_lock:
            cabinets = list(_known_cabinets)
        for cabinet_id in cabinets:
            _publish_heartbeat(client, cabinet_id)
        if cabinets:
            print(f"[SIM] Heartbeat sent for {len(cabinets)} cabinet(s): {', '.join(cabinets)}")


def _on_connect(client, userdata, flags, reason_code, properties=None):
    if reason_code == 0:
        print(f"[SIM] Connected. Subscribing to {OPEN_COMMAND_TOPIC} and {SYNC_COMMAND_TOPIC}")
        client.subscribe([(OPEN_COMMAND_TOPIC, 1), (SYNC_COMMAND_TOPIC, 1)])
        # Announce seeded cabinets ONLINE right after (re)connect.
        with _known_lock:
            cabinets = list(_known_cabinets)
        for cabinet_id in cabinets:
            _publish_heartbeat(client, cabinet_id)
    else:
        print(f"[SIM] Connect failed, reason_code={reason_code}")


def _report_box_hw_state(client: mqtt.Client, locker_id: str, box_id, slot_index, hw_state: str):
    """GAP 2: report a box's physical door/sensor state on the status channel
    iot-service persists (`cabinet/{lockerId}/locker/{slotIndex}/status`). Kept
    separate from the order-driven status — hardware truth only. Without a
    slotIndex (older backend) fall back to the old shape: box id in `slotIndex`."""
    if not client.is_connected():
        return
    if slot_index is None:
        topic = f"cabinet/{locker_id}/locker/{box_id}/status"
        payload = {"slotIndex": box_id, "hwState": hw_state}
    else:
        topic = f"cabinet/{locker_id}/locker/{slot_index}/status"
        payload = {"slotIndex": slot_index, "boxId": box_id, "hwState": hw_state,
                   "doorOpen": hw_state == "OPEN"}
    payload["timestamp"] = datetime.now(timezone.utc).isoformat()
    client.publish(topic, json.dumps(payload), qos=1)
    print(f"[SIM] HW state -> {topic}: box={box_id} slot={slot_index} {hw_state}")


def _reply_after_delay(client: mqtt.Client, locker_id: str, command_id, box_id, slot_index):
    time.sleep(SIM_DELAY_SECONDS)
    failed = SIM_FORCE_FAIL
    status = "FAILED" if failed else "SUCCESS"
    payload = {
        "commandId": command_id,
        "boxId": box_id,
        "slotIndex": slot_index,
        "status": status,
        "hwState": "CLOSED" if failed else "OPEN",
        "errorCode": "SIMULATED_FAILURE" if failed else None,
        "errorMessage": "Simulated hardware failure" if failed else "Simulated: door opened",
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }
    result_topic = f"cabinet/{locker_id}/command/open/result"
    client.publish(result_topic, json.dumps(payload), qos=1)
    icon = "FAILED" if failed else "OK"
    print(f"[SIM] {icon} -> {result_topic}: {json.dumps(payload)}")

    # On a successful open, report the door physically OPEN now and CLOSED shortly
    # after — gives iot-service real hardware telemetry to persist (GAP 2).
    if not failed and box_id is not None:
        _report_box_hw_state(client, locker_id, box_id, slot_index, "OPEN")
        threading.Timer(
            SIM_DOOR_CLOSE_SECONDS,
            _report_box_hw_state,
            args=(client, locker_id, box_id, slot_index, "CLOSED"),
        ).start()


def _on_message(client, userdata, msg):
    parts = msg.topic.split("/")
    if len(parts) < 2:
        return
    locker_id = parts[1]
    # Any traffic for this cabinet means it's alive — start heartbeating it.
    _learn_cabinet(client, locker_id)
    try:
        data = json.loads(msg.payload.decode())
    except json.JSONDecodeError:
        print(f"[SIM] Ignoring non-JSON payload on {msg.topic}")
        return

    if msg.topic.endswith("/command/sync"):
        # Booking -> IoT sync: no reply expected, just mirror the cell state.
        box_id = data.get("boxId", data.get("box_id"))
        state = data.get("state")
        order_id = data.get("orderId")
        order_part = f" order={order_id}" if order_id is not None else ""
        print(f"[SIM] SYNC: cabinet display updated -> locker={locker_id} box={box_id} "
              f"slot={data.get('slotIndex')} state={state}{order_part}")
        return

    command_id = data.get("commandId")
    box_id = data.get("boxId", data.get("box_id"))
    slot_index = data.get("slotIndex")
    print(f"[SIM] OPEN request: locker={locker_id} box={box_id} slot={slot_index} commandId={command_id}")
    threading.Thread(
        target=_reply_after_delay,
        args=(client, locker_id, command_id, box_id, slot_index),
        daemon=True,
    ).start()


def main():
    broker = _resolve_broker()
    host, port = broker["host"], broker["port"]
    with _known_lock:
        _known_cabinets.update(SIM_HEARTBEAT_CABINETS)
    print("=" * 60)
    print("  Demo cabinet simulator (no hardware required)")
    print(f"  Broker: {host}:{port} ({broker['transport']}{', TLS' if broker['tls'] else ''})")
    print(f"  Reply delay: {SIM_DELAY_SECONDS}s, force fail: {SIM_FORCE_FAIL}")
    print(f"  Heartbeat: every {SIM_HEARTBEAT_SECONDS}s for known cabinets")
    if SIM_HEARTBEAT_CABINETS:
        print(f"  Seeded cabinets (ONLINE on connect): {', '.join(SIM_HEARTBEAT_CABINETS)}")
    else:
        print("  Cabinets learned from traffic (set SIM_HEARTBEAT_CABINETS=2,3 to pre-seed)")
    print("  This stands in for the real cabinet runtime (main.py) until")
    print("  Raspberry Pi/Arduino hardware + the setup handshake are ready.")
    print("=" * 60)

    client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, transport=broker["transport"])
    if broker["transport"] == "websockets":
        client.ws_set_options(path=broker["path"])
    if broker["tls"]:
        client.tls_set()
    username = os.getenv("MQTT_USERNAME")
    if username:
        if not broker["tls"]:
            raise SystemExit("MQTT_USERNAME set but broker URL has no TLS (use mqtts:// or wss://)")
        client.username_pw_set(username, os.getenv("MQTT_PASSWORD", ""))
    client.on_connect = _on_connect
    client.on_message = _on_message
    client.connect(host, port, keepalive=60)

    threading.Thread(target=_heartbeat_loop, args=(client,), daemon=True).start()

    try:
        client.loop_forever()
    except KeyboardInterrupt:
        print("\n[SIM] Shutting down...")
        client.disconnect()


if __name__ == "__main__":
    main()
