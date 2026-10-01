"""Build the Grafana dashboards as JSON from Python.

Run `python3 grafana/build.py` after editing; commit the generated JSON.
Grafana provisions the files from grafana/dashboards/ read-only, so the
repo stays the source of truth and UI edits cannot drift from it.
"""

import json
from pathlib import Path

OUT = Path(__file__).parent / "dashboards"
PROM = {"type": "prometheus", "uid": "prometheus"}
LOKI = {"type": "loki", "uid": "loki"}
TEMPO = {"type": "tempo", "uid": "tempo"}


class Layout:
    """Places panels left to right on Grafana's 24-column grid."""

    def __init__(self):
        self.x = self.y = self.row_h = 0
        self.next_id = 1

    def place(self, panel, w, h):
        if self.x + w > 24:
            self.x, self.y, self.row_h = 0, self.y + self.row_h, 0
        panel["gridPos"] = {"x": self.x, "y": self.y, "w": w, "h": h}
        panel["id"] = self.next_id
        self.next_id += 1
        self.x += w
        self.row_h = max(self.row_h, h)
        return panel

    def row(self, title):
        self.x, self.y, self.row_h = 0, self.y + self.row_h, 0
        panel = self.place({"type": "row", "title": title, "collapsed": False, "panels": []}, 24, 1)
        self.x, self.y, self.row_h = 0, self.y + 1, 0
        return panel


def target(expr, legend="", ds=PROM, ref="A", **extra):
    t = {"datasource": ds, "expr": expr, "refId": ref}
    if legend:
        t["legendFormat"] = legend
    t.update(extra)
    return t


def stat(title, expr, unit="short", legend="", decimals=None, description=""):
    p = {
        "type": "stat", "title": title, "description": description, "datasource": PROM,
        "targets": [target(expr, legend)],
        "fieldConfig": {"defaults": {"unit": unit}, "overrides": []},
        "options": {"reduceOptions": {"calcs": ["lastNotNull"]}, "colorMode": "value", "graphMode": "area"},
    }
    if decimals is not None:
        p["fieldConfig"]["defaults"]["decimals"] = decimals
    return p


def timeseries(title, targets, unit="short", stack=False, description=""):
    return {
        "type": "timeseries", "title": title, "description": description, "datasource": PROM,
        "targets": [target(e, l, ref=chr(65 + i)) for i, (e, l) in enumerate(targets)],
        "fieldConfig": {
            "defaults": {
                "unit": unit,
                "custom": {"drawStyle": "line", "lineWidth": 1, "fillOpacity": 15 if stack else 0,
                           "stacking": {"mode": "normal" if stack else "none"}, "showPoints": "never"},
            },
            "overrides": [],
        },
        "options": {"legend": {"displayMode": "table", "placement": "bottom", "calcs": ["mean", "max"]},
                    "tooltip": {"mode": "multi"}},
    }


def heatmap(title, expr, description=""):
    return {
        "type": "heatmap", "title": title, "description": description, "datasource": PROM,
        "targets": [target(expr, "{{le}}", format="heatmap")],
        "options": {"calculate": False, "yAxis": {"unit": "s"}, "color": {"scheme": "Oranges"},
                    "cellGap": 1, "tooltip": {"show": True, "yHistogram": True}},
    }


def logs(title, expr, description=""):
    return {
        "type": "logs", "title": title, "description": description, "datasource": LOKI,
        "targets": [target(expr, ds=LOKI)],
        "options": {"showTime": True, "wrapLogMessage": True, "sortOrder": "Descending",
                    "enableLogDetails": True, "prettifyLogMessage": False},
    }


def table(title, expr, ds=PROM, description="", instant=True):
    t = target(expr, ds=ds)
    if ds is PROM:
        t.update({"instant": instant, "format": "table"})
    return {"type": "table", "title": title, "description": description, "datasource": ds,
            "targets": [t], "options": {"showHeader": True}}


def dashboard(uid, title, description, panels, variables=(), refresh="30s", time_from="now-6h"):
    return {
        "uid": uid, "title": title, "description": description, "tags": ["agent-observability"],
        "timezone": "browser", "schemaVersion": 41, "version": 1, "editable": False,
        "refresh": refresh, "time": {"from": time_from, "to": "now"},
        "templating": {"list": list(variables)}, "panels": panels,
        "links": [{"type": "dashboards", "tags": ["agent-observability"], "asDropdown": True, "title": "Dashboards"}],
    }


def query_var(name, label, query, ds=PROM):
    return {"name": name, "label": label, "type": "query", "datasource": ds,
            "query": {"query": query, "refId": name}, "definition": query,
            "includeAll": True, "allValue": ".*", "multi": True, "current": {"text": "All", "value": "$__all"}, "refresh": 2}


def local_llm():
    L = Layout()
    m = 'gen_ai_request_model=~"$model"'
    p = [
        L.row("Service level"),
        L.place(stat("Requests / min", f"sum(rate(gen_ai_client_operation_duration_seconds_count{{{m}}}[5m])) * 60",
                     decimals=1), 4, 4),
        L.place(stat("Error ratio (1h)", "sli:gen_ai_availability_errors:ratio_rate1h", "percentunit", decimals=2,
                     description="SLO: 99% of requests without an upstream error."), 4, 4),
        L.place(stat("Slow first token (1h)", "sli:gen_ai_ttft_slow:ratio_rate1h", "percentunit", decimals=1,
                     description="SLO: 95% of streamed chat requests see a first token within 4 s."), 4, 4),
        L.place(stat("TTFT p95", f"max(model:gen_ai_ttft_seconds:p95_5m{{{m}}})", "s", decimals=2), 4, 4),
        L.place(stat("Decode tok/s", f"avg(model:gen_ai_decode_tokens_per_second:mean5m{{{m}}})", decimals=1), 4, 4),
        L.place(stat("Ollama up", "ollama_up", "bool_yes_no"), 4, 4),

        L.row("Latency"),
        L.place(heatmap("Time to first token",
                        f"sum by (le) (increase(gen_ai_server_time_to_first_token_seconds_bucket{{{m}}}[$__rate_interval]))",
                        description="Streamed requests only. Bands above 10 s are cold model loads or CPU spill."), 12, 9),
        L.place(timeseries("TTFT p50 / p95 by model", [
            (f"model:gen_ai_ttft_seconds:p50_5m{{{m}}}", "p50 {{gen_ai_request_model}}"),
            (f"model:gen_ai_ttft_seconds:p95_5m{{{m}}}", "p95 {{gen_ai_request_model}}"),
        ], "s"), 12, 9),
        L.place(timeseries("Decode speed (tokens/s)", [
            (f"model:gen_ai_decode_tokens_per_second:mean5m{{{m}}}", "{{gen_ai_request_model}}"),
        ], description="Output tokens per second of decode time, from Ollama's eval_duration."), 12, 8),
        L.place(timeseries("Model load time p95", [
            (f"histogram_quantile(0.95, sum by (le, gen_ai_request_model) (rate(ollama_model_load_duration_seconds_bucket{{{m}}}[15m])))",
             "{{gen_ai_request_model}}"),
        ], "s", description="Near zero when warm. High values mean the model was evicted between calls: raise OLLAMA_KEEP_ALIVE or stop model thrash."), 12, 8),

        L.row("Throughput"),
        L.place(timeseries("Tokens / s", [
            (f"sum by (gen_ai_token_type) (model:gen_ai_tokens:rate5m{{{m}}})", "{{gen_ai_token_type}}"),
        ], stack=True), 12, 8),
        L.place(timeseries("Requests / s by model and API", [
            (f"sum by (gen_ai_request_model, api) (rate(gen_ai_client_operation_duration_seconds_count{{{m}}}[5m]))",
             "{{gen_ai_request_model}} ({{api}})"),
            (f"sum by (gen_ai_request_model, error_type) (rate(gen_ai_client_operation_duration_seconds_count{{{m},error_type!=\"\"}}[5m]))",
             "errors {{gen_ai_request_model}} {{error_type}}"),
        ], "reqps"), 12, 8),

        L.row("Resources"),
        L.place(table("Resident models", "ollama_loaded_model_bytes",
                      description="From /api/ps. processor=split or cpu means the model does not fit in VRAM."), 8, 8),
        L.place(timeseries("GPU utilisation and memory", [
            ("nvidia_smi_utilization_gpu_ratio", "GPU util"),
            ("nvidia_smi_memory_used_bytes / nvidia_smi_memory_total_bytes", "VRAM used"),
        ], "percentunit", description="From utkuozdemir/nvidia_gpu_exporter. Read against decode speed: low util while decoding means CPU spill."), 8, 8),
        L.place(timeseries("Host CPU busy", [
            ('1 - avg(rate(node_cpu_seconds_total{job="node",mode="idle"}[5m]))', "CPU busy"),
        ], "percentunit"), 8, 8),
    ]
    return dashboard("local-llm", "Local LLM (Ollama)",
                     "Latency, throughput and SLOs for a self-hosted Ollama server, metered by the proxy.",
                     p, [query_var("model", "Model", "label_values(gen_ai_client_operation_duration_seconds_count, gen_ai_request_model)")])


def local_llm_public():
    """Variable-free copy of local-llm for Grafana public sharing.

    Public dashboards do not resolve template variables, so every $model filter
    becomes .* and the variable is dropped. Share this one, not local-llm.
    """
    d = json.loads(json.dumps(local_llm()).replace('=~\\"$model\\"', '=~\\".*\\"'))
    d["uid"] = "local-llm-public"
    d["title"] = "Local LLM (Ollama), public"
    d["templating"] = {"list": []}
    return d


def claude_code():
    L = Layout()
    ev = '{service_name="claude-code"}'
    p = [
        L.row("Spend and volume"),
        L.place(stat("Cost (range)", 'sum(increase(claude_code_cost_usage_USD_total[$__range]))', "currencyUSD", decimals=2), 4, 4),
        L.place(stat("Tokens (range)", 'sum(increase(claude_code_token_usage_tokens_total[$__range]))', "short"), 4, 4),
        L.place(stat("Cache read share (1h)", "claude_code_cache_read:ratio_rate1h", "percentunit", decimals=1,
                     description="Input tokens served from the prompt cache. Drops after compaction or when the system prompt changes."), 4, 4),
        L.place(stat("Sessions (range)", 'sum(increase(claude_code_session_count_total[$__range]))', decimals=0), 4, 4),
        L.place(stat("Active time (range)", 'sum(increase(claude_code_active_time_seconds_total[$__range]))', "s"), 4, 4),
        L.place(stat("Lines changed (range)", 'sum(increase(claude_code_lines_of_code_count_total[$__range]))', decimals=0), 4, 4),

        L.place(timeseries("Cost per hour by model", [
            ("model:claude_code_cost_usd:rate1h", "{{model}}"),
        ], "currencyUSD", stack=True), 12, 8),
        L.place(timeseries("Tokens / s by type", [
            ("sum by (type) (type:claude_code_tokens:rate5m)", "{{type}}"),
        ], stack=True, description="cacheRead dominating is healthy: long sessions re-read their context cheaply."), 12, 8),
        L.place(timeseries("Cost by source (main, subagent, auxiliary)", [
            ("sum by (query_source) (increase(claude_code_cost_usage_USD_total[1h]))", "{{query_source}}"),
        ], "currencyUSD", stack=True, description="Subagent fan-out shows up here first."), 12, 8),
        L.place(timeseries("Cost by skill and agent", [
            ('sum by (skill_name) (increase(claude_code_cost_usage_USD_total{skill_name!=""}[1h]))', "skill {{skill_name}}"),
            ('sum by (agent_name) (increase(claude_code_cost_usage_USD_total{agent_name!=""}[1h]))', "agent {{agent_name}}"),
        ], "currencyUSD"), 12, 8),

        L.row("Tools and API (events in Loki)"),
        L.place(table("Tool calls by tool and outcome",
                      f'sum by (tool_name, success) (count_over_time({ev} | event_name="tool_result" [$__range]))',
                      ds=LOKI), 8, 9),
        L.place({
            "type": "timeseries", "title": "Tool duration p95 by tool", "datasource": LOKI,
            "targets": [target(f'quantile_over_time(0.95, {ev} | event_name="tool_result" | unwrap duration_ms [5m]) by (tool_name)',
                               "{{tool_name}}", ds=LOKI)],
            "fieldConfig": {"defaults": {"unit": "ms"}, "overrides": []},
        }, 8, 9),
        L.place({
            "type": "timeseries", "title": "API latency p95 and errors", "datasource": LOKI,
            "targets": [
                target(f'quantile_over_time(0.95, {ev} | event_name="api_request" | unwrap duration_ms [5m]) by (model)',
                       "p95 {{model}}", ds=LOKI),
                target(f'sum by (status_code) (count_over_time({ev} | event_name="api_error" [5m]))',
                       "errors {{status_code}}", ds=LOKI, ref="B"),
            ],
            "fieldConfig": {"defaults": {"unit": "ms"}, "overrides": []},
        }, 8, 9),
        L.place(logs("Errors and refusals",
                     f'{ev} | event_name=~"api_error|api_refusal|api_retries_exhausted|internal_error"'), 12, 9),
        L.place(logs("Permission decisions",
                     f'{ev} | event_name=~"tool_decision|permission_mode_changed"'), 12, 9),

        L.row("Traces"),
        L.place({
            "type": "table", "title": "Slowest interactions (Tempo)", "datasource": TEMPO,
            "targets": [{"datasource": TEMPO, "refId": "A", "queryType": "traceql", "limit": 20,
                         "query": '{resource.service.name="claude-code" && name="claude_code.interaction"} | select(span.interaction.duration_ms)'}],
        }, 12, 9),
        L.place(timeseries("Span rate and p95 by span name (Tempo span metrics)", [
            ('sum by (span_name) (rate(traces_spanmetrics_calls_total{service="claude-code"}[5m]))', "{{span_name}}"),
        ], "reqps"), 12, 9),
    ]
    return dashboard("claude-code", "Claude Code agents",
                     "Spend, tokens, tools and traces from Claude Code's OpenTelemetry export.",
                     p, time_from="now-24h")


if __name__ == "__main__":
    OUT.mkdir(exist_ok=True)
    for d in (local_llm(), local_llm_public(), claude_code()):
        path = OUT / f"{d['uid']}.json"
        path.write_text(json.dumps(d, indent=2) + "\n")
        print(f"wrote {path} ({len(d['panels'])} panels)")
