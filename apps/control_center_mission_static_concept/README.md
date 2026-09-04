# Mission Board static concept

This directory is a progressively enhanced HTML/CSS design concept. When it is
served alone on port 8778, the missing preview API leaves the frozen design
snapshot unchanged. When it is mounted at `/board/` by the isolated Mission
preview, it polls that preview's allowlisted, read-only snapshot. It has no
run-control actions.

Serve it locally only to review the visual design:

```bash
python3 -m http.server 8778 --bind 127.0.0.1 \
  --directory apps/control_center_mission_static_concept
```

The connected version is served by `apps.control_center_mission_preview` at
`http://127.0.0.1:8777/board/`.
