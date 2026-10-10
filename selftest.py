#!/usr/bin/env python3
"""Standalone self-check for the Tern Session Media backend.

Runs media-store.py exactly the way Tern does: one JSON request on stdin, one
JSON response on stdout, a fresh process per call. Every case gets its own
TERN_MEDIA_STATE_DIR under a retained temp directory, printed after the checks.

    python3 selftest.py

Exits non-zero on the first failing expectation set; no third-party packages.
"""

from __future__ import annotations

import hashlib
import json
import os
import pty
import shutil
import signal
import socket
import sqlite3
import struct
import subprocess
import sys
import tempfile
import time
import zlib

PLUGIN_DIR = os.path.dirname(os.path.abspath(__file__))
BACKEND = os.path.join(PLUGIN_DIR, "media-store.py")

FAILURES: list[str] = []
CHECKS = 0
TEMP_DIRS: list[str] = []

SESSION_A = "11111111-1111-4111-8111-111111111111"
SESSION_B = "22222222-2222-4222-8222-222222222222"
SESSION_C = "33333333-3333-4333-8333-333333333333"

# Mirrored from media-store.py so the checks below stay behavioural: an item
# payload, its encoded form, and the transcript bounds.
ITEM_LIMIT = 64 * 1024 * 1024
ENCODED_LIMIT = 4 * ((ITEM_LIMIT + 2) // 3) + 4
TRANSCRIPT_LIMIT = 1024 * 1024 * 1024
LINE_LIMIT = ENCODED_LIMIT + 1024 * 1024


def check(label: str, ok: bool, detail: str = "") -> bool:
    global CHECKS
    CHECKS += 1
    if ok:
        print("PASS %s" % label)
        return True
    FAILURES.append(label)
    print("FAIL %s%s" % (label, (" :: " + detail) if detail else ""))
    return False


def env_for(root: str) -> dict:
    env = dict(os.environ)
    env["TERN_MEDIA_STATE_DIR"] = root
    return env


def fresh_state(tag: str) -> str:
    root = tempfile.mkdtemp(prefix="tern-media-%s-" % tag)
    TEMP_DIRS.append(root)
    return root


def call(request: dict, env: dict, timeout: int = 180) -> tuple[int, dict | None]:
    done = subprocess.run(
        [sys.executable, BACKEND],
        input=json.dumps(request),
        capture_output=True,
        text=True,
        env=env,
        timeout=timeout,
    )
    try:
        parsed = json.loads(done.stdout)
    except ValueError:
        parsed = None
    return done.returncode, parsed


def session(sid: str, pane: str, window: str, tern: str, label: str, cwd: str) -> dict:
    return {
        "id": sid,
        "label": label,
        "cwd": cwd,
        "host": "testhost",
        "pane_id": pane,
        "window_key": window,
        "tern_session_id": tern,
        "tern_session_name": "Tern " + tern,
    }


def make_png(rgb: tuple[int, int, int], size: int = 4) -> bytes:
    """A real, decodable PNG, so preview tooling sees an actual image."""
    red, green, blue = rgb
    raw = b"".join(b"\x00" + bytes((red, green, blue)) * size for _ in range(size))

    def chunk(tag: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + tag + data + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)

    header = struct.pack(">IIBBBBB", size, size, 8, 2, 0, 0, 0)
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", header)
        + chunk(b"IDAT", zlib.compress(raw, 9))
        + chunk(b"IEND", b"")
    )


def write_file(directory: str, name: str, payload: bytes) -> str:
    os.makedirs(directory, exist_ok=True)
    path = os.path.join(directory, name)
    with open(path, "wb") as handle:
        handle.write(payload)
    return path


def register(env: dict, s: dict, items: list[dict]) -> tuple[int, dict | None]:
    return call({"op": "register", "session": s, "items": items}, env)


def plain_errors(entries) -> bool:
    return isinstance(entries, list) and all(
        isinstance(e, dict) and isinstance(e.get("name"), str) and isinstance(e.get("error"), str) for e in entries
    )


# Discovery resolves the pane's own live conversation from the process table and
# the transcripts that process holds open. The checks below therefore run a real
# holder process and a stand-in `tern` CLI: the backend sees exactly what a host
# would report, and no check ever touches the real OMP session store.

HOLDER_CODE = (
    "import os,sys,time\n"
    "ready, trigger, extra, paths = sys.argv[1], sys.argv[2], sys.argv[3], sys.argv[4:]\n"
    "handles = [open(path, 'rb') for path in paths]\n"
    "open(ready, 'w').close()\n"
    "deadline = time.time() + 20\n"
    "while extra and not os.path.exists(trigger) and time.time() < deadline:\n"
    "    time.sleep(0.02)\n"
    "if extra:\n"
    "    handles.append(open(extra, 'rb'))\n"
    "time.sleep(120)\n"
)


def hold_open(root: str, tag: str, paths: list[str], trigger: str = "", extra: str = "") -> subprocess.Popen:
    """A child process that holds transcripts open, so the fd scan sees them.

    It returns only once the files are known to be open, so a discovery can never
    race the holder's own startup. A second transcript is opened once the trigger
    appears, which is how a conversation switch is staged while a discovery is
    already running.
    """
    ready = os.path.join(root, "holder-%s.ready" % tag)
    process = subprocess.Popen(
        [sys.executable, "-c", HOLDER_CODE, ready, trigger, extra, *paths],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    deadline = time.time() + 20
    while not os.path.exists(ready) and process.poll() is None and time.time() < deadline:
        time.sleep(0.02)
    return process


def stop_holder(process: subprocess.Popen) -> None:
    process.kill()
    process.wait(timeout=30)


def await_ready(ready: str, pid: int) -> None:
    """Wait for a forked child to signal it is set up, or for it to die."""
    deadline = time.time() + 20
    while not os.path.exists(ready) and time.time() < deadline:
        try:
            done, _ = os.waitpid(pid, os.WNOHANG)
        except ChildProcessError:
            return
        if done == pid:
            return
        time.sleep(0.02)


def stop_forked(pid: int) -> None:
    try:
        os.kill(pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    try:
        os.waitpid(pid, 0)
    except ChildProcessError:
        pass


def tty_holder(root: str, tag: str) -> tuple[int, str, int, int]:
    """A real process with its own controlling terminal and nothing open.

    It stands in for a cold-resumed OMP: alive and foreground on a fresh pty,
    but holding no transcript. Returns (pid, terminal id, master fd, time before
    fork) — the master stays open so the OS cannot hand the same tty number to another
    process mid-check. The terminal id is OMP's own: /dev/ stripped, slashes
    hyphenated, exactly what names the breadcrumb file.
    """
    master, slave = pty.openpty()
    slave_name = os.ttyname(slave)
    ready = os.path.join(root, "tty-%s.ready" % tag)
    before_fork_ns = time.time_ns()
    pid = os.fork()
    if pid == 0:
        try:
            os.close(master)
            os.login_tty(slave)
            os.execv(sys.executable, [sys.executable, "-c", HOLDER_CODE, ready, "", ""])
        finally:
            os._exit(1)
    os.close(slave)
    await_ready(ready, pid)
    terminal_id = slave_name[len("/dev/") :].replace("/", "-") if slave_name.startswith("/dev/") else slave_name
    return pid, terminal_id, master, before_fork_ns


def daemon_holder(root: str, tag: str) -> int:
    """A live process with no controlling terminal: ps answers ?? for it, so
    no terminal breadcrumb can ever belong to it."""
    ready = os.path.join(root, "daemon-%s.ready" % tag)
    pid = os.fork()
    if pid == 0:
        try:
            os.setsid()
            devnull = os.open(os.devnull, os.O_RDWR)
            os.dup2(devnull, 0)
            os.dup2(devnull, 1)
            os.dup2(devnull, 2)
            os.execv(sys.executable, [sys.executable, "-c", HOLDER_CODE, ready, "", ""])
        finally:
            os._exit(1)
    await_ready(ready, pid)
    return pid


def stamp_breadcrumb(home: str, terminal_id: str, cwd: str, target: str) -> str:
    """OMP's own terminal breadcrumb: cwd, then the session file, one per line."""
    directory = os.path.join(home, ".omp", "agent", "terminal-sessions")
    os.makedirs(directory, exist_ok=True)
    path = os.path.join(directory, terminal_id)
    with open(path, "w", encoding="utf-8") as handle:
        handle.write("%s\n%s\n" % (cwd, target))
    return path


def fake_tern(
    directory: str, payloads: list[dict], switch_second: bool = False, sleep_second: str = "", second_script: str = ""
) -> str:
    """A `tern` stand-in that reports one foreground process per invocation.

    Only the shape that matters is reproduced: the pane's foreground process
    with its pid, name, argv and cwd. switch_second/sleep_second let the second
    invocation — the stability re-check — declare that the process changed while
    its transcript was being read, and second_script runs on that same
    invocation, which is how a breadcrumb rewrite mid-resolution is staged.
    """
    state = os.path.join(directory, "state")
    os.makedirs(state, exist_ok=True)
    for index, payload in enumerate(payloads):
        write_file(state, "payload-%d.json" % index, json.dumps(payload).encode("utf-8"))
    if switch_second:
        write_file(state, "switch-second", b"")
    if sleep_second:
        write_file(state, "sleep-second", sleep_second.encode("ascii"))
    if second_script:
        write_file(state, "second-script", second_script.encode("utf-8"))
    script = os.path.join(directory, "bin", "tern")
    os.makedirs(os.path.dirname(script), exist_ok=True)
    last = len(payloads) - 1
    with open(script, "w", encoding="utf-8") as handle:
        handle.write(
            "#!/bin/sh\n"
            'state="%s"\n'
            'calls=$(cat "$state/calls" 2>/dev/null || echo 0)\n'
            'echo $((calls + 1)) > "$state/calls"\n'
            'if [ "$calls" -ge %d ]; then calls=%d; fi\n'
            'if [ "$calls" = "%d" ]; then\n'
            '  [ -f "$state/switch-second" ] && : > "$state/switch"\n'
            '  [ -f "$state/sleep-second" ] && sleep "$(cat "$state/sleep-second")"\n'
            '  [ -f "$state/second-script" ] && sh "$state/second-script"\n'
            "fi\n"
            'cat "$state/payload-$calls.json"\n' % (state, last, last, last)
        )
    os.chmod(script, 0o755)
    return os.path.join(directory, "bin")


def discover_env(root: str, home: str, bin_dir: str) -> dict:
    """A discovery run's env: its own state, a temp OMP store and a stand-in CLI."""
    env = env_for(root)
    env["HOME"] = home
    env["PATH"] = bin_dir + os.pathsep + env.get("PATH", "")
    return env


def omp_transcript(directory: str, stamp: str, sid: str, cwd: str, records: list[dict], parent: str = "") -> str:
    """Write a session transcript: a title preamble, a session header, then records."""
    os.makedirs(directory, exist_ok=True)
    header = {"type": "session", "id": sid, "cwd": cwd, "title": "Chat " + sid[:4]}
    if parent:
        header["parentSession"] = parent
    path = os.path.join(directory, "%s_%s.jsonl" % (stamp, sid))
    with open(path, "w", encoding="utf-8") as handle:
        for record in [{"type": "title", "title": header["title"]}, header, *records]:
            handle.write(json.dumps(record) + "\n")
    return path


# ---------------------------------------------------------------------------


def case_basic(root: str) -> None:
    env = env_for(root)
    s = session(SESSION_A, "pane-a", "win-a", "tern-a", "Alpha", root)
    code, res = call({"op": "touch", "session": s}, env)
    check("touch binds and echoes the session", code == 0 and bool(res) and res.get("ok") and res["session"]["id"] == SESSION_A, str(res))

    payload = make_png((10, 20, 30))
    shot = write_file(root, "shot.png", payload)
    code, res = register(env, s, [{"path": shot, "source": "created"}])
    ok = check("register stores a real file", code == 0 and bool(res) and res.get("ok") and len(res["items"]) == 1 and res["errors"] == [], str(res))
    if not ok:
        return
    item = res["items"][0]
    check(
        "item matches the contract shape",
        set(item) == {
            "id", "session_id", "name", "path", "stored_path", "preview_path", "sha256",
            "mime", "kind", "source", "recorded_at", "tags", "version", "session_label",
        },
        str(sorted(item)),
    )
    check(
        "image classified with hash and version 1",
        item["kind"] == "image" and item["mime"] == "image/png" and item["version"] == 1
        and item["sha256"] == hashlib.sha256(payload).hexdigest(),
        str(item),
    )
    check(
        "retained original keeps the exact bytes",
        os.path.isfile(item["stored_path"]) and open(item["stored_path"], "rb").read() == payload,
        item["stored_path"],
    )
    check("preview is cached or absent", item["preview_path"] is None or os.path.isfile(item["preview_path"]), str(item["preview_path"]))
    check("the retained original carries a mime-appropriate suffix", item["stored_path"].endswith(".png"), item["stored_path"])

    # Every kind must land as a typed file, or a native viewer sees an
    # extensionless hash and treats the bytes as text.
    typed_kinds = (
        ("clip.mp3", b"ID3\x03\x00\x00\x00\x00\x00\x00", "audio/mpeg", "audio", ".mp3"),
        ("clip.mp4", b"\x00\x00\x00\x18ftypmp42\x00\x00\x00\x00mp42isom", "video/mp4", "video", ".mp4"),
        ("doc.pdf", b"%PDF-1.4\n1 0 obj\n<<>>\nendobj\ntrailer\n%%EOF\n", "application/pdf", "document", ".pdf"),
    )
    for name, blob, expect_mime, expect_kind, expect_suffix in typed_kinds:
        _, typed = register(env, s, [{"path": write_file(root, name, blob), "source": "viewed"}])
        entries = typed.get("items") or []
        check(
            "%s is retained as %s" % (expect_kind, expect_suffix),
            bool(entries)
            and entries[0]["kind"] == expect_kind
            and entries[0]["mime"] == expect_mime
            and entries[0]["stored_path"].endswith(expect_suffix)
            and open(entries[0]["stored_path"], "rb").read() == blob,
            str(typed),
        )
    check("session label is joined in", item["session_label"] == "Alpha", str(item["session_label"]))
    check("source recorded", item["source"] == "created" and item["path"] == os.path.realpath(shot), str(item["source"]))

    # A relative path resolves against the conversation's cwd, never the
    # process cwd (the gallery backend runs with the plugin directory).
    os.makedirs(os.path.join(root, "sub"), exist_ok=True)
    nested = write_file(os.path.join(root, "sub"), "rel.png", make_png((11, 12, 13)))
    _, rel = register(env, s, [{"path": os.path.join("sub", "rel.png"), "source": "viewed"}])
    check(
        "relative paths resolve against the session cwd",
        bool(rel.get("items")) and rel["items"][0]["path"] == os.path.realpath(nested),
        str(rel),
    )


def case_overwrite(root: str) -> None:
    env = env_for(root)
    s = session(SESSION_B, "pane-b", "win-b", "tern-b", "Beta", root)
    first_bytes = make_png((1, 2, 3))
    path = write_file(root, "repeat.png", first_bytes)
    _, first = register(env, s, [{"path": path, "source": "viewed"}])
    original = first["items"][0]

    second_bytes = make_png((200, 100, 50))
    write_file(root, "repeat.png", second_bytes)
    _, second = register(env, s, [{"path": path, "source": "viewed"}])
    overwritten = second["items"][0]
    check(
        "changed bytes at the same path add a version",
        overwritten["id"] != original["id"] and overwritten["version"] == 2,
        str(overwritten),
    )
    check(
        "the older version keeps its own bytes",
        os.path.isfile(original["stored_path"])
        and open(original["stored_path"], "rb").read() == first_bytes
        and os.path.isfile(overwritten["stored_path"])
        and open(overwritten["stored_path"], "rb").read() == second_bytes,
        "%s vs %s" % (original["stored_path"], overwritten["stored_path"]),
    )

    _, third = register(env, s, [{"path": path, "source": "viewed"}])
    check("a repeated identical event dedupes", third["items"][0]["id"] == overwritten["id"], str(third["items"][0]))
    _, listing = call({"op": "list", "session_id": SESSION_B, "scope": "conversation"}, env)
    check("no duplicate rows accumulate", listing["total"] == 2, str(listing["total"]))

    third_bytes = make_png((60, 70, 80))
    write_file(root, "repeat.png", third_bytes)
    _, fourth = register(env, s, [{"path": path, "source": "viewed"}])
    newest = fourth["items"][0]
    check(
        "versions increase monotonically under repeated rewrites",
        newest["version"] == 3 and newest["id"] not in (original["id"], overwritten["id"]),
        str(newest),
    )
    check(
        "every retained version keeps its own bytes",
        all(
            os.path.isfile(entry["stored_path"]) and open(entry["stored_path"], "rb").read() == payload
            for entry, payload in ((original, first_bytes), (overwritten, second_bytes), (newest, third_bytes))
        ),
        str([original["stored_path"], overwritten["stored_path"], newest["stored_path"]]),
    )


def case_conversation_scope(root: str) -> None:
    env = env_for(root)
    a = session(SESSION_A, "pane-a", "win-a", "tern-a", "Alpha", root)
    b = session(SESSION_B, "pane-b", "win-b", "tern-b", "Beta", root)
    _, ra = register(env, a, [{"path": write_file(root, "a.png", make_png((1, 1, 1))), "source": "viewed"}])
    _, rb = register(env, b, [{"path": write_file(root, "b.png", make_png((2, 2, 2))), "source": "viewed"}])
    ids = (ra["items"][0]["id"], rb["items"][0]["id"])

    _, la = call({"op": "list", "session_id": SESSION_A, "scope": "conversation"}, env)
    _, lb = call({"op": "list", "session_id": SESSION_B, "scope": "conversation"}, env)
    check(
        "conversation scope never bleeds across sessions",
        [i["id"] for i in la["items"]] == [ids[0]] and [i["id"] for i in lb["items"]] == [ids[1]],
        "%s / %s" % ([i["id"] for i in la["items"]], [i["id"] for i in lb["items"]]),
    )
    check("list reports totals and sessions", la["total"] == 1 and {s["id"] for s in la["sessions"]} == {SESSION_A, SESSION_B}, str(la["sessions"]))

    _, by_pane = call({"op": "list", "pane_id": "pane-a", "window_key": "win-a"}, env)
    check(
        "most recent pane binding resolves the conversation",
        by_pane["session"] is not None and by_pane["session"]["id"] == SESSION_A and [i["id"] for i in by_pane["items"]] == [ids[0]],
        str(by_pane["session"]),
    )
    _, blank_key = call({"op": "list", "pane_id": "pane-a", "window_key": ""}, env)
    check("a blank window key still isolates by pane", bool(blank_key.get("session")) and blank_key["session"]["id"] == SESSION_A, str(blank_key.get("session")))


def case_workspace_and_child_binding(root: str) -> None:
    env = env_for(root)
    a = session(SESSION_A, "pane-a", "win-a", "tern-a", "Alpha", root)
    c = session(SESSION_C, "pane-c", "win-c", "tern-c", "Gamma", root)
    register(env, a, [{"path": write_file(root, "wa.png", make_png((3, 3, 3))), "source": "viewed"}])
    register(env, c, [{"path": write_file(root, "wc.png", make_png((4, 4, 4))), "source": "viewed"}])

    _, ws = call({"op": "list", "pane_id": "pane-a", "window_key": "win-a", "scope": "workspace"}, env)
    check(
        "workspace scope follows the binding's tern_session_id",
        ws["total"] == 1 and all(i["session_id"] == SESSION_A for i in ws["items"]),
        str([(i["session_id"], i["id"]) for i in ws["items"]]),
    )
    _, orphan = call({"op": "list", "pane_id": "pane-missing", "scope": "workspace"}, env)
    check("workspace scope does not fall back to every item", orphan["total"] == 0 and orphan["items"] == [], str(orphan["total"]))

    # A child OMP session reports {id, host} only and must not rebind the pane.
    _, child = call({"op": "register", "session": {"id": SESSION_A, "host": "testhost"}, "items": [{"data": _b64(make_png((7, 7, 7))), "mime": "image/png", "name": "child.png", "source": "viewed"}]}, env)
    check("child-style session still stores under the parent", bool(child) and child.get("ok") and len(child["items"]) == 1, str(child))
    _, after = call({"op": "list", "pane_id": "pane-a", "scope": "workspace"}, env)
    check(
        "child upsert preserves pane, window and workspace binding",
        after["session"] is not None
        and after["session"]["pane_id"] == "pane-a"
        and after["session"]["window_key"] == "win-a"
        and after["session"]["tern_session_id"] == "tern-a"
        and after["session"]["label"] == "Alpha",
        str(after["session"]),
    )


def case_tags_and_search(root: str) -> None:
    env = env_for(root)
    s = session(SESSION_A, "pane-a", "win-a", "tern-a", "Alpha", root)
    _, res = register(env, s, [{"path": write_file(root, "tagged.png", make_png((5, 5, 5))), "source": "viewed"}])
    item_id = res["items"][0]["id"]

    code, tagged = call({"op": "tag", "id": item_id, "tags": ["zebra", "alpha"]}, env)
    check("tag replaces user tags", code == 0 and bool(tagged) and tagged.get("ok") and tagged["item"]["tags"] == ["zebra", "alpha"], str(tagged))

    _, found = call({"op": "list", "session_id": SESSION_A, "scope": "conversation", "search": "zebra"}, env)
    _, missing = call({"op": "list", "session_id": SESSION_A, "scope": "conversation", "search": "nonexistent-tag-xyz"}, env)
    check("search finds tagged items", found["total"] == 1 and found["items"][0]["id"] == item_id, str(found["total"]))
    check("search excludes unrelated items", missing["total"] == 0 and missing["items"] == [], str(missing["total"]))


def case_restart_persistence(root: str) -> None:
    env = env_for(root)
    s = session(SESSION_A, "pane-a", "win-a", "tern-a", "Alpha", root)
    _, res = register(env, s, [{"path": write_file(root, "persist.png", make_png((6, 6, 6))), "source": "viewed"}])
    item_id = res["items"][0]["id"]

    # A brand new process against the same state has no memory except the disk.
    _, again = call({"op": "list", "scope": "all", "limit": 40}, env)
    check("a later process still sees the item", any(i["id"] == item_id for i in again["items"]), str(again["total"]))
    _, sessions = call({"op": "list", "session_id": SESSION_A, "scope": "conversation"}, env)
    check("sessions persist across processes", any(s["id"] == SESSION_A for s in sessions["sessions"]), str(sessions["sessions"]))
    _, watch = call({"op": "watch", "session_id": SESSION_A, "path": root}, env)
    _, with_watch = call({"op": "list", "pane_id": "pane-a", "window_key": "win-a"}, env)
    check("watch persists and is reported", watch.get("ok") and root in with_watch["watch_roots"], str(with_watch["watch_roots"]))


def case_rejections(root: str) -> None:
    env = env_for(root)
    s = session(SESSION_A, "pane-a", "win-a", "tern-a", "Alpha", root)
    good1 = write_file(root, "good1.png", make_png((1, 1, 1)))
    good2 = write_file(root, "good2.png", make_png((2, 2, 2)))
    text = write_file(root, "clip.txt", b"not media\n")
    link = os.path.join(root, "link.png")
    os.symlink(good1, link)
    directory = os.path.join(root, "dir.png")
    os.makedirs(directory, exist_ok=True)
    oversize = os.path.join(root, "big.png")
    with open(oversize, "wb") as handle:
        handle.truncate(65 * 1024 * 1024)
    empty = write_file(root, "empty.png", b"")

    for label, path in (
        ("unsupported extension", text),
        ("symlink", link),
        ("nonregular path", directory),
        ("oversize file", oversize),
        ("empty file", empty),
    ):
        code, res = register(env, s, [{"path": path, "source": "viewed"}])
        check(
            "rejects %s with a per-item error" % label,
            code == 0 and bool(res) and res.get("ok") and res["items"] == [] and len(res["errors"]) == 1 and plain_errors(res["errors"]),
            str(res),
        )

    _, mixed = register(env, s, [{"path": good1, "source": "viewed"}, {"path": text, "source": "viewed"}, {"path": good2, "source": "viewed"}])
    check(
        "a partially failing batch reports explicit per-item errors",
        mixed.get("ok") and len(mixed["items"]) == 2 and len(mixed["errors"]) == 1,
        str(mixed),
    )
    _, unknown = call({"op": "tag", "id": 999999, "tags": ["x"]}, env)
    check("tagging an unknown id is an explicit error", unknown.get("ok") is False and isinstance(unknown.get("error"), str), str(unknown))


def case_backfill(root: str) -> None:
    env = env_for(root)
    s = session(SESSION_A, "pane-a", "win-a", "tern-a", "Alpha", root)
    found = write_file(root, "found.png", make_png((8, 8, 8)))
    payload = make_png((9, 9, 9))
    transcript = os.path.join(root, "session.jsonl")
    lines = [
        json.dumps({"type": "message", "role": "user", "content": [{"type": "image", "data": _b64(payload), "mimeType": "image/png"}]}),
        json.dumps({"type": "message", "role": "assistant", "content": [{"type": "text", "text": "see found.png and also missing/absent.png and https://example.com/x.png"}]}),
        "{not json",
    ]
    with open(transcript, "w", encoding="utf-8") as handle:
        handle.write("\n".join(lines) + "\n")

    code, res = call({"op": "backfill", "session": s, "transcript_path": transcript}, env)
    ok = check("backfill answers with the register shape", code == 0 and bool(res) and res.get("ok") and plain_errors(res["errors"]), str(res))
    if not ok:
        return
    check("backfill keeps retained image blocks", any(i["kind"] == "image" and i["sha256"] == hashlib.sha256(payload).hexdigest() for i in res["items"]), str([i["name"] for i in res["items"]]))
    check("backfill imports the clearly referenced local file", any(i["path"] == os.path.realpath(found) for i in res["items"]), str([i["path"] for i in res["items"]]))
    check("no transcript text becomes an item name", all("see" not in i["name"] for i in res["items"]), str([i["name"] for i in res["items"]]))

    _, repeat = call({"op": "backfill", "session": s, "transcript_path": transcript}, env)
    _, listing = call({"op": "list", "session_id": SESSION_A, "scope": "conversation"}, env)
    check("re-running a backfill does not duplicate rows", listing["total"] == len(res["items"]), "%s vs %s" % (listing["total"], len(res["items"])))

    # Paths with spaces and @ (an Insync Google Drive root) must survive
    # markdown angle-bracket links and backticked references.
    spaced_dir = os.path.join(root, "media @ dir")
    os.makedirs(spaced_dir, exist_ok=True)
    spaced = write_file(spaced_dir, "shot one.png", make_png((41, 42, 43)))
    text = "linked [shot](<%s>) and backticked `%s`" % (spaced, spaced)
    paths_transcript = os.path.join(root, "paths.jsonl")
    with open(paths_transcript, "w", encoding="utf-8") as handle:
        handle.write(json.dumps({"type": "message", "role": "user", "content": [{"type": "text", "text": text}]}) + "\n")
    _, spaced_res = call({"op": "backfill", "session": s, "transcript_path": paths_transcript}, env)
    hits = [i for i in spaced_res.get("items", []) if i["path"] == os.path.realpath(spaced)]
    check(
        "backfill keeps spaced and @ paths from markdown and backticks",
        len(hits) == 1 and hits[0]["name"] == "shot one.png",
        str(spaced_res),
    )


def case_discover(root: str) -> None:
    env = env_for(root)
    home = os.path.join(root, "home")
    store = os.path.join(home, ".omp", "agent", "sessions")
    work = os.path.join(root, "work")
    os.makedirs(work, exist_ok=True)

    inline = make_png((51, 52, 53))
    blob = make_png((61, 62, 63))
    blob_digest = hashlib.sha256(blob).hexdigest()
    blob_dir = os.path.join(root, "blobs", blob_digest[:2])
    os.makedirs(blob_dir, exist_ok=True)
    write_file(blob_dir, blob_digest, blob)
    referenced = write_file(work, "shot one.png", make_png((71, 72, 73)))
    relative = write_file(work, "rel.png", make_png((74, 75, 76)))
    advisor_media = write_file(work, "advisor.png", make_png((81, 82, 83)))
    child_media = write_file(work, "child.png", make_png((91, 92, 93)))
    other_media = write_file(work, "other.png", make_png((101, 102, 103)))

    def payload(process: subprocess.Popen, name: str = "18.8.7", argv: tuple = ("omp",)) -> dict:
        # The live CLI reports the command's version in `name` and the command
        # itself in argv, so both halves are exercised as the host reports them.
        return {"foreground": {"pid": process.pid, "name": name, "argv": list(argv), "cwd": work}}

    def discover(
        pane: str,
        payloads: list[dict],
        tag: str,
        second_switch: bool = False,
        second_sleep: str = "",
        **fields,
    ):
        bin_dir = fake_tern(
            os.path.join(root, "bin-" + tag), payloads, switch_second=second_switch, sleep_second=second_sleep
        )
        request = {"op": "discover", "pane_id": pane, "host": socket.gethostname(), "cwd": work, "label": "Chat"}
        request.update(fields)
        return call(request, discover_env(root, home, bin_dir))

    def message(text: str) -> dict:
        return {"type": "message", "role": "user", "content": [{"type": "text", "text": text}]}

    main = omp_transcript(
        os.path.join(store, "conv-main"),
        "2026-01-01T00-00-00-000Z",
        SESSION_A,
        work,
        [
            {"type": "message", "role": "user", "content": [
                {"type": "image", "data": _b64(inline), "mimeType": "image/png"}]},
            {"type": "message", "role": "user", "content": [
                {"type": "image", "data": "blob:sha256:" + blob_digest, "mimeType": "image/png"}]},
            message("rendered rel.png and opened `%s`" % referenced),
        ],
    )
    # The advisor transcript sits inside the conversation's own stem directory
    # and names no parent, so only the layout can exclude it.
    advisor = write_file(
        os.path.join(store, "conv-main", os.path.basename(main)[: -len(".jsonl")]),
        "__advisor.jsonl",
        (
            json.dumps({"type": "session", "id": SESSION_C, "cwd": work, "title": "Advisor"})
            + "\n"
            + json.dumps({"type": "message", "role": "user", "content": [
                {"type": "text", "text": "opened `%s`" % advisor_media}]})
            + "\n"
        ).encode("utf-8"),
    )
    child = omp_transcript(
        os.path.join(store, "conv-child"),
        "2026-01-02T00-00-00-000Z",
        SESSION_B,
        work,
        [message("opened `%s`" % child_media)],
        parent=main,
    )

    live = hold_open(root, "live", [main, advisor, child])
    try:
        code, res = discover(
            "pane-live",
            [payload(live)] * 2,
            "live",
            window_key="win-live",
            tern_session_id="tern-live",
            tern_session_name="Tern tern-live",
        )
        ok = check(
            "discover answers with a session, its items and plain errors",
            code == 0 and bool(res) and res.get("ok") is True and plain_errors(res.get("errors")),
            str(res),
        )
        if ok:
            bound = res.get("session") or {}
            check(
                "discover binds the pane to the transcript's own conversation",
                bound.get("id") == SESSION_A
                and bound.get("pane_id") == "pane-live"
                and bound.get("window_key") == "win-live"
                and bound.get("tern_session_id") == "tern-live"
                and bound.get("cwd") == work
                and bound.get("label") == "Chat",
                str(bound),
            )
            digests = {item["sha256"] for item in res["items"]}
            paths = {item["path"] for item in res["items"] if item["path"] is not None}
            check(
                "discover imports the conversation's inline and blob images",
                {hashlib.sha256(inline).hexdigest(), blob_digest} <= digests,
                str(sorted(digests)),
            )
            check(
                "discover imports the files the conversation referenced, resolved in its cwd",
                {os.path.realpath(referenced), os.path.realpath(relative)} <= paths,
                str(sorted(paths)),
            )
            check(
                "discover never imports the advisor or a child transcript",
                not {os.path.realpath(advisor_media), os.path.realpath(child_media)} & paths,
                str(sorted(paths)),
            )
            _, listed = call({"op": "list", "pane_id": "pane-live", "window_key": "win-live"}, env)
            check(
                "the pane's gallery shows the discovered media without a manual import",
                bool(listed.get("session"))
                and listed["session"]["id"] == SESSION_A
                and listed["total"] == len(res["items"]),
                str(listed.get("total")),
            )
            _, repeat = discover("pane-live", [payload(live)] * 2, "live", window_key="win-live")
            _, after = call({"op": "list", "pane_id": "pane-live", "window_key": "win-live"}, env)
            check(
                "a repeated discover dedupes instead of importing again",
                bool(repeat) and repeat.get("ok") is True and after["total"] == len(res["items"]),
                "%s vs %s" % (after["total"], len(res["items"])),
            )
    finally:
        stop_holder(live)

    # A second live conversation in the same process, sharing the cwd, must
    # never be chosen by recency: the choice is refused instead.
    other = omp_transcript(
        os.path.join(store, "conv-other"),
        "2026-01-03T00-00-00-000Z",
        SESSION_C,
        work,
        [message("opened `%s`" % other_media)],
    )
    ambiguous = hold_open(root, "amb", [main, other])
    try:
        code, res = discover("pane-amb", [payload(ambiguous)] * 2, "amb")
        _, listed = call({"op": "list", "pane_id": "pane-amb"}, env)
        check(
            "two live conversations in one process never pick one",
            code == 0
            and bool(res)
            and res.get("ok") is True
            and res.get("session") is None
            and res.get("items") == []
            and bool(res.get("errors"))
            and plain_errors(res.get("errors"))
            and listed.get("session") is None,
            str(res),
        )
    finally:
        stop_holder(ambiguous)

    # A pane running a shell instead of OMP fails closed, and the binding it
    # already has survives the attempt untouched.
    call(
        {"op": "touch", "session": session(SESSION_B, "pane-shell", "win-shell", "tern-shell", "Beta", work)},
        env,
    )
    shell = hold_open(root, "shell", [main])
    try:
        code, res = discover("pane-shell", [payload(shell, name="zsh", argv=("zsh",))] * 2, "shell")
        _, listed = call({"op": "list", "pane_id": "pane-shell"}, env)
        check(
            "a pane whose foreground is not OMP fails closed and keeps its binding",
            code == 0
            and bool(res)
            and res.get("session") is None
            and res.get("items") == []
            and bool(res.get("errors"))
            and plain_errors(res.get("errors"))
            and bool(listed.get("session"))
            and listed["session"]["id"] == SESSION_B,
            str(res),
        )
    finally:
        stop_holder(shell)

    # A transcript outside this host's OMP session store is never imported, even
    # with a valid header: discovery resolves conversations, never paths.
    outside = omp_transcript(
        os.path.join(root, "elsewhere"),
        "2026-01-04T00-00-00-000Z",
        SESSION_A,
        work,
        [message("opened `%s`" % relative)],
    )
    stray = hold_open(root, "stray", [outside])
    try:
        code, res = discover("pane-stray", [payload(stray)] * 2, "stray")
        check(
            "a transcript outside the OMP session store is never imported",
            code == 0
            and bool(res)
            and res.get("session") is None
            and res.get("items") == []
            and bool(res.get("errors"))
            and plain_errors(res.get("errors")),
            str(res),
        )
    finally:
        stop_holder(stray)

    # The conversation switching mid-read must import nothing and bind nothing.
    switched = hold_open(
        root, "switch", [other], trigger=os.path.join(root, "bin-switch", "state", "switch"), extra=main
    )
    try:
        code, res = discover(
            "pane-switch", [payload(switched)] * 2, "switch", second_switch=True, second_sleep="0.5"
        )
        _, listed = call({"op": "list", "pane_id": "pane-switch"}, env)
        check(
            "a conversation switch during discovery imports and binds nothing",
            code == 0
            and bool(res)
            and res.get("session") is None
            and res.get("items") == []
            and bool(res.get("errors"))
            and plain_errors(res.get("errors"))
            and listed.get("session") is None,
            str(res),
        )
    finally:
        stop_holder(switched)

    code, res = call({"op": "discover"}, env)
    check(
        "discover without a pane fails closed",
        code == 0
        and bool(res)
        and res.get("ok") is True
        and res.get("session") is None
        and res.get("items") == []
        and bool(res.get("errors"))
        and plain_errors(res.get("errors")),
        str(res),
    )

    empty_bin = os.path.join(root, "empty-bin")
    os.makedirs(empty_bin, exist_ok=True)
    bare = env_for(root)
    bare["HOME"] = home
    bare["PATH"] = empty_bin
    code, res = call({"op": "discover", "pane_id": "pane-live", "host": socket.gethostname()}, bare)
    _, listed = call({"op": "list", "pane_id": "pane-live"}, env)
    check(
        "discover without the Tern CLI fails closed and leaves the binding alone",
        code == 0
        and bool(res)
        and res.get("session") is None
        and res.get("items") == []
        and bool(res.get("errors"))
        and bool(listed.get("session"))
        and listed["session"]["id"] == SESSION_A,
        str(res),
    )


def case_discover_cold(root: str) -> None:
    """A cold-resumed conversation holds no transcript open; the terminal's own
    breadcrumb is the only live proof of which conversation the pane is in."""
    env = env_for(root)
    home = os.path.join(root, "home")
    store = os.path.join(home, ".omp", "agent", "sessions")
    work = os.path.join(root, "work")
    os.makedirs(work, exist_ok=True)

    def message(text: str) -> dict:
        return {"type": "message", "role": "user", "content": [{"type": "text", "text": text}]}

    def payload(pid: int) -> dict:
        return {"foreground": {"pid": pid, "name": "18.8.7", "argv": ["omp"], "cwd": work}}

    def discover(pane: str, payloads: list[dict], tag: str, second_script: str = "", **fields):
        bin_dir = fake_tern(os.path.join(root, "bin-" + tag), payloads, second_script=second_script)
        request = {"op": "discover", "pane_id": pane, "host": socket.gethostname(), "cwd": work, "label": "Chat"}
        request.update(fields)
        return call(request, discover_env(root, home, bin_dir))

    def failed(res: dict | None, code: int) -> bool:
        return (
            code == 0
            and bool(res)
            and res.get("ok") is True
            and res.get("session") is None
            and res.get("items") == []
            and bool(res.get("errors"))
            and plain_errors(res.get("errors"))
        )

    first_media = write_file(work, "cold one.png", make_png((111, 112, 113)))
    second_media = write_file(work, "cold two.png", make_png((114, 115, 116)))
    cold = omp_transcript(
        os.path.join(store, "conv-cold"),
        "2026-01-05T00-00-00-000Z",
        SESSION_A,
        work,
        [message("kept `%s` and `%s`" % (first_media, second_media))],
    )
    other_media = write_file(work, "other.png", make_png((117, 118, 119)))
    other = omp_transcript(
        os.path.join(store, "conv-other"),
        "2026-01-06T00-00-00-000Z",
        SESSION_C,
        work,
        [message("opened `%s`" % other_media)],
    )

    pid, terminal_id, master, before_fork_ns = tty_holder(root, "cold")
    try:
        crumb = stamp_breadcrumb(home, terminal_id, work, cold)
        code, res = discover("pane-cold", [payload(pid)] * 2, "cold", window_key="win-cold")
        ok = check(
            "a cold-resumed conversation is found through its terminal breadcrumb",
            code == 0
            and bool(res)
            and res.get("ok") is True
            and bool(res.get("session"))
            and res["session"]["id"] == SESSION_A
            and plain_errors(res.get("errors")),
            str(res),
        )
        if ok:
            paths = {item["path"] for item in res["items"] if item["path"] is not None}
            check(
                "the cold conversation's own images appear without a manual import",
                {os.path.realpath(first_media), os.path.realpath(second_media)} <= paths
                and os.path.realpath(other_media) not in paths,
                str(sorted(paths)),
            )
            _, listed = call({"op": "list", "pane_id": "pane-cold", "window_key": "win-cold"}, env)
            check(
                "the pane binds and lists the cold conversation's media",
                bool(listed.get("session"))
                and listed["session"]["id"] == SESSION_A
                and listed["total"] == len(res["items"]),
                str(listed.get("total")),
            )

        # A stamp older than the process belongs to the terminal's previous
        # occupant: discovery refuses it and the pane keeps its binding.
        call(
            {"op": "touch", "session": session(SESSION_B, "pane-stale", "win-stale", "tern-stale", "Beta", work)},
            env,
        )
        crumb = stamp_breadcrumb(home, terminal_id, work, cold)
        old = time.time_ns() - 3600 * 1_000_000_000
        os.utime(crumb, ns=(old, old))
        code, res = discover("pane-stale", [payload(pid)] * 2, "stale")
        _, listed = call({"op": "list", "pane_id": "pane-stale"}, env)
        check(
            "a breadcrumb older than the process is a prior occupant's and binds nothing",
            failed(res, code)
            and bool(listed.get("session"))
            and listed["session"]["id"] == SESSION_B,
            str(res),
        )

        # Nanosecond stamps from immediately before fork must also be refused.
        # Whole-second boot telemetry used to mistake these for current stamps.
        stamp_breadcrumb(home, terminal_id, work, cold)
        os.utime(crumb, ns=(before_fork_ns, before_fork_ns))
        code, res = discover("pane-subsecond-stale", [payload(pid)] * 2, "subsecond-stale")
        _, listed = call({"op": "list", "pane_id": "pane-subsecond-stale"}, env)
        check(
            "a breadcrumb predating fork at subsecond precision imports and binds nothing",
            failed(res, code) and listed.get("session") is None and listed.get("items") == [],
            str(res),
        )

        explicit_resume = payload(pid)
        explicit_resume["foreground"]["argv"].append("--resume=" + cold)
        code, res = discover("pane-explicit-resume", [explicit_resume] * 2, "explicit-resume")
        check(
            "an unchanged older breadcrumb agrees with the live process's exact resume path",
            code == 0 and bool(res.get("session")) and res["session"]["id"] == SESSION_A
            and {item["path"] for item in res["items"]} == {os.path.realpath(first_media), os.path.realpath(second_media)},
            str(res),
        )
        wrong_resume = payload(pid)
        wrong_resume["foreground"]["argv"].extend(["--resume", other])
        code, res = discover("pane-wrong-resume", [wrong_resume] * 2, "wrong-resume")
        check(
            "a prior breadcrumb cannot override a different explicit resume path",
            failed(res, code),
            str(res),
        )

        # A breadcrumb rewritten mid-resolution — the conversation switching
        # while discovery reads — must import nothing and bind nothing.
        stamp_breadcrumb(home, terminal_id, work, cold)
        rewrite = "printf '%%s\\n%%s\\n' '%s' '%s' > '%s'" % (work, other, crumb)
        code, res = discover("pane-race", [payload(pid)] * 2, "race", second_script=rewrite)
        _, listed = call({"op": "list", "pane_id": "pane-race"}, env)
        check(
            "a breadcrumb switched mid-resolution imports and binds nothing",
            failed(res, code) and listed.get("session") is None,
            str(res),
        )

        # A target outside this host's session store is refused no matter what
        # the terminal claims.
        outside = omp_transcript(
            os.path.join(root, "elsewhere"),
            "2026-01-07T00-00-00-000Z",
            SESSION_A,
            work,
            [message("opened `%s`" % first_media)],
        )
        stamp_breadcrumb(home, terminal_id, work, outside)
        code, res = discover("pane-outside", [payload(pid)] * 2, "outside")
        check(
            "a breadcrumb pointing outside the session store binds nothing",
            failed(res, code),
            str(res),
        )

        # A child transcript names its parent and is never the pane's own
        # conversation, even when the breadcrumb points straight at it.
        child = omp_transcript(
            os.path.join(store, "conv-child"),
            "2026-01-08T00-00-00-000Z",
            SESSION_B,
            work,
            [message("opened `%s`" % first_media)],
            parent=cold,
        )
        stamp_breadcrumb(home, terminal_id, work, child)
        code, res = discover("pane-child", [payload(pid)] * 2, "crumbchild")
        check(
            "a breadcrumb pointing at a child transcript binds nothing",
            failed(res, code),
            str(res),
        )
    finally:
        stop_forked(pid)
        os.close(master)

    # A foreground OMP with no controlling terminal has no breadcrumb to trust.
    daemon = daemon_holder(root, "notty")
    try:
        code, res = discover("pane-notty", [payload(daemon)] * 2, "notty")
        check(
            "a process with no controlling terminal binds nothing",
            failed(res, code),
            str(res),
        )
    finally:
        stop_forked(daemon)


def case_touch_identity(root: str) -> None:
    env = env_for(root)
    code, res = call({"op": "touch", "session": {"id": "fixture-conversation-a", "host": "fixture", "pane_id": "5"}}, env)
    check(
        "touch keeps an explicit session id",
        code == 0 and bool(res) and res.get("ok") and res["session"]["id"] == "fixture-conversation-a",
        str(res),
    )
    _, by_pane = call({"op": "list", "pane_id": "5", "window_key": ""}, env)
    check(
        "the explicit id binds its pane",
        bool(by_pane.get("session")) and by_pane["session"]["id"] == "fixture-conversation-a",
        str(by_pane.get("session")),
    )
    _, again = call(
        {"op": "touch", "session": {"id": "fixture-conversation-a", "host": "fixture", "pane_id": "5", "label": "Fixture", "tern_session_id": "tern-5"}},
        env,
    )
    _, enriched = call({"op": "list", "pane_id": "5"}, env)
    check(
        "enriching a session keeps the id and adds the workspace",
        bool(again.get("session"))
        and again["session"]["id"] == "fixture-conversation-a"
        and enriched["session"]["tern_session_id"] == "tern-5"
        and enriched["session"]["label"] == "Fixture",
        str(enriched.get("session")),
    )


def case_blob_references(root: str) -> None:
    env = env_for(root)
    s = session(SESSION_A, "pane-a", "win-a", "tern-a", "Alpha", root)
    payload = make_png((21, 22, 23))
    digest = hashlib.sha256(payload).hexdigest()
    blob_dir = os.path.join(root, "blobs", digest[:2])
    os.makedirs(blob_dir, exist_ok=True)
    write_file(blob_dir, digest, payload)

    code, res = register(env, s, [{"data": "blob:sha256:" + digest, "mime": "image/png", "name": "capture", "source": "viewed"}])
    first = res["items"][0] if isinstance(res, dict) and res.get("items") else None
    check(
        "register resolves a blob reference to retained bytes",
        code == 0 and first is not None and first["sha256"] == digest
        and open(first["stored_path"], "rb").read() == payload,
        str(res),
    )
    check(
        "a resolved blob is an image with an extension",
        first is not None and first["kind"] == "image" and first["name"] == "capture.png",
        str(first),
    )
    _, repeat = register(env, s, [{"data": "blob:sha256:" + digest, "mime": "image/png", "name": "capture", "source": "viewed"}])
    check(
        "repeated blob events dedupe",
        first is not None and bool(repeat.get("items")) and repeat["items"][0]["id"] == first["id"],
        str(repeat),
    )

    webp = b"RIFF\x20\x00\x00\x00WEBPVP8 " + b"\x00" * 16
    webp_digest = hashlib.sha256(webp).hexdigest()
    webp_dir = os.path.join(root, "blobs", webp_digest[:2])
    os.makedirs(webp_dir, exist_ok=True)
    write_file(webp_dir, webp_digest, webp)
    _, typed = register(env, s, [{"data": "blob:sha256:" + webp_digest, "mime": "image/webp", "name": "clip", "source": "viewed"}])
    typed_first = typed["items"][0] if typed.get("items") else None
    check(
        "a blob reference keeps a typed suffix for its own mime",
        typed_first is not None
        and typed_first["kind"] == "image"
        and typed_first["name"] == "clip.webp"
        and typed_first["stored_path"].endswith(".webp")
        and open(typed_first["stored_path"], "rb").read() == webp,
        str(typed),
    )

    missing = "f" * 64
    code, bad = register(env, s, [{"data": "blob:sha256:" + missing, "mime": "image/png", "source": "viewed"}])
    check(
        "a missing blob is an explicit per-item error",
        code == 0 and bool(bad) and bad.get("ok") and bad["items"] == [] and len(bad["errors"]) == 1
        and plain_errors(bad["errors"]) and "blob" in bad["errors"][0]["error"],
        str(bad),
    )

    # Transcripts resolve blobs from a directory beside the session file.
    session_dir = os.path.join(root, "sessions", "abc")
    sibling = os.path.join(session_dir, "blobs")
    os.makedirs(sibling, exist_ok=True)
    other = make_png((31, 32, 33))
    other_digest = hashlib.sha256(other).hexdigest()
    write_file(sibling, other_digest + ".png", other)
    transcript = os.path.join(session_dir, "abc.jsonl")
    with open(transcript, "w", encoding="utf-8") as handle:
        handle.write(
            json.dumps(
                {"type": "message", "role": "user", "content": [{"type": "image", "data": "blob:sha256:" + other_digest, "mimeType": "image/png"}]}
            )
            + "\n"
        )
    _, imported = call({"op": "backfill", "session": s, "transcript_path": transcript}, env)
    check(
        "backfill resolves a transcript-sibling blob",
        any(i["sha256"] == other_digest for i in imported.get("items", [])),
        str(imported),
    )


def case_binding_order(root: str) -> None:
    env = env_for(root)
    a = session(SESSION_A, "pane-o", "win-o", "tern-o", "Alpha", root)
    b = session(SESSION_B, "pane-o", "win-o", "tern-o", "Beta", root)
    call({"op": "touch", "session": a}, env)
    call({"op": "touch", "session": b}, env)
    _, first = call({"op": "list", "pane_id": "pane-o", "window_key": "win-o"}, env)
    check(
        "the newest binding wins inside one second",
        bool(first.get("session")) and first["session"]["id"] == SESSION_B,
        str(first.get("session")),
    )

    # The older session keeps the lower rowid, so an ordering that falls back
    # to rowid inside one second would return it instead of the fresh bind.
    call({"op": "touch", "session": a}, env)
    _, second = call({"op": "list", "pane_id": "pane-o", "window_key": "win-o"}, env)
    check(
        "a same-second rebind returns the session that just bound",
        bool(second.get("session")) and second["session"]["id"] == SESSION_A,
        str(second.get("session")),
    )

    # A child OMP session reports {id, host} only and must not rebind the pane.
    _, child = call(
        {
            "op": "register",
            "session": {"id": SESSION_B, "host": "testhost"},
            "items": [
                {"data": _b64(make_png((5, 5, 5))), "mime": "image/png", "name": "late.png", "source": "viewed"}
            ],
        },
        env,
    )
    check(
        "a child-style capture still stores under its session",
        bool(child) and child.get("ok") and len(child["items"]) == 1,
        str(child),
    )
    _, third = call({"op": "list", "pane_id": "pane-o", "window_key": "win-o"}, env)
    check(
        "a late child capture does not steal the pane binding",
        bool(third.get("session")) and third["session"]["id"] == SESSION_A,
        str(third.get("session")),
    )


def case_legacy_migration(root: str) -> None:
    env = env_for(root)
    legacy = sqlite3.connect(os.path.join(root, "media.db"))
    legacy.execute(
        """
        CREATE TABLE sessions (
            id TEXT PRIMARY KEY,
            label TEXT NOT NULL DEFAULT '',
            cwd TEXT NOT NULL DEFAULT '',
            host TEXT NOT NULL DEFAULT '',
            pane_id TEXT NOT NULL DEFAULT '',
            window_key TEXT NOT NULL DEFAULT '',
            tern_session_id TEXT NOT NULL DEFAULT '',
            tern_session_name TEXT NOT NULL DEFAULT '',
            touched_at TEXT NOT NULL
        )
        """
    )
    legacy.execute(
        "INSERT INTO sessions (id, label, pane_id, window_key, tern_session_id,"
        " tern_session_name, touched_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
        (SESSION_C, "Legacy", "pane-l", "win-l", "tern-l", "Tern tern-l", "2020-01-01T00:00:00+00:00"),
    )
    legacy.commit()
    legacy.close()

    _, listed = call({"op": "list", "pane_id": "pane-l", "window_key": "win-l"}, env)
    check(
        "a pre-bound_at database still resolves its pane",
        bool(listed)
        and listed.get("ok")
        and listed["session"] is not None
        and listed["session"]["id"] == SESSION_C,
        str(listed.get("session")),
    )
    _, sessions = call({"op": "list", "scope": "all"}, env)
    check(
        "migrating a pre-bound_at database keeps its sessions",
        any(entry["id"] == SESSION_C for entry in sessions["sessions"]),
        str(sessions["sessions"]),
    )


def case_input_limits(root: str) -> None:
    env = env_for(root)
    s = session(SESSION_A, "pane-a", "win-a", "tern-a", "Alpha", root)

    # A path substituted with a non-regular file must be refused by the
    # descriptor check, never awaited: the open is non-blocking.
    fifo = os.path.join(root, "pipe.png")
    os.mkfifo(fifo)
    try:
        code, res = call(
            {"op": "register", "session": s, "items": [{"path": fifo, "source": "viewed"}]},
            env,
            timeout=30,
        )
        check(
            "a FIFO path is rejected instead of blocking the read",
            code == 0
            and bool(res)
            and res.get("ok")
            and res["items"] == []
            and len(res["errors"]) == 1
            and plain_errors(res["errors"]),
            str(res),
        )
    except subprocess.TimeoutExpired:
        check("a FIFO path is rejected instead of blocking the read", False, "backend blocked on a FIFO")

    # An encoded payload is bounded before it is copied or decoded.
    oversized = "A" * (ENCODED_LIMIT + 1)
    code, res = register(
        env, s, [{"data": oversized, "mime": "image/png", "name": "huge.png", "source": "viewed"}]
    )
    check(
        "an oversized encoded payload is rejected before decoding",
        code == 0
        and bool(res)
        and res.get("ok")
        and res["items"] == []
        and len(res["errors"]) == 1
        and plain_errors(res["errors"]),
        str(res),
    )

    # A transcript is bounded before any line reaches the JSON parser.
    huge = os.path.join(root, "huge.jsonl")
    with open(huge, "wb") as handle:
        handle.truncate(TRANSCRIPT_LIMIT + 1)
    code, res = call({"op": "backfill", "session": s, "transcript_path": huge}, env)
    check(
        "an oversized transcript is rejected",
        code == 1 and bool(res) and res.get("ok") is False and isinstance(res.get("error"), str),
        str(res),
    )

    long_line = os.path.join(root, "longline.jsonl")
    with open(long_line, "wb") as handle:
        handle.truncate(LINE_LIMIT + 1)
    code, res = call({"op": "backfill", "session": s, "transcript_path": long_line}, env)
    check(
        "an over-long transcript line is rejected",
        code == 1 and bool(res) and res.get("ok") is False and isinstance(res.get("error"), str),
        str(res),
    )


def case_shared_initialization(root: str) -> None:
    env = env_for(root)
    s = session(SESSION_A, "pane-a", "win-a", "tern-a", "Alpha", root)

    # A first-use request must wait out a held write transaction instead of
    # failing on a journal-mode change: initialisation acquires its write lock
    # with BEGIN IMMEDIATE, which honours busy_timeout.
    holder = sqlite3.connect(os.path.join(root, "media.db"), timeout=0, isolation_level=None)
    holder.execute("BEGIN IMMEDIATE")
    boot = subprocess.Popen(
        [sys.executable, BACKEND],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=env,
    )
    boot.stdin.write(json.dumps({"op": "touch", "session": s}))
    boot.stdin.close()
    boot.stdin = None
    time.sleep(1.0)
    holder.execute("ROLLBACK")
    holder.close()
    boot_out = ""
    try:
        boot_out, _ = boot.communicate(timeout=40)
        code = boot.returncode
    except subprocess.TimeoutExpired:
        boot.kill()
        code = None
        boot.communicate()
    try:
        booted = json.loads(boot_out) if boot_out.strip() else None
    except ValueError:
        booted = None
    check(
        "a first-use request waits out a held writer instead of failing",
        code == 0 and isinstance(booted, dict) and booted.get("ok") is True,
        "%s %s" % (code, booted),
    )

    # Several initial refresh requests race the same empty state directory.
    fresh = fresh_state("concurrent")
    race_env = env_for(fresh)
    requests = [
        {
            "op": "touch",
            "session": session(
                "%08d-0000-4000-8000-000000000000" % i,
                "pane-%d" % i,
                "win-%d" % i,
                "tern-%d" % i,
                "R%d" % i,
                fresh,
            ),
        }
        for i in range(6)
    ]
    procs = [
        subprocess.Popen(
            [sys.executable, BACKEND],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env=race_env,
        )
        for _ in requests
    ]
    for proc, request in zip(procs, requests):
        try:
            proc.stdin.write(json.dumps(request))
            proc.stdin.close()
            proc.stdin = None
        except BrokenPipeError:
            pass
    outcomes = []
    for proc in procs:
        out, _ = proc.communicate(timeout=120)
        try:
            parsed = json.loads(out)
        except ValueError:
            parsed = None
        outcomes.append(proc.returncode == 0 and isinstance(parsed, dict) and parsed.get("ok") is True)
    check("racing first-use requests all succeed", all(outcomes), str(outcomes))

    _, listed = call({"op": "list", "scope": "all"}, race_env)
    check(
        "each racing session is stored exactly once",
        bool(listed) and listed.get("ok") and {row["id"] for row in listed["sessions"]}
        == {request["session"]["id"] for request in requests},
        str(listed and listed.get("sessions")),
    )

    # An initialised store answers read/list without re-running writes, so a
    # concurrent writer cannot block it.
    _, stored = register(env, s, [{"path": write_file(root, "coexist.png", make_png((7, 7, 7))), "source": "viewed"}])
    holder = sqlite3.connect(os.path.join(root, "media.db"), timeout=0, isolation_level=None)
    holder.execute("BEGIN IMMEDIATE")
    try:
        code, listed = call({"op": "list", "scope": "all"}, env, timeout=15)
        check(
            "read/list coexists with a held write transaction",
            code == 0 and bool(listed) and listed.get("ok") and listed["total"] == 1,
            str(listed),
        )
    except subprocess.TimeoutExpired:
        check("read/list coexists with a held write transaction", False, "backend blocked on a held writer")
    finally:
        holder.execute("ROLLBACK")
        holder.close()


def _b64(payload: bytes) -> str:
    import base64

    return base64.b64encode(payload).decode("ascii")


def main() -> int:
    if not os.path.isfile(BACKEND):
        print("FAIL media-store.py is missing next to selftest.py")
        return 1
    print("Self-check: %s" % BACKEND)
    cases = (
        ("basic register", case_basic),
        ("overwrite versions", case_overwrite),
        ("conversation scope", case_conversation_scope),
        ("workspace and child binding", case_workspace_and_child_binding),
        ("touch identity", case_touch_identity),
        ("tags and search", case_tags_and_search),
        ("restart persistence", case_restart_persistence),
        ("input rejection", case_rejections),
        ("blob references", case_blob_references),
        ("transcript backfill", case_backfill),
        ("discover live conversation", case_discover),
        ("discover cold-resumed conversation", case_discover_cold),
        ("binding order", case_binding_order),
        ("legacy migration", case_legacy_migration),
        ("shared initialization", case_shared_initialization),
        ("input limits", case_input_limits),
    )
    try:
        for tag, case in cases:
            root = fresh_state(tag.split()[0])
            print("\n-- %s" % tag)
            case(root)
    finally:
        for root in TEMP_DIRS:
            print("Self-check fixture retained: %s" % root)
    print("\n%d checks, %d failed" % (CHECKS, len(FAILURES)))
    for label in FAILURES:
        print("  failed: %s" % label)
    return 1 if FAILURES else 0


if __name__ == "__main__":
    sys.exit(main())
