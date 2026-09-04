# RUN-ef84410db47f4937 snapshot bundle

This directory contains a compressed, byte-preserving snapshot of the external
run `RUN-ef84410db47f4937/`. The six parts are each at most 45,000,000 bytes;
the unsplit archive is intentionally not stored in Git. `manifest.json` covers
all 813 files (2,279,319,439 source bytes), including zero-byte lock files, with
relative path, byte size, mode, and SHA-256. `archive.sha256` records the
compressed archive hash:

`a480840d88c2563824715b589e42031b3b2b4d6f26deb177ffd085bb6dc7b591`

From a clone, reconstruct and verify the archive (the temporary directory is
the only location written by these commands):

```sh
repo="$(git rev-parse --show-toplevel)"
bundle="$repo/runs/RUN-ef84410db47f4937.bundle"
tmp="$(mktemp -d)"
trap 'rm -rf "$tmp"' EXIT
cat "$bundle"/run.tar.gz.part-* > "$tmp/run.tar.gz"
(cd "$tmp" && shasum -a 256 -c "$bundle/archive.sha256")
mkdir "$tmp/extracted"
tar -xzf "$tmp/run.tar.gz" -p -C "$tmp/extracted"
```

Verify every extracted file against the manifest (including size and mode):

```sh
python3 - "$tmp/extracted/RUN-ef84410db47f4937" "$bundle/manifest.json" <<'PY'
import hashlib, json, os, stat, sys

root, manifest_path = sys.argv[1:]
manifest = json.load(open(manifest_path, encoding="utf-8"))
expected = {entry["path"]: entry for entry in manifest["files"]}
for rel, entry in expected.items():
    path = os.path.join(root, rel)
    st = os.stat(path, follow_symlinks=False)
    if not stat.S_ISREG(st.st_mode):
        raise SystemExit(f"FAIL non-regular file: {rel}")
    mode = format(stat.S_IMODE(st.st_mode), "04o")
    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        for block in iter(lambda: fh.read(1024 * 1024), b""):
            digest.update(block)
    if st.st_size != entry["bytes"] or mode != entry["mode"] or digest.hexdigest() != entry["sha256"]:
        raise SystemExit(f"FAIL manifest mismatch: {rel}")
actual = []
for directory, _, names in os.walk(root):
    actual.extend(os.path.relpath(os.path.join(directory, name), root) for name in names)
if sorted(actual) != sorted(expected):
    raise SystemExit("FAIL extracted file set differs from manifest")
print(f"PASS {len(actual)} files; sizes, modes, and SHA-256 values match")
PY
```
