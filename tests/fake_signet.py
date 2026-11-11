#!/usr/bin/env python3
"""A fake `signet` for the tests. It keeps a drive in one JSON file and
answers the verbs and flags the plugin uses, in the shapes the public CLI
specification describes. It proves the plugin's logic against those shapes,
not the real client; the real runs happen on staging.

Environment:
  SIGNET_FAKE_STORE      the JSON file that is the drive (required)
  SIGNET_FAKE_FAIL       "<exit>:<code>" to make the next server-bound commands fail
  SIGNET_FAKE_FAIL_N     how many commands fail (default 1); "upload:<k>" fails only
                         the k-th upload of the store's life; "delete:<k>" the k-th delete
  SIGNET_FAKE_LOG        append every argv here (optional)
"""
import json
import os
import sys
import time
import hashlib
import secrets

STORE = os.environ["SIGNET_FAKE_STORE"]


def load():
    try:
        with open(STORE) as f:
            return json.load(f)
    except FileNotFoundError:
        return {"shares": {}, "folders": {}, "files": {}, "uploads": 0, "deletes": 0, "fails_left": None,
                "quota": {"bytes_used": 0, "bytes_quota": 1 << 30}}


def save(d):
    tmp = STORE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(d, f)
    os.replace(tmp, STORE)


def err(exit_code, code, message):
    sys.stderr.write(json.dumps({"error": {"code": code, "message": message, "details": {}}}) + "\n")
    sys.exit(exit_code)


def main(argv):
    if os.environ.get("SIGNET_FAKE_LOG"):
        with open(os.environ["SIGNET_FAKE_LOG"], "a") as f:
            f.write(json.dumps(argv) + "\n")
    # strip the common flags the wrapper always adds
    args, i = [], 0
    while i < len(argv):
        a = argv[i]
        if a in ("--server-url", "--key", "--kem-key"):
            i += 2
            continue
        if a in ("--json-errors", "--json"):
            i += 1
            continue
        args.append(a)
        i += 1
    if args[:1] == ["version"]:
        print("signet 0.0.0-fake")
        return
    d = load()
    # failure injection
    spec = os.environ.get("SIGNET_FAKE_FAIL")
    if spec:
        n = os.environ.get("SIGNET_FAKE_FAIL_N", "1")
        exit_code, _, code = spec.partition(":")
        if n.startswith("upload:"):
            k = int(n.split(":")[1])
            if args[:2] == ["file", "upload"] and d["uploads"] + 1 == k:
                err(int(exit_code), code, f"planted failure on upload {k}")
        elif n.startswith("delete:"):
            k = int(n.split(":")[1])
            if args[:2] == ["file", "delete"] and d.get("deletes", 0) + 1 == k:
                err(int(exit_code), code, f"planted failure on delete {k}")
        else:
            if d.get("fails_left") is None:
                d["fails_left"] = int(n)
            if d["fails_left"] > 0:
                d["fails_left"] -= 1
                save(d)
                err(int(exit_code), code, "planted failure")
    out = dispatch(d, args)
    save(d)
    if out is not None:
        print(json.dumps(out))


def dispatch(d, args):
    if args[:1] == ["whoami"]:
        return {"account_id": "acc-fake", "account_type": "prsn", "handle": "test-agent",
                "prsn_sharing_capability": "none", "guardian": "chris",
                "drive_access": {"status": "authorized", "reconfirm_interval_days": 30, "overdue": False}}
    if args[:2] == ["transparency-verify", "--self"]:
        mode = os.environ.get("SIGNET_FAKE_TV", "clean")
        if mode == "clean":
            print(json.dumps({"verdict": "clean", "epoch": 2, "entries": [{"fp": "a"}, {"fp": "b"}, {"fp": "c"}, {"fp": "d"}]})); sys.exit(0)
        if mode == "pending":
            print(json.dumps({"verdict": "not_yet_published", "epoch": 2, "scanned": 7, "held_not_in_published_log": ["a", "b", "c", "d"]}))
            err(62, "unanchored", "4 of your keys are not in epoch 2's published prefix yet")
        print(json.dumps({"verdict": "foreign_key", "epoch": 2}))
        err(60, "transparency_violation", "a key under this account's index that this client does not hold")
    if args[:1] == ["connect"]:
        return {"handle": "test-agent", "enrollment_confirmed": True}
    if args[:1] == ["quota"]:
        q = d["quota"]
        return {"bytes_used": q["bytes_used"], "bytes_quota": q["bytes_quota"],
                "bytes_available": q["bytes_quota"] - q["bytes_used"]}
    if args[:2] == ["share", "list"]:
        return []   # folders shared WITH the caller; an agent's own top-level folders are not here
    if args[:2] == ["share", "create"]:
        name = args[2]
        if name in d["shares"]:
            err(27, "conflict", "share exists")
        fid = "sh-" + secrets.token_hex(4)
        d["shares"][name] = fid
        d["folders"][name] = fid
        return {"folder_id": fid}
    if args[:2] == ["folder", "list"]:
        parent = args[2] if len(args) > 2 else ""
        rows = []
        for path, fid in d["folders"].items():
            if parent and path.startswith(parent + "/") and "/" not in path[len(parent) + 1:]:
                rows.append({"name": path.rsplit("/", 1)[1], "folder_id": fid})
            if not parent and "/" not in path:
                rows.append({"name": path, "folder_id": fid})
        return rows
    if args[:2] == ["folder", "create"]:
        path = args[2]
        parent = path.rsplit("/", 1)[0]
        if parent not in d["folders"]:
            err(2, "invalid_arguments", f"parent not found: {parent}")
        fid = "fo-" + secrets.token_hex(4)
        d["folders"][path] = fid          # duplicates allowed, like the real server
        return {"folder_id": fid}
    if args[:2] == ["file", "list"]:
        folder = args[2]
        if folder not in d["folders"]:
            err(2, "invalid_arguments", f"no such folder: {folder}")
        rows = []
        for path, f in d["files"].items():
            if path.rsplit("/", 1)[0] == folder:
                rows.append({"name": path.rsplit("/", 1)[1], "file_id": f["file_id"],
                             "size_bytes": f["size"] + 64, "created_at": f["created_at"],
                             "modified_at": f["created_at"]})
        # A second file under a name already there: the real server cannot refuse it, since names
        # are encrypted on the client. A test plants one in the store's "duplicates" list.
        for dup in d.get("duplicates", []):
            if dup.get("folder") == folder:
                rows.append({"name": dup["name"], "file_id": dup.get("file_id", "fi-dup"),
                             "size_bytes": dup.get("size", 0) + 64, "created_at": "2026-11-11T00:00:00Z",
                             "modified_at": "2026-11-11T00:00:00Z"})
        return rows
    if args[:2] == ["file", "upload"]:
        dest = args[2]
        src = args[args.index("--in") + 1]
        folder = dest.rsplit("/", 1)[0]
        if folder not in d["folders"]:
            err(2, "invalid_arguments", f"no such folder: {folder}")
        if dest in d["files"]:
            err(27, "conflict", "a file of that name exists (v1 is create-only)")
        with open(src, "rb") as f:
            data = f.read()
        q = d["quota"]
        if q["bytes_used"] + len(data) > q["bytes_quota"]:
            err(18, "insufficient_storage", "the upload does not fit")
        q["bytes_used"] += len(data)
        d["uploads"] += 1
        fid = "fi-" + secrets.token_hex(4)
        d["files"][dest] = {"file_id": fid, "size": len(data), "sha256": hashlib.sha256(data).hexdigest(),
                            "created_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                            "blob": data.hex()}
        return {"file_id": fid, "folder_id": d["folders"][folder], "plaintext_bytes": len(data), "chunk_count": 1}
    if args[:2] == ["file", "delete"]:
        paths = [a for a in args[2:] if a != "--yes"]
        if "--yes" not in args:
            err(2, "invalid_arguments", "deletes are permanent; pass --yes for scripted use")
        n = 0
        d["deletes"] = d.get("deletes", 0) + 1
        for path in paths:
            f = d["files"].pop(path, None)
            if f is None:
                err(26, "not_found", f"no such file: {path}")
            d["quota"]["bytes_used"] -= f["size"]
            n += 1
        return {"deleted": n}
    if args[:2] == ["file", "download"]:
        path = args[2]
        out = args[args.index("--out") + 1]
        f = d["files"].get(path)
        if f is None:
            err(26, "not_found", f"no such file: {path}")
        with open(out, "wb") as fh:
            fh.write(bytes.fromhex(f["blob"]))
        return None
    err(2, "invalid_arguments", f"the fake does not know: {' '.join(args)}")


if __name__ == "__main__":
    main(sys.argv[1:])
