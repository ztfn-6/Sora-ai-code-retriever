#!/usr/bin/env python3
"""
multi_grab_sora_codes_mt.py

- Creates/uses many user_ids (cached in user_ids.json).
- Registers missing user_ids using a ThreadPool (50 threads).
- Starts many Socket.IO clients; emitter loops run in a ThreadPool (50 threads).
- Each client respects its own server-provided rate limit.
- Prints unique CODE FOUND messages (green), saves codes to codes.txt.
"""
import os
import time
import json
import threading
import requests
import socketio
import sys
import re
from typing import Optional, List
from concurrent.futures import ThreadPoolExecutor, as_completed

# ---------- CONFIG ----------
SERVER_BASE = "https://server.escaping.work"
REGISTER_EP = SERVER_BASE + "/register"
USER_IDS_FILE = "user_ids.json"
CODES_FILE = "codes.txt"

DEFAULT_EMIT_INTERVAL = 1.0   # base per-client interval
STOP_ON_FIRST = True
COPY_TO_CLIPBOARD = False

# thread counts requested
USERID_CREATION_THREADS = 50
CLIENT_THREADPOOL_WORKERS = 50

CLIENT_CONNECT_STAGGER = 0.01  # small stagger between connect calls (seconds)

# ---------- GLOBALS ----------
seen_codes = set()
seen_lock = threading.Lock()

clients: List["ClientWrapper"] = []
clients_lock = threading.Lock()

stop_all = threading.Event()
found_event = threading.Event()

# ---------- UTILITIES ----------
def save_codes_to_file(code: str):
    try:
        with open(CODES_FILE, "a") as f:
            f.write(code + "\n")
    except Exception as e:
        print("[!] Failed to save code:", e)

def save_user_ids_list(uids: List[str]):
    try:
        with open(USER_IDS_FILE, "w") as f:
            json.dump(uids, f)
    except Exception as e:
        print("[!] Failed to save user ids:", e)

def load_user_ids_list() -> List[str]:
    if os.path.exists(USER_IDS_FILE):
        try:
            with open(USER_IDS_FILE, "r") as f:
                return json.load(f)
        except Exception:
            return []
    return []

def try_copy_to_clipboard(text: str):
    if not COPY_TO_CLIPBOARD:
        return
    try:
        import pyperclip
        pyperclip.copy(text)
        print("[*] Code copied to clipboard.")
    except Exception as e:
        print("[!] Failed to copy to clipboard:", e)

# ---------- Client wrapper ----------
class ClientWrapper:
    def __init__(self, user_id: str, idx: int, base_interval: float):
        self.user_id = user_id
        self.idx = idx
        self.base_interval = base_interval
        self.sio = socketio.Client(reconnection=True, logger=False, engineio_logger=False)
        self.next_emit_time = 0.0
        self.connected = False
        self._lock = threading.Lock()
        self.last_server_message = None
        self._install_handlers()

    def _install_handlers(self):
        @self.sio.event
        def connect():
            self.connected = True
            print(f"[client {self.idx}] connected")

        @self.sio.event
        def disconnect():
            self.connected = False
            print(f"[client {self.idx}] disconnected")

        @self.sio.on('codeResponse')
        def _on_code_response(data):
            try:
                if isinstance(data, dict) and data.get("success"):
                    code = data.get("code")
                    if not code:
                        return
                    with seen_lock:
                        if code in seen_codes:
                            return
                        seen_codes.add(code)
                    save_codes_to_file(code)
                    try_copy_to_clipboard(code)
                    print(f"\n\033[92mCODE FOUND: {code}\033[0m\n")
                    found_event.set()
                    if STOP_ON_FIRST:
                        stop_all.set()
                else:
                    msg = data.get("message") if isinstance(data, dict) else None
                    if msg:
                        # extract wait seconds if any
                        m = re.search(r'(\d+)\s*seconds?', msg, flags=re.IGNORECASE)
                        if m:
                            s = int(m.group(1))
                            with self._lock:
                                self.next_emit_time = time.time() + s
                        # store last server message for debugging if needed
                        self.last_server_message = msg
            except Exception as e:
                print(f"[client {self.idx}] error processing codeResponse:", e)

        @self.sio.on('sora_error')
        def _on_sora_error(data):
            print(f"[client {self.idx}] sora_error: {data}")

        # minimal handlers for other events (no spam)
        @self.sio.on('inviteCount')
        def _on_invite_count(data):
            pass

        @self.sio.on('userCount')
        def _on_user_count(data):
            pass

    def connect(self):
        try:
            # non-blocking connect
            self.sio.connect(SERVER_BASE, auth={"user_id": self.user_id}, wait=False, transports=["websocket"])
        except Exception as e:
            print(f"[client {self.idx}] connect failed:", e)

    def run_emitter(self):
        # emitter loop run inside threadpool worker
        while not stop_all.is_set():
            now = time.time()
            with self._lock:
                ne = self.next_emit_time
            if now >= ne:
                if self.sio.connected:
                    try:
                        self.sio.emit('getCode')
                    except Exception:
                        # ignore transient emit errors
                        pass
                # schedule next attempt
                with self._lock:
                    self.next_emit_time = time.time() + self.base_interval
            time.sleep(0.05)

    def disconnect(self):
        try:
            self.sio.disconnect()
        except Exception:
            pass

# ---------- Manager: create/register uids (multithreaded) ----------
def create_or_load_user_ids(desired_count: int) -> List[str]:
    existing = load_user_ids_list()
    if len(existing) >= desired_count:
        return existing[:desired_count]

    uids = existing[:]
    to_create = desired_count - len(uids)
    print(f"[manager] Need to create {to_create} new user_ids (total will be {desired_count})")

    uids_lock = threading.Lock()
    created_count = [len(uids)]

    def register_one(_):
        nonlocal created_count
        # try until success or global stop
        while not stop_all.is_set():
            try:
                r = requests.post(REGISTER_EP, timeout=15)
                if r.status_code == 200:
                    uid = r.text.strip()
                    with uids_lock:
                        uids.append(uid)
                        created_count[0] += 1
                        print(f"[manager] created user_id {created_count[0]}/{desired_count}")
                    return uid
                else:
                    # brief delay and retry
                    time.sleep(1)
            except Exception:
                time.sleep(1)
        return None

    # ThreadPool to create user ids concurrently
    with ThreadPoolExecutor(max_workers=USERID_CREATION_THREADS) as exec:
        futures = [exec.submit(register_one, i) for i in range(to_create)]
        for future in as_completed(futures):
            # if stop_all set, break
            if stop_all.is_set():
                break
            # future result not strictly required here; progress printed inside
            try:
                future.result(timeout=0.1)
            except Exception:
                pass

    save_user_ids_list(uids)
    return uids[:desired_count]

# ---------- Entry & orchestration ----------
def main():
    global STOP_ON_FIRST, COPY_TO_CLIPBOARD, DEFAULT_EMIT_INTERVAL

    # ask stop on first
    while True:
        resp = input("Do you want to stop at the 1st code found? (yes/no): ").strip().lower()
        if resp in ("yes", "y"):
            STOP_ON_FIRST = True
            break
        if resp in ("no", "n"):
            STOP_ON_FIRST = False
            break
        print("Please answer yes or no.")

    # how many user ids
    while True:
        try:
            n = int(input("How many user IDs to create/use? (e.g. 50, 100, 200): ").strip())
            if n <= 0:
                print("Enter a positive integer.")
                continue
            break
        except Exception:
            print("Please enter a valid integer.")

    # parse args
    if "--continuous" in sys.argv:
        STOP_ON_FIRST = False
    if "--interval" in sys.argv:
        try:
            idx = sys.argv.index("--interval")
            DEFAULT_EMIT_INTERVAL = float(sys.argv[idx + 1])
        except Exception:
            pass
    if "--copy" in sys.argv:
        COPY_TO_CLIPBOARD = True

    print(f"[manager] ensuring {n} user_ids (may reuse cached '{USER_IDS_FILE}')")
    user_ids = create_or_load_user_ids(n)
    if len(user_ids) < n:
        print("[manager] failed to obtain enough user ids, exiting.")
        return

    # create client wrappers
    print("[manager] creating clients...")
    for idx, uid in enumerate(user_ids):
        cw = ClientWrapper(uid, idx + 1, base_interval=DEFAULT_EMIT_INTERVAL)
        with clients_lock:
            clients.append(cw)

    # use a ThreadPoolExecutor to connect clients concurrently (staggered connect)
    print("[manager] connecting clients (thread pool)...")
    with ThreadPoolExecutor(max_workers=CLIENT_THREADPOOL_WORKERS) as exec:
        connect_futures = []
        for c in clients:
            # submit connect calls; call returns quickly because wait=False in connect
            connect_futures.append(exec.submit(c.connect))
            time.sleep(CLIENT_CONNECT_STAGGER)
        # wait briefly for connections to establish
        time.sleep(1.0)

        # now start each client's emitter loop in the same executor pool
        print("[manager] starting client emitter loops (thread pool workers)...")
        emitter_futures = []
        for c in clients:
            emitter_futures.append(exec.submit(c.run_emitter))

        print("[*] Looking for codes.. (press Ctrl+C to stop)")
        try:
            # main monitor loop
            while not stop_all.is_set():
                # if STOP_ON_FIRST and found_event is set, stop_all will be set by client
                time.sleep(0.5)
        except KeyboardInterrupt:
            print("\n[!] Interrupted by user. Stopping...")
            stop_all.set()
        finally:
            # attempt graceful disconnect
            print("[manager] disconnecting clients...")
            for c in clients:
                try:
                    c.disconnect()
                except:
                    pass
            # allow futures to finish briefly
            time.sleep(0.5)

    print("[*] Finished. Found codes saved to", CODES_FILE)

if __name__ == "__main__":
    main()
