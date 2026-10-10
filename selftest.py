#!/usr/bin/env python3
"""Standalone self-check for the Tern Session Media backend.

Runs media-store.py exactly the way Tern does: one JSON request on stdin, one
JSON response on stdout, a fresh process per call. Every case gets its own
TERN_MEDIA_STATE_DIR under a temp directory that is removed afterwards.

    python3 selftest.py

Exits non-zero on the first failing expectation set; no third-party packages.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import sqlite3
import struct
import subprocess
import sys
import tempfile
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
        ("binding order", case_binding_order),
        ("legacy migration", case_legacy_migration),
        ("input limits", case_input_limits),
    )
    try:
        for tag, case in cases:
            root = fresh_state(tag.split()[0])
            print("\n-- %s" % tag)
            case(root)
    finally:
        for root in TEMP_DIRS:
            shutil.rmtree(root, ignore_errors=True)
    print("\n%d checks, %d failed" % (CHECKS, len(FAILURES)))
    for label in FAILURES:
        print("  failed: %s" % label)
    return 1 if FAILURES else 0


if __name__ == "__main__":
    sys.exit(main())
