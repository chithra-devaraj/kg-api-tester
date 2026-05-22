"""
Generates downloadable HTML and TXT reports from test run results.
"""

from datetime import datetime, timezone
from typing import List, Optional
from test_engine import TestResult, GroundTruth, RunConfig, RESULT_PASS, RESULT_FAIL, RESULT_WARN, RESULT_ERROR


RESULT_COLOR = {
    RESULT_PASS:  "#1a7f3c",
    RESULT_FAIL:  "#c0392b",
    RESULT_WARN:  "#d68910",
    RESULT_ERROR: "#7d3c98",
    "SKIP":       "#7f8c8d",
}

RESULT_BG = {
    RESULT_PASS:  "#eafaf1",
    RESULT_FAIL:  "#fdedec",
    RESULT_WARN:  "#fef9e7",
    RESULT_ERROR: "#f5eef8",
    "SKIP":       "#f2f3f4",
}

RESULT_ICON = {
    RESULT_PASS:  "✅",
    RESULT_FAIL:  "❌",
    RESULT_WARN:  "⚠️",
    RESULT_ERROR: "🔴",
    "SKIP":       "⏭️",
}


def build_html_report(
    config: RunConfig,
    results: list[TestResult],
    ground_truth: Optional[GroundTruth],
    started_at: str,
) -> str:

    total   = len(results)
    passed  = sum(1 for r in results if r.result == RESULT_PASS)
    failed  = sum(1 for r in results if r.result == RESULT_FAIL)
    warned  = sum(1 for r in results if r.result == RESULT_WARN)
    errored = sum(1 for r in results if r.result == RESULT_ERROR)
    bugs    = [r for r in results if r.bug_id]

    pass_pct = int(passed / total * 100) if total else 0

    gt_html = _build_ground_truth_html(ground_truth)
    rows    = _build_result_rows(results)
    detail  = _build_detail_sections(results)
    bug_sec = _build_bugs_section(bugs)

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>KG Query API Test Report</title>
<style>
  * {{ box-sizing: border-box; margin: 0; padding: 0; }}
  body {{ font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, monospace; background: #f8f9fa; color: #212529; }}
  .header {{ background: linear-gradient(135deg, #1a1a2e 0%, #16213e 100%); color: #fff; padding: 32px 40px; }}
  .header h1 {{ font-size: 1.6rem; font-weight: 600; margin-bottom: 8px; }}
  .header .meta {{ font-size: 0.85rem; opacity: 0.75; line-height: 1.8; }}
  .container {{ max-width: 1200px; margin: 0 auto; padding: 24px 24px; }}
  .scorecard {{ display: grid; grid-template-columns: repeat(5, 1fr); gap: 16px; margin-bottom: 28px; }}
  .score-card {{ background: #fff; border-radius: 10px; padding: 20px; text-align: center; box-shadow: 0 1px 6px rgba(0,0,0,.07); }}
  .score-card .num  {{ font-size: 2.2rem; font-weight: 700; }}
  .score-card .lbl  {{ font-size: 0.78rem; text-transform: uppercase; letter-spacing: .05em; color: #6c757d; margin-top: 4px; }}
  .progress-bar {{ background: #e9ecef; border-radius: 8px; height: 10px; overflow: hidden; margin: 4px 0 0; }}
  .progress-fill {{ height: 100%; border-radius: 8px; background: #1a7f3c; transition: width .3s; }}
  section {{ background: #fff; border-radius: 10px; box-shadow: 0 1px 6px rgba(0,0,0,.07); margin-bottom: 24px; overflow: hidden; }}
  section h2 {{ font-size: 1rem; font-weight: 600; padding: 16px 20px; background: #f1f3f5; border-bottom: 1px solid #e9ecef; }}
  table {{ width: 100%; border-collapse: collapse; }}
  th {{ background: #f8f9fa; padding: 10px 14px; text-align: left; font-size: 0.8rem; text-transform: uppercase; letter-spacing: .04em; color: #6c757d; border-bottom: 2px solid #e9ecef; }}
  td {{ padding: 10px 14px; font-size: 0.875rem; border-bottom: 1px solid #f1f3f5; vertical-align: top; }}
  tr:hover td {{ background: #fafbfc; }}
  .badge {{ display: inline-block; padding: 2px 10px; border-radius: 12px; font-size: 0.75rem; font-weight: 600; }}
  .cat-pos    {{ background: #d4edda; color: #155724; }}
  .cat-neg    {{ background: #f8d7da; color: #721c24; }}
  .cat-sch    {{ background: #cce5ff; color: #004085; }}
  .cat-custom {{ background: #fff3cd; color: #856404; }}
  pre {{ background: #1e1e2e; color: #cdd6f4; padding: 14px; border-radius: 6px; font-size: 0.78rem; overflow-x: auto; white-space: pre-wrap; word-break: break-all; margin: 0; }}
  details {{ border: 1px solid #e9ecef; border-radius: 8px; margin: 12px 20px; }}
  summary {{ padding: 12px 16px; cursor: pointer; font-size: 0.875rem; font-weight: 500; list-style: none; display: flex; align-items: center; gap: 10px; }}
  summary::-webkit-details-marker {{ display: none; }}
  .detail-body {{ padding: 16px; border-top: 1px solid #e9ecef; }}
  .detail-grid {{ display: grid; grid-template-columns: 1fr 1fr; gap: 16px; }}
  .detail-label {{ font-size: 0.75rem; text-transform: uppercase; letter-spacing: .05em; color: #6c757d; margin-bottom: 6px; }}
  .bug-card {{ border-left: 4px solid #c0392b; background: #fff; border-radius: 0 8px 8px 0; padding: 14px 18px; margin: 12px 20px; }}
  .bug-card .bug-id   {{ font-weight: 700; color: #c0392b; font-size: 0.9rem; }}
  .bug-card .bug-sev  {{ display: inline-block; margin-left: 8px; padding: 1px 8px; border-radius: 8px; font-size: 0.72rem; font-weight: 600; }}
  .sev-high   {{ background: #fde8e8; color: #c0392b; }}
  .sev-medium {{ background: #fef3cd; color: #856404; }}
  .sev-low    {{ background: #e8f4fd; color: #1a5276; }}
  .bug-desc {{ margin-top: 6px; font-size: 0.875rem; color: #555; }}
  .ground-truth-grid {{ display: grid; grid-template-columns: 1fr 1fr; gap: 16px; padding: 16px 20px; }}
  .session-meta {{ padding: 16px 20px; display: grid; grid-template-columns: repeat(3, 1fr); gap: 12px; font-size: 0.85rem; }}
  .meta-item .key {{ font-size: 0.72rem; text-transform: uppercase; letter-spacing:.05em; color: #6c757d; }}
  .meta-item .val {{ margin-top: 2px; }}
  .download-bar {{ text-align: center; padding: 24px; }}
  .btn {{ display: inline-block; padding: 10px 24px; border-radius: 8px; font-size: 0.9rem; font-weight: 600; cursor: pointer; text-decoration: none; border: none; }}
  .btn-primary {{ background: #0d6efd; color: #fff; }}
  .btn-secondary {{ background: #6c757d; color: #fff; margin-left: 12px; }}
  @media(max-width: 768px) {{ .scorecard {{ grid-template-columns: repeat(3,1fr); }} .detail-grid {{ grid-template-columns: 1fr; }} .ground-truth-grid {{ grid-template-columns: 1fr; }} .session-meta {{ grid-template-columns: 1fr 1fr; }} }}
</style>
</head>
<body>

<div class="header">
  <h1>KG Query API — Test Report</h1>
  <div class="meta">
    Generated: {datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")} &nbsp;|&nbsp;
    Environment: {config.graphql_url} &nbsp;|&nbsp;
    Auth: Basic ({config.username})
    {f'<br>Jira: <strong>{config.jira_ticket}</strong>' if config.jira_ticket else ''}
    {f' &nbsp;|&nbsp; PR: <a href="{config.pr_details}" style="color:#89dceb">{config.pr_details}</a>' if config.pr_details else ''}
    {f'<br><br><strong>Jira Details:</strong><pre style="margin-top:8px;background:#0d1117;padding:12px;border-radius:6px;font-size:.78rem;white-space:pre-wrap">{config.jira_summary}</pre>' if config.jira_summary else ''}
    {f'<br><br><strong>PR Analysis:</strong><pre style="margin-top:8px;background:#0d1117;padding:12px;border-radius:6px;font-size:.78rem;white-space:pre-wrap">{config.pr_summary}</pre>' if config.pr_summary else ''}
  </div>
</div>

<div class="container">

<!-- Scorecards -->
<div class="scorecard">
  <div class="score-card">
    <div class="num">{total}</div>
    <div class="lbl">Total Tests</div>
    <div class="progress-bar"><div class="progress-fill" style="width:{pass_pct}%"></div></div>
  </div>
  <div class="score-card">
    <div class="num" style="color:{RESULT_COLOR[RESULT_PASS]}">{passed}</div>
    <div class="lbl">Passed ✅</div>
  </div>
  <div class="score-card">
    <div class="num" style="color:{RESULT_COLOR[RESULT_FAIL]}">{failed}</div>
    <div class="lbl">Failed ❌</div>
  </div>
  <div class="score-card">
    <div class="num" style="color:{RESULT_COLOR[RESULT_WARN]}">{warned}</div>
    <div class="lbl">Warnings ⚠️</div>
  </div>
  <div class="score-card">
    <div class="num" style="color:#c0392b">{len(bugs)}</div>
    <div class="lbl">Bugs Found 🐛</div>
  </div>
</div>

<!-- Session Metadata -->
<section>
  <h2>Session Details</h2>
  <div class="session-meta">
    <div class="meta-item"><div class="key">GraphQL Endpoint</div><div class="val">{config.graphql_url}</div></div>
    <div class="meta-item"><div class="key">REST Endpoint</div><div class="val">{config.rest_base_url}</div></div>
    <div class="meta-item"><div class="key">Asset ID</div><div class="val">{config.asset_id}</div></div>
    <div class="meta-item"><div class="key">Asset Type</div><div class="val">{config.asset_type}</div></div>
    <div class="meta-item"><div class="key">Relation Type</div><div class="val">{config.relation_type}</div></div>
    <div class="meta-item"><div class="key">Target Types</div><div class="val">{', '.join(config.target_types)}</div></div>
    {f'<div class="meta-item"><div class="key">Jira Ticket</div><div class="val">{config.jira_ticket}</div></div>' if config.jira_ticket else ''}
    {f'<div class="meta-item"><div class="key">PR Details</div><div class="val">{config.pr_details}</div></div>' if config.pr_details else ''}
    {f'<div class="meta-item"><div class="key">Notes</div><div class="val">{config.notes}</div></div>' if config.notes else ''}
  </div>
</section>

<!-- Ground Truth -->
{gt_html}

<!-- Summary Table -->
<section>
  <h2>Test Results Summary</h2>
  <table>
    <thead><tr>
      <th>ID</th><th>Test Case</th><th>Category</th><th>Result</th><th>Duration</th><th>Notes</th>
    </tr></thead>
    <tbody>
{rows}
    </tbody>
  </table>
</section>

<!-- Detailed Results -->
<section>
  <h2>Detailed Test Results</h2>
{detail}
</section>

<!-- Bugs -->
{bug_sec}

</div>
</body>
</html>"""


def _build_ground_truth_html(gt: Optional[GroundTruth]) -> str:
    if not gt:
        return ""
    if gt.error:
        return f'<section><h2>REST Ground Truth</h2><div style="padding:16px;color:#c0392b">⚠️ REST unavailable: {gt.error}</div></section>'

    import json
    t = json.dumps(gt.targets_response, indent=2)
    s = json.dumps(gt.sources_response, indent=2)
    return f"""<section>
  <h2>REST API Ground Truth</h2>
  <div class="ground-truth-grid">
    <div>
      <div class="detail-label" style="padding:0 0 6px">GET {gt.targets_url}</div>
      <pre>{t}</pre>
    </div>
    <div>
      <div class="detail-label" style="padding:0 0 6px">GET {gt.sources_url}</div>
      <pre>{s}</pre>
    </div>
  </div>
</section>"""


def _build_result_rows(results: list[TestResult]) -> str:
    rows = []
    for r in results:
        color = RESULT_COLOR.get(r.result, "#555")
        bg    = RESULT_BG.get(r.result, "#fff")
        icon  = RESULT_ICON.get(r.result, "")
        cat_cls = {"POSITIVE": "cat-pos", "NEGATIVE": "cat-neg", "SCHEMA": "cat-sch", "CUSTOM": "cat-custom"}.get(r.category, "cat-sch")
        rows.append(
            f'<tr style="background:{bg}">'
            f'<td><strong>{r.test_id}</strong></td>'
            f'<td>{r.name}</td>'
            f'<td><span class="badge {cat_cls}">{r.category}</span></td>'
            f'<td><strong style="color:{color}">{icon} {r.result}</strong></td>'
            f'<td>{r.duration_ms}ms</td>'
            f'<td style="font-size:0.8rem">{r.validation_notes[:100]}</td>'
            f'</tr>'
        )
    return "\n".join(rows)


def _build_detail_sections(results: list[TestResult]) -> str:
    parts = []
    for r in results:
        color = RESULT_COLOR.get(r.result, "#555")
        icon  = RESULT_ICON.get(r.result, "")
        bug_html = ""
        if r.bug_id:
            sev_cls = f"sev-{r.bug_severity.lower()}" if r.bug_severity else "sev-medium"
            bug_html = f'<div class="bug-card"><span class="bug-id">{r.bug_id}</span><span class="bug-sev {sev_cls}">{r.bug_severity}</span><div class="bug-desc">{r.bug_description}</div></div>'

        parts.append(f"""<details>
  <summary>
    <strong style="color:{color}">{icon} {r.test_id}</strong>
    <span style="color:#555">{r.name}</span>
    <span style="margin-left:auto;font-size:0.75rem;color:#6c757d">{r.duration_ms}ms</span>
  </summary>
  <div class="detail-body">
    <div class="detail-grid">
      <div>
        <div class="detail-label">GraphQL Query (sent)</div>
        <pre>{_esc(r.graphql_query)}</pre>
      </div>
      <div>
        <div class="detail-label">Response</div>
        <pre>{_esc(r.response_raw)}</pre>
      </div>
    </div>
    <div style="margin-top:12px;padding:10px 14px;background:#f8f9fa;border-radius:6px;font-size:0.875rem">
      <strong>Validation:</strong> {r.validation_notes}
    </div>
    {bug_html}
  </div>
</details>""")

    return "\n".join(parts)


def _build_bugs_section(bugs: list[TestResult]) -> str:
    if not bugs:
        return '<section><h2>Bugs Found</h2><div style="padding:20px;color:#1a7f3c">✅ No bugs detected in this run.</div></section>'

    seen_ids: set[str] = set()
    cards = []
    for r in bugs:
        if r.bug_id in seen_ids:
            continue
        seen_ids.add(r.bug_id)
        sev_cls = f"sev-{r.bug_severity.lower()}" if r.bug_severity else "sev-medium"
        cards.append(f"""<div class="bug-card">
  <span class="bug-id">{r.bug_id}</span>
  <span class="bug-sev {sev_cls}">{r.bug_severity}</span>
  <div class="bug-desc" style="margin-top:8px"><strong>Description:</strong> {r.bug_description}</div>
  <div class="bug-desc"><strong>Reproduced by:</strong> {r.test_id} — {r.name}</div>
  <div class="bug-desc"><strong>Actual:</strong> {r.validation_notes}</div>
</div>""")

    return f'<section><h2>Bugs Found 🐛 ({len(seen_ids)})</h2>{"".join(cards)}</section>'


def _esc(s: str) -> str:
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


# ------------------------------------------------------------------ #
# TXT report  (matches the format of the original report)
# ------------------------------------------------------------------ #

def build_txt_report(
    config: RunConfig,
    results: list[TestResult],
    ground_truth: Optional[GroundTruth],
    started_at: str,
) -> str:
    import json
    lines: list[str] = []

    w = "=" * 80
    d = "-" * 80

    lines += [
        w,
        "KG QUERY API — TEST REPORT",
        f"Endpoint : {config.graphql_url}",
        f"REST     : {config.rest_base_url}",
        f"Auth     : Basic ({config.username})",
        f"Date     : {started_at[:10]}",
        f"Asset ID : {config.asset_id}",
        f"AssetType: {config.asset_type}",
        f"RelType  : {config.relation_type}",
        f"Targets  : {', '.join(config.target_types)}",
    ]
    if config.jira_ticket:
        lines.append(f"Jira     : {config.jira_ticket}")
    if config.pr_details:
        lines.append(f"PR       : {config.pr_details}")
    if config.notes:
        lines.append(f"Notes    : {config.notes}")
    lines += [w, ""]

    # Ground truth
    if ground_truth:
        lines += ["REST API GROUND TRUTH", w, ""]
        if ground_truth.error:
            lines += [f"⚠️  REST unavailable: {ground_truth.error}", ""]
        else:
            lines += [
                f"--- REST: Outgoing targets (/targets) ---",
                f"REQUEST:  GET {ground_truth.targets_url}",
                "",
                "RESPONSE:",
                json.dumps(ground_truth.targets_response, indent=2),
                "",
                f"--- REST: Incoming sources (/sources) ---",
                f"REQUEST:  GET {ground_truth.sources_url}",
                "",
                "RESPONSE:",
                json.dumps(ground_truth.sources_response, indent=2),
                "",
            ]

    # Positive tests
    pos = [r for r in results if r.category == "POSITIVE"]
    neg = [r for r in results if r.category == "NEGATIVE"]
    sch = [r for r in results if r.category == "SCHEMA"]

    for label, group in [("POSITIVE TEST CASES", pos), ("NEGATIVE TEST CASES", neg), ("SCHEMA FIELD TESTS", sch)]:
        if not group:
            continue
        lines += [w, label, w, ""]
        for r in group:
            icon = RESULT_ICON.get(r.result, r.result)
            lines += [
                d,
                f"{r.test_id} | {r.name} ({r.result} {icon})",
                d,
                "GraphQL Query:",
                f"  {r.graphql_query}",
                "",
                "Response:",
                r.response_raw,
                "",
                f"Validation: {r.validation_notes}",
            ]
            if r.bug_id:
                lines += ["", f"{r.bug_id} [{r.bug_severity}]: {r.bug_description}"]
            lines.append("")

    # Summary table
    total   = len(results)
    passed  = sum(1 for r in results if r.result == "PASS")
    failed  = sum(1 for r in results if r.result == "FAIL")
    warned  = sum(1 for r in results if r.result == "WARN")
    errored = sum(1 for r in results if r.result == "ERROR")
    bugs    = [r for r in results if r.bug_id]

    lines += [w, "TEST SUMMARY", w, ""]
    col_w = [6, 60, 8]
    header = f"+{'-'*col_w[0]}+{'-'*col_w[1]}+{'-'*col_w[1]}+"
    lines.append(header)
    lines.append(f"| {'TC':<{col_w[0]-2}} | {'Test Case':<{col_w[1]-2}} | {'Result':<{col_w[2]}} |")
    lines.append(header)
    for r in results:
        icon = RESULT_ICON.get(r.result, r.result)
        lines.append(f"| {r.test_id:<{col_w[0]-2}} | {r.name:<{col_w[1]-2}} | {r.result} {icon} |")
    lines.append(header)
    lines += [
        "",
        f"TOTAL: {total} | PASS: {passed} ✅ | WARN: {warned} ⚠️  | FAIL: {failed} ❌ | ERROR: {errored} 🔴",
        "",
    ]

    # Bugs
    if bugs:
        lines += [w, "BUGS FOUND", w, ""]
        seen: set[str] = set()
        for r in bugs:
            if r.bug_id in seen:
                continue
            seen.add(r.bug_id)
            lines += [
                f"[{r.bug_id}] Severity: {r.bug_severity}",
                f"  Description : {r.bug_description}",
                f"  Reproduced  : {r.test_id} — {r.name}",
                f"  Actual      : {r.validation_notes}",
                "",
            ]
    else:
        lines += [w, "BUGS FOUND", w, "", "✅ No bugs detected in this run.", ""]

    lines += [w, "END OF REPORT", w]
    return "\n".join(lines)
