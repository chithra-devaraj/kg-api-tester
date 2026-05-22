"""
Test case generation and execution for KG Query API.
Auto-generates positive + negative GraphQL test cases from user-supplied config,
validates against REST ground truth, and classifies each result.
"""

import json
import re
import subprocess
import requests
from dataclasses import dataclass, field
from typing import Any, Optional
from datetime import datetime, timezone


RESULT_PASS  = "PASS"
RESULT_FAIL  = "FAIL"
RESULT_WARN  = "WARN"
RESULT_ERROR = "ERROR"
RESULT_SKIP  = "SKIP"


@dataclass
class TestResult:
    test_id: str
    name: str
    category: str          # POSITIVE / NEGATIVE / SCHEMA
    graphql_query: str
    request_body: str
    response_raw: str
    status_code: int
    result: str            # PASS / FAIL / WARN / ERROR / SKIP
    validation_notes: str
    bug_id: str = ""
    bug_severity: str = ""
    bug_description: str = ""
    duration_ms: int = 0


@dataclass
class GroundTruth:
    targets_url: str
    targets_response: dict
    sources_url: str
    sources_response: dict
    error: str = ""


@dataclass
class RunConfig:
    graphql_url: str
    rest_base_url: str
    asset_id: str
    asset_type: str
    relation_type: str
    target_types: list[str]
    username: str
    password: str
    tenant_id: str = ""
    pr_details: str = ""
    jira_ticket: str = ""
    jira_token: str = ""
    notes: str = ""
    custom_payload: str = ""
    pr_summary: str = ""
    jira_summary: str = ""
    anthropic_api_key: str = ""
    run_positive: bool = True
    run_negative: bool = True
    run_schema: bool = True
    run_rest_comparison: bool = True


class TestRunner:

    def __init__(self, config: RunConfig, progress_callback=None):
        self.config = config
        self.progress = progress_callback or (lambda msg: None)
        self.results: list[TestResult] = []
        self.ground_truth: Optional[GroundTruth] = None
        self.started_at = datetime.now(timezone.utc).isoformat()

    # ------------------------------------------------------------------ #
    # PR / Jira context fetch
    # ------------------------------------------------------------------ #

    def _fetch_pr_summary(self) -> str:
        pr = self.config.pr_details.strip()
        if not pr:
            return ""
        m = re.search(r'github\.com/([^/]+/[^/]+)/pull/(\d+)', pr)
        if not m:
            return f"PR ref: {pr}"
        repo, num = m.group(1), m.group(2)
        try:
            view = subprocess.run(
                ["gh", "pr", "view", num, "--repo", repo,
                 "--json", "title,state,author,body,additions,deletions,changedFiles,files"],
                capture_output=True, text=True, timeout=15
            )
            diff = subprocess.run(
                ["gh", "pr", "diff", num, "--repo", repo],
                capture_output=True, text=True, timeout=30
            )
            if view.returncode != 0:
                return f"(gh pr view failed: {view.stderr.strip()})"
            data = json.loads(view.stdout)
            changed_files = [f.get("path", "") for f in data.get("files", [])]
            files_str = "\n  ".join(changed_files[:20]) or "(none)"
            diff_snippet = diff.stdout[:3000] if diff.returncode == 0 else "(diff unavailable)"
            return (
                f"Title:   {data.get('title','')}\n"
                f"State:   {data.get('state','')}  |  Author: {data.get('author',{}).get('login','')}\n"
                f"Changes: +{data.get('additions',0)} -{data.get('deletions',0)} across {data.get('changedFiles',0)} files\n\n"
                f"Files changed:\n  {files_str}\n\n"
                f"Description:\n{(data.get('body','') or '(no description)')[:800]}\n\n"
                f"Diff (first 3000 chars):\n{diff_snippet}"
            )
        except Exception as e:
            return f"(could not fetch PR via gh: {e})"

    def _fetch_jira_summary(self) -> str:
        ticket = self.config.jira_ticket.strip()
        token  = self.config.jira_token.strip()
        if not ticket:
            return ""
        if not token:
            return f"Ticket: {ticket} — add Jira API token to fetch details"
        base = "https://engineering-collibra.atlassian.net"
        try:
            headers = {"Authorization": f"Bearer {token}", "Accept": "application/json"}
            for api_ver in ("3", "2"):
                url = f"{base}/rest/api/{api_ver}/issue/{ticket}"
                r = requests.get(url, headers=headers, timeout=10, verify=False)
                if r.status_code == 200:
                    break
            if r.status_code != 200:
                return f"Ticket: {ticket} (Jira returned {r.status_code})"
            d        = r.json()
            fields   = d.get("fields", {})
            summary  = fields.get("summary", "")
            status   = fields.get("status", {}).get("name", "")
            assignee = (fields.get("assignee") or {}).get("displayName", "Unassigned")
            priority = (fields.get("priority") or {}).get("name", "")
            raw_desc = fields.get("description", "") or ""
            if isinstance(raw_desc, dict):
                parts = []
                for block in raw_desc.get("content", []):
                    for inline in block.get("content", []):
                        if inline.get("type") == "text":
                            parts.append(inline.get("text", ""))
                description = " ".join(parts)
            else:
                description = str(raw_desc)
            return (
                f"Ticket:   {ticket}\n"
                f"Summary:  {summary}\n"
                f"Status:   {status}  |  Priority: {priority}  |  Assignee: {assignee}\n\n"
                f"Description:\n{description[:2000]}"
            )
        except Exception as e:
            return f"Ticket: {ticket} (fetch error: {e})"

    # ------------------------------------------------------------------ #
    # HTTP helpers
    # ------------------------------------------------------------------ #

    def _auth(self):
        return (self.config.username, self.config.password)

    def _headers(self):
        h = {"Content-Type": "application/json", "Accept": "application/json"}
        if self.config.tenant_id:
            h["x-tenant-environment"] = self.config.tenant_id
        return h

    def _graphql(self, query: str, endpoint: str = "") -> tuple[dict, int, int]:
        """Execute a GraphQL query. Returns (parsed_response, status_code, duration_ms)."""
        import time
        body = json.dumps({"query": query})
        url = endpoint or self.config.graphql_url
        t0 = time.monotonic()
        try:
            r = requests.post(
                url,
                data=body,
                headers=self._headers(),
                auth=self._auth(),
                timeout=30,
                verify=False,
            )
            ms = int((time.monotonic() - t0) * 1000)
            try:
                return r.json(), r.status_code, ms
            except Exception:
                return {"raw": r.text}, r.status_code, ms
        except requests.RequestException as exc:
            ms = int((time.monotonic() - t0) * 1000)
            return {"connection_error": str(exc)}, 0, ms

    def _rest_get(self, url: str) -> tuple[dict, int]:
        try:
            r = requests.get(url, headers=self._headers(), auth=self._auth(), timeout=30, verify=False)
            try:
                return r.json(), r.status_code
            except Exception:
                return {"raw": r.text}, r.status_code
        except requests.RequestException as exc:
            return {"connection_error": str(exc)}, 0

    # ------------------------------------------------------------------ #
    # REST ground truth
    # ------------------------------------------------------------------ #

    def fetch_ground_truth(self) -> GroundTruth:
        base = self.config.rest_base_url.rstrip("/")
        asset = self.config.asset_id
        rt = self.config.relation_type

        targets_url = f"{base}/assets/{asset}/relations/{rt}/targets"
        sources_url = f"{base}/assets/{asset}/relations/{rt}/sources"

        self.progress(f"Fetching REST ground truth — targets ...")
        t_resp, _ = self._rest_get(targets_url)

        self.progress(f"Fetching REST ground truth — sources ...")
        s_resp, _ = self._rest_get(sources_url)

        gt = GroundTruth(
            targets_url=targets_url,
            targets_response=t_resp,
            sources_url=sources_url,
            sources_response=s_resp,
        )
        if "connection_error" in t_resp:
            gt.error = t_resp["connection_error"]
        self.ground_truth = gt
        return gt

    # ------------------------------------------------------------------ #
    # Test case generation — payload-driven, PR-driven, notes-driven
    # ------------------------------------------------------------------ #

    def _all_target_types_str(self) -> str:
        return json.dumps(self.config.target_types) if self.config.target_types else '["BusinessTerm"]'

    def _first_target_type(self) -> str:
        return self.config.target_types[0] if self.config.target_types else "BusinessTerm"

    # ---- Query parser --------------------------------------------------

    def _parse_query(self, query: str) -> dict:
        """Extract structural info from a GraphQL query string."""
        q = query.strip()
        root_m  = re.search(r'\b(assets|outgoingRelations|incomingRelations)\b', q)
        type_m  = re.search(r'\btype\s*:\s*\[?"(\w+)"', q)
        rt_m    = re.search(r'\brelationType\s*:\s*"([^"]+)"', q)
        tt_m    = re.findall(r'\btargetType\s*:\s*\["(\w+)"', q)
        lim_m   = re.search(r'\blimit\s*:\s*(\d+)', q)
        off_m   = re.search(r'\boffset\s*:\s*(\d+)', q)
        sel_fields = list(set(re.findall(
            r'\b(id|displayName|fullName|publicId|name|createdOn|modifiedOn|value|values)\b', q
        )))
        return {
            "root":          root_m.group(1) if root_m else "assets",
            "asset_type":    type_m.group(1) if type_m else (self.config.asset_type or "BusinessTerm"),
            "relation_type": rt_m.group(1)   if rt_m   else self.config.relation_type,
            "target_types":  tt_m or self.config.target_types or ["BusinessTerm"],
            "limit":         int(lim_m.group(1)) if lim_m else None,
            "offset":        int(off_m.group(1)) if off_m else None,
            "has_order":     bool(re.search(r'\border\s*:', q)),
            "has_where":     bool(re.search(r'\bwhere\s*:', q)),
            "has_relations": bool(re.search(r'\b(out|in)goingRelations\b', q)),
            "sel_fields":    sel_fields,
        }

    # ---- Query builders ------------------------------------------------

    def _q(self, at: str, fields: str, extra_args: str = "") -> str:
        args = f'type: ["{at}"]'
        if extra_args:
            args += f", {extra_args}"
        return f'{{ assets({args}) {{ {fields} }} }}'

    def _q_asset(self, at: str, aid: str, fields: str, extra_args: str = "") -> str:
        args = f'type: ["{at}"], where: {{ id: {{ eq: "{aid}" }} }}'
        if extra_args:
            args += f", {extra_args}"
        return f'{{ assets({args}) {{ {fields} }} }}'

    def _q_rel(self, at: str, aid: str, rt: str, tt: str, rel_fields: str, extra_rel_args: str = "") -> str:
        rel_args = f'relationType: "{rt}", targetType: {tt}'
        if extra_rel_args:
            rel_args += f", {extra_rel_args}"
        return f'{{ assets(type: ["{at}"], where: {{ id: {{ eq: "{aid}" }} }}) {{ id displayName outgoingRelations({rel_args}) {{ target {{ {rel_fields} }} }} }} }}'

    # ---- Query modification helpers ------------------------------------

    def _bracket_end(self, s: str, start: int, open_c: str, close_c: str) -> int:
        """Find the matching close bracket, respecting string literals and nesting."""
        depth = 0
        i = start
        in_str = False
        while i < len(s):
            c = s[i]
            if in_str:
                if c == '\\':
                    i += 2
                    continue
                if c == '"':
                    in_str = False
            else:
                if c == '"':
                    in_str = True
                elif c == open_c:
                    depth += 1
                elif c == close_c:
                    depth -= 1
                    if depth == 0:
                        return i
            i += 1
        return len(s) - 1

    def _get_root_field_bounds(self, query: str) -> dict:
        """
        Finds the root field (first field inside the operation braces) and returns
        positional bounds: name_end, args_start, args_end, sel_start, sel_end.
        Returns {} on parse failure.
        """
        q = query
        op_brace = q.find('{')
        if op_brace == -1:
            return {}
        op_close = self._bracket_end(q, op_brace, '{', '}')

        pos = op_brace + 1
        while pos < op_close and q[pos] in ' \t\n\r':
            pos += 1

        name_start = pos
        while pos < op_close and (q[pos].isalnum() or q[pos] == '_'):
            pos += 1
        name_end = pos

        if name_start == name_end:
            return {}

        while pos < op_close and q[pos] in ' \t\n\r':
            pos += 1

        bounds: dict = {"name_end": name_end, "args_start": -1, "args_end": -1,
                        "sel_start": -1, "sel_end": -1}

        if pos < op_close and q[pos] == '(':
            bounds["args_start"] = pos
            bounds["args_end"] = self._bracket_end(q, pos, '(', ')')
            pos = bounds["args_end"] + 1
            while pos < op_close and q[pos] in ' \t\n\r':
                pos += 1

        if pos < op_close and q[pos] == '{':
            bounds["sel_start"] = pos
            bounds["sel_end"] = self._bracket_end(q, pos, '{', '}')

        return bounds

    def _add_fields_to_query(self, query: str, new_fields: str) -> str:
        """Add fields to the root field's selection set (just before closing })."""
        b = self._get_root_field_bounds(query)
        if b.get("sel_end", -1) == -1:
            return query
        i = b["sel_end"]
        return query[:i] + ' ' + new_fields + ' ' + query[i:]

    def _add_args_to_query(self, query: str, new_args: str) -> str:
        """Append arguments to the root field.  Inserts (args) if none exist."""
        b = self._get_root_field_bounds(query)
        if not b:
            return query
        if b["args_start"] == -1:
            i = b["name_end"]
            return query[:i] + f'({new_args})' + query[i:]
        else:
            i = b["args_end"]
            return query[:i] + f', {new_args}' + query[i:]

    # ---- AI-driven generation ------------------------------------------

    def _generate_cases_via_ai(self, pq: dict, raw_query: str, schema_fields: list) -> list[dict]:
        """Call Claude API with all context to generate test cases intelligently."""
        api_key = self.config.anthropic_api_key.strip()
        if not api_key:
            return []

        at  = pq["asset_type"]
        rt  = pq["relation_type"] or ""
        tt  = json.dumps(pq["target_types"])
        aid = self.config.asset_id

        schema_lines = "\n".join(
            f"  {f['name']}: {self._resolve_gql_type(f.get('type', {}))}"
            for f in schema_fields
        ) or "  (introspection unavailable)"

        prompt = f"""You are a GraphQL API tester for the Knowledge Graph Query API.

Analyze the inputs below and generate a comprehensive, variable-length test suite.
The NUMBER of tests should depend on what you find: more PR changes → more targeted tests,
richer schema → more field tests, specific notes → tests for those things.

━━ BASE QUERY (user's payload — all tests MUST start from this) ━━
{raw_query}

━━ SCHEMA FIELDS on {at} ━━
{schema_lines}

━━ CONFIGURATION ━━
Asset Type : {at}
Asset ID   : {aid or "(not provided)"}
Rel Type   : {rt or "(not provided)"}
Target Types: {tt}

━━ PR CODE CHANGES ━━
{(self.config.pr_summary or "(no PR provided)")[:3000]}

━━ JIRA TICKET ━━
{(self.config.jira_summary or "(no Jira ticket provided)")[:1500]}

━━ NOTES / TESTING PROMPT ━━
{self.config.notes or "(no notes provided)"}

━━ INSTRUCTIONS ━━
1. TC-01 MUST be the exact base query unchanged.
2. Every other test MUST modify the base query by adding fields or args — never write a completely different query.
3. For EACH schema field not already in the base query: add a test that includes it.
4. For EACH field/feature detected in the PR diff: add a targeted test.
5. For ANYTHING in the Notes: treat it as a test prompt and generate those tests.
6. Always include: pagination, ordering (by real schema fields), where filter (if assetId given), outgoing + incoming relations (if relationType given).
7. Negative tests: undefined field, object without subselection, limit:-1, offset:-1, order by non-existent field, outgoingRelations without relationType, empty targetType [].
8. Return ONLY a valid JSON array — no markdown, no explanation:

[
  {{
    "id": "TC-01",
    "name": "Exact payload (as provided)",
    "category": "custom",
    "query": "<exact base query>",
    "expect_error": false
  }},
  ...
]

category must be one of: "custom", "positive", "negative"
"""
        try:
            import anthropic
            client = anthropic.Anthropic(api_key=api_key)
            self.progress("Calling Claude API to generate test cases from your inputs ...")
            msg = client.messages.create(
                model="claude-haiku-4-5-20251001",
                max_tokens=6000,
                messages=[{"role": "user", "content": prompt}],
            )
            raw_resp = msg.content[0].text.strip()

            # Extract JSON array from response
            m = re.search(r'\[.*\]', raw_resp, re.DOTALL)
            if not m:
                self.progress("WARN: Claude response did not contain a JSON array — falling back")
                return []

            ai_cases = json.loads(m.group())
            self.progress(f"Claude generated {len(ai_cases)} test cases based on your inputs")

            cases = []
            for tc in ai_cases:
                query = tc.get("query", "").strip()
                if not query:
                    continue
                expect_err = tc.get("expect_error", False)
                cat = tc.get("category", "positive")
                c: dict = {
                    "id":       tc.get("id", f"AI-{len(cases)+1:02d}"),
                    "name":     tc.get("name", "AI generated test"),
                    "category": cat,
                    "query":    query,
                    "validator": self._validate_has_error if expect_err else self._validate_data_no_errors,
                }
                if tc.get("id", "").endswith("01") and cat == "custom":
                    c["original_input"] = query
                cases.append(c)
            return cases

        except Exception as exc:
            self.progress(f"WARN: Claude API call failed ({exc}) — falling back to schema-driven generation")
            return []

    # ---- Payload-driven generation (fallback when no API key) ----------

    def _generate_payload_variations(self, pq: dict, raw_query: str) -> list[dict]:
        at  = pq["asset_type"]
        rt  = pq["relation_type"] or ""
        tt  = json.dumps(pq["target_types"])
        tt0 = json.dumps([pq["target_types"][0]])
        aid = self.config.asset_id

        # All variations are derived from the user's base query.
        # Fall back to a minimal query only when no custom payload was provided.
        base = raw_query.strip() if raw_query.strip() else self._q(at, "id displayName", "limit: 10")

        cases: list[dict] = []
        idx = 1
        n   = 1

        def pos(cid, name, query, validator=None, bug=None):
            return {"id": cid, "name": name, "category": "positive", "query": query,
                    "validator": validator or self._validate_data_no_errors,
                    **({"bug_candidate": bug} if bug else {})}

        def neg(cid, name, query, validator=None, bug=None):
            return {"id": cid, "name": name, "category": "negative", "query": query,
                    "validator": validator or self._validate_has_error,
                    **({"bug_candidate": bug} if bug else {})}

        # TC-01: exact payload
        cases.append({"id": f"TC-{idx:02d}", "name": "Exact payload (as provided)",
                       "category": "custom", "query": base, "original_input": base,
                       "validator": self._validate_data_no_errors})
        idx += 1

        # TC-02: add missing scalar fields
        scalar_adds = [f for f in ["displayName", "fullName", "createdOn", "modifiedOn"] if f not in base]
        if scalar_adds:
            cases.append(pos(f"TC-{idx:02d}",
                             f"Payload + scalar fields ({', '.join(scalar_adds)})",
                             self._add_fields_to_query(base, " ".join(scalar_adds))))
        idx += 1

        # TC-03: user audit fields
        cases.append(pos(f"TC-{idx:02d}", "Payload + createdBy { id name } + modifiedBy { id name }",
                         self._add_fields_to_query(base, "createdBy { id name } modifiedBy { id name }")))
        idx += 1

        # TC-04: type + status
        cases.append(pos(f"TC-{idx:02d}", "Payload + type { publicId } + status { name }",
                         self._add_fields_to_query(base, "type { publicId } status { name }")))
        idx += 1

        # TC-05: domain with community
        cases.append(pos(f"TC-{idx:02d}", "Payload + domain { id name community { id name } }",
                         self._add_fields_to_query(base, "domain { id name community { id name } }")))
        idx += 1

        # TC-06: string attributes
        cases.append(pos(f"TC-{idx:02d}", "Payload + stringAttributes { id value }",
                         self._add_fields_to_query(base, "stringAttributes { id value }")))
        idx += 1

        # TC-07: all attribute types
        cases.append(pos(f"TC-{idx:02d}", "Payload + all attribute types (numeric/boolean/date/multivalue)",
                         self._add_fields_to_query(base,
                             "numericAttributes { id value } booleanAttributes { id value } "
                             "dateAttributes { id value } multiValueAttributes { id values }")))
        idx += 1

        # TC-08: responsibilities + tags
        cases.append(pos(f"TC-{idx:02d}", "Payload + allResponsibilities + tags",
                         self._add_fields_to_query(base,
                             "allResponsibilities { role { id publicId } } tags { id name }")))
        idx += 1

        # TC-09..TC-14: ordering variants
        for order_name, order_arg in [
            ("Order by displayName asc",    "order: { displayName: asc }"),
            ("Order by displayName desc",   "order: { displayName: desc }"),
            ("Order by domain.name asc",    "order: { domain: { name: asc } }"),
            ("Order by createdOn desc",     "order: { createdOn: desc }"),
            ("Order by type.publicId desc", "order: { type: { publicId: desc } }"),
            ("Order by status.id asc",      "order: { status: { id: asc } }"),
        ]:
            cases.append(pos(f"TC-{idx:02d}", f"Payload + {order_name}",
                             self._add_args_to_query(base, f"{order_arg}, limit: 10")))
            idx += 1

        # TC-15: pagination limit:5 offset:0
        cases.append(pos(f"TC-{idx:02d}", "Payload + pagination limit:5 offset:0",
                         self._add_args_to_query(base, "limit: 5, offset: 0")))
        idx += 1

        # TC-16: pagination page 2
        cases.append(pos(f"TC-{idx:02d}", "Payload + pagination page 2 (limit:5 offset:5)",
                         self._add_args_to_query(base, "limit: 5, offset: 5")))
        idx += 1

        # TC-17: limit:0
        cases.append(pos(f"TC-{idx:02d}", "Payload + limit:0 (empty result edge case)",
                         self._add_args_to_query(base, "limit: 0")))
        idx += 1

        # TC-18: where filter by asset_id
        if aid and not pq["has_where"]:
            cases.append(pos(f"TC-{idx:02d}", "Payload + where filter by asset id",
                             self._add_args_to_query(base, f'where: {{ id: {{ eq: "{aid}" }} }}')))
            idx += 1

        # TC-19/TC-20: outgoing + incoming relations
        if aid and rt:
            base_w = base if pq["has_where"] else \
                self._add_args_to_query(base, f'where: {{ id: {{ eq: "{aid}" }} }}')
            cases.append(pos(f"TC-{idx:02d}", f"Payload + outgoingRelations ({rt})",
                             self._add_fields_to_query(base_w,
                                 f'outgoingRelations(relationType: "{rt}", targetType: {tt}) '
                                 f'{{ target {{ id displayName }} }}'),
                             validator=self._validate_rest_targets_subset))
            idx += 1
            cases.append(pos(f"TC-{idx:02d}", f"Payload + incomingRelations ({rt})",
                             self._add_fields_to_query(base_w,
                                 f'incomingRelations(relationType: "{rt}", targetType: {tt}) '
                                 f'{{ target {{ id displayName }} }}')))
            idx += 1

        # TC-21: deep AssetType hierarchy
        cases.append(pos(f"TC-{idx:02d}", "Payload + deep AssetType hierarchy",
                         self._add_fields_to_query(base,
                             "type { publicId name parentType { publicId name parentType { publicId } } }")))
        idx += 1

        # TC-22: combined fields
        cases.append(pos(f"TC-{idx:02d}", "Payload + combined (scalars + domain + type + status)",
                         self._add_fields_to_query(base,
                             "fullName createdOn modifiedOn domain { id name } type { publicId } status { name }")))

        # ---- Negative cases (all derived from base query) ---------------

        # TC-N01: undefined field
        cases.append(neg(f"TC-N{n:02d}", "Negative: undefined field __nonExistentField__",
                         self._add_fields_to_query(base, "__nonExistentField__"),
                         validator=self._validate_field_undefined_error))
        n += 1

        # TC-N02: object field without subselection
        cases.append(neg(f"TC-N{n:02d}", "Negative: object field 'status' without subselection",
                         self._add_fields_to_query(base, "status")))
        n += 1

        # TC-N03: empty type []
        if re.search(r'\btype\s*:', base):
            q_n3 = re.sub(r'type\s*:\s*\[[^\]]*\]', 'type: []', base, count=1)
        else:
            q_n3 = self._add_args_to_query(base, "type: []")
        cases.append(neg(f"TC-N{n:02d}", "Negative: empty type [] → expect empty result",
                         q_n3, validator=self._validate_data_no_errors))
        n += 1

        # TC-N04: limit:-1
        cases.append(neg(f"TC-N{n:02d}", "Negative: limit:-1 → expect validation error",
                         self._add_args_to_query(base, "limit: -1"),
                         bug={"id": "BUG-NEG-LIMIT", "severity": "Low",
                              "desc": "limit:-1 silently ignored — no ValidationError raised"}))
        n += 1

        # TC-N05: offset:-1
        cases.append(neg(f"TC-N{n:02d}", "Negative: offset:-1 → expect validation error",
                         self._add_args_to_query(base, "offset: -1")))
        n += 1

        # TC-N06: order by non-existent field
        cases.append(neg(f"TC-N{n:02d}", "Negative: order by non-existent field",
                         self._add_args_to_query(base, "order: { nonExistentField: asc }")))
        n += 1

        # TC-N07: status { nonExistentStatusField }
        cases.append(neg(f"TC-N{n:02d}", "Negative: status { nonExistentStatusField }",
                         self._add_fields_to_query(base, "status { nonExistentStatusField }"),
                         validator=self._validate_field_undefined_error))
        n += 1

        # TC-N08: outgoingRelations without relationType
        if aid:
            cases.append(neg(f"TC-N{n:02d}", "Negative: outgoingRelations missing relationType",
                             self._add_fields_to_query(base,
                                 f'outgoingRelations(targetType: {tt0}) {{ target {{ id }} }}')))
            n += 1

        # TC-N09: outgoingRelations with empty targetType []
        if aid and rt:
            cases.append(neg(f"TC-N{n:02d}", "Negative: outgoingRelations targetType: []",
                             self._add_fields_to_query(base,
                                 f'outgoingRelations(relationType: "{rt}", targetType: []) '
                                 f'{{ target {{ id }} }}'),
                             validator=self._validate_empty_target_type,
                             bug={"id": "BUG-EMPTY-TT", "severity": "Medium",
                                  "desc": "Empty targetType [] causes unhandled DataFetchingException"}))

        return cases

    # ---- PR diff-driven generation ------------------------------------

    def _generate_from_pr_diff(self) -> list[dict]:
        if not self.config.pr_summary:
            return []
        diff_start = self.config.pr_summary.find("Diff")
        diff = self.config.pr_summary[diff_start:] if diff_start != -1 else self.config.pr_summary

        at  = self.config.asset_type or "BusinessTerm"
        aid = self.config.asset_id
        rt  = self.config.relation_type or ""
        tt  = self._all_target_types_str()

        # Extract added field/method names from diff + lines
        added_lines = [l[1:] for l in diff.split("\n") if l.startswith("+") and not l.startswith("+++")]
        added_text  = " ".join(added_lines)
        added_words = set(re.findall(r'\b([a-z][a-zA-Z]{2,})\b', added_text))

        # Map KG-known fields to targeted test queries
        field_query_map = {
            "domain":              self._q(at, "id displayName domain { id name }", "limit: 3"),
            "community":           self._q(at, "id displayName domain { id name community { id name } }", "limit: 3"),
            "status":              self._q(at, "id displayName status { id name }", "limit: 3"),
            "tags":                self._q(at, "id displayName tags { id name }", "limit: 3"),
            "allResponsibilities": self._q(at, "id displayName allResponsibilities { role { id publicId } }", "limit: 3"),
            "stringAttributes":    self._q(at, "id displayName stringAttributes { id value }", "limit: 3"),
            "numericAttributes":   self._q(at, "id displayName numericAttributes { id value }", "limit: 3"),
            "booleanAttributes":   self._q(at, "id displayName booleanAttributes { id value }", "limit: 3"),
            "dateAttributes":      self._q(at, "id displayName dateAttributes { id value }", "limit: 3"),
            "outgoingRelations":   self._q_rel(at, aid, rt, tt, "id displayName") if aid and rt else None,
            "incomingRelations":   f'{{ assets(type: ["{at}"], where: {{ id: {{ eq: "{aid}" }} }}) {{ id incomingRelations(relationType: "{rt}", targetType: {tt}) {{ target {{ id displayName }} }} }} }}' if aid and rt else None,
            "relationType":        self._q_rel(at, aid, rt, tt, "id displayName") if aid and rt else None,
            "targetType":          self._q_rel(at, aid, rt, tt, "id displayName") if aid and rt else None,
        }

        cases = []
        pr_idx = 1
        for field, query in field_query_map.items():
            if field in added_words and query:
                cases.append({
                    "id":       f"PR-{pr_idx:02d}",
                    "name":     f"PR-targeted: '{field}' detected in diff",
                    "category": "pr_targeted",
                    "query":    query,
                    "validator": self._validate_data_no_errors,
                })
                pr_idx += 1

        if not cases:
            self.progress("PR diff analyzed — no directly mappable KG fields detected; running payload variations only")

        return cases

    # ---- Notes/prompt-driven generation --------------------------------

    def _generate_from_notes(self, pq: dict) -> list[dict]:
        notes = self.config.notes.strip().lower()
        if not notes:
            return []

        at  = pq["asset_type"]
        aid = self.config.asset_id
        rt  = pq["relation_type"] or self.config.relation_type or ""
        tt  = json.dumps(pq["target_types"])
        cases = []
        idx = 1

        def add(name, query, cat="notes", validator=None):
            cases.append({"id": f"NOTE-{idx:02d}", "name": f"Notes: {name}", "category": cat,
                           "query": query, "validator": validator or self._validate_data_no_errors})

        keyword_tests = {
            "pagina":   [("pagination limit:3 offset:0",  self._q(at, "id displayName", "limit: 3, offset: 0")),
                         ("pagination limit:3 offset:3",  self._q(at, "id displayName", "limit: 3, offset: 3"))],
            "order":    [("order asc by displayName",     self._q(at, "id displayName", "order: { displayName: asc }, limit: 5")),
                         ("order desc by displayName",    self._q(at, "id displayName", "order: { displayName: desc }, limit: 5"))],
            "domain":   [("domain { id name }",           self._q(at, "id displayName domain { id name }", "limit: 3")),
                         ("domain.community { name }",    self._q(at, "id domain { id name community { id name } }", "limit: 3"))],
            "relation": [("outgoingRelations" if rt else None,
                          self._q_rel(at, aid, rt, tt, "id displayName") if aid and rt else None)],
            "status":   [("status { id name }",           self._q(at, "id displayName status { id name }", "limit: 3"))],
            "tag":      [("tags { id name }",             self._q(at, "id displayName tags { id name }", "limit: 3"))],
            "attribute":[ ("stringAttributes",             self._q(at, "id displayName stringAttributes { id value }", "limit: 3")),
                          ("numericAttributes",            self._q(at, "id displayName numericAttributes { id value }", "limit: 3"))],
            "responsib":[("allResponsibilities { role }",  self._q(at, "id displayName allResponsibilities { role { id publicId } }", "limit: 3"))],
            "schema":   [("all scalar fields",             self._q(at, "id displayName fullName createdOn modifiedOn", "limit: 3"))],
            "negative": [("invalid field → expect error", self._q(at, "id __invalidField__", "limit: 1"))],
            "empty":    [("empty targetType []" if rt and aid else None,
                          f'{{ assets(type: ["{at}"], where: {{ id: {{ eq: "{aid}" }} }}) {{ id outgoingRelations(relationType: "{rt}", targetType: []) {{ target {{ id }} }} }} }}' if rt and aid else None)],
        }

        for keyword, tests in keyword_tests.items():
            if keyword in notes:
                for item in tests:
                    if item[0] and item[1]:
                        add(item[0], item[1])
                        idx += 1

        return cases

    # ---- Schema introspection cases ------------------------------------

    def _UNUSED_generate_positive_cases(self) -> list[dict]:
        at  = self.config.asset_type
        aid = self.config.asset_id
        rt  = self.config.relation_type
        all_tt = self._all_target_types_str()
        tt0 = json.dumps([self._first_target_type()])
        tt1 = json.dumps([self.config.target_types[1]]) if len(self.config.target_types) > 1 else None

        if not aid or not rt:
            self.progress("SKIP: Positive cases require Asset ID and Relation Type — skipping P-xx suite")
            return []

        cases = []

        # P-01 Single first targetType
        cases.append({
            "id": "P-01", "name": f"outgoingRelations — targetType {self._first_target_type()}",
            "query": f'{{ assets(type: ["{at}"], where: {{ id: {{ eq: "{aid}" }} }}) {{ id displayName outgoingRelations(relationType: "{rt}", targetType: {tt0}) {{ target {{ id displayName fullName }} }} }} }}',
            "expected": "outgoingRelations list, validated against REST /targets",
            "validator": self._validate_rest_targets_subset,
        })

        # P-02 Second targetType (if provided)
        if tt1:
            cases.append({
                "id": "P-02", "name": f"outgoingRelations — targetType {self.config.target_types[1]}",
                "query": f'{{ assets(type: ["{at}"], where: {{ id: {{ eq: "{aid}" }} }}) {{ id displayName outgoingRelations(relationType: "{rt}", targetType: {tt1}) {{ target {{ id displayName fullName }} }} }} }}',
                "expected": "outgoingRelations list for second type",
                "validator": self._validate_non_empty_data,
            })

        # P-03 All targetTypes
        cases.append({
            "id": "P-03", "name": f"outgoingRelations — all targetTypes {all_tt}",
            "query": f'{{ assets(type: ["{at}"], where: {{ id: {{ eq: "{aid}" }} }}) {{ id displayName outgoingRelations(relationType: "{rt}", targetType: {all_tt}) {{ target {{ id displayName fullName }} }} }} }}',
            "expected": "All targets from REST /targets",
            "validator": self._validate_rest_targets_full,
        })

        # P-04 Pagination limit:1 offset:0
        cases.append({
            "id": "P-04", "name": "Pagination limit:1 offset:0",
            "query": f'{{ assets(type: ["{at}"], where: {{ id: {{ eq: "{aid}" }} }}) {{ id displayName outgoingRelations(relationType: "{rt}", targetType: {all_tt}, limit: 1, offset: 0) {{ target {{ id displayName }} }} }} }}',
            "expected": "Exactly 1 result",
            "validator": self._validate_exactly_one_relation,
        })

        # P-05 Pagination limit:2 offset:1
        cases.append({
            "id": "P-05", "name": "Pagination limit:2 offset:1",
            "query": f'{{ assets(type: ["{at}"], where: {{ id: {{ eq: "{aid}" }} }}) {{ id displayName outgoingRelations(relationType: "{rt}", targetType: {all_tt}, limit: 2, offset: 1) {{ target {{ id displayName }} }} }} }}',
            "expected": "At most 2 results, skipping first",
            "validator": self._validate_at_most_two_relations,
        })

        # P-06 limit:0
        cases.append({
            "id": "P-06", "name": "limit:0 on relations (edge case)",
            "query": f'{{ assets(type: ["{at}"], where: {{ id: {{ eq: "{aid}" }} }}) {{ id displayName outgoingRelations(relationType: "{rt}", targetType: {all_tt}, limit: 0) {{ target {{ id displayName }} }} }} }}',
            "expected": "Empty array []",
            "validator": self._validate_empty_relations,
        })

        # P-07 Order asc
        cases.append({
            "id": "P-07", "name": "Order by target displayName asc",
            "query": f'{{ assets(type: ["{at}"], where: {{ id: {{ eq: "{aid}" }} }}) {{ id displayName outgoingRelations(relationType: "{rt}", targetType: {all_tt}, order: [{{ target: [{{ displayName: asc }}] }}]) {{ target {{ id displayName }} }} }} }}',
            "expected": "Results sorted A-Z",
            "validator": self._validate_sorted_asc,
        })

        # P-08 Order desc
        cases.append({
            "id": "P-08", "name": "Order by target displayName desc",
            "query": f'{{ assets(type: ["{at}"], where: {{ id: {{ eq: "{aid}" }} }}) {{ id displayName outgoingRelations(relationType: "{rt}", targetType: {all_tt}, order: [{{ target: [{{ displayName: desc }}] }}]) {{ target {{ id displayName }} }} }} }}',
            "expected": "Results sorted Z-A",
            "validator": self._validate_sorted_desc,
        })

        # P-09 where filter
        cases.append({
            "id": "P-09", "name": "where filter on target displayName (contains)",
            "query": f'{{ assets(type: ["{at}"], where: {{ id: {{ eq: "{aid}" }} }}) {{ id displayName outgoingRelations(relationType: "{rt}", targetType: {all_tt}, where: {{ target: {{ displayName: {{ contains: "a" }} }} }}) {{ target {{ id displayName }} }} }} }}',
            "expected": "Only targets whose displayName contains 'a'",
            "validator": self._validate_where_filter,
        })

        # P-10 incomingRelations
        cases.append({
            "id": "P-10", "name": f"incomingRelations — all targetTypes",
            "query": f'{{ assets(type: ["{at}"], where: {{ id: {{ eq: "{aid}" }} }}) {{ id displayName incomingRelations(relationType: "{rt}", targetType: {all_tt}) {{ target {{ id displayName fullName }} }} }} }}',
            "expected": "incomingRelations list, validated against REST /sources",
            "validator": self._validate_rest_sources,
        })

        # P-11 Both outgoing + incoming
        cases.append({
            "id": "P-11", "name": "Both outgoingRelations + incomingRelations in same query",
            "query": f'{{ assets(type: ["{at}"], where: {{ id: {{ eq: "{aid}" }} }}) {{ id displayName outgoingRelations(relationType: "{rt}", targetType: {all_tt}) {{ target {{ id displayName }} }} incomingRelations(relationType: "{rt}", targetType: {all_tt}) {{ target {{ id displayName }} }} }} }}',
            "expected": "Both fields present in response",
            "validator": self._validate_both_directions,
        })

        # P-12 Filter assets WITH relations (empty:false)
        cases.append({
            "id": "P-12", "name": "Filter assets WITH relations (empty:false)",
            "query": f'{{ assets(type: ["{at}"], where: {{ outgoingRelations: {{ relationType: "{rt}", targetType: {tt0}, empty: false }} }}, limit: 5) {{ id displayName }} }}',
            "expected": "Only assets that have matching outgoing relations",
            "validator": self._validate_data_no_errors,
        })

        # P-13 Filter assets WITHOUT relations (empty:true)
        cases.append({
            "id": "P-13", "name": "Filter assets WITHOUT relations (empty:true)",
            "query": f'{{ assets(type: ["{at}"], where: {{ outgoingRelations: {{ relationType: "{rt}", targetType: {tt0}, empty: true }} }}, limit: 5) {{ id displayName }} }}',
            "expected": "Only assets without matching outgoing relations",
            "validator": self._validate_data_no_errors,
        })

        # P-14 Nested 2 levels deep
        cases.append({
            "id": "P-14", "name": "Nested outgoingRelations — 2 levels deep",
            "query": f'{{ assets(type: ["{at}"], where: {{ id: {{ eq: "{aid}" }} }}) {{ id displayName outgoingRelations(relationType: "{rt}", targetType: {all_tt}) {{ target {{ id displayName outgoingRelations(relationType: "{rt}", targetType: {all_tt}) {{ target {{ id displayName }} }} }} }} }} }}',
            "expected": "Nested relations returned, no server error",
            "validator": self._validate_data_no_errors,
        })

        # P-15 maxRepetitionDepth:2
        cases.append({
            "id": "P-15", "name": "maxRepetitionDepth:2",
            "query": f'{{ assets(type: ["{at}"], where: {{ id: {{ eq: "{aid}" }} }}) {{ id displayName outgoingRelations(relationType: "{rt}", targetType: {all_tt}, maxRepetitionDepth: 2) {{ target {{ id displayName }} }} }} }}',
            "expected": "No duplicate targets (WARN if duplicates found)",
            "validator": self._validate_no_duplicates,
        })

        # P-16 Large result set
        cases.append({
            "id": "P-16", "name": "Large result set limit:1000",
            "query": f'{{ assets(type: ["{at}"], limit: 1000) {{ id displayName outgoingRelations(relationType: "{rt}", targetType: {all_tt}) {{ target {{ id displayName }} }} }} }}',
            "expected": "Up to 1000 assets returned without server error",
            "validator": self._validate_data_no_errors,
        })

        return cases

    def _UNUSED_generate_negative_cases(self) -> list[dict]:
        at  = self.config.asset_type
        aid = self.config.asset_id
        rt  = self.config.relation_type
        all_tt = self._all_target_types_str()

        if not aid or not rt:
            self.progress("SKIP: Negative cases require Asset ID and Relation Type — skipping N-xx suite")
            return []

        return [
            {
                "id": "N-01", "name": "Missing relationType argument",
                "query": f'{{ assets(type: ["{at}"], limit: 3) {{ id displayName outgoingRelations(targetType: ["{self._first_target_type()}"]) {{ target {{ id displayName }} }} }} }}',
                "expected": "ValidationError: Missing field argument 'relationType'",
                "validator": self._validate_missing_arg_error,
                "extra": {"expected_error_fragment": "relationType"},
            },
            {
                "id": "N-02", "name": "Missing targetType argument",
                "query": f'{{ assets(type: ["{at}"], limit: 3) {{ id displayName outgoingRelations(relationType: "{rt}") {{ target {{ id displayName }} }} }} }}',
                "expected": "ValidationError: Missing field argument 'targetType'",
                "validator": self._validate_missing_arg_error,
                "extra": {"expected_error_fragment": "targetType"},
            },
            {
                "id": "N-03", "name": "Invalid relationType + invalid targetType — single asset",
                "query": f'{{ assets(type: ["{at}"], where: {{ id: {{ eq: "{aid}" }} }}) {{ id displayName outgoingRelations(relationType: "INVALID_TYPE", targetType: ["INVALID_TARGET"]) {{ target {{ id displayName }} }} }} }}',
                "expected": "Empty outgoingRelations [] — no server error",
                "validator": self._validate_empty_relations,
            },
            {
                "id": "N-04", "name": "Invalid relationType + invalid targetType — limit:1000",
                "query": f'{{ assets(type: ["{at}"], limit: 1000) {{ id displayName outgoingRelations(relationType: "INVALID_TYPE", targetType: ["INVALID_TARGET"]) {{ target {{ id displayName }} }} }} }}',
                "expected": "All assets return outgoingRelations:[] — no error",
                "validator": self._validate_data_no_errors,
            },
            {
                "id": "N-05", "name": "Negative limit:-1 on relations",
                "query": f'{{ assets(type: ["{at}"], where: {{ id: {{ eq: "{aid}" }} }}) {{ id displayName outgoingRelations(relationType: "{rt}", targetType: {all_tt}, limit: -1) {{ target {{ id displayName }} }} }} }}',
                "expected": "ValidationError rejecting limit < 0",
                "validator": self._validate_negative_limit,
                "bug_candidate": {"id": "BUG-NEG-LIMIT", "severity": "Medium", "desc": "Negative limit:-1 causes DataFetchingException instead of ValidationError"},
            },
            {
                "id": "N-06", "name": "Very large offset:9999 (beyond data)",
                "query": f'{{ assets(type: ["{at}"], where: {{ id: {{ eq: "{aid}" }} }}) {{ id displayName outgoingRelations(relationType: "{rt}", targetType: {all_tt}, offset: 9999) {{ target {{ id displayName }} }} }} }}',
                "expected": "Empty outgoingRelations [] gracefully",
                "validator": self._validate_empty_relations,
            },
            {
                "id": "N-07a", "name": "assetType { publicId name } on relation target",
                "query": f'{{ assets(type: ["{at}"], where: {{ id: {{ eq: "{aid}" }} }}) {{ id displayName outgoingRelations(relationType: "{rt}", targetType: {all_tt}) {{ target {{ id displayName assetType {{ publicId name }} }} }} }} }}',
                "expected": "assetType returned — known BUG-1 if DataFetchingException",
                "validator": self._validate_relationship_backed_field,
                "bug_candidate": {"id": "BUG-1", "severity": "High", "desc": "assetType (HAS_ASSET_TYPE) causes DataFetchingException on relation target"},
            },
            {
                "id": "N-07b", "name": "domain { id name } on relation target",
                "query": f'{{ assets(type: ["{at}"], where: {{ id: {{ eq: "{aid}" }} }}) {{ id displayName outgoingRelations(relationType: "{rt}", targetType: {all_tt}) {{ target {{ id displayName domain {{ id name }} }} }} }} }}',
                "expected": "domain returned — known BUG-1 if DataFetchingException",
                "validator": self._validate_relationship_backed_field,
                "bug_candidate": {"id": "BUG-1", "severity": "High", "desc": "domain (IS_PART_OF) causes DataFetchingException on relation target"},
            },
            {
                "id": "N-07c", "name": "allResponsibilities { role { id } } on relation target",
                "query": f'{{ assets(type: ["{at}"], where: {{ id: {{ eq: "{aid}" }} }}) {{ id displayName outgoingRelations(relationType: "{rt}", targetType: {all_tt}) {{ target {{ id displayName allResponsibilities {{ role {{ id }} }} }} }} }} }}',
                "expected": "allResponsibilities returned — known BUG-1 if DataFetchingException",
                "validator": self._validate_relationship_backed_field,
                "bug_candidate": {"id": "BUG-1", "severity": "High", "desc": "allResponsibilities (HAS_RESOURCE recursive) causes DataFetchingException on relation target"},
            },
            {
                "id": "N-08", "name": "status { id } on relation target",
                "query": f'{{ assets(type: ["{at}"], where: {{ id: {{ eq: "{aid}" }} }}) {{ id displayName outgoingRelations(relationType: "{rt}", targetType: {all_tt}) {{ target {{ id displayName status {{ id }} }} }} }} }}',
                "expected": "status returned correctly (embedded field — should PASS)",
                "validator": self._validate_data_no_errors,
            },
            {
                "id": "N-09", "name": "Non-existent field on relation target",
                "query": f'{{ assets(type: ["{at}"], where: {{ id: {{ eq: "{aid}" }} }}) {{ id outgoingRelations(relationType: "{rt}", targetType: {all_tt}) {{ target {{ id nonExistentField }} }} }} }}',
                "expected": "ValidationError: Field 'nonExistentField' in type 'Asset' is undefined",
                "validator": self._validate_field_undefined_error,
            },
            {
                "id": "N-10", "name": "Negative offset:-1 on relations",
                "query": f'{{ assets(type: ["{at}"], where: {{ id: {{ eq: "{aid}" }} }}) {{ id displayName outgoingRelations(relationType: "{rt}", targetType: {all_tt}, offset: -1) {{ target {{ id displayName }} }} }} }}',
                "expected": "ValidationError rejecting offset < 0",
                "validator": self._validate_negative_offset,
                "bug_candidate": {"id": "BUG-NEG-OFFSET", "severity": "Low", "desc": "offset:-1 silently treated as offset=0 — no ValidationError raised"},
            },
            {
                "id": "N-11", "name": "Empty targetType [] array",
                "query": f'{{ assets(type: ["{at}"], where: {{ id: {{ eq: "{aid}" }} }}) {{ id displayName outgoingRelations(relationType: "{rt}", targetType: []) {{ target {{ id displayName }} }} }} }}',
                "expected": "Empty [] result or clear ValidationError — NOT DataFetchingException",
                "validator": self._validate_empty_target_type,
                "bug_candidate": {"id": "BUG-EMPTY-TT", "severity": "Medium", "desc": "Empty targetType [] causes unhandled DataFetchingException"},
            },
        ]

    def _introspect_type(self, type_name: str) -> list[dict]:
        """Run introspection on a GraphQL type, return list of field dicts."""
        query = (
            f'{{ __type(name: "{type_name}") {{ '
            f'fields {{ name args {{ name type {{ kind name ofType {{ kind name }} }} }} '
            f'type {{ kind name ofType {{ kind name ofType {{ kind name }} }} }} }} }} }}'
        )
        resp, status, _ = self._graphql(query)
        if status != 200 or resp.get("errors") or not resp.get("data", {}).get("__type"):
            return []
        return resp["data"]["__type"].get("fields") or []

    def _resolve_gql_type(self, t: dict) -> str:
        """Unwrap NON_NULL/LIST wrappers to get the base named type."""
        while t and t.get("name") is None:
            t = t.get("ofType") or {}
        return (t or {}).get("name", "")

    def _field_selection(self, field: dict) -> str:
        """Return a minimal field selection string for a given introspected field."""
        base = self._resolve_gql_type(field.get("type", {}))
        kind = field.get("type", {}).get("kind", "")
        # Unwrap NON_NULL
        if kind == "NON_NULL":
            kind = (field["type"].get("ofType") or {}).get("kind", "")
        name = field["name"]
        # Relations need mandatory args — skip inline, handled separately
        if name in ("outgoingRelations", "incomingRelations"):
            return ""
        # Scalars / enums — select directly
        if kind in ("SCALAR", "ENUM") or base in ("String", "Boolean", "Int", "Float", "ID"):
            return name
        # Object / list of objects — select id sub-field
        return f"{name} {{ id }}"

    def _generate_schema_cases(self) -> list[dict]:
        at     = self.config.asset_type or "BusinessTerm"
        aid    = self.config.asset_id
        rt     = self.config.relation_type
        all_tt = self._all_target_types_str()
        tt0    = json.dumps([self._first_target_type()])

        self.progress("Introspecting GraphQL schema for Asset type ...")
        asset_fields = self._introspect_type("Asset")

        if not asset_fields:
            self.progress("WARN: Introspection unavailable — using built-in field list")
            asset_fields = [
                {"name": "id",          "type": {"kind": "SCALAR",  "name": "ID",     "ofType": None}, "args": []},
                {"name": "displayName", "type": {"kind": "SCALAR",  "name": "String",  "ofType": None}, "args": []},
                {"name": "fullName",    "type": {"kind": "SCALAR",  "name": "String",  "ofType": None}, "args": []},
                {"name": "createdOn",   "type": {"kind": "SCALAR",  "name": "String",  "ofType": None}, "args": []},
                {"name": "modifiedOn",  "type": {"kind": "SCALAR",  "name": "String",  "ofType": None}, "args": []},
                {"name": "status",      "type": {"kind": "OBJECT",  "name": "Status",  "ofType": None}, "args": []},
                {"name": "type",        "type": {"kind": "OBJECT",  "name": "AssetType","ofType": None}, "args": []},
                {"name": "domain",      "type": {"kind": "OBJECT",  "name": "Domain",  "ofType": None}, "args": []},
                {"name": "createdBy",   "type": {"kind": "OBJECT",  "name": "User",    "ofType": None}, "args": []},
                {"name": "modifiedBy",  "type": {"kind": "OBJECT",  "name": "User",    "ofType": None}, "args": []},
                {"name": "tags",        "type": {"kind": "LIST",    "name": None,      "ofType": {"kind":"OBJECT","name":"Tag","ofType":None}}, "args": []},
                {"name": "stringAttributes",   "type": {"kind": "LIST", "name": None, "ofType": {"kind":"OBJECT","name":"StringAttribute","ofType":None}}, "args": []},
                {"name": "numericAttributes",  "type": {"kind": "LIST", "name": None, "ofType": {"kind":"OBJECT","name":"NumericAttribute","ofType":None}}, "args": []},
                {"name": "booleanAttributes",  "type": {"kind": "LIST", "name": None, "ofType": {"kind":"OBJECT","name":"BooleanAttribute","ofType":None}}, "args": []},
                {"name": "dateAttributes",     "type": {"kind": "LIST", "name": None, "ofType": {"kind":"OBJECT","name":"DateAttribute","ofType":None}}, "args": []},
                {"name": "allResponsibilities","type": {"kind": "LIST", "name": None, "ofType": {"kind":"OBJECT","name":"Responsibility","ofType":None}}, "args": []},
            ]

        self.progress(f"Schema: {len(asset_fields)} fields found on Asset — generating S-xx cases ...")

        cases = []
        idx   = 1

        # S-xx: one case per discoverable field on Asset (as top-level + as relation target)
        for field in asset_fields:
            sel = self._field_selection(field)
            if not sel:
                continue

            # Positive: field directly on assets()
            cases.append({
                "id":       f"S-{idx:02d}a",
                "name":     f"Asset field: {field['name']} (direct)",
                "category": "schema",
                "query":    f'{{ assets(type: ["{at}"], limit: 1) {{ id {sel} }} }}',
                "validator": self._validate_data_no_errors,
            })

            # Positive: same field on outgoingRelations target (only if relation data available)
            if aid and rt:
                cases.append({
                    "id":       f"S-{idx:02d}b",
                    "name":     f"Asset field: {field['name']} (relation target)",
                    "category": "schema",
                    "query":    f'{{ assets(type: ["{at}"], where: {{ id: {{ eq: "{aid}" }} }}) {{ id outgoingRelations(relationType: "{rt}", targetType: {all_tt}) {{ target {{ id {sel} }} }} }} }}',
                    "validator": self._validate_data_no_errors,
                })
            idx += 1

        # Positive: relation fields with correct args
        if aid and rt:
            cases.append({
                "id": f"S-{idx:02d}", "name": "outgoingRelations with relationType + targetType",
                "category": "schema",
                "query": f'{{ assets(type: ["{at}"], where: {{ id: {{ eq: "{aid}" }} }}) {{ id outgoingRelations(relationType: "{rt}", targetType: {all_tt}) {{ target {{ id displayName }} }} }} }}',
                "validator": self._validate_data_no_errors,
            })
            idx += 1
            cases.append({
                "id": f"S-{idx:02d}", "name": "incomingRelations with relationType + targetType",
                "category": "schema",
                "query": f'{{ assets(type: ["{at}"], where: {{ id: {{ eq: "{aid}" }} }}) {{ id incomingRelations(relationType: "{rt}", targetType: {all_tt}) {{ target {{ id displayName }} }} }} }}',
                "validator": self._validate_data_no_errors,
            })
            idx += 1

        # Negative: non-existent field
        cases.append({
            "id": f"S-{idx:02d}", "name": "Schema: non-existent field __badField__",
            "category": "schema",
            "query": f'{{ assets(type: ["{at}"], limit: 1) {{ id __badField__ }} }}',
            "validator": self._validate_field_undefined_error,
        })
        idx += 1

        # Negative: invalid type argument
        cases.append({
            "id": f"S-{idx:02d}", "name": "Schema: invalid type name returns empty",
            "category": "schema",
            "query": f'{{ assets(type: ["__NonExistentType__"], limit: 1) {{ id displayName }} }}',
            "validator": self._validate_data_no_errors,
        })
        idx += 1

        # Negative: missing required arg on outgoingRelations
        if aid:
            cases.append({
                "id": f"S-{idx:02d}", "name": "Schema: outgoingRelations missing relationType arg",
                "category": "schema",
                "query": f'{{ assets(type: ["{at}"], where: {{ id: {{ eq: "{aid}" }} }}) {{ id outgoingRelations(targetType: {tt0}) {{ target {{ id }} }} }} }}',
                "validator": self._validate_has_error,
            })
            idx += 1

        # Positive: all scalar fields in one query
        scalar_sels = [f["name"] for f in asset_fields
                       if self._resolve_gql_type(f.get("type", {})) in ("String", "ID", "Boolean", "Int", "Float")
                       or f.get("type", {}).get("kind") == "SCALAR"]
        if scalar_sels:
            cases.append({
                "id": f"S-{idx:02d}", "name": "Schema: all scalar fields in one query",
                "category": "schema",
                "query": f'{{ assets(type: ["{at}"], limit: 1) {{ {" ".join(scalar_sels[:10])} }} }}',
                "validator": self._validate_data_no_errors,
            })

        return cases

    # ------------------------------------------------------------------ #
    # Validators
    # ------------------------------------------------------------------ #

    def _has_data_error(self, resp: dict) -> bool:
        return bool(resp.get("errors"))

    def _is_connection_error(self, resp: dict) -> bool:
        return "connection_error" in resp

    def _get_relations(self, resp: dict, direction: str = "outgoingRelations") -> list:
        try:
            return resp["data"]["assets"][0][direction]
        except (KeyError, IndexError, TypeError):
            return []

    def _validate_data_no_errors(self, resp, _case, _gt) -> tuple[str, str]:
        if self._is_connection_error(resp):
            return RESULT_ERROR, f"Connection error: {resp['connection_error']}"
        if self._has_data_error(resp):
            return RESULT_FAIL, f"Unexpected server error: {resp['errors'][0].get('message', '')}"
        return RESULT_PASS, "No errors, data returned correctly"

    def _validate_non_empty_data(self, resp, _case, _gt) -> tuple[str, str]:
        result, notes = self._validate_data_no_errors(resp, _case, _gt)
        if result != RESULT_PASS:
            return result, notes
        rels = self._get_relations(resp)
        if not rels:
            return RESULT_WARN, "No relations returned — may be expected if no data of this type"
        return RESULT_PASS, f"{len(rels)} relation(s) returned"

    def _validate_rest_targets_subset(self, resp, _case, gt) -> tuple[str, str]:
        if self._has_data_error(resp):
            return RESULT_FAIL, f"GraphQL error: {resp['errors'][0].get('message', '')}"
        rels = self._get_relations(resp)
        if gt and gt.targets_response and "results" in gt.targets_response and not gt.error:
            rest_ids = {r["id"] for r in gt.targets_response["results"]}
            gql_ids  = {r["target"]["id"] for r in rels if "target" in r}
            extra    = gql_ids - rest_ids
            if extra:
                return RESULT_FAIL, f"GraphQL returned IDs not in REST: {extra}"
            return RESULT_PASS, f"{len(rels)} results — all IDs present in REST /targets"
        return RESULT_PASS, f"{len(rels)} relations returned (no REST comparison available)"

    def _validate_rest_targets_full(self, resp, _case, gt) -> tuple[str, str]:
        if self._has_data_error(resp):
            return RESULT_FAIL, f"GraphQL error: {resp['errors'][0].get('message', '')}"
        rels = self._get_relations(resp)
        if gt and gt.targets_response and "results" in gt.targets_response and not gt.error:
            rest_ids = {r["id"] for r in gt.targets_response["results"]}
            gql_ids  = {r["target"]["id"] for r in rels if "target" in r}
            missing  = rest_ids - gql_ids
            extra    = gql_ids  - rest_ids
            if missing:
                return RESULT_FAIL, f"Missing IDs vs REST: {missing}"
            if extra:
                return RESULT_FAIL, f"Extra IDs vs REST: {extra}"
            return RESULT_PASS, f"All {len(rest_ids)} REST targets match GraphQL"
        return RESULT_PASS, f"{len(rels)} relations returned (no REST comparison)"

    def _validate_rest_sources(self, resp, _case, gt) -> tuple[str, str]:
        if self._has_data_error(resp):
            return RESULT_FAIL, f"GraphQL error: {resp['errors'][0].get('message', '')}"
        rels = self._get_relations(resp, "incomingRelations")
        if gt and gt.sources_response and "results" in gt.sources_response and not gt.error:
            rest_ids = {r["id"] for r in gt.sources_response["results"]}
            gql_ids  = {r["target"]["id"] for r in rels if "target" in r}
            if rest_ids != gql_ids:
                return RESULT_FAIL, f"Mismatch with REST /sources. Missing: {rest_ids - gql_ids}  Extra: {gql_ids - rest_ids}"
            return RESULT_PASS, f"All {len(rest_ids)} REST sources match GraphQL incomingRelations"
        return RESULT_PASS, f"{len(rels)} incomingRelations returned"

    def _validate_exactly_one_relation(self, resp, _case, _gt) -> tuple[str, str]:
        if self._has_data_error(resp):
            return RESULT_FAIL, f"GraphQL error: {resp['errors'][0].get('message', '')}"
        rels = self._get_relations(resp)
        if len(rels) != 1:
            return RESULT_FAIL, f"Expected 1 result, got {len(rels)}"
        return RESULT_PASS, "limit:1 — exactly 1 result returned"

    def _validate_at_most_two_relations(self, resp, _case, _gt) -> tuple[str, str]:
        if self._has_data_error(resp):
            return RESULT_FAIL, f"GraphQL error: {resp['errors'][0].get('message', '')}"
        rels = self._get_relations(resp)
        if len(rels) > 2:
            return RESULT_FAIL, f"Expected at most 2 results, got {len(rels)}"
        return RESULT_PASS, f"limit:2 offset:1 — {len(rels)} result(s) returned"

    def _validate_empty_relations(self, resp, _case, _gt) -> tuple[str, str]:
        if self._has_data_error(resp):
            return RESULT_FAIL, f"Unexpected error: {resp['errors'][0].get('message', '')}"
        rels = self._get_relations(resp)
        if rels:
            return RESULT_FAIL, f"Expected empty [], got {len(rels)} results"
        return RESULT_PASS, "Empty array returned gracefully"

    def _validate_sorted_asc(self, resp, _case, _gt) -> tuple[str, str]:
        if self._has_data_error(resp):
            return RESULT_FAIL, f"GraphQL error: {resp['errors'][0].get('message', '')}"
        rels = self._get_relations(resp)
        names = [r["target"].get("displayName", "") for r in rels if "target" in r]
        if names != sorted(names, key=str.casefold):
            return RESULT_FAIL, f"Not sorted asc: {names}"
        return RESULT_PASS, f"Sorted A→Z correctly: {names}"

    def _validate_sorted_desc(self, resp, _case, _gt) -> tuple[str, str]:
        if self._has_data_error(resp):
            return RESULT_FAIL, f"GraphQL error: {resp['errors'][0].get('message', '')}"
        rels = self._get_relations(resp)
        names = [r["target"].get("displayName", "") for r in rels if "target" in r]
        if names != sorted(names, key=str.casefold, reverse=True):
            return RESULT_FAIL, f"Not sorted desc: {names}"
        return RESULT_PASS, f"Sorted Z→A correctly: {names}"

    def _validate_where_filter(self, resp, _case, _gt) -> tuple[str, str]:
        if self._has_data_error(resp):
            return RESULT_FAIL, f"GraphQL error: {resp['errors'][0].get('message', '')}"
        rels = self._get_relations(resp)
        non_matching = [r["target"]["displayName"] for r in rels if "a" not in r.get("target", {}).get("displayName", "").lower()]
        if non_matching:
            return RESULT_FAIL, f"Filter not applied — names without 'a': {non_matching}"
        return RESULT_PASS, f"Filter contains:'a' applied correctly, {len(rels)} result(s)"

    def _validate_both_directions(self, resp, _case, _gt) -> tuple[str, str]:
        if self._has_data_error(resp):
            return RESULT_FAIL, f"GraphQL error: {resp['errors'][0].get('message', '')}"
        try:
            asset = resp["data"]["assets"][0]
            has_out = "outgoingRelations" in asset
            has_in  = "incomingRelations" in asset
            if not (has_out and has_in):
                return RESULT_FAIL, "Missing outgoing or incoming field in response"
            return RESULT_PASS, f"Both directions present — {len(asset['outgoingRelations'])} outgoing, {len(asset['incomingRelations'])} incoming"
        except (KeyError, IndexError):
            return RESULT_FAIL, "Unexpected response structure"

    def _validate_missing_arg_error(self, resp, case, _gt) -> tuple[str, str]:
        errors = resp.get("errors", [])
        if not errors:
            return RESULT_FAIL, "Expected ValidationError but got no errors"
        msg = errors[0].get("message", "")
        fragment = case.get("extra", {}).get("expected_error_fragment", "")
        if "MissingFieldArgument" in msg or fragment in msg:
            return RESULT_PASS, f"Correct ValidationError: {msg}"
        return RESULT_FAIL, f"Unexpected error message: {msg}"

    def _validate_negative_limit(self, resp, case, _gt) -> tuple[str, str]:
        if self._has_data_error(resp):
            errors = resp.get("errors", [])
            msg = errors[0].get("message", "") if errors else ""
            if "DataFetchingException" in msg or "DataFetchingException" in str(errors):
                bug = case.get("bug_candidate", {})
                return RESULT_FAIL, f"BUG: {bug.get('desc', 'DataFetchingException on limit:-1')}. Expected: ValidationError"
            if "ValidationError" in msg or "Validation error" in msg:
                return RESULT_PASS, f"Correct ValidationError for limit:-1"
            return RESULT_FAIL, f"Unexpected error: {msg}"
        return RESULT_FAIL, "No error raised for limit:-1 — input validation missing"

    def _validate_negative_offset(self, resp, case, _gt) -> tuple[str, str]:
        if self._has_data_error(resp):
            errors = resp.get("errors", [])
            msg = errors[0].get("message", "") if errors else ""
            if "ValidationError" in msg or "Validation error" in msg:
                return RESULT_PASS, "Correct ValidationError for offset:-1"
            return RESULT_FAIL, f"Unexpected error: {msg}"
        rels = self._get_relations(resp)
        bug = case.get("bug_candidate", {})
        return RESULT_FAIL, f"BUG: {bug.get('desc', 'offset:-1 silently treated as 0')} — {len(rels)} results returned"

    def _validate_relationship_backed_field(self, resp, case, _gt) -> tuple[str, str]:
        if self._is_connection_error(resp):
            return RESULT_ERROR, f"Connection error: {resp['connection_error']}"
        if self._has_data_error(resp):
            errors = resp.get("errors", [])
            msg = errors[0].get("message", "") if errors else ""
            bug = case.get("bug_candidate", {})
            return RESULT_FAIL, f"BUG: {bug.get('desc', 'DataFetchingException on relationship-backed field')}"
        return RESULT_PASS, "Relationship-backed field returned correctly (bug may be fixed)"

    def _validate_field_undefined_error(self, resp, _case, _gt) -> tuple[str, str]:
        errors = resp.get("errors", [])
        if not errors:
            return RESULT_FAIL, "Expected ValidationError for undefined field but got no errors"
        msg = errors[0].get("message", "")
        if "FieldUndefined" in msg or "is undefined" in msg:
            return RESULT_PASS, f"Correct ValidationError: {msg}"
        return RESULT_FAIL, f"Unexpected error: {msg}"

    def _validate_has_error(self, resp, _case, _gt) -> tuple[str, str]:
        errors = resp.get("errors", [])
        if errors:
            return RESULT_PASS, f"Expected error returned: {errors[0].get('message','')[:120]}"
        return RESULT_FAIL, "Expected an error response but got none"

    def _validate_empty_target_type(self, resp, case, _gt) -> tuple[str, str]:
        if self._has_data_error(resp):
            errors = resp.get("errors", [])
            msg = errors[0].get("message", "") if errors else ""
            if "DataFetchingException" in msg or "data fetching" in msg.lower():
                bug = case.get("bug_candidate", {})
                return RESULT_FAIL, f"BUG: {bug.get('desc', 'Empty targetType [] causes DataFetchingException')}"
            if "ValidationError" in msg or "Validation error" in msg:
                return RESULT_PASS, f"Acceptable ValidationError for empty targetType: {msg}"
        rels = self._get_relations(resp)
        if rels == []:
            return RESULT_PASS, "Empty targetType [] returns empty relations — acceptable"
        return RESULT_FAIL, f"Unexpected: {len(rels)} relations returned for empty targetType"

    def _validate_no_duplicates(self, resp, _case, _gt) -> tuple[str, str]:
        if self._has_data_error(resp):
            return RESULT_FAIL, f"GraphQL error: {resp['errors'][0].get('message', '')}"
        rels = self._get_relations(resp)
        ids = [r["target"]["id"] for r in rels if "target" in r]
        if len(ids) != len(set(ids)):
            dupes = [i for i in set(ids) if ids.count(i) > 1]
            return RESULT_WARN, f"Duplicate targets found: {dupes} — maxRepetitionDepth behaviour needs investigation"
        return RESULT_PASS, f"{len(ids)} unique targets — no duplicates"

    def _generate_custom_payload_case(self) -> list[dict]:
        raw = self.config.custom_payload.strip()
        endpoint_override = ""
        query = raw  # default: use exactly as typed
        try:
            parsed = json.loads(raw)
            # If JSON with a "query" key, extract it; preserve raw for display
            if isinstance(parsed, dict) and "query" in parsed:
                query = parsed["query"]
            endpoint_override = parsed.get("endpoint", "").strip() if isinstance(parsed, dict) else ""
        except Exception:
            pass  # raw GraphQL string — use as-is
        return [{
            "id":                "CUSTOM-01",
            "name":              "Custom user-supplied payload",
            "category":          "custom",
            "query":             query,
            "original_input":    raw,       # preserved verbatim for report display
            "endpoint_override": endpoint_override,
            "validator":         self._validate_data_no_errors,
        }]

    # ------------------------------------------------------------------ #
    # Main run loop
    # ------------------------------------------------------------------ #

    def run(self) -> list[TestResult]:
        import urllib3
        import base64
        urllib3.disable_warnings()

        computed = base64.b64encode(f"{self.config.username}:{self.config.password}".encode()).decode()
        self.progress(f"Auth header: Basic {computed}")
        self.progress(f"Tenant header: x-tenant-environment: {self.config.tenant_id}")
        self.progress(f"GraphQL URL: {self.config.graphql_url}")

        if self.config.pr_details.strip():
            self.progress("Fetching PR details via gh CLI ...")
            self.config.pr_summary = self._fetch_pr_summary()
            if self.config.pr_summary:
                self.progress(f"PR: {self.config.pr_summary.splitlines()[0]}")

        if self.config.jira_ticket.strip():
            self.progress("Fetching Jira ticket details ...")
            self.config.jira_summary = self._fetch_jira_summary()
            self.progress(f"Jira: {self.config.jira_summary.splitlines()[0]}")

        if self.config.run_rest_comparison:
            self.ground_truth = self.fetch_ground_truth()

        # Parse the custom payload (or fall back to config fields)
        raw_query = ""
        pq = {}
        if self.config.custom_payload.strip():
            raw = self.config.custom_payload.strip()
            try:
                parsed = json.loads(raw)
                raw_query = parsed.get("query", raw) if isinstance(parsed, dict) else raw
            except Exception:
                raw_query = raw
            pq = self._parse_query(raw_query)
            self.progress(f"Payload parsed — root: {pq['root']}, type: {pq['asset_type']}, fields: {pq['sel_fields']}")
        else:
            pq = self._parse_query("")
            pq["asset_type"]    = self.config.asset_type or "BusinessTerm"
            pq["relation_type"] = self.config.relation_type
            pq["target_types"]  = self.config.target_types or ["BusinessTerm"]

        all_cases: list[dict] = []

        # Introspect schema once — used by both AI and fallback generation
        at = pq.get("asset_type") or self.config.asset_type or "BusinessTerm"
        self.progress(f"Introspecting schema for {at} ...")
        schema_fields = self._introspect_type(at)
        if schema_fields:
            self.progress(f"Schema: {len(schema_fields)} fields found on {at}")
        else:
            self.progress(f"WARN: Schema introspection unavailable — will use fallback field list")

        # 1. AI-driven generation (primary): Claude analyzes PR, Jira, notes, schema, payload
        if self.config.anthropic_api_key.strip():
            ai_cases = self._generate_cases_via_ai(pq, raw_query or "", schema_fields)
            if ai_cases:
                all_cases += ai_cases
                self.progress(f"AI generation complete — {len(ai_cases)} test cases")
            else:
                self.progress("AI generation produced no cases — using schema-driven fallback")
                all_cases += self._generate_payload_variations(pq, raw_query or "")
        else:
            # 1b. Schema-driven fallback when no API key
            self.progress("No Anthropic API key — using schema-driven test generation ...")
            all_cases += self._generate_payload_variations(pq, raw_query or "")

            # Additional PR + notes passes (only needed without AI)
            if self.config.pr_summary:
                self.progress("Generating PR-targeted test cases from diff ...")
                all_cases += self._generate_from_pr_diff()
            if self.config.notes.strip():
                self.progress("Generating notes-guided test cases ...")
                all_cases += self._generate_from_notes(pq)

        # 2. Schema compatibility cases (always — tests every field as direct + relation target)
        if self.config.run_schema:
            all_cases += self._generate_schema_cases()

        total = len(all_cases)
        for idx, case in enumerate(all_cases, 1):
            self.progress(f"[{idx}/{total}] Running {case['id']}: {case['name']} ...")

            endpoint = case.get("endpoint_override") or self.config.graphql_url
            resp, status_code, duration_ms = self._graphql(case["query"], endpoint=endpoint)

            if self._is_connection_error(resp):
                result_str   = RESULT_ERROR
                notes        = f"Connection error: {resp['connection_error']}"
            else:
                validator = case.get("validator", self._validate_data_no_errors)
                result_str, notes = validator(resp, case, self.ground_truth)

            bug_id   = ""
            bug_sev  = ""
            bug_desc = ""
            if result_str == RESULT_FAIL and "bug_candidate" in case:
                bc       = case["bug_candidate"]
                bug_id   = bc.get("id", "")
                bug_sev  = bc.get("severity", "")
                bug_desc = bc.get("desc", "")

            _raw_cat = case.get("category", "positive").upper()
            _cat = {"PR_TARGETED": "POSITIVE", "NOTES": "POSITIVE"}.get(_raw_cat, _raw_cat)

            # For custom cases, show original input as typed; for others show generated query
            _display_query = case.get("original_input") or case["query"]

            tr = TestResult(
                test_id          = case["id"],
                name             = case["name"],
                category         = _cat,
                graphql_query    = _display_query,
                request_body     = json.dumps({"query": case["query"]}, indent=2),
                response_raw     = json.dumps(resp, indent=2),
                status_code      = status_code,
                result           = result_str,
                validation_notes = notes,
                bug_id           = bug_id,
                bug_severity     = bug_sev,
                bug_description  = bug_desc,
                duration_ms      = duration_ms,
            )
            self.results.append(tr)
            self.progress(f"  → {result_str}: {notes[:80]}")

        return self.results
