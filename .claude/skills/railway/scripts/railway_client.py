"""Railway public GraphQL API client (stdlib) — deploy status, logs, variables, redeploys.

The Railway CLI is not available in cloud Claude sessions; this module covers the
operational surface we need via https://backboard.railway.com/graphql/v2 instead.

Auth (.env auto-loaded; project token wins when both are set):
  RAILWAY_PROJECT_TOKEN  project token (Railway: Project -> Settings -> Tokens),
                         scoped to ONE project + environment; sent as the
                         Project-Access-Token header. Recommended (least privilege).
  RAILWAY_API_TOKEN      account/workspace token (Account Settings -> Tokens);
                         sent as Authorization: Bearer. Needs RAILWAY_PROJECT_ID
                         (or --project) unless the token sees exactly one project.

Optional context (all overridable per-call/per-command):
  RAILWAY_PROJECT_ID, RAILWAY_ENVIRONMENT_ID, RAILWAY_SERVICE (name or id).

CLI: python3 .claude/skills/railway/scripts/railway_client.py <command> [...]
Commands: whoami | status | deployments | logs | vars | set | unset | redeploy |
restart | cancel. Global flags: --project --environment --json. With --json the
LAST stdout line is a JSON summary (repo convention). --self-test runs offline.
"""

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

DEFAULT_API_URL = "https://backboard.railway.com/graphql/v2"


def _load_dotenv():
    here = Path(__file__).resolve()
    for parent in [here.parent, *here.parents]:
        env_path = parent / ".env"
        if env_path.is_file():
            for raw in env_path.read_text().splitlines():
                line = raw.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, _, v = line.partition("=")
                os.environ.setdefault(k.strip(), v.split(" #")[0].strip().strip('"').strip("'"))
            return


class RailwayError(RuntimeError):
    pass


# ---- GraphQL documents ----------------------------------------------------
Q_ME = "query { me { name email } }"
Q_PROJECT_TOKEN = "query { projectToken { projectId environmentId } }"
Q_PROJECTS = "query { projects(first: 100) { edges { node { id name } } } }"
Q_PROJECT = """
query Project($id: String!) {
  project(id: $id) {
    id name
    environments { edges { node { id name } } }
    services { edges { node { id name } } }
  }
}
"""
Q_DEPLOYMENTS = """
query Deployments($input: DeploymentListInput!, $first: Int!) {
  deployments(input: $input, first: $first) {
    edges { node { id status createdAt staticUrl meta } }
  }
}
"""
Q_DEPLOY_LOGS = """
query DeploymentLogs($id: String!, $limit: Int!) {
  deploymentLogs(deploymentId: $id, limit: $limit) { timestamp severity message }
}
"""
Q_BUILD_LOGS = """
query BuildLogs($id: String!, $limit: Int!) {
  buildLogs(deploymentId: $id, limit: $limit) { timestamp severity message }
}
"""
Q_VARIABLES = """
query Variables($projectId: String!, $environmentId: String!, $serviceId: String) {
  variables(projectId: $projectId, environmentId: $environmentId, serviceId: $serviceId)
}
"""
M_VARIABLE_UPSERT = "mutation VariableUpsert($input: VariableUpsertInput!) { variableUpsert(input: $input) }"
M_VARIABLE_DELETE = "mutation VariableDelete($input: VariableDeleteInput!) { variableDelete(input: $input) }"
M_SERVICE_REDEPLOY = """
mutation ServiceInstanceRedeploy($environmentId: String!, $serviceId: String!) {
  serviceInstanceRedeploy(environmentId: $environmentId, serviceId: $serviceId)
}
"""
M_DEPLOYMENT_RESTART = "mutation DeploymentRestart($id: String!) { deploymentRestart(id: $id) }"
M_DEPLOYMENT_CANCEL = "mutation DeploymentCancel($id: String!) { deploymentCancel(id: $id) }"


class RailwayClient:
    def __init__(self, api_token=None, project_token=None, api_url=None):
        _load_dotenv()
        self.project_token = project_token or os.environ.get("RAILWAY_PROJECT_TOKEN")
        self.api_token = api_token or os.environ.get("RAILWAY_API_TOKEN")
        if not self.project_token and not self.api_token:
            raise RailwayError(
                "no Railway token: set RAILWAY_PROJECT_TOKEN (Project -> Settings -> "
                "Tokens; recommended) or RAILWAY_API_TOKEN (.env or environment).")
        self.api_url = (api_url or os.environ.get("RAILWAY_API_URL") or DEFAULT_API_URL).rstrip("/")

    def _request(self, query, variables=None):
        body = json.dumps({"query": query, "variables": variables or {}}).encode()
        req = urllib.request.Request(self.api_url, data=body, method="POST")
        req.add_header("Content-Type", "application/json")
        req.add_header("Accept", "application/json")
        if self.project_token:
            req.add_header("Project-Access-Token", self.project_token)
        else:
            req.add_header("Authorization", f"Bearer {self.api_token}")
        last = None
        for attempt in range(5):
            try:
                with urllib.request.urlopen(req, timeout=60) as resp:
                    raw = resp.read().decode("utf-8")
                payload = json.loads(raw) if raw else {}
            except urllib.error.HTTPError as e:
                detail = e.read().decode("utf-8", "replace")[:300]
                if (e.code == 429 or 500 <= e.code < 600) and attempt < 4:
                    retry_after = e.headers.get("Retry-After") if e.headers else None
                    delay = int(retry_after) if (retry_after or "").isdigit() else 2 ** attempt
                    time.sleep(min(delay, 30))
                    last = RailwayError(f"HTTP {e.code} from Railway API: {detail}")
                    continue
                raise RailwayError(f"HTTP {e.code} from Railway API: {detail}") from e
            except urllib.error.URLError as e:
                if attempt < 4:
                    time.sleep(2 ** attempt)
                    last = RailwayError(f"Network error reaching Railway API: {e.reason}")
                    continue
                raise RailwayError(f"Network error reaching Railway API: {e.reason}") from e
            errors = payload.get("errors")
            if errors:  # GraphQL errors arrive with HTTP 200 — surface them clearly
                msgs = "; ".join(str(e.get("message", e)) for e in errors)[:500]
                raise RailwayError(f"Railway GraphQL error: {msgs}")
            return payload.get("data") or {}
        raise last

    # ---- context resolution ---------------------------------------------
    def resolve_context(self, project_id=None, environment_id=None):
        """(project_id, environment_id) from args -> env vars -> the token itself.
        Project tokens carry both; account tokens fall back to the only project /
        the environment named 'production' when unambiguous."""
        project_id = project_id or os.environ.get("RAILWAY_PROJECT_ID")
        environment_id = environment_id or os.environ.get("RAILWAY_ENVIRONMENT_ID")
        if self.project_token:
            info = (self._request(Q_PROJECT_TOKEN).get("projectToken") or {})
            return (project_id or info.get("projectId"),
                    environment_id or info.get("environmentId"))
        if not project_id:
            projects = self.list_projects()
            if len(projects) == 1:
                project_id = projects[0]["id"]
            else:
                names = ", ".join(f"{p['name']}={p['id']}" for p in projects) or "(none visible)"
                raise RailwayError(f"set RAILWAY_PROJECT_ID or pass --project. Visible: {names}")
        if not environment_id:
            envs = self.get_project(project_id)["environments"]
            prod = [e for e in envs if (e.get("name") or "").lower() == "production"]
            if prod:
                environment_id = prod[0]["id"]
            elif len(envs) == 1:
                environment_id = envs[0]["id"]
            else:
                names = ", ".join(f"{e['name']}={e['id']}" for e in envs)
                raise RailwayError(f"set RAILWAY_ENVIRONMENT_ID or pass --environment. Available: {names}")
        return project_id, environment_id

    # ---- queries ----------------------------------------------------------
    def me(self):
        return self._request(Q_ME).get("me") or {}

    def list_projects(self):
        edges = ((self._request(Q_PROJECTS).get("projects") or {}).get("edges")) or []
        return [e["node"] for e in edges]

    def get_project(self, project_id):
        """Project with environments/services flattened to [{id, name}]."""
        p = self._request(Q_PROJECT, {"id": project_id}).get("project")
        if not p:
            raise RailwayError(f"project {project_id!r} not found or not visible to this token")
        return {
            "id": p["id"], "name": p["name"],
            "environments": [e["node"] for e in (p.get("environments") or {}).get("edges", [])],
            "services": [e["node"] for e in (p.get("services") or {}).get("edges", [])],
        }

    def deployments(self, project_id, environment_id=None, service_id=None, first=10):
        inp = {"projectId": project_id}
        if environment_id:
            inp["environmentId"] = environment_id
        if service_id:
            inp["serviceId"] = service_id
        data = self._request(Q_DEPLOYMENTS, {"input": inp, "first": int(first)})
        edges = ((data.get("deployments") or {}).get("edges")) or []
        return [e["node"] for e in edges]

    def deployment_logs(self, deployment_id, limit=100, build=False):
        q = Q_BUILD_LOGS if build else Q_DEPLOY_LOGS
        data = self._request(q, {"id": deployment_id, "limit": int(limit)})
        return data.get("buildLogs" if build else "deploymentLogs") or []

    def get_variables(self, project_id, environment_id, service_id=None):
        """{name: value} map. service_id=None reads environment-level shared vars."""
        data = self._request(Q_VARIABLES, {
            "projectId": project_id, "environmentId": environment_id, "serviceId": service_id})
        return data.get("variables") or {}

    # ---- mutations --------------------------------------------------------
    def variable_upsert(self, project_id, environment_id, name, value, service_id=None):
        inp = {"projectId": project_id, "environmentId": environment_id,
               "name": name, "value": value}
        if service_id:
            inp["serviceId"] = service_id
        self._request(M_VARIABLE_UPSERT, {"input": inp})

    def variable_delete(self, project_id, environment_id, name, service_id=None):
        inp = {"projectId": project_id, "environmentId": environment_id, "name": name}
        if service_id:
            inp["serviceId"] = service_id
        self._request(M_VARIABLE_DELETE, {"input": inp})

    def service_redeploy(self, environment_id, service_id):
        """Redeploy the service's latest deployment (same commit, fresh build/run)."""
        self._request(M_SERVICE_REDEPLOY, {"environmentId": environment_id, "serviceId": service_id})

    def deployment_restart(self, deployment_id):
        self._request(M_DEPLOYMENT_RESTART, {"id": deployment_id})

    def deployment_cancel(self, deployment_id):
        self._request(M_DEPLOYMENT_CANCEL, {"id": deployment_id})


# ---- pure helpers (CLI + self-test) ----------------------------------------
def mask(value):
    """Redact a variable value for display: first 4 chars + length."""
    s = "" if value is None else str(value)
    if len(s) <= 6:
        return "•••"
    return f"{s[:4]}…({len(s)} chars)"


def parse_assignments(pairs):
    """['A=1','B=x=y'] -> {'A':'1','B':'x=y'}; raises RailwayError on a bare name."""
    out = {}
    for p in pairs:
        name, sep, value = p.partition("=")
        if not sep or not name.strip():
            raise RailwayError(f"expected NAME=VALUE, got {p!r}")
        out[name.strip()] = value
    return out


def pick_service(services, wanted):
    """Match a service by id or case-insensitive name; unambiguous default when
    the project has exactly one service. `services` = [{id, name}]."""
    if wanted:
        for s in services:
            if s["id"] == wanted or (s.get("name") or "").lower() == wanted.lower():
                return s
        names = ", ".join(s.get("name", s["id"]) for s in services)
        raise RailwayError(f"service {wanted!r} not found. Available: {names}")
    if len(services) == 1:
        return services[0]
    names = ", ".join(s.get("name", s["id"]) for s in services)
    raise RailwayError(f"multiple services — pass --service or set RAILWAY_SERVICE. Available: {names}")


def _age(created_at):
    """Compact age like '3h' / '2d' from an ISO createdAt, '' if unparseable."""
    try:
        dt = datetime.fromisoformat(str(created_at).replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return ""
    secs = max(0, int((datetime.now(timezone.utc) - dt).total_seconds()))
    for unit, div in (("d", 86400), ("h", 3600), ("m", 60)):
        if secs >= div:
            return f"{secs // div}{unit}"
    return f"{secs}s"


def _commit_line(meta):
    m = meta if isinstance(meta, dict) else {}
    msg = (m.get("commitMessage") or "").splitlines()[0][:60] if m.get("commitMessage") else ""
    sha = (m.get("commitHash") or "")[:7]
    return " ".join(x for x in (sha, msg) if x)


# ---- CLI --------------------------------------------------------------------
def _service_id_for(client, args, project_id, required=True):
    wanted = args.service or os.environ.get("RAILWAY_SERVICE")
    services = client.get_project(project_id)["services"]
    if not required and not wanted:
        return None
    return pick_service(services, wanted)["id"]


def cmd_whoami(client, args):
    project_id, environment_id = client.resolve_context(args.project, args.environment)
    out = {"auth": "project_token" if client.project_token else "api_token",
           "project_id": project_id, "environment_id": environment_id}
    if not client.project_token:
        me = client.me()
        out["user"] = {"name": me.get("name"), "email": me.get("email")}
        print(f"token user: {me.get('name')} <{me.get('email')}>")
    project = client.get_project(project_id)
    env_name = next((e["name"] for e in project["environments"] if e["id"] == environment_id), "?")
    out["project"], out["environment"] = project["name"], env_name
    print(f"auth: {out['auth']}")
    print(f"project: {project['name']} ({project_id})")
    print(f"environment: {env_name} ({environment_id})")
    print("services: " + ", ".join(s["name"] for s in project["services"]))
    return out


def cmd_status(client, args):
    project_id, environment_id = client.resolve_context(args.project, args.environment)
    project = client.get_project(project_id)
    rows = []
    for svc in project["services"]:
        deps = client.deployments(project_id, environment_id, svc["id"], first=1)
        d = deps[0] if deps else {}
        rows.append({"service": svc["name"], "service_id": svc["id"],
                     "deployment_id": d.get("id"), "status": d.get("status"),
                     "created_at": d.get("createdAt"), "age": _age(d.get("createdAt")),
                     "url": d.get("staticUrl"), "commit": _commit_line(d.get("meta"))})
    print(f"project {project['name']} — environment "
          f"{next((e['name'] for e in project['environments'] if e['id'] == environment_id), environment_id)}")
    for r in rows:
        print(f"  {r['service']:<20} {str(r['status'] or '(no deployments)'):<12} "
              f"{r['age']:>4}  {r['url'] or ''}  {r['commit']}")
    return {"project": project["name"], "environment_id": environment_id, "services": rows}


def cmd_deployments(client, args):
    project_id, environment_id = client.resolve_context(args.project, args.environment)
    service_id = _service_id_for(client, args, project_id, required=False)
    deps = client.deployments(project_id, environment_id, service_id, first=args.limit)
    out = []
    for d in deps:
        row = {"id": d.get("id"), "status": d.get("status"), "created_at": d.get("createdAt"),
               "url": d.get("staticUrl"), "commit": _commit_line(d.get("meta"))}
        out.append(row)
        print(f"  {row['id']}  {str(row['status']):<12} {_age(row['created_at']):>4}  {row['commit']}")
    if not out:
        print("  (no deployments)")
    return {"deployments": out}


def cmd_logs(client, args):
    project_id, environment_id = client.resolve_context(args.project, args.environment)
    deployment_id = args.deployment
    if not deployment_id:
        service_id = _service_id_for(client, args, project_id, required=False)
        deps = client.deployments(project_id, environment_id, service_id, first=1)
        if not deps:
            raise RailwayError("no deployments found to read logs from")
        deployment_id = deps[0]["id"]
        print(f"latest deployment: {deployment_id} ({deps[0].get('status')})")
    lines = client.deployment_logs(deployment_id, limit=args.limit, build=args.build)
    for ln in lines:
        sev = (ln.get("severity") or "").lower()
        print(f"{ln.get('timestamp', '')} [{sev or '-'}] {ln.get('message', '')}")
    return {"deployment_id": deployment_id, "kind": "build" if args.build else "runtime",
            "lines": len(lines)}


def cmd_vars(client, args):
    project_id, environment_id = client.resolve_context(args.project, args.environment)
    service_id = None if args.shared else _service_id_for(client, args, project_id)
    values = client.get_variables(project_id, environment_id, service_id)
    for name in sorted(values):
        print(f"  {name}={values[name] if args.show_values else mask(values[name])}")
    return {"service_id": service_id, "count": len(values),
            "names": sorted(values),
            **({"values": values} if args.show_values else {})}


def cmd_set(client, args):
    project_id, environment_id = client.resolve_context(args.project, args.environment)
    service_id = None if args.shared else _service_id_for(client, args, project_id)
    assignments = parse_assignments(args.pairs)
    for name, value in assignments.items():
        client.variable_upsert(project_id, environment_id, name, value, service_id)
        print(f"set {name} ({mask(value)})")
    print("note: variable changes take effect on the NEXT deploy — run `redeploy` to apply now")
    return {"set": sorted(assignments), "service_id": service_id}


def cmd_unset(client, args):
    project_id, environment_id = client.resolve_context(args.project, args.environment)
    service_id = None if args.shared else _service_id_for(client, args, project_id)
    for name in args.names:
        client.variable_delete(project_id, environment_id, name, service_id)
        print(f"deleted {name}")
    print("note: variable changes take effect on the NEXT deploy — run `redeploy` to apply now")
    return {"deleted": list(args.names), "service_id": service_id}


def cmd_redeploy(client, args):
    project_id, environment_id = client.resolve_context(args.project, args.environment)
    service_id = _service_id_for(client, args, project_id)
    client.service_redeploy(environment_id, service_id)
    print(f"redeploy triggered for service {service_id} — follow with: status / logs --build")
    return {"redeployed": service_id, "environment_id": environment_id}


def cmd_restart(client, args):
    project_id, environment_id = client.resolve_context(args.project, args.environment)
    deployment_id = args.deployment
    if not deployment_id:
        service_id = _service_id_for(client, args, project_id)
        deps = client.deployments(project_id, environment_id, service_id, first=1)
        if not deps:
            raise RailwayError("no deployments found to restart")
        deployment_id = deps[0]["id"]
    client.deployment_restart(deployment_id)
    print(f"restart triggered for deployment {deployment_id}")
    return {"restarted": deployment_id}


def cmd_cancel(client, args):
    client.deployment_cancel(args.deployment)
    print(f"cancel requested for deployment {args.deployment}")
    return {"cancelled": args.deployment}


def self_test():
    """Offline checks — no token, network, or Railway account needed."""
    failures = []

    def check(label, cond):
        print(f"{'PASS' if cond else 'FAIL'} {label}")
        if not cond:
            failures.append(label)

    m = mask("supersecretvalue")
    check("mask redacts", m == "supe…(16 chars)" and "secret" not in m)
    check("mask short values fully hidden", mask("abc") == "•••")
    check("parse_assignments keeps '=' in values",
          parse_assignments(["A=1", "B=x=y"]) == {"A": "1", "B": "x=y"})
    try:
        parse_assignments(["NOEQUALS"])
        check("parse_assignments rejects bare names", False)
    except RailwayError:
        check("parse_assignments rejects bare names", True)

    services = [{"id": "svc1", "name": "sdr-console"}, {"id": "svc2", "name": "MongoDB"}]
    check("pick_service by name (case-insensitive)",
          pick_service(services, "SDR-Console")["id"] == "svc1")
    check("pick_service by id", pick_service(services, "svc2")["name"] == "MongoDB")
    check("pick_service single-service default", pick_service([services[0]], None)["id"] == "svc1")
    try:
        pick_service(services, None)
        check("pick_service ambiguity error lists options", False)
    except RailwayError as e:
        check("pick_service ambiguity error lists options", "sdr-console" in str(e))

    check("_commit_line from meta",
          _commit_line({"commitHash": "abcdef1234", "commitMessage": "Fix\nbody"}) == "abcdef1 Fix")
    check("_age handles garbage", _age("nope") == "" and _age(None) == "")

    for name, doc in [(n, v) for n, v in globals().items() if n.startswith(("Q_", "M_"))]:
        check(f"{name} braces balanced", doc.count("{") == doc.count("}") and doc.count("{") > 0)

    summary = {"ok": not failures, "failures": failures}
    print(json.dumps(summary))
    return 0 if not failures else 1


COMMANDS = {
    "whoami": cmd_whoami, "status": cmd_status, "deployments": cmd_deployments,
    "logs": cmd_logs, "vars": cmd_vars, "set": cmd_set, "unset": cmd_unset,
    "redeploy": cmd_redeploy, "restart": cmd_restart, "cancel": cmd_cancel,
}


def main(argv=None):
    ap = argparse.ArgumentParser(description="Railway API operations (stdlib GraphQL client)")
    ap.add_argument("--self-test", action="store_true", help="offline checks, no token needed")
    sub = ap.add_subparsers(dest="command")
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--project", help="project id (default: RAILWAY_PROJECT_ID / token scope)")
    common.add_argument("--environment", help="environment id (default: RAILWAY_ENVIRONMENT_ID / "
                                              "token scope / the 'production' environment)")
    common.add_argument("--json", action="store_true", help="print a JSON summary as the last line")
    svc = argparse.ArgumentParser(add_help=False)
    svc.add_argument("--service", help="service name or id (default: RAILWAY_SERVICE; "
                                       "unambiguous when the project has one service)")

    sub.add_parser("whoami", parents=[common], help="validate the token + resolved context")
    sub.add_parser("status", parents=[common], help="latest deployment per service")
    p = sub.add_parser("deployments", parents=[common, svc], help="recent deployments")
    p.add_argument("--limit", type=int, default=10)
    p = sub.add_parser("logs", parents=[common, svc], help="deployment logs")
    p.add_argument("--deployment", help="deployment id (default: the service's latest)")
    p.add_argument("--build", action="store_true", help="build logs instead of runtime logs")
    p.add_argument("--limit", type=int, default=100)
    p = sub.add_parser("vars", parents=[common, svc], help="list variables (values masked)")
    p.add_argument("--shared", action="store_true", help="environment-level shared vars, not a service's")
    p.add_argument("--show-values", action="store_true",
                   help="print real values (secrets land in the transcript — avoid)")
    p = sub.add_parser("set", parents=[common, svc], help="upsert variables (NAME=VALUE ...)")
    p.add_argument("pairs", nargs="+", metavar="NAME=VALUE")
    p.add_argument("--shared", action="store_true")
    p = sub.add_parser("unset", parents=[common, svc], help="delete variables")
    p.add_argument("names", nargs="+", metavar="NAME")
    p.add_argument("--shared", action="store_true")
    sub.add_parser("redeploy", parents=[common, svc], help="redeploy the service's latest deployment")
    p = sub.add_parser("restart", parents=[common, svc], help="restart a deployment's containers")
    p.add_argument("--deployment", help="deployment id (default: the service's latest)")
    p = sub.add_parser("cancel", parents=[common], help="cancel an in-progress deployment")
    p.add_argument("--deployment", required=True)

    args = ap.parse_args(argv)
    if args.self_test:
        return self_test()
    if not args.command:
        ap.print_help()
        return 2
    try:
        client = RailwayClient()
        summary = COMMANDS[args.command](client, args)
    except RailwayError as e:
        print(f"error: {e}", file=sys.stderr)
        if getattr(args, "json", False):
            print(json.dumps({"ok": False, "error": str(e)}))
        return 1
    if getattr(args, "json", False):
        print(json.dumps({"ok": True, "command": args.command, **(summary or {})}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
