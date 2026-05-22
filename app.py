"""
KG Query API Test Tool — Flask web application.
Run:  python app.py
Open: http://localhost:5001
"""

import json
import os
import queue
import threading
import uuid
from datetime import datetime, timezone

from flask import Flask, Response, jsonify, render_template, request, send_file

from report_builder import build_html_report, build_txt_report
from test_engine import RunConfig, TestRunner

app = Flask(__name__)

# In-memory session store: sid -> {status, results, logs, config, ground_truth, started_at}
SESSIONS: dict[str, dict] = {}

KNOWN_ENVS = {
    "dev-aws":  {
        "graphql": "https://kg-query.dev-aws.cp.collibra-ops.com/graphql",
        "rest":    "https://kg-query.dev-aws.cp.collibra-ops.com/rest/knowledgeGraph/v1",
    },
    "dev-gcp":  {
        "graphql": "https://kg-query.dev-gcp.cp.collibra-ops.com/graphql",
        "rest":    "https://kg-query.dev-gcp.cp.collibra-ops.com/rest/knowledgeGraph/v1",
    },
    "uat-gcp":  {
        "graphql": "https://kg-query-main.uat-gcp.cp.collibra-ops.com/graphql",
        "rest":    "https://kg-query-main.uat-gcp.cp.collibra-ops.com/rest/knowledgeGraph/v1",
    },
    "prod-aws": {
        "graphql": "https://kg-query.prod-aws.cp.collibra-ops.com/graphql",
        "rest":    "https://kg-query.prod-aws.cp.collibra-ops.com/rest/knowledgeGraph/v1",
    },
    "prod-gcp": {
        "graphql": "https://kg-query.prod-gcp.cp.collibra-ops.com/graphql",
        "rest":    "https://kg-query.prod-gcp.cp.collibra-ops.com/rest/knowledgeGraph/v1",
    },
    "custom": {"graphql": "", "rest": ""},
}


@app.route("/")
def index():
    return render_template("index.html", envs=KNOWN_ENVS)


@app.route("/envs")
def envs():
    return jsonify(KNOWN_ENVS)


@app.route("/run", methods=["POST"])
def run_tests():
    data = request.get_json(force=True)

    target_types_raw = data.get("target_types", "")
    target_types = [t.strip() for t in target_types_raw.split(",") if t.strip()]
    if not target_types:
        return jsonify({"error": "At least one target type is required"}), 400

    config = RunConfig(
        graphql_url       = data.get("graphql_url", "").strip(),
        rest_base_url     = data.get("rest_url", "").strip(),
        asset_id          = data.get("asset_id", "").strip(),
        asset_type        = data.get("asset_type", "BusinessTerm").strip(),
        relation_type     = data.get("relation_type", "").strip(),
        target_types      = target_types,
        username          = data.get("username", "").strip(),
        password          = data.get("password", "").strip(),
        tenant_id         = data.get("tenant_id", "").strip(),
        pr_details        = data.get("pr_details", "").strip(),
        jira_ticket       = data.get("jira_ticket", "").strip(),
        jira_token        = data.get("jira_token", "").strip(),
        notes             = data.get("notes", "").strip(),
        custom_payload    = data.get("custom_payload", "").strip(),
        anthropic_api_key = data.get("anthropic_api_key", "").strip() or os.environ.get("ANTHROPIC_API_KEY", ""),
        run_positive      = data.get("run_positive", True),
        run_negative      = data.get("run_negative", True),
        run_schema        = data.get("run_schema", True),
        run_rest_comparison = data.get("run_rest_comparison", True),
    )

    if not config.username or not config.password:
        return jsonify({"error": "Username and Password are required"}), 400

    # Auto-disable REST comparison if REST URL not provided
    if not config.rest_base_url:
        config.run_rest_comparison = False

    # GraphQL URL required unless custom payload provides its own endpoint
    if not config.graphql_url:
        try:
            import json as _json
            cp = _json.loads(config.custom_payload)
            if not cp.get("endpoint", "").strip():
                return jsonify({"error": "GraphQL URL is required (or include 'endpoint' in custom payload)"}), 400
        except Exception:
            return jsonify({"error": "GraphQL URL is required"}), 400

    sid = str(uuid.uuid4())
    log_q: queue.Queue = queue.Queue()

    SESSIONS[sid] = {
        "status":       "running",
        "logs":         [],
        "results":      [],
        "config":       config,
        "ground_truth": None,
        "started_at":   datetime.now(timezone.utc).isoformat(),
        "log_queue":    log_q,
    }

    def run():
        try:
            def progress(msg: str):
                SESSIONS[sid]["logs"].append(msg)
                log_q.put(msg)

            runner = TestRunner(config, progress_callback=progress)
            results = runner.run()

            SESSIONS[sid]["results"]      = results
            SESSIONS[sid]["ground_truth"] = runner.ground_truth
            SESSIONS[sid]["status"]       = "done"
            log_q.put("__DONE__")
        except Exception as exc:
            SESSIONS[sid]["status"] = "error"
            SESSIONS[sid]["logs"].append(f"FATAL: {exc}")
            log_q.put(f"FATAL: {exc}")
            log_q.put("__DONE__")

    threading.Thread(target=run, daemon=True).start()
    return jsonify({"session_id": sid})


@app.route("/stream/<sid>")
def stream(sid: str):
    """Server-Sent Events stream for live log output."""
    session = SESSIONS.get(sid)
    if not session:
        return jsonify({"error": "Session not found"}), 404

    def generate():
        log_q: queue.Queue = session["log_queue"]
        while True:
            try:
                msg = log_q.get(timeout=60)
                if msg == "__DONE__":
                    yield f"data: __DONE__\n\n"
                    break
                yield f"data: {msg}\n\n"
            except queue.Empty:
                yield f"data: __KEEPALIVE__\n\n"

    return Response(generate(), mimetype="text/event-stream",
                    headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@app.route("/status/<sid>")
def status(sid: str):
    session = SESSIONS.get(sid)
    if not session:
        return jsonify({"error": "not found"}), 404

    results = session.get("results", [])
    summary = {
        "status":  session["status"],
        "total":   len(results),
        "passed":  sum(1 for r in results if r.result == "PASS"),
        "failed":  sum(1 for r in results if r.result == "FAIL"),
        "warned":  sum(1 for r in results if r.result == "WARN"),
        "errored": sum(1 for r in results if r.result == "ERROR"),
        "bugs":    sum(1 for r in results if r.bug_id),
        "logs":    session["logs"][-50:],
    }
    return jsonify(summary)


@app.route("/download/html/<sid>")
def download_html(sid: str):
    session = SESSIONS.get(sid)
    if not session or session["status"] != "done":
        return jsonify({"error": "Not ready"}), 404

    html = build_html_report(
        session["config"],
        session["results"],
        session["ground_truth"],
        session["started_at"],
    )
    tmp = f"/tmp/kg-report-{sid}.html"
    with open(tmp, "w") as f:
        f.write(html)
    return send_file(tmp, as_attachment=True,
                     download_name=f"kg-test-report-{session['started_at'][:10]}.html",
                     mimetype="text/html")


@app.route("/download/txt/<sid>")
def download_txt(sid: str):
    session = SESSIONS.get(sid)
    if not session or session["status"] != "done":
        return jsonify({"error": "Not ready"}), 404

    txt = build_txt_report(
        session["config"],
        session["results"],
        session["ground_truth"],
        session["started_at"],
    )
    tmp = f"/tmp/kg-report-{sid}.txt"
    with open(tmp, "w") as f:
        f.write(txt)
    return send_file(tmp, as_attachment=True,
                     download_name=f"kg-test-report-{session['started_at'][:10]}.txt",
                     mimetype="text/plain")


if __name__ == "__main__":
    app.run(debug=True, port=5001, threaded=True)
