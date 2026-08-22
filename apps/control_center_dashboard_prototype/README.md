# Control Center — Dashboard Theme Prototype

This is a separate visual prototype of the Auto Foundry Control Center. It
reuses the existing read-only data projection and browser behavior, but applies
the color, typography, card, and navigation language of the latest generated
Auto Foundry dashboard.

The original prototype under `apps/control_center/` is not modified. The server
binds to loopback only, loads the deterministic fixture by default, and does not
enable run commands.

Run from the repository root:

```bash
python3 -m apps.control_center_dashboard_prototype.server
```

Then open <http://127.0.0.1:8766>.

To inspect an existing run without mutating it, add an explicit read-only root:

```bash
python3 -m apps.control_center_dashboard_prototype.server \
  --runs-root /absolute/path/to/runs
```
