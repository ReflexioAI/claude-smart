"""Execute the shipped dashboard routing and client code against isolated backends."""

from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import subprocess

import pytest

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(scope="module")
def compiled_dashboard(tmp_path_factory):
    target = tmp_path_factory.mktemp("dashboard-modules")
    node = shutil.which("node")
    compiler = ROOT / "node_modules/typescript/bin/tsc"
    assert node and compiler.is_file(), (
        "npm ci is required for dashboard contract tests"
    )
    result = subprocess.run(
        [
            node,
            str(compiler),
            "plugin/dashboard/lib/data-source.ts",
            "plugin/dashboard/lib/reflexio-client.ts",
            "--outDir",
            str(target),
            "--module",
            "commonjs",
            "--target",
            "ES2022",
            "--esModuleInterop",
            "--skipLibCheck",
            "--strict",
            "--typeRoots",
            str(ROOT / "node_modules/@types"),
        ],
        cwd=ROOT,
        text=True,
        capture_output=True,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    # Transpile the actual Next route; framework response behavior is the only stub.
    route = ROOT / "plugin/dashboard/app/api/reflexio/[...path]/route.ts"
    result = subprocess.run(
        [
            node,
            "-e",
            "const fs=require('fs'),ts=require('typescript'); console.log(ts.transpileModule(fs.readFileSync(process.argv[1],'utf8'),{compilerOptions:{module:ts.ModuleKind.CommonJS,target:ts.ScriptTarget.ES2022}}).outputText)",
            str(route),
        ],
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=True,
    )
    (target / "proxy.js").write_text(
        result.stdout.replace('"@/lib/data-source"', '"./data-source"').replace(
            '"next/server"', '"./next-server-stub"'
        )
    )
    (target / "next-server-stub.js").write_text("exports.NextResponse = Response;")
    return target


def run_module(compiled, home, script):
    env = {
        k: v
        for k, v in os.environ.items()
        if not k.startswith(("REFLEXIO_", "CLAUDE_SMART_"))
    }
    env["HOME"] = str(home)
    result = subprocess.run(
        [shutil.which("node"), "-e", script],
        cwd=compiled,
        env=env,
        text=True,
        capture_output=True,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    return json.loads(result.stdout)


@pytest.mark.parametrize("active", ["local", "managed"])
@pytest.mark.parametrize("quoted", [True, False])
def test_selected_backend_routes_all_methods_without_crossing_credentials(
    compiled_dashboard, tmp_path, active, quoted
):
    state = tmp_path / ".claude-smart"
    state.mkdir()
    active_url, active_key = (
        ("http://localhost:18071/", "local-secret")
        if active == "local"
        else ("https://managed.example/", "cloud-secret")
    )
    config = state / ".env"
    config.write_text(
        f'REFLEXIO_URL="{active_url}"\nREFLEXIO_API_KEY="{active_key}"\nCLAUDE_SMART_LOCAL_URL="http://localhost:18071/"\nCLAUDE_SMART_LOCAL_API_KEY="local-secret"\nCLAUDE_SMART_MANAGED_URL="https://managed.example/"\nCLAUDE_SMART_MANAGED_API_KEY="cloud-secret"\n'
    )
    if not quoted:
        config.write_text(config.read_text().replace('"', ""))
    original = config.read_bytes()
    result = run_module(
        compiled_dashboard,
        tmp_path,
        """
      const m = require('./data-source');
      const proxy = require('./proxy');
      (async () => {
        const calls = [];
        global.fetch = async (url, options) => { calls.push({url, method:options.method, authorization:options.headers.get("authorization")}); return Response.json({success:true}); };
        for (const source of ['local','managed']) {
          const connection = (await m.dataSources())[source];
          for (const endpoint of ['get_dashboard_stats','update_user_playbook']) {
            const request = new Request('http://dashboard/api/reflexio/api/'+endpoint, {method:'POST',body:'{}',headers:{'x-claude-smart-source':source,'x-claude-smart-connection':m.connectionIdentity(connection),authorization:'Bearer browser-secret'}});
            const response = await proxy.POST(request,{params:Promise.resolve({path:['api',endpoint]})});
            if (!response.ok) throw new Error(await response.text());
          }
        }
        console.log(JSON.stringify({defaultSource:(await m.dataSources()).defaultSource,calls}));
      })();
    """,
    )
    assert result["defaultSource"] == active
    assert [item["authorization"] for item in result["calls"]] == [
        "Bearer local-secret"
    ] * 2 + ["Bearer cloud-secret"] * 2
    assert [item["url"].split("/api/")[0] for item in result["calls"]] == [
        "http://localhost:18071"
    ] * 2 + ["https://managed.example"] * 2
    assert config.read_bytes() == original, (
        "Viewing a backend must not rewrite runtime settings"
    )


def test_default_local_target_never_receives_managed_credentials(
    compiled_dashboard, tmp_path
):
    state = tmp_path / ".claude-smart"
    state.mkdir()
    (state / ".env").write_text(
        'REFLEXIO_URL="https://managed.example/"\nREFLEXIO_API_KEY="cloud-secret"\n'
    )
    result = run_module(
        compiled_dashboard,
        tmp_path,
        """
      const m = require('./data-source');
      (async () => console.log(JSON.stringify(await m.selectedConnection(new Request('http://dashboard',{headers:{'x-claude-smart-source':'local'}})))))();
    """,
    )
    assert result["source"] == "local"
    assert result["apiKey"] == ""
    assert result["url"].startswith("http://localhost:")


@pytest.mark.parametrize("selection", ["unknown", "https://attacker.example"])
def test_source_is_an_allowlisted_choice_not_a_destination_url(
    compiled_dashboard, tmp_path, selection
):
    result = run_module(
        compiled_dashboard,
        tmp_path,
        f"""
      const m = require('./data-source');
      (async () => {{ try {{ await m.selectedConnection(new Request('http://dashboard',{{headers:{{'x-claude-smart-source':{json.dumps(selection)}}}}})); console.log('false'); }} catch {{ console.log('true'); }} }})();
    """,
    )
    assert result is True


def test_backend_reported_failure_is_not_a_zero_count(compiled_dashboard, tmp_path):
    result = run_module(
        compiled_dashboard,
        tmp_path,
        """
      const m = require('./reflexio-client');
      global.fetch = async () => Response.json({success:false,msg:'storage unavailable'});
      (async () => { try { await m.reflexio.getDashboardStats(); console.log('false'); } catch(e) { console.log(JSON.stringify(e.message)); } })();
    """,
    )
    assert result == "storage unavailable"


def test_dashboard_summary_and_detail_reads_follow_selected_source(
    compiled_dashboard, tmp_path
):
    result = run_module(
        compiled_dashboard,
        tmp_path,
        """
      const m = require('./reflexio-client');
      const calls = [];
      global.fetch = async (url, opts) => {
        calls.push({url,source:opts.headers.get('x-claude-smart-source'),body:JSON.parse(opts.body)});
        if (url.endsWith('get_dashboard_stats')) return Response.json({success:true,stats:{current_period:{total_playbooks:868}}});
        return Response.json({items:[],next_cursor:'next-page'});
      };
      (async () => {
        m.selectDataSource('managed');
        const summary = await m.reflexio.getDashboardStats();
        await m.reflexio.getRequests('page-token');
        await m.reflexio.getTranscript('request-id','transcript-token');
        console.log(JSON.stringify({summary,calls}));
      })();
    """,
    )
    assert result["summary"]["stats"]["current_period"]["total_playbooks"] == 868
    assert all(call["source"] == "managed" for call in result["calls"])
    assert result["calls"][1]["body"]["cursor"] == "page-token"
    assert result["calls"][2]["body"]["request_id"] == "request-id"
    assert result["calls"][2]["body"]["cursor"] == "transcript-token"


def test_stale_connection_identity_cannot_retarget_cached_record_ids(
    compiled_dashboard, tmp_path
):
    state = tmp_path / ".claude-smart"
    state.mkdir()
    (state / ".env").write_text(
        'REFLEXIO_URL="https://managed.example/"\nREFLEXIO_API_KEY="new-account-key"\n'
    )
    result = run_module(
        compiled_dashboard,
        tmp_path,
        """
      const m = require('./data-source');
      const oldId = m.connectionIdentity({url:'https://managed.example/',apiKey:'old-account-key'});
      (async () => {
        try { await m.selectedConnection(new Request('http://dashboard',{headers:{'x-claude-smart-source':'managed','x-claude-smart-connection':oldId}})); console.log('false'); }
        catch(e) { console.log(JSON.stringify(e.message)); }
      })();
    """,
    )
    assert (
        result
        == "This backend connection changed. Reload the dashboard before continuing."
    )


@pytest.mark.parametrize("stale_key", ["", "stale-cloud-secret"])
def test_unconfigured_runtime_uses_local_default(
    compiled_dashboard, tmp_path, stale_key
):
    state = tmp_path / ".claude-smart"
    state.mkdir()
    (state / ".env").write_text(f'REFLEXIO_URL=""\nREFLEXIO_API_KEY="{stale_key}"\n')
    result = run_module(
        compiled_dashboard,
        tmp_path,
        """
      const m = require('./data-source');
      (async () => console.log(JSON.stringify(await m.selectedConnection(new Request('http://dashboard')))))();
    """,
    )
    assert result["source"] == "local"
    assert result["url"].startswith("http://localhost:")
    assert result["apiKey"] == ""


def test_usage_counts_retain_kind_and_source_identity(compiled_dashboard, tmp_path):
    result = run_module(
        compiled_dashboard,
        tmp_path,
        """
      const m = require('./reflexio-client');
      global.fetch = async () => Response.json({success:true,stats:[
        {kind:'profile',real_id:'p1',applied_count:3},
        {kind:'user_playbook',real_id:'7',applied_count:4},
        {kind:'agent_playbook',real_id:'7',applied_count:5},
        {kind:'playbook',real_id:'7',applied_count:9}
      ]});
      (async () => console.log(JSON.stringify((await m.reflexio.getAppliedRules()).stats)))();
    """,
    )
    assert [(s["kind"], s.get("source_kind"), s["applied_count"]) for s in result] == [
        ("profile", "profile", 3),
        ("playbook", "user_playbook", 4),
        ("playbook", "agent_playbook", 5),
        ("playbook", None, 9),
    ]


def test_proxy_blocks_stale_or_unscoped_mutations_before_forwarding(
    compiled_dashboard, tmp_path
):
    result = run_module(
        compiled_dashboard,
        tmp_path,
        """
      const proxy = require('./proxy');
      let forwarded = 0;
      global.fetch = async () => { forwarded++; return Response.json({success:true}); };
      (async () => {
        const statuses = [];
        for (const headers of [{}, {'x-claude-smart-connection':'old-account-identity'}]) {
          const response = await proxy.PUT(new Request('http://dashboard/api/reflexio/api/update_user_playbook',{method:'PUT',body:'{}',headers}),{params:Promise.resolve({path:['api','update_user_playbook']})});
          statuses.push(response.status);
        }
        console.log(JSON.stringify({statuses,forwarded}));
      })();
    """,
    )
    assert result == {"statuses": [409, 409], "forwarded": 0}


@pytest.mark.parametrize(
    "remembered,expected",
    [
        ("http://localhost:8071", "http://localhost:9012"),
        ("http://localhost:8071/", "http://localhost:9012"),
        ("http://127.0.0.1:8071", "http://localhost:9012"),
        ("http://127.0.0.1:8071/", "http://localhost:9012"),
        ("http://localhost:9090/", "http://localhost:9090"),
    ],
)
def test_remembered_local_endpoint_follows_bundled_port_only_for_defaults(
    compiled_dashboard, tmp_path, remembered, expected
):
    state = tmp_path / ".claude-smart"
    state.mkdir()
    (state / ".env").write_text(
        f'REFLEXIO_URL="https://managed.example/"\nREFLEXIO_API_KEY="cloud-secret"\nCLAUDE_SMART_LOCAL_URL="{remembered}"\nCLAUDE_SMART_LOCAL_API_KEY="local-secret"\n'
    )
    result = run_module(
        compiled_dashboard,
        tmp_path,
        """
      process.env.BACKEND_PORT='9012';
      const m = require('./data-source');
      (async () => console.log(JSON.stringify(await m.selectedConnection(new Request('http://dashboard',{headers:{'x-claude-smart-source':'local'}})))))();
    """,
    )
    assert result["url"] == expected
    assert result["apiKey"] == "local-secret"


def test_saving_settings_does_not_bind_an_orphaned_key_to_localhost(
    compiled_dashboard, tmp_path
):
    state = tmp_path / ".claude-smart"
    state.mkdir()
    (state / ".env").write_text(
        'REFLEXIO_URL=""\nREFLEXIO_API_KEY="orphaned-cloud-key"\n'
    )
    result = run_module(
        compiled_dashboard,
        tmp_path,
        """
      const config = require('./config-file');
      const source = require('./data-source');
      (async () => {
        await config.writeConfig({REFLEXIO_URL:'http://localhost:8071/',CLAUDE_SMART_LEARNING_MODEL:'local-model'});
        console.log(JSON.stringify(await source.selectedConnection(new Request('http://dashboard'))));
      })();
    """,
    )
    assert result["source"] == "local"
    assert result["apiKey"] == ""


def test_server_configuration_reads_and_saves_keep_connection_identity(
    compiled_dashboard, tmp_path
):
    result = run_module(
        compiled_dashboard,
        tmp_path,
        """
      const m = require('./reflexio-client');
      const calls=[];
      global.fetch=async (url,opts)=>{
        calls.push({url,method:opts.method,source:opts.headers.get('x-claude-smart-source'),identity:opts.headers.get('x-claude-smart-connection'),body:opts.body ? JSON.parse(opts.body) : null});
        return Response.json(url.endsWith('get_config') ? {window_size:30} : {success:true});
      };
      (async()=>{
        for (const source of ['local','managed']) {
          m.selectDataSource(source,source+'-identity');
          const config=await m.reflexio.getServerConfiguration();
          await m.reflexio.setServerConfiguration({...config,window_size:31});
        }
        console.log(JSON.stringify(calls));
      })();
    """,
    )
    assert [row["source"] for row in result] == ["local", "local", "managed", "managed"]
    assert [row["identity"] for row in result] == [
        "local-identity",
        "local-identity",
        "managed-identity",
        "managed-identity",
    ]
    for read, write in (result[:2], result[2:]):
        assert read["method"] == "GET"
        assert read["url"].endswith("get_config")
        assert write["method"] == "POST"
        assert write["url"].endswith("set_config")
        assert write["body"]["window_size"] == 31
