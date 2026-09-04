# Mission preview

An isolated, read-only redesign of the Mission page. It reads an existing run
through `OperationalRepository`, exposes only `GET /api/config` and
`GET /api/snapshot`, and has no launch or run-control surface.

The graph preview is served at `/`. The requirement-board design is served at
`/board/` and reads the same allowlisted snapshot. Both surfaces remain
read-only.

```bash
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src:. \
python3 -m apps.control_center_mission_preview.server \
  --run-root /absolute/path/to/RUN-... \
  --run-id RUN-... \
  --port 8777
```

Open `http://127.0.0.1:8777/board/` for the connected Mission Board.
