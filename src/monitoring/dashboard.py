from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from fastapi import FastAPI
from fastapi.responses import HTMLResponse

dashboard_app = FastAPI(title="Minions Dashboard")

_DASHBOARD_HTML = """\
<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Minions Dashboard</title>
<style>
  * { margin: 0; padding: 0; box-sizing: border-box; }
  body { font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
         background: #0f1117; color: #e0e0e0; padding: 2rem; }
  h1 { font-size: 1.8rem; margin-bottom: 1.5rem; color: #7c8aff; }
  .cards { display: flex; gap: 1.2rem; flex-wrap: wrap; margin-bottom: 2rem; }
  .card { background: #1a1d2e; border-radius: 10px; padding: 1.2rem 1.6rem;
          min-width: 180px; flex: 1; }
  .card h3 { font-size: 0.85rem; text-transform: uppercase; color: #888; margin-bottom: 0.4rem; }
  .card .value { font-size: 1.6rem; font-weight: 700; }
  table { width: 100%; border-collapse: collapse; background: #1a1d2e;
          border-radius: 10px; overflow: hidden; }
  th, td { text-align: left; padding: 0.75rem 1rem; }
  th { background: #252840; color: #aaa; font-weight: 600; font-size: 0.8rem;
       text-transform: uppercase; }
  tr:nth-child(even) { background: #1e2133; }
  .status-completed { color: #4caf50; }
  .status-running { color: #ff9800; }
  .status-failed { color: #f44336; }
  .status-pending { color: #888; }
  .refresh { margin-bottom: 1rem; color: #555; font-size: 0.8rem; }
</style>
</head>
<body>
<h1>Minions Dashboard</h1>
<div class="refresh" id="refresh"></div>
<div class="cards" id="cards"></div>
<h2 style="margin-bottom:0.8rem;">Recent Runs</h2>
<table>
  <thead><tr><th>Task ID</th><th>Status</th><th>Description</th><th>Created</th></tr></thead>
  <tbody id="runs"></tbody>
</table>
<script>
async function load() {
  try {
    const stats = await (await fetch('/dashboard/api/stats')).json();
    document.getElementById('cards').innerHTML = `
      <div class="card"><h3>Total Runs</h3><div class="value">${stats.total_tasks}</div></div>
      <div class="card"><h3>Active</h3><div class="value">${stats.active_tasks}</div></div>
      <div class="card"><h3>Queued</h3><div class="value">${stats.queued_tasks}</div></div>
      <div class="card"><h3>Failed</h3><div class="value">${stats.failed_tasks}</div></div>
      <div class="card"><h3>Success Rate</h3><div class="value">${(stats.success_rate*100).toFixed(1)}%</div></div>
      <div class="card"><h3>Total Tokens</h3><div class="value">${stats.total_tokens.toLocaleString()}</div></div>
      <div class="card"><h3>Avg Duration</h3><div class="value">${stats.avg_duration.toFixed(1)}s</div></div>
      <div class="card"><h3>Avg CI Rounds</h3><div class="value">${stats.avg_ci_rounds.toFixed(1)}</div></div>
    `;
    const rows = stats.recent_tasks.map(t => `
      <tr>
        <td style="font-family:monospace;font-size:0.85rem">${t.id.slice(0,12)}</td>
        <td class="status-${t.status}">${t.status}</td>
        <td>${t.description.slice(0,80)}</td>
        <td>${new Date(t.created_at).toLocaleString()}</td>
      </tr>`).join('');
    document.getElementById('runs').innerHTML = rows || '<tr><td colspan="4" style="color:#666">No tasks yet</td></tr>';
    document.getElementById('refresh').textContent = 'Last updated: ' + new Date().toLocaleTimeString();
  } catch(e) { console.error(e); }
}
load();
setInterval(load, 5000);
</script>
</body>
</html>
"""


@dashboard_app.get("/", response_class=HTMLResponse)
async def dashboard_page() -> str:
    return _DASHBOARD_HTML


@dashboard_app.get("/api/stats")
async def dashboard_stats() -> dict[str, Any]:
    """Return JSON stats consumed by the dashboard frontend."""
    # Import here to avoid circular imports at module load time
    from src.api.routes import _metrics, _run_results, _run_statuses, _tasks, worker_status

    tasks = list(_tasks.values())
    active = sum(1 for t in tasks if t.status == "running")
    queued = sum(1 for t in tasks if t.status == "pending")
    completed = sum(1 for t in tasks if t.status == "completed")
    failed = sum(1 for t in tasks if t.status == "failed")
    total = len(tasks)
    success_rate = (completed / total) if total > 0 else 0.0

    total_tokens = sum(s.tokens_used for s in _run_statuses.values())
    ci_conclusions: dict[str, int] = {}
    for result in _run_results.values():
        if result.ci_conclusion:
            ci_conclusions[result.ci_conclusion] = ci_conclusions.get(result.ci_conclusion, 0) + 1
    priorities = {
        "high": sum(1 for t in tasks if t.priority == "high"),
        "normal": sum(1 for t in tasks if t.priority == "normal"),
        "low": sum(1 for t in tasks if t.priority == "low"),
    }

    recent = sorted(tasks, key=lambda t: t.created_at, reverse=True)[:20]
    metric_summary = _metrics.get_summary()

    return {
        "total_tasks": total,
        "active_tasks": active,
        "queued_tasks": queued,
        "failed_tasks": failed,
        "success_rate": round(success_rate, 4),
        "total_tokens": total_tokens,
        "avg_duration": metric_summary["avg_duration"],
        "avg_ci_rounds": metric_summary["avg_ci_rounds"],
        "ci_conclusions": ci_conclusions,
        "priorities": priorities,
        "worker": worker_status(),
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "recent_tasks": [
            {
                "id": t.id,
                "status": t.status,
                "description": t.description,
                "created_at": t.created_at.isoformat(),
            }
            for t in recent
        ],
    }
