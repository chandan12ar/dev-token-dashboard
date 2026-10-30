// ai-fluency-insights:managed-statusline
// Captures Anthropic's real rate_limits (five_hour/seven_day) for
// ai_fluency_insights.py's plan-usage panel and toast notifications.
// See docs/DASHBOARD_GUIDE.md#plan-usage-limits.
const fs = require('fs');
const os = require('os');
const path = require('path');

let raw = '';
process.stdin.on('data', d => raw += d);
process.stdin.on('end', () => {
  let j = {};
  try { j = JSON.parse(raw); } catch (e) { /* fall through with j={} */ }

  if (j.rate_limits) {
    const outPath = path.join(os.homedir(), '.claude', 'rate_limits_latest.json');
    let merged = {};
    try {
      const prev = JSON.parse(fs.readFileSync(outPath, 'utf8'));
      merged = Object.assign({}, prev.rate_limits);
    } catch (e) { /* no previous snapshot, or unreadable -- start fresh */ }
    Object.assign(merged, j.rate_limits);
    const payload = JSON.stringify({
      rate_limits: merged,
      captured_at: Date.now() / 1000,
    });
    try {
      const tmp = outPath + '.tmp';
      fs.writeFileSync(tmp, payload);
      fs.renameSync(tmp, outPath);
    } catch (e) { /* best-effort; never break the status line over this */ }
  }

  const dir = (j.workspace && j.workspace.current_dir) || j.cwd || '';
  const model = (j.model && j.model.display_name) || '';
  const ctx = j.context_window && j.context_window.used_percentage;
  const rl = j.rate_limits || {};
  const five = rl.five_hour && rl.five_hour.used_percentage;
  const seven = rl.seven_day && rl.seven_day.used_percentage;

  let line = dir + '  ' + model;
  if (ctx != null) line += `  ctx ${Math.round(ctx)}%`;
  if (five != null) line += `  5h ${Math.round(five)}%`;
  if (seven != null) line += `  wk ${Math.round(seven)}%`;
  process.stdout.write(line);
});
