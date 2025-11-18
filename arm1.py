#!/usr/bin/env python3
"""
Manipulator control — Tkinter GUI + MQTT + Gamepad (pygame)

Poprawiona wersja listenera pygame: nie wykonuje pygame.joystick.quit() co pętlę,
utrzymuje obiekty Joystick i tylko wykrywa zmiany (hot-plug). Dzięki temu preview
wartości z kontrolera nie znika natychmiast do 0.
"""

import tkinter as tk
from tkinter import ttk
import json
import threading
import time
import queue
import logging
import sys

import paho.mqtt.client as mqtt
import pygame

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("manipulator")

# ---------------- MQTT / topics / broker ----------------
BROKER_ADDRESS = "192.168.11.11"
BROKER_PORT = 1883
MQTT_USER = "user"
MQTT_PASS = "user"

SUB_TOPIC = "orion/topic/manipulator/outbound"   # enkodery z ramienia (subscribe)
PUB_TOPIC = "orion/topic/manipulator/controller/inbound"    # komendy do ramienia (publish)

# ---------------- manipulator axes ----------------
AXES = [
    "rotate_turret",
    "flex_forearm",
    "flex_arm",
    "flex_gripper",
    "rotate_gripper",
    "end_effector",
]

# ---------------- gamepad mapping (default) ----------------
# Dostosuj indeksy jeśli pad u Ciebie ma inne.


GAMEPAD_MAP = {
    "rotate_gripper": (0, False),
    "flex_gripper":   (1, True),
    # rotate_turret handled manually
    "flex_arm":       (4, True),
    "flex_forearm":   (3, False),  
    "end_effector":   (5, False),
}



GAMEPAD_DEADZONE = 0.08
GAMEPAD_POLL_HZ = 30.0

# ---------------- MQTT worker ----------------
class MQTTWorker:
    def __init__(self, incoming_q, status_callback=None):
        self.incoming_q = incoming_q
        self.status_callback = status_callback
        self.client = mqtt.Client()
        self.client.username_pw_set(MQTT_USER, MQTT_PASS)
        self.client.on_connect = self._on_connect
        self.client.on_message = self._on_message
        self.client.on_disconnect = self._on_disconnect
        self._connected = False

    def start(self, host=BROKER_ADDRESS, port=BROKER_PORT):
        try:
            self.client.connect(host, port)
            self.client.loop_start()
        except Exception as e:
            logger.exception("MQTT connect error")
            if self.status_callback:
                self.status_callback(False, f"connect error: {e}")

    def stop(self):
        try:
            self.client.loop_stop()
            self.client.disconnect()
        except Exception:
            pass

    def _on_connect(self, client, userdata, flags, rc):
        ok = (rc == 0)
        self._connected = ok
        if self.status_callback:
            self.status_callback(ok, f"rc={rc}")
        logger.info("MQTT connected (rc=%s)", rc)
        if ok:
            client.subscribe(SUB_TOPIC)
            logger.info("Subscribed to %s", SUB_TOPIC)

    def _on_disconnect(self, client, userdata, rc):
        self._connected = False
        if self.status_callback:
            self.status_callback(False, f"disconnected rc={rc}")
        logger.warning("MQTT disconnected (rc=%s)", rc)

    def _on_message(self, client, userdata, msg):
        try:
            payload_str = msg.payload.decode()
            message = json.loads(payload_str)
            self.incoming_q.put({"topic": msg.topic, "message": message})
        except Exception as e:
            logger.warning("Failed to parse MQTT message: %s", e)

    def publish_control(self, payload_dict):
        """
        Publish a control payload_dict where payload_dict is {axis: value, ...}
        """
        message = {"eventType": "control", "payload": payload_dict}
        try:
            self.client.publish(PUB_TOPIC, json.dumps(message))
            logger.debug("Published %s -> %s", PUB_TOPIC, message)
        except Exception:
            logger.exception("MQTT publish failed")


# ---------------- Tkinter GUI ----------------
class ManipulatorApp(tk.Tk):
     
    
    def send_zeros(self):
        payload = {axis: 0 for axis in AXES}
        self.mqtt.publish_control(payload)
        for axis in AXES:
            self.preview_values[axis] = 0
            self.last_sent[axis] = 0
            self.update_preview_row(axis)
            self.update_status_row(axis)

    def __init__(self, mqtt_worker, gamepad_preview_q):
        super().__init__()
        self.title("Manipulator Control — Preview + MQTT + Gamepad (fixed)")
        self.geometry("1000x600")

        self.mqtt = mqtt_worker
        self.gamepad_preview_q = gamepad_preview_q
        self.incoming_q = mqtt_worker.incoming_q

        # state
        self.encoders = {axis: 0 for axis in AXES}
        self.last_sent = {axis: 0 for axis in AXES}
        self.preview_values = {axis: 0 for axis in AXES}  # controller preview

        self.create_widgets()
        self.after(100, self.process_mqtt_incoming)
        self.after(int(1000 / GAMEPAD_POLL_HZ), self.process_gamepad_preview)

    def create_widgets(self):
        main = ttk.Frame(self)
        main.pack(fill=tk.BOTH, expand=True, padx=8, pady=8)

        # Top: Controller input preview table + send buttons
        top_frame = ttk.LabelFrame(main, text="Controller Input (Preview)")
        top_frame.pack(side=tk.TOP, fill=tk.BOTH, expand=False, padx=6, pady=6)

        cols = ("Axis", "Preview")
        self.preview_tree = ttk.Treeview(top_frame, columns=cols, show='headings', height=len(AXES))
        for c in cols:
            self.preview_tree.heading(c, text=c)
        self.preview_tree.column("Axis", width=220, anchor='w')
        self.preview_tree.column("Preview", width=120, anchor='center')
        for axis in AXES:
            self.preview_tree.insert('', 'end', iid=axis, values=(axis, 0))
        self.preview_tree.pack(side=tk.LEFT, fill=tk.BOTH, expand=True, padx=(6,0), pady=6)

        preview_buttons = ttk.Frame(top_frame)
        preview_buttons.pack(side=tk.RIGHT, fill=tk.Y, padx=6, pady=6)

        btn_reset = ttk.Button(preview_buttons, text="Reset All to 0", command=self.send_zeros)
        btn_reset.pack(fill=tk.X, pady=(6, 6))


        # Middle / bottom: Arm status table (Encoder + Last Sent)
        bottom_frame = ttk.LabelFrame(main, text="Arm Status / Last Sent")
        bottom_frame.pack(side=tk.TOP, fill=tk.BOTH, expand=True, padx=6, pady=6)

        cols = ("Axis", "Encoder", "Last Sent")
        self.status_tree = ttk.Treeview(bottom_frame, columns=cols, show='headings', height=len(AXES))
        for c in cols:
            self.status_tree.heading(c, text=c)
        self.status_tree.column("Axis", width=220, anchor='w')
        self.status_tree.column("Encoder", width=120, anchor='center')
        self.status_tree.column("Last Sent", width=120, anchor='center')
        for axis in AXES:
            self.status_tree.insert('', 'end', iid=axis, values=(axis, self.encoders[axis], self.last_sent[axis]))
        self.status_tree.pack(fill=tk.BOTH, expand=True, padx=6, pady=6)

        # Bottom controls
        controls = ttk.Frame(main)
        controls.pack(side=tk.TOP, fill=tk.X, padx=6, pady=(0,6))

        self.conn_status = ttk.Label(controls, text=f"Connecting to MQTT... (sub {SUB_TOPIC} / pub {PUB_TOPIC})")
        self.conn_status.pack(side=tk.LEFT, padx=(0, 12))

        btn_refresh_preview = ttk.Button(controls, text="Refresh Preview", command=self.refresh_preview_table)
        btn_refresh_preview.pack(side=tk.LEFT, padx=6)

        btn_clear_last = ttk.Button(controls, text="Clear Last Sent", command=self.clear_last_sent)
        btn_clear_last.pack(side=tk.LEFT, padx=6)

    # ---------- MQTT incoming processing ----------
    def process_mqtt_incoming(self):
        try:
            while True:
                item = self.incoming_q.get_nowait()
                if 'error' in item:
                    continue
                message = item.get('message', {})
                payload = message.get('payload', {})
                # update encoders and table
                for k, v in payload.items():
                    if k in AXES:
                        self.encoders[k] = v
                        self.update_status_row(k)
        except queue.Empty:
            pass
        finally:
            self.after(100, self.process_mqtt_incoming)

    # ---------- Gamepad preview processing ----------
    def process_gamepad_preview(self):
        """
        Read preview updates produced by gamepad thread and update preview table.
        Gamepad thread puts dicts: {axis: value} or individual {'axis':..., 'value':...}
        """
        try:
            while True:
                item = self.gamepad_preview_q.get_nowait()
                # accept either {'axis':..., 'value':...} or {axis: value, ...}
                if isinstance(item, dict) and 'axis' in item and 'value' in item:
                    a = item['axis']
                    v = int(item['value'])
                    if a in AXES:
                        v_int = int(v)
                        if self.preview_values[a] != v_int:
                            self.preview_values[a] = v_int
                            self.update_preview_row(a)
                            self.mqtt.publish_control({a: v_int})
                            self.last_sent[a] = v_int
                            self.update_status_row(a)

                elif isinstance(item, dict):
                    # bulk dict
                    for a, v in item.items():
                        if a in AXES:
                            v_int = int(v)
                            if self.preview_values[a] != v_int:
                                self.preview_values[a] = v_int
                                self.update_preview_row(a)
                                self.mqtt.publish_control({a: v_int})
                                self.last_sent[a] = v_int
                                self.update_status_row(a)

        except queue.Empty:
            pass
        finally:
            self.after(int(1000 / GAMEPAD_POLL_HZ), self.process_gamepad_preview)

    def update_preview_row(self, axis):
        self.preview_tree.item(axis, values=(axis, self.preview_values.get(axis, 0)))

    def update_status_row(self, axis):
        self.status_tree.item(axis, values=(axis, self.encoders.get(axis, 0), self.last_sent.get(axis, 0)))

    # ---------- Send handlers ----------
    def send_selected_from_preview(self):
        sel = self.preview_tree.selection()
        if not sel:
            return
        payload = {}
        for iid in sel:
            axis = iid
            payload[axis] = int(self.preview_values.get(axis, 0))
        # publish
        self.mqtt.publish_control(payload)
        # update last_sent
        for axis, val in payload.items():
            self.last_sent[axis] = val
            self.update_status_row(axis)

    def send_all_from_preview(self):
        payload = {axis: int(v) for axis, v in self.preview_values.items()}
        self.mqtt.publish_control(payload)
        for axis, val in payload.items():
            self.last_sent[axis] = val
            self.update_status_row(axis)

    def refresh_preview_table(self):
        for axis in AXES:
            self.update_preview_row(axis)

    def clear_last_sent(self):
        for axis in AXES:
            self.last_sent[axis] = 0
            self.update_status_row(axis)


# ---------------- Gamepad listener (pygame) - improved (no quit/init each loop) ----------------
def start_gamepad_listener(gamepad_preview_q, poll_hz=GAMEPAD_POLL_HZ, map_config=GAMEPAD_MAP, deadzone=GAMEPAD_DEADZONE):
    """
    Improved listener:
    - initializes pygame and joystick objects once,
    - keeps Joystick objects alive (no repeated quit/init),
    - supports hot-plug by scanning counts and creating/destroying objects when count changes,
    - calls pygame.event.pump() before reads,
    - pushes per-axis preview {'axis':..., 'value':...} to GUI queue.
    """
    log = logging.getLogger("gamepad")

    def normalize_axis(val, invert=False):
        if invert:
            val = -val
        if abs(val) < deadzone:
            return 0.0
        if val > 1.0: val = 1.0
        if val < -1.0: val = -1.0
        return val

    def thread_fn():
        try:
            pygame.init()
            pygame.joystick.init()
            log.info("Gamepad listener started (pygame init). Poll hz=%.1f", poll_hz)
            joysticks = {}  # idx -> Joystick object
            poll_interval = 1.0 / max(1.0, poll_hz)

            # initial scan
            count = pygame.joystick.get_count()
            for idx in range(count):
                try:
                    js = pygame.joystick.Joystick(idx)
                    js.init()
                    joysticks[idx] = js
                    log.info("Initial joystick %d: %s (axes=%d buttons=%d hats=%d)", idx, js.get_name(), js.get_numaxes(), js.get_numbuttons(), js.get_numhats())
                except Exception:
                    log.exception("Failed to init joystick %d on initial scan", idx)

            while True:
                # detect changes in joystick count (hot-plug)
                try:
                    new_count = pygame.joystick.get_count()
                    if new_count != len(joysticks):
                        # add new ones
                        for idx in range(new_count):
                            if idx not in joysticks:
                                try:
                                    js = pygame.joystick.Joystick(idx)
                                    js.init()
                                    joysticks[idx] = js
                                    log.info("Joystick connected %d: %s (axes=%d buttons=%d hats=%d)", idx, js.get_name(), js.get_numaxes(), js.get_numbuttons(), js.get_numhats())
                                except Exception:
                                    log.exception("Failed to init newly connected joystick %d", idx)
                        # remove missing ones
                        to_remove = [i for i in list(joysticks.keys()) if i >= new_count]
                        for r in to_remove:
                            try:
                                joysticks[r].quit()
                            except Exception:
                                pass
                            joysticks.pop(r, None)
                            log.info("Joystick %d disconnected (removed)", r)
                    # For each joystick, read values
                    for idx, js in list(joysticks.items()):
                        try:
                            # ensure events processed
                            pygame.event.pump()
                            axes_raw = [round(js.get_axis(a), 4) for a in range(js.get_numaxes())]
                            buttons_raw = [js.get_button(b) for b in range(js.get_numbuttons())]
                            hats_raw = [js.get_hat(h) for h in range(js.get_numhats())]

                            # debug
                            log.debug("RAW %s idx=%d axes=%s buttons=%s hats=%s", js.get_name(), idx, axes_raw, buttons_raw, hats_raw)

                            # build and push per-axis preview
                            for manip_axis, (ctrl_axis_idx, invert_flag) in map_config.items():
                                # specjalna obsługa triggerów dla rotate_turret
                                if manip_axis == "rotate_turret":
                                    if len(axes_raw) > 5:
                                        lt = axes_raw[2]  # Left Trigger
                                        rt = axes_raw[5]  # Right Trigger
                                        # zakładamy zakres 0.0–1.0 dla triggerów
                                        valf = rt - lt  # RT dodatnie, LT ujemne
                                        val100 = int(round(normalize_axis(valf, False) * 100))
                                        gamepad_preview_q.put({"axis": "rotate_turret", "value": val100})
                                    continue  # pominąć normalne mapowanie

                                valf = None
                                if isinstance(ctrl_axis_idx, int) and 0 <= ctrl_axis_idx < len(axes_raw):
                                    valf = normalize_axis(axes_raw[ctrl_axis_idx], invert_flag)
                                else:
                                    # fallback to hat if available
                                    if len(hats_raw) > 0:
                                        hx, hy = hats_raw[0]
                                        if manip_axis.endswith('_gripper') and 'rotate' in manip_axis:
                                            valf = float(hx)
                                        elif 'flex' in manip_axis or 'arm' in manip_axis:
                                            valf = float(hy)
                                        else:
                                            valf = 0.0
                                val100 = int(round(valf * 100)) if valf is not None else 0
                                # push to GUI queue
                                gamepad_preview_q.put({"axis": manip_axis, "value": val100})
                        except Exception:
                            log.exception("Error reading joystick %d", idx)

                except Exception:
                    log.exception("Error in gamepad scan loop")

                time.sleep(poll_interval)

        except Exception:
            log.exception("Gamepad listener crashed")

    t = threading.Thread(target=thread_fn, daemon=True, name="GamepadListener")
    t.start()
    return t


# ---------------- Main ----------------
def main():
    incoming_q = queue.Queue()
    gamepad_preview_q = queue.Queue()

    mqtt_worker = MQTTWorker(incoming_q, status_callback=lambda ok, info: logger.info("MQTT status: %s %s", ok, info))
    mqtt_worker.start()

    # start gamepad listener
    start_gamepad_listener(gamepad_preview_q)

    # start GUI
    app = ManipulatorApp(mqtt_worker, gamepad_preview_q)
    try:
        app.mainloop()
    except KeyboardInterrupt:
        logger.info("Exiting on KeyboardInterrupt")
    finally:
        mqtt_worker.stop()


if __name__ == "__main__":
    main()
