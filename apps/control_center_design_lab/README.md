# Auto Foundry Mission Display Design Lab

Standalone, read-only design fixture containing twelve alternative mission-display concepts. The second wave explores action-level graphs, ephemeral agent sessions, temporal braids, flame nesting, semantic zoom, and a kinetic event field.

This preview does not import Control Center code, read a run, or mutate run state. It uses one deterministic synthetic fixture and event sequence across all concepts so the interaction models can be compared directly. The global granularity switch is presentation-only: Everything shows the full fixture and Signal reduces dense concepts to active/critical context.

Run locally:

```bash
python3 -m http.server 8780 --bind 127.0.0.1 --directory apps/control_center_design_lab
```

Then open `http://127.0.0.1:8780/`.
