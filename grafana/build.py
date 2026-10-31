"""Build the Grafana dashboards as JSON from Python.

Run `python3 grafana/build.py` after editing; commit the generated JSON.
Grafana provisions the files from grafana/dashboards/ read-only, so the
repo stays the source of truth and UI edits cannot drift from it.
"""

import json
import re
from datetime import datetime, timezone
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


def stat(title, expr, unit="short", legend="", decimals=None, description="", ds=PROM):
    t = target(expr, legend, ds=ds)
    if ds is LOKI:
        t["queryType"] = "instant"
    p = {
        "type": "stat", "title": title, "description": description, "datasource": ds,
        "targets": [t],
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


def host():
    """Full host view for the box that runs Ollama: node_exporter, nvidia-smi, cAdvisor.

    Trimmed 2026-10-07 from 53 to 34 panels: duplicates, diagnostic-depth and
    stack self-monitoring panels removed. Pre-trim version in git history.
    """
    L = Layout()
    n = 'job="node"'
    cores = f'count(node_cpu_seconds_total{{{n},mode="idle"}})'
    fs = f'{n},fstype!~"tmpfs|overlay|squashfs|nsfs|ramfs|fuse.*",mountpoint!~"/run.*|/var/lib/docker.*|/snap.*"'
    dev = f'{n},device!~"loop.*|ram.*|zd.*"'
    nic = f'{n},device!~"lo|veth.*|docker.*|br-.*|tailscale.*"'
    p = [
        L.row("Overview"),
        L.place(stat("Uptime", f'time() - node_boot_time_seconds{{{n}}}', "s"), 3, 4),
        L.place(stat("CPU busy", f'1 - avg(rate(node_cpu_seconds_total{{{n},mode="idle"}}[5m]))', "percentunit", decimals=1), 3, 4),
        L.place(stat("Load 5 per core", f'node_load5{{{n}}} / scalar({cores})', decimals=2), 3, 4),
        L.place(stat("Memory used", f'1 - node_memory_MemAvailable_bytes{{{n}}} / node_memory_MemTotal_bytes{{{n}}}', "percentunit", decimals=1), 3, 4),
        L.place(stat("Root filesystem used", f'1 - node_filesystem_avail_bytes{{{n},mountpoint="/"}} / node_filesystem_size_bytes{{{n},mountpoint="/"}}', "percentunit", decimals=1), 3, 4),
        L.place(stat("GPU temperature", 'max(nvidia_smi_temperature_gpu)', "celsius"), 3, 4),
        L.place(stat("GPU power", 'sum(nvidia_smi_power_draw_watts)', "watt"), 3, 4),
        L.place(stat("Failed systemd units", f'count(node_systemd_unit_state{{{n},state="failed"}} == 1) or vector(0)', decimals=0), 3, 4),

        L.row("CPU"),
        L.place(timeseries("CPU by mode", [
            (f'sum by (mode) (rate(node_cpu_seconds_total{{{n},mode!="idle"}}[5m])) / scalar({cores})', "{{mode}}"),
        ], "percentunit", stack=True), 12, 8),
        L.place(timeseries("Busy per core", [
            (f'1 - rate(node_cpu_seconds_total{{{n},mode="idle"}}[5m])', "cpu {{cpu}}"),
        ], "percentunit"), 12, 8),
        L.place(timeseries("Load average", [
            (f'node_load1{{{n}}}', "1m"), (f'node_load5{{{n}}}', "5m"), (f'node_load15{{{n}}}', "15m"), (cores, "cores"),
        ]), 12, 8),
        L.place(timeseries("Pressure stall (PSI)", [
            (f'rate(node_pressure_cpu_waiting_seconds_total{{{n}}}[5m])', "cpu waiting"),
            (f'rate(node_pressure_memory_waiting_seconds_total{{{n}}}[5m])', "memory waiting"),
            (f'rate(node_pressure_memory_stalled_seconds_total{{{n}}}[5m])', "memory stalled"),
            (f'rate(node_pressure_io_waiting_seconds_total{{{n}}}[5m])', "io waiting"),
            (f'rate(node_pressure_io_stalled_seconds_total{{{n}}}[5m])', "io stalled"),
        ], "percentunit", description="Share of wall time tasks spent waiting on the resource. Stalled = all tasks blocked at once."), 12, 8),

        L.row("Memory"),
        L.place(timeseries("Memory breakdown", [
            (f'node_memory_MemTotal_bytes{{{n}}} - node_memory_MemFree_bytes{{{n}}} - node_memory_Buffers_bytes{{{n}}} - node_memory_Cached_bytes{{{n}}} - node_memory_SReclaimable_bytes{{{n}}}', "used"),
            (f'node_memory_Buffers_bytes{{{n}}}', "buffers"),
            (f'node_memory_Cached_bytes{{{n}}} + node_memory_SReclaimable_bytes{{{n}}}', "cache"),
            (f'node_memory_MemFree_bytes{{{n}}}', "free"),
        ], "bytes", stack=True), 12, 8),
        L.place(timeseries("Swap and paging", [
            (f'node_memory_SwapTotal_bytes{{{n}}} - node_memory_SwapFree_bytes{{{n}}}', "swap used"),
        ], "bytes"), 12, 8),
        L.place(timeseries("ZFS ARC", [
            (f'node_zfs_arc_size{{{n}}}', "ARC size"), (f'node_zfs_arc_c_max{{{n}}}', "ARC max"),
        ], "bytes", description="ZFS cache competes with model loading for RAM."), 24, 8),

        L.row("Disk"),
        L.place(timeseries("Throughput", [
            (f'rate(node_disk_read_bytes_total{{{dev}}}[5m])', "{{device}} read"),
            (f'-rate(node_disk_written_bytes_total{{{dev}}}[5m])', "{{device}} write"),
        ], "Bps", description="Writes plotted below zero."), 8, 8),
        L.place(timeseries("IOPS", [
            (f'rate(node_disk_reads_completed_total{{{dev}}}[5m])', "{{device}} read"),
            (f'-rate(node_disk_writes_completed_total{{{dev}}}[5m])', "{{device}} write"),
        ], "iops"), 8, 8),
        L.place(timeseries("Utilisation and latency", [
            (f'rate(node_disk_io_time_seconds_total{{{dev}}}[5m])', "{{device}} busy"),
        ], "percentunit"), 8, 8),
        L.place(timeseries("Read and write latency", [
            (f'rate(node_disk_read_time_seconds_total{{{dev}}}[5m]) / rate(node_disk_reads_completed_total{{{dev}}}[5m])', "{{device}} read"),
            (f'rate(node_disk_write_time_seconds_total{{{dev}}}[5m]) / rate(node_disk_writes_completed_total{{{dev}}}[5m])', "{{device}} write"),
        ], "s"), 8, 8),
        L.place(timeseries("Filesystem used", [
            (f'1 - node_filesystem_avail_bytes{{{fs}}} / node_filesystem_size_bytes{{{fs}}}', "{{mountpoint}}"),
        ], "percentunit"), 16, 8),

        L.row("Network"),
        L.place(timeseries("Traffic", [
            (f'rate(node_network_receive_bytes_total{{{nic}}}[5m]) * 8', "{{device}} in"),
            (f'-rate(node_network_transmit_bytes_total{{{nic}}}[5m]) * 8', "{{device}} out"),
        ], "bps", description="Outbound plotted below zero. Tailscale and container bridges excluded."), 8, 8),
        L.place(timeseries("Errors and drops", [
            (f'rate(node_network_receive_errs_total{{{nic}}}[5m])', "{{device}} rx errors"),
            (f'rate(node_network_transmit_errs_total{{{nic}}}[5m])', "{{device}} tx errors"),
            (f'rate(node_network_receive_drop_total{{{nic}}}[5m])', "{{device}} rx drops"),
            (f'rate(node_network_transmit_drop_total{{{nic}}}[5m])', "{{device}} tx drops"),
        ]), 8, 8),
        L.place(timeseries("TCP", [
            (f'node_netstat_Tcp_CurrEstab{{{n}}}', "established"),
            (f'node_sockstat_TCP_tw{{{n}}}', "time-wait"),
            (f'rate(node_netstat_Tcp_RetransSegs{{{n}}}[5m])', "retransmits/s"),
        ]), 8, 8),

        L.row("GPU and sensors"),
        L.place(timeseries("Utilisation", [
            ("nvidia_smi_utilization_gpu_ratio", "GPU"), ("nvidia_smi_utilization_memory_ratio", "memory controller"),
            ("nvidia_smi_utilization_encoder_ratio", "encoder"), ("nvidia_smi_utilization_decoder_ratio", "decoder"),
        ], "percentunit"), 8, 8),
        L.place(timeseries("VRAM", [
            ("nvidia_smi_memory_used_bytes", "used"), ("nvidia_smi_memory_total_bytes", "total"),
        ], "bytes"), 8, 8),
        L.place(timeseries("Temperature and fan", [
            ("nvidia_smi_temperature_gpu", "temperature"),
        ], "celsius"), 8, 8),
        L.place(timeseries("Power", [
            ("nvidia_smi_power_draw_watts", "draw"), ("nvidia_smi_enforced_power_limit_watts", "limit"),
        ], "watt"), 8, 8),
        L.place(timeseries("Clocks", [
            ("nvidia_smi_clocks_current_sm_clock_hz", "SM"), ("nvidia_smi_clocks_current_memory_clock_hz", "memory"),
            ("nvidia_smi_clocks_max_sm_clock_hz", "SM max"),
        ], "hertz"), 8, 8),
        L.place(timeseries("Throttling", [
            ("nvidia_smi_clocks_event_reasons_sw_power_cap", "power cap"),
            ("nvidia_smi_clocks_event_reasons_hw_slowdown", "hw slowdown"),
            ("nvidia_smi_clocks_event_reasons_hw_thermal_slowdown", "hw thermal"),
            ("nvidia_smi_clocks_event_reasons_sw_thermal_slowdown", "sw thermal"),
            ("nvidia_smi_fan_speed_ratio", "fan speed"),
        ], description="1 = active. Power cap during decode is normal; thermal is not."), 8, 8),

        L.place(timeseries("Temperatures", [
            (f'node_hwmon_temp_celsius{{{n}}}', "{{chip}} {{sensor}}"),
        ], "celsius", description="Board and CPU sensors from hwmon; GPU temperature is in the row above."), 24, 8),

        L.row("Containers"),
        L.place(timeseries("Container CPU", [
            ('sum by (name) (rate(container_cpu_usage_seconds_total{job="cadvisor",name!=""}[5m]))', "{{name}}"),
        ], description="Cores used per container."), 12, 8),
        L.place(timeseries("Container memory", [
            ('container_memory_working_set_bytes{job="cadvisor",name!=""}', "{{name}}"),
        ], "bytes"), 12, 8),
        L.place(timeseries("Container network", [
            ('sum by (name) (rate(container_network_receive_bytes_total{job="cadvisor",name!=""}[5m])) * 8', "{{name}} in"),
            ('-sum by (name) (rate(container_network_transmit_bytes_total{job="cadvisor",name!=""}[5m])) * 8', "{{name}} out"),
        ], "bps"), 12, 8),
        L.place(timeseries("Container disk I/O", [
            ('sum by (name) (rate(container_fs_reads_bytes_total{job="cadvisor",name!=""}[5m]))', "{{name}} read"),
            ('-sum by (name) (rate(container_fs_writes_bytes_total{job="cadvisor",name!=""}[5m]))', "{{name}} write"),
        ], "Bps"), 12, 8),

    ]
    return dashboard("host", "Host (Ollama box)",
                     "CPU, memory, disk, network, GPU, sensors and containers on the host that runs Ollama.",
                     p, time_from="now-6h")


def host_public():
    """Public cut of host(): no unit, container, mount, disk, NIC, sensor or job names.

    Per-device series are summed or maxed into one line each; the containers
    row is dropped. Share this one, not host.
    """
    d = host()
    n = 'job="node"'
    fs = f'{n},fstype!~"tmpfs|overlay|squashfs|nsfs|ramfs|fuse.*",mountpoint!~"/run.*|/var/lib/docker.*|/snap.*"'
    dev = f'{n},device!~"loop.*|ram.*|zd.*"'
    nic = f'{n},device!~"lo|veth.*|docker.*|br-.*|tailscale.*"'
    swap = {
        "Throughput": [(f'sum(rate(node_disk_read_bytes_total{{{dev}}}[5m]))', "read"),
                       (f'-sum(rate(node_disk_written_bytes_total{{{dev}}}[5m]))', "write")],
        "IOPS": [(f'sum(rate(node_disk_reads_completed_total{{{dev}}}[5m]))', "read"),
                 (f'-sum(rate(node_disk_writes_completed_total{{{dev}}}[5m]))', "write")],
        "Utilisation and latency": [(f'max(rate(node_disk_io_time_seconds_total{{{dev}}}[5m]))', "busiest disk")],
        "Read and write latency": [
            (f'sum(rate(node_disk_read_time_seconds_total{{{dev}}}[5m])) / sum(rate(node_disk_reads_completed_total{{{dev}}}[5m]))', "read"),
            (f'sum(rate(node_disk_write_time_seconds_total{{{dev}}}[5m])) / sum(rate(node_disk_writes_completed_total{{{dev}}}[5m]))', "write")],
        "Filesystem used": [(f'max(1 - node_filesystem_avail_bytes{{{fs}}} / node_filesystem_size_bytes{{{fs}}})', "fullest filesystem")],
        "Traffic": [(f'sum(rate(node_network_receive_bytes_total{{{nic}}}[5m])) * 8', "in"),
                    (f'-sum(rate(node_network_transmit_bytes_total{{{nic}}}[5m])) * 8', "out")],
        "Errors and drops": [
            (f'sum(rate(node_network_receive_errs_total{{{nic}}}[5m]) + rate(node_network_transmit_errs_total{{{nic}}}[5m]))', "errors"),
            (f'sum(rate(node_network_receive_drop_total{{{nic}}}[5m]) + rate(node_network_transmit_drop_total{{{nic}}}[5m]))', "drops")],
        "Temperatures": [(f'max(node_hwmon_temp_celsius{{{n}}})', "hottest sensor"),
                         (f'avg(node_hwmon_temp_celsius{{{n}}})', "average")],
    }
    out, skip = [], False
    for p in d["panels"]:
        if p.get("type") == "row":
            skip = p["title"] == "Containers"
        if skip:
            continue
        if p.get("title") in swap:
            p["targets"] = [target(e, l, ref=chr(65 + i)) for i, (e, l) in enumerate(swap[p["title"]])]
        for tg in p.get("targets", []):
            tg["expr"] = f'max without (instance, job, uuid, name, device, mountpoint, fstype, chip, sensor) ({tg["expr"]})'
        out.append(p)
    d["panels"] = out
    d["uid"] = "host-public"
    d["title"] = "Host (Ollama box), public"
    return d


CC_EVENTS = '{service_name=~"claude-code.*"}'
CC_REQUESTS = CC_EVENTS + ' | event_name="api_request"'
CC_TOKENS = (("input_tokens", "input"), ("output_tokens", "output"),
             ("cache_read_tokens", "cacheRead"), ("cache_creation_tokens", "cacheCreation"))
# Threshold of the ClaudeCodeSpendSpike alert (prometheus/rules/alerts.yml); keep the two in step.
CC_SPEND_ALERT_USD = 40
# When Prometheus loaded the rule with that threshold. Earlier ALERTS samples come from older
# rules (USD 20, and before 2026-10-09 07:27 UTC the inflated counters), so the panel replays
# the current rule before this point. Move it whenever the threshold or the input changes.
CC_SPEND_ALERT_SINCE = int(datetime(2026, 10, 9, 11, 0, 21, tzinfo=timezone.utc).timestamp())


def spend_alert_state():
    """State of ClaudeCodeSpendSpike as one row: 0 below the threshold, 1 pending, 2 firing.

    No Alertmanager runs, and public dashboards drop data-source annotations (the
    public annotations endpoint returns none), so the alert shows as a panel.
    From CC_SPEND_ALERT_SINCE the row is the alert's own ALERTS series. Before it, the
    row replays the rule on the recorded hourly cost: pending above the threshold,
    firing once the last 10 minutes all were. The recorded cost starts 2026-10-09
    07:29 UTC; earlier ranges show no data. max_over_time over the step keeps short
    pending spells visible on long ranges.
    """
    alerts = 'ALERTS{alertname="ClaudeCodeSpendSpike", alertstate="%s"}'
    cost = "sum(model:claude_code_cost_usd:sum1h)"
    live = f"(max(2 * {alerts % 'firing'} or {alerts % 'pending'}) or vector(0))"
    replay = f"(({cost} > bool {CC_SPEND_ALERT_USD}) + (min_over_time({cost}[10m:1m]) > bool {CC_SPEND_ALERT_USD}))"
    now = (f"({live} and on() (vector(time()) >= {CC_SPEND_ALERT_SINCE})) "
           f"or ({replay} and on() (vector(time()) < {CC_SPEND_ALERT_SINCE}))")
    expr = f"max_over_time(({now})[$__interval:1m])"
    states = (("below USD %d" % CC_SPEND_ALERT_USD, "green"), ("pending", "yellow"), ("firing", "red"))
    return {
        "type": "state-timeline", "datasource": PROM, "interval": "1m",
        "title": f"Spend alert: trailing hour above USD {CC_SPEND_ALERT_USD}",
        "description": f"The ClaudeCodeSpendSpike Prometheus alert. Pending once the cost of the trailing hour, all models "
                       f"together, passes USD {CC_SPEND_ALERT_USD}; firing after 10 minutes above it. Before the rule went "
                       f"live on 2026-10-09 at 11:00 UTC the row replays it on the recorded hourly cost, which starts "
                       f"at 07:29 UTC that day.",
        "targets": [target(expr, "ClaudeCodeSpendSpike")],
        # Colours come from the value mappings; with threshold colours the legend lists "< 1", "1+", "2+".
        "fieldConfig": {"defaults": {
            "color": {"mode": "fixed", "fixedColor": "green"},
            "mappings": [{"type": "value", "options": {
                str(i): {"text": t, "color": c, "index": i} for i, (t, c) in enumerate(states)}}],
            "custom": {"fillOpacity": 80, "lineWidth": 0},
        }, "overrides": []},
        "options": {"showValue": "never", "mergeValues": True, "alignValue": "left", "rowHeight": 0.8,
                    "legend": {"showLegend": True, "displayMode": "list", "placement": "bottom"},
                    "tooltip": {"mode": "single"}},
    }


def request_sum(field, window, by=(), where=""):
    """Sum of one field of Claude Code's api_request events over `window` (LogQL).

    Claude Code writes one api_request event per API call with its cost and token
    counts, so the sum is exact. `keep` drops every other label before the unwrap;
    without it each request (request_id, trace_id) would be a series of its own.
    """
    sel = CC_REQUESTS + (f" | {where}" if where else "")
    inner = f"sum_over_time({sel} | keep {', '.join((*by, field))} | unwrap {field} [{window}])"
    return f"sum by ({', '.join(by)}) ({inner})" if by else f"sum({inner})"


def claude_code():
    """Spend and tokens come from api_request events in Loki, not from Claude Code's counters.

    The desktop app runs many Claude Code processes at once, and without a per-process
    attribute they all write the same Prometheus series; every interleaved export reads
    as a counter reset (230,328 resets and USD 1.97M of phantom increase in the week to
    2026-10-09). See the README, "Cost and tokens from events".
    """
    L = Layout()
    ev = CC_EVENTS
    r = "$__range"
    cache_share = (f"{request_sum('cache_read_tokens', r)} / ({request_sum('input_tokens', r)} + "
                   f"{request_sum('cache_read_tokens', r)} + {request_sum('cache_creation_tokens', r)})")
    cost_hour = loki_series("Cost per hour by model", [
        (request_sum("cost_usd", "1h", ("model",)), "{{model}}"),
    ], "currencyUSD", stack=True,
        description=f"Cost of the API calls in the trailing hour. The red line is the USD {CC_SPEND_ALERT_USD} threshold "
                    "of the spend alert; the stacked total is what the alert reads.")
    # Threshold line and band above it; the soft max keeps the line on screen on quiet days.
    cost_hour["fieldConfig"]["defaults"]["thresholds"] = {"mode": "absolute", "steps": [
        {"color": "transparent", "value": None}, {"color": "red", "value": CC_SPEND_ALERT_USD}]}
    cost_hour["fieldConfig"]["defaults"]["custom"].update(
        thresholdsStyle={"mode": "dashed+area"}, axisSoftMax=CC_SPEND_ALERT_USD * 1.25)
    p = [
        L.row("Spend and volume"),
        L.place(stat("Cost (range)", request_sum("cost_usd", r), "currencyUSD", decimals=2, ds=LOKI,
                     description="Sum of cost_usd over every API call in the range, as Claude Code prices it."), 4, 4),
        L.place(stat("Tokens (range)", " + ".join(request_sum(f, r) for f, _ in CC_TOKENS), ds=LOKI,
                     description="Input, output, cache read and cache write tokens of every API call in the range."), 4, 4),
        L.place(stat("Cache read share (range)", cache_share, "percentunit", decimals=1, ds=LOKI,
                     description="Input tokens served from the prompt cache. Drops after compaction or when the system prompt changes."), 4, 4),
        L.place(stat("API requests (range)", f"sum(count_over_time({CC_REQUESTS} [{r}]))", decimals=0, ds=LOKI), 4, 4),
        L.place(stat("Prompts (range)", f'sum(count_over_time({ev} | event_name="user_prompt" [{r}]))', decimals=0, ds=LOKI), 4, 4),
        L.place(stat("Tool calls (range)", f'sum(count_over_time({ev} | event_name="tool_result" [{r}]))', decimals=0, ds=LOKI), 4, 4),

        L.place(spend_alert_state(), 24, 4),
        L.place(cost_hour, 12, 8),
        L.place(loki_series("Tokens / s by type", [
            (f"sum(rate({CC_REQUESTS} | keep {f} | unwrap {f} [5m]))", name) for f, name in CC_TOKENS
        ], stack=True, description="cacheRead dominating is healthy: long sessions re-read their context cheaply."), 12, 8),
        L.place(loki_series("Cost by source (main, subagent, auxiliary)", [
            (request_sum("cost_usd", "1h", ("query_source",)), "{{query_source}}"),
        ], "currencyUSD", stack=True,
            description="Trailing hour. sdk is the desktop app's main loop, agent:* are subagents, the rest are side calls "
                        "(prompt suggestions, compaction, web fetch). Subagent fan-out shows up here first."), 12, 8),
        L.place(loki_series("Cost by skill and agent", [
            (request_sum("cost_usd", "1h", ("skill_name",), 'skill_name!=""'), "skill {{skill_name}}"),
            (request_sum("cost_usd", "1h", ("agent_name",), 'agent_name!=""'), "agent {{agent_name}}"),
        ], "currencyUSD", description="Trailing hour."), 12, 8),

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
                         "query": '{resource.service.name=~"claude-code.*" && name="claude_code.interaction"} | select(span.interaction.duration_ms)'}],
        }, 12, 9),
        L.place(timeseries("Span rate and p95 by span name (Tempo span metrics)", [
            ('sum by (span_name) (rate(traces_spanmetrics_calls_total{service=~"claude-code.*"}[5m]))', "{{span_name}}"),
        ], "reqps"), 12, 9),
    ]
    return dashboard("claude-code", "Claude Code agents",
                     "Spend, tokens, tools and traces from Claude Code's OpenTelemetry export. "
                     "Spend and tokens are summed from one event per API call.",
                     p, time_from="now-24h")


def claude_code_public():
    """Public cut of claude_code(): aggregates only, no log lines, traces or skill names.

    The log and trace rows show commands and file paths, and skill and MCP names
    describe private work, so those panels are dropped. The spend row stays: its
    Loki queries return sums by model, source or token type, never a log line.
    Every query also sheds the identifying labels (process_owner, MCP, skill, OS
    and version details).
    """
    d = claude_code()
    drop_rows = {"Tools and API (events in Loki)", "Traces"}
    drop_titles = {"Cost by skill and agent"}
    strip = ("instance, job, process_owner, os_type, os_version, host_arch, service_version, service_name, "
             "terminal_type, mcp_server_name, mcp_tool_name, skill_name, agent_name, session_id, "
             "user_account_uuid, user_email, organization_id, claude_deployment_mode, start_type")
    out, skip = [], False
    for p in d["panels"]:
        if p.get("type") == "row":
            skip = p["title"] in drop_rows
        if skip or p.get("title") in drop_titles:
            continue
        for tg in p.get("targets", []):
            tg["expr"] = f'sum without ({strip}) ({tg["expr"]})'
        out.append(p)
    d["panels"] = out
    d["uid"] = "claude-code-public"
    d["title"] = "Claude Code agents, public"
    return d


# ---------------------------------------------------------------- website deploys

DEPLOY_SITES = ("machinebehavior.io", "tychat.io", "uncovertechtalent.com")


def loki_series(title, targets, unit="short", draw="line", stack=False, description=""):
    p = timeseries(title, [], unit, stack, description)
    p["datasource"] = LOKI
    p["targets"] = [target(e, l, ds=LOKI, ref=chr(65 + i)) for i, (e, l) in enumerate(targets)]
    if draw == "bars":
        p["fieldConfig"]["defaults"]["custom"].update(drawStyle="bars", fillOpacity=80, lineWidth=0)
    return p


def site_deploys():
    """Website deploys: every GitHub Actions run of the three sites, the gate that guards them,
    and what the deploy shipped (map snapshot) next to the decision-layer probe it reports.

    Public by construction: no template variables, and the exporter keeps titles, SHAs and
    URLs of private repos out of Loki. Share this dashboard as it is.
    """
    L = Layout()
    outcome_colors = [{"matcher": {"id": "byName", "options": o}, "properties": [{"id": "color", "value": {"mode": "fixed", "fixedColor": c}}]}
                      for o, c in (("deployed", "green"), ("blocked", "red"), ("failed", "orange"), ("cancelled", "text"))]
    deployed_map = [{"type": "value", "options": {"1": {"text": "deployed", "color": "green", "index": 0},
                                                  "0": {"text": "not deployed", "color": "red", "index": 1}}}]
    check_map = [{"type": "value", "options": {"0": {"text": "fail", "color": "red", "index": 0},
                                               "1": {"text": "pending", "color": "blue", "index": 1},
                                               "2": {"text": "partial", "color": "yellow", "index": 2},
                                               "3": {"text": "pass", "color": "green", "index": 3}}}]
    def now(panel, steps=((None, "blue"),), size=None):
        """Current value only (the exporter's series start when it does), with explicit colours."""
        for t in panel["targets"]:
            t["instant"] = True
        panel["options"]["graphMode"] = "none"
        panel["fieldConfig"]["defaults"]["thresholds"] = {"mode": "absolute", "steps": [{"value": v, "color": c} for v, c in steps]}
        if size:
            panel["options"]["text"] = {"valueSize": size}
        return panel

    p = [L.row("Right now")]

    last = stat("Latest run deployed", "site_deploy_last_deployed", legend="{{site}}",
                description="1 when the latest run deployed. A run the gate blocked leaves the previous build live and shows here as not deployed.")
    last["fieldConfig"]["defaults"]["mappings"] = deployed_map
    last["options"].update(colorMode="background", graphMode="none", textMode="value_and_name")
    p.append(L.place(now(last), 9, 5))
    ago = stat("Since the latest deploy", "time() - site_deploy_last_finished_timestamp_seconds", "s", "{{site}}",
               description="Time since each site's latest deploy run finished.")
    ago["options"].update(graphMode="none", textMode="value_and_name")
    p.append(L.place(now(ago, ((None, "green"), (86400, "yellow"), (7 * 86400, "red"))), 9, 5))
    p.append(L.place(now(stat("Open gate findings", "sum(site_conformity_findings_open)",
                              description="Findings the conformity gate holds open across the three sites."), ((None, "green"), (1, "red"))), 3, 5))
    p.append(L.place(now(stat("Runs seen", "sum(site_deploy_runs_total)",
                              description="Completed deploy runs the exporter has read from GitHub Actions.")), 3, 5))

    p.append(L.row("History"))
    runs = loki_series("Deploy runs by outcome", [
        ('sum by (outcome) (count_over_time({job="site-deploys"}[$__interval]))', "{{outcome}}")], draw="bars", stack=True,
        description="Every completed run of the deploy workflows, per hour. Blocked = the conformity gate failed and the deploy job never ran.")
    runs["interval"] = "1h"
    p.append(L.place(runs, 12, 8))
    dur = loki_series("Run duration, push to live", [
        ('max by (site) (max_over_time({job="site-deploys", outcome="deployed"} | json | unwrap duration_s [$__interval]))', "{{site}}")],
        "s", description="Wall time of each deployed run, from the first job starting to the deploy job finishing.")
    dur["fieldConfig"]["defaults"]["custom"].update(drawStyle="points", pointSize=6, showPoints="always")
    p.append(L.place(dur, 12, 8))
    marked = dur["id"]
    p[-2]["fieldConfig"]["overrides"] = outcome_colors

    steps = {
        "type": "bargauge", "title": "Where the time goes (machinebehavior.io)", "datasource": LOKI,
        "description": "Average duration of each workflow step over the selected range.",
        "targets": [target('avg by (step) (avg_over_time({job="site-deploy-steps", site="machinebehavior.io"} | json | unwrap duration_s [$__interval]))',
                           "{{step}}", ds=LOKI, queryType="range")],
        "interval": "1h",
        "fieldConfig": {"defaults": {"unit": "s", "color": {"mode": "continuous-BlYlRd"}}, "overrides": []},
        "options": {"orientation": "horizontal", "displayMode": "gradient", "showUnfilled": True, "valueMode": "color",
                    "reduceOptions": {"calcs": ["mean"], "values": False}},
    }
    p.append(L.place(steps, 12, 9))
    gate = {
        "type": "state-timeline", "title": "Gate checks (machinebehavior.io)", "datasource": LOKI,
        "description": "Result of each conformity check on every run. Partial and pending checks are recorded and do not block; a fail blocks the deploy.",
        "targets": [target('min by (check) (min_over_time({job="site-gate-checks", site="machinebehavior.io"} | json | unwrap state [$__interval]))',
                           "{{check}}", ds=LOKI, queryType="range")],
        "interval": "1h",
        "fieldConfig": {"defaults": {"mappings": check_map, "custom": {"fillOpacity": 80, "lineWidth": 0}}, "overrides": []},
        "options": {"showValue": "never", "rowHeight": 0.8, "mergeValues": True, "legend": {"showLegend": False}},
    }
    p.append(L.place(gate, 12, 9))

    p.append(L.row("What the deploy shipped"))
    for title, expr, desc in (
            ("Map nodes", 'site_map_nodes', "Pages, posts, repos, threads and tags in the map snapshot the latest deploy shipped."),
            ("Map links", 'site_map_links', "Links in that snapshot."),
            ("Links carried forward", 'site_map_carried_links', "Substack refuses the CI runner, so links of unreachable pages come from the previous snapshot."),
            ("Pages dropped", 'site_map_dropped_pages', "Pages that answered with something other than HTML.")):
        p.append(L.place(now(stat(title, expr, description=desc), size=44), 3, 5))
    fold = stat("Decision probe: fold rate", "site_probe_fold_ratio", "percentunit", "{{arm}}",
                description="Weekly probe from the home server: share of WAIT runs where a reference model switched to BUY under scripted pushback. "
                            "Record-only until 2026-11-04; it never blocks a deploy.")
    fold["options"].update(graphMode="none", textMode="value_and_name")
    p.append(L.place(now(fold), 12, 5))

    p.append(L.row("Run log"))
    runs = logs("Deploy runs", '{job="site-deploys"} | json | line_format "{{.site}}  #{{.run_number}}  {{.outcome}}  {{.duration_s}}s  gate={{.gate}}  {{.title}}"',
                description="One line per run. Titles show for the public repos only.")
    p.append(L.place(runs, 24, 10))

    d = dashboard("site-deploys", "Website deploys",
                  "GitHub Actions deploys of machinebehavior.io, tychat.io and uncovertechtalent.com, the conformity gate in front of them, "
                  "and what each deploy shipped.", p, refresh="1m", time_from="now-7d")
    d["annotations"] = {"list": [{
        "datasource": LOKI, "enable": True, "name": "Deploys", "iconColor": "rgba(255, 181, 71, 0.9)",
        "expr": '{job="site-deploys"} | json | line_format "{{.site}} #{{.run_number}} {{.outcome}} in {{.duration_s}}s"',
        "tagKeys": "site,outcome", "titleFormat": "Deploy", "textFormat": "",
        "filter": {"exclude": False, "ids": [marked]},
    }]}
    return d


def searxng():
    """The house search engine: a SearXNG on a laptop, its proxy egress and the search wrapper.

    The exporter (client/searxng_exporter.py) pushes from the laptop, so a sleeping laptop
    shows as a gap. Internal only: engine names, egress and failure reasons are operational
    detail, and nothing here is shared publicly.
    """
    L = Layout()
    srch = 'searxng_searches_total{kind="search"}'
    hist = lambda q, m, w: f"histogram_quantile({q}, sum by (le) (rate({m}_bucket[{w}])))"
    p = [
        L.row("Now"),
        L.place(stat("SearXNG up", "searxng_up", "bool_yes_no",
                     description="The exporter's GET / against the instance, every 30 s."), 3, 4),
        L.place(stat("Proxy egress", "searxng_proxy_up", "bool_yes_no",
                     description="Yes when the HTTP proxy answers and exits from the expected IP. The proxied engines depend on it."), 3, 4),
        L.place(stat("Tunnel", 'node_systemd_unit_state{name="aws-socks.service", state="active"}', "bool_yes_no",
                     description="The SSH tunnel on the home server that carries the proxy egress."), 3, 4),
        L.place(stat("Searches / min", f"sum(rate({srch}[5m])) * 60", decimals=1,
                     description="Calls of search.sh, cache hits included."), 3, 4),
        L.place(stat("Latency p95 (1h)", hist(0.95, "searxng_search_duration_seconds", "1h"), "s", decimals=2,
                     description="Upstream calls only (cache misses), without the pacing wait."), 3, 4),
        L.place(stat("Cache hits (24h)", f'sum(increase({srch[:-1]}, cache="hit"}}[24h])) / sum(increase({srch}[24h]))',
                     "percentunit", decimals=0), 3, 4),
        L.place(stat("DEGRADED exits (24h)", f'sum(increase({srch[:-1]}, outcome="degraded"}}[24h]))', decimals=0,
                     description="search.sh exit 5: no results while engines were suspended."), 3, 4),
        L.place(stat("Engines failing now", "count(searxng_engine_state >= 2) or vector(0)", decimals=0,
                     description="Engines whose latest call was rate-limited, blocked, timed out or failed."), 3, 4),

        L.row("Searches"),
        L.place(timeseries("Searches per minute by outcome", [
            (f"sum by (cache, outcome) (rate({srch}[5m])) * 60", "{{outcome}} ({{cache}})"),
        ], stack=True, description="ok = results; empty = none and no engine failed; degraded = none while engines were suspended (exit 5)."), 12, 8),
        L.place(timeseries("Search latency (cache misses)", [
            (hist(0.50, "searxng_search_duration_seconds", "15m"), "p50"),
            (hist(0.95, "searxng_search_duration_seconds", "15m"), "p95"),
            ("sum(rate(searxng_search_wait_seconds_total[15m])) / sum(rate(searxng_search_results_count[15m]))", "mean pacing wait"),
        ], "s", description="Time SearXNG takes to answer, and separately the time search.sh queued for its pacing lock."), 12, 8),
        L.place(timeseries("Results per query", [
            (hist(0.50, "searxng_search_results", "30m"), "p50"),
            ("sum(rate(searxng_search_results_sum[30m])) / sum(rate(searxng_search_results_count[30m]))", "mean"),
        ], description="Merged results per upstream call, all engines together."), 12, 8),
        L.place(timeseries("Engines left out by pacing (per min)", [
            ("sum by (engine) (rate(searxng_engine_skipped_total[15m])) * 60", "{{engine}}"),
        ], description="search.sh leaves an engine out of a call while it is inside its per-engine gap (SEARXNG_ENGINE_GAPS in search.sh)."), 12, 8),

        L.row("Engines"),
    ]
    states = {
        "type": "state-timeline", "title": "Engine state (latest call)", "datasource": PROM,
        "description": "Status of each engine's latest call through search.sh. Rate-limited and blocked engines are suspended "
                       "by SearXNG (300 s and 900 s) and skipped without an upstream request until the suspension ends.",
        "targets": [target("max by (engine) (searxng_engine_state)", "{{engine}}")],
        "fieldConfig": {"defaults": {"custom": {"fillOpacity": 80, "lineWidth": 0}, "mappings": [{"type": "value", "options": {
            "0": {"text": "ok", "color": "green"}, "1": {"text": "empty", "color": "blue"},
            "2": {"text": "rate-limited", "color": "orange"}, "3": {"text": "blocked", "color": "red"},
            "4": {"text": "timeout", "color": "purple"}, "5": {"text": "error", "color": "dark-red"}}}]}, "overrides": []},
        "options": {"showValue": "never", "rowHeight": 0.8, "mergeValues": True, "legend": {"showLegend": False}},
    }
    p.append(L.place(states, 24, 8))
    p += [
        L.place(timeseries("Failed engine calls per minute", [
            ('sum by (engine, status) (rate(searxng_engine_calls_total{status!~"ok|empty"}[15m])) * 60', "{{engine}} {{status}}"),
        ], stack=True, description="Includes calls SearXNG skipped because the engine was still suspended."), 12, 8),
        L.place(timeseries("Results contributed per engine (per min)", [
            ("sum by (engine) (rate(searxng_engine_results_total[15m])) * 60", "{{engine}}"),
        ], stack=True, description="A result found by two engines counts for both."), 12, 8),
        L.place(timeseries("Upstream requests per engine (per min, all clients)", [
            ("sum by (engine) (rate(searxng_engine_upstream_requests_total[5m])) * 60", "{{engine}}"),
        ], description="SearXNG's own counter: every client, not only search.sh. Suspended engines send nothing."), 12, 8),
    ]
    errors = table("Error share since SearXNG started", "searxng_engine_error_ratio",
                   description="From /stats/errors: share of each engine's requests that ended in this error class. Resets on restart.")
    errors["transformations"] = [{"id": "organize", "options": {
        "excludeByName": {"Time": True, "__name__": True, "instance": True, "job": True, "service_name": True}}}]
    errors["fieldConfig"] = {"defaults": {"unit": "percentunit", "decimals": 0}, "overrides": []}
    p.append(L.place(errors, 12, 8))

    p.append(L.row("Egress"))
    egress = table("Engine egress", "searxng_engine_info",
                   description="Enabled general engines and whether they go direct or through the proxy egress, read from the live settings.yml.")
    egress["transformations"] = [{"id": "organize", "options": {
        "excludeByName": {"Time": True, "Value": True, "__name__": True, "instance": True, "job": True, "service_name": True}}}]
    p.append(L.place(egress, 8, 8))
    p.append(L.place(timeseries("Probe latency", [
        ("searxng_probe_duration_seconds", "SearXNG GET /"),
        ("searxng_proxy_probe_duration_seconds", "proxy egress round trip"),
    ], "s"), 16, 8))
    return dashboard("searxng", "SearXNG (house search)",
                     "Health of the self-hosted SearXNG behind search.sh: searches, latency, results, per-engine state, "
                     "rate limits and the proxy egress. Internal; not shared.", p, refresh="1m", time_from="now-24h")


def searxng_public():
    """Public cut of the SearXNG dashboard: instance up, searches, success rate, and each engine's status.

    Shows nothing of the egress setup (no proxy, exit IPs, hosts or which engine leaves through
    which exit) and no raw error text. Every query aggregates to the engine or to a single value,
    so no instance or job labels reach the public data frames.
    """
    L = Layout()
    srch = 'searxng_searches_total{kind="search"'
    state_map = [{"type": "value", "options": {
        "0": {"text": "ok", "color": "green"}, "1": {"text": "no results", "color": "blue"},
        "2": {"text": "rate-limited", "color": "orange"}, "3": {"text": "blocked", "color": "red"},
        "4": {"text": "timeout", "color": "purple"}, "5": {"text": "error", "color": "dark-red"}}}]
    p = [
        L.row("Now"),
        L.place(stat("Search engine up", "max(searxng_up)", "bool_yes_no",
                     description="Whether the self-hosted SearXNG answers, checked every 30 s."), 4, 4),
        L.place(stat("Searches per hour", f"sum(increase({srch}}}[1h]))", decimals=0,
                     description="Searches made by the agents in the last hour, cached answers included."), 4, 4),
        L.place(stat("Searches with results (24h)",
                     f'sum(increase({srch}, outcome="ok"}}[24h])) / sum(increase({srch}}}[24h]))', "percentunit", decimals=1,
                     description="Share of searches in the last 24 hours that returned at least one result."), 4, 4),
        L.place(stat("Results per search (24h)",
                     "sum(increase(searxng_search_results_sum[24h])) / sum(increase(searxng_search_results_count[24h]))",
                     decimals=0, description="Merged results per search, all engines together."), 4, 4),
        L.place(stat("Median search time (24h)",
                     "histogram_quantile(0.5, sum by (le) (increase(searxng_search_duration_seconds_bucket[24h])))", "s",
                     decimals=2, description="Time SearXNG takes to answer a search that is not served from the local cache."), 4, 4),
        L.place(stat("Engines answering now", "count(max by (engine) (searxng_engine_state) <= 1) or vector(0)", decimals=0,
                     description="Engines whose latest call came back without an error."), 4, 4),
        L.row("Search engines"),
    ]
    q = lambda expr, ref: {**target(expr, ref=ref), "instant": True, "format": "table"}
    answered = 'sum by (engine) (increase(searxng_engine_calls_total{status=~"ok|empty"}[24h]))'
    calls = "sum by (engine) (increase(searxng_engine_calls_total[24h]))"
    engines = {
        "type": "table", "title": "Search engines in use", "datasource": PROM,
        "description": "Every engine SearXNG queries for these searches. Now: the result of the engine's latest call "
                       "(blank when nobody searched in the last 30 minutes). Rate-limited and blocked engines are "
                       "paused by SearXNG and tried again a few minutes later. Answered: share of calls in the last 24 hours "
                       "that came back without an error. Median time: the engine's own answer time over the last 24 hours.",
        "targets": [
            q("max by (engine) (searxng_engine_state)", "A"),
            q(f"({answered} / {calls}) and ({calls} > 0)", "B"),
            q("max by (engine) (quantile_over_time(0.5, searxng_engine_response_seconds[24h]))", "C"),
            q("sum by (engine) (increase(searxng_engine_results_total[24h]))", "D"),
        ],
        "transformations": [
            {"id": "merge", "options": {}},
            {"id": "organize", "options": {
                "excludeByName": {"Time": True},
                "renameByName": {"engine": "Engine", "Value #A": "Now", "Value #B": "Answered (24h)",
                                 "Value #C": "Median time", "Value #D": "Results (24h)"}}},
            {"id": "sortBy", "options": {"sort": [{"field": "Engine"}]}},
        ],
        "fieldConfig": {"defaults": {}, "overrides": [
            {"matcher": {"id": "byName", "options": "Now"}, "properties": [
                {"id": "mappings", "value": state_map},
                {"id": "custom.cellOptions", "value": {"type": "color-background"}}]},
            {"matcher": {"id": "byName", "options": "Answered (24h)"}, "properties": [
                {"id": "unit", "value": "percentunit"}, {"id": "decimals", "value": 0}]},
            {"matcher": {"id": "byName", "options": "Median time"}, "properties": [
                {"id": "unit", "value": "s"}, {"id": "decimals", "value": 1}]},
            {"matcher": {"id": "byName", "options": "Results (24h)"}, "properties": [{"id": "decimals", "value": 0}]},
        ]},
        "options": {"showHeader": True},
    }
    p.append(L.place(engines, 12, 12))
    states = {
        "type": "state-timeline", "title": "Engine status over time", "datasource": PROM,
        "description": "Result of each engine's latest call. Gaps mean nobody searched.",
        "targets": [target("max by (engine) (searxng_engine_state)", "{{engine}}")],
        "fieldConfig": {"defaults": {"custom": {"fillOpacity": 80, "lineWidth": 0}, "mappings": state_map}, "overrides": []},
        "options": {"showValue": "never", "rowHeight": 0.8, "mergeValues": True, "legend": {"showLegend": False}},
    }
    p.append(L.place(states, 12, 12))
    p += [
        L.row("Searches"),
        L.place(timeseries("Searches per hour", [
            (f"sum by (outcome) (increase({srch}}}[1h]))", "{{outcome}}"),
        ], stack=True, description="ok: results. empty: no results and no engine failed. degraded: no results while engines were paused."), 8, 8),
        L.place(timeseries("Search time", [
            ("histogram_quantile(0.50, sum by (le) (rate(searxng_search_duration_seconds_bucket[30m])))", "median"),
            ("histogram_quantile(0.95, sum by (le) (rate(searxng_search_duration_seconds_bucket[30m])))", "95th percentile"),
        ], "s", description="Searches not served from the local cache."), 8, 8),
        L.place(timeseries("Results per hour by engine", [
            ("sum by (engine) (increase(searxng_engine_results_total[1h]))", "{{engine}}"),
        ], stack=True, description="A result found by two engines counts for both."), 8, 8),
    ]
    return dashboard("searxng-public", "SearXNG search engines",
                     "Status of the self-hosted SearXNG the agents search through: whether it answers, how many searches "
                     "return results, and how each search engine is doing.", p, refresh="1m", time_from="now-24h")



USD = "currencyUSD"
MIXED = {"type": "datasource", "uid": "-- Mixed --"}
EXPR = {"type": "__expr__", "uid": "__expr__"}


def ratio_gauge(title, expr, description, steps=((None, "green"), (0.8, "orange"), (1, "red")), max_=1.2):
    return {"type": "gauge", "title": title, "description": description, "datasource": PROM,
            "targets": [target(expr)],
            "fieldConfig": {"defaults": {"unit": "percentunit", "decimals": 0, "min": 0, "max": max_,
                                         "thresholds": {"mode": "absolute", "steps": [
                                             {"color": c, "value": v} for v, c in steps]}}, "overrides": []},
            "options": {"reduceOptions": {"calcs": ["lastNotNull"]}, "showThresholdMarkers": True}}


def pivot_bars(title, expr, row, col, description, stack=True):
    """Instant series with `row` and `col` labels (a day and a service, say) as stacked bars per row value."""
    t = target(expr, format="table", instant=True)
    return {"type": "barchart", "title": title, "description": description, "datasource": PROM, "targets": [t],
            "transformations": [
                {"id": "groupingToMatrix", "options": {"columnField": col, "rowField": row, "valueField": "Value",
                                                       "emptyValue": "zero"}},
                {"id": "sortBy", "options": {"sort": [{"field": f"{row}\\{col}"}]}}],
            "fieldConfig": {"defaults": {"unit": USD, "custom": {"fillOpacity": 80, "lineWidth": 0}}, "overrides": []},
            "options": {"xField": f"{row}\\{col}", "stacking": "normal" if stack else "none", "xTickLabelRotation": -45,
                        "xTickLabelSpacing": 100, "legend": {"displayMode": "table", "placement": "right", "calcs": ["sum"]},
                        "tooltip": {"mode": "multi"}}}


def instant(panel):
    """Current-value panel: read the latest sample, no sparkline, no colour on amounts."""
    for t in panel["targets"]:
        if t.get("datasource") == PROM:
            t.update({"instant": True, "range": False})
    if panel["type"] == "stat":
        panel["options"]["graphMode"] = "none"
        panel["options"].setdefault("colorMode", "none")
    return panel


def expr_sum(title, parts, description, unit=USD, decimals=2, math=None):
    """One number from several data sources: each query reduced to its last value, then added, server side.

    `parts` are (ref, target) pairs; the targets run hidden and the sum is the visible result.
    `math` replaces the sum with another Grafana math expression over the reduced values ($RA, $RB, ...).
    Each query ends in `or vector(0)`, so a source with no data in the range is a zero rather than
    a missing operand (a reduce over an empty result returns nothing, and the math then returns
    nothing); the reduce's replaceNN turns a NaN into zero. A Loki query with `or vector(0)` is
    refused over a window longer than Loki's max_query_length (30d1h by default), which the month
    to date passes on the last day of a 31-day month: loki/loki.yml raises the limit to 32d.
    Checked on 2026-10-27 over a range with no data in any source: the sum read 0.
    """
    targets = []
    for ref, t in parts:
        targets.append({**t, "refId": ref, "hide": True})
        targets.append({"datasource": EXPR, "refId": "R" + ref, "type": "reduce", "expression": ref, "reducer": "last",
                        "settings": {"mode": "replaceNN", "replaceWithValue": 0}, "hide": True})
    targets.append({"datasource": EXPR, "refId": "SUM", "type": "math",
                    "expression": math or " + ".join(f"$R{ref}" for ref, _ in parts)})
    return {"type": "stat", "title": title, "description": description, "datasource": MIXED, "targets": targets,
            "fieldConfig": {"defaults": {"unit": unit, "decimals": decimals}, "overrides": []},
            "options": {"reduceOptions": {"calcs": ["lastNotNull"]}, "colorMode": "none", "graphMode": "none"}}


def aws_spend():
    """AWS account spend from the cost exporter (the cost and usage report in S3, or Cost Explorer)
    and AWS Budgets.

    Internal only: the panels show real spend and are never shared publicly. The report lands up to
    three times a day (Cost Explorer: fetched every 12 hours, lagging a day), so this dashboard moves
    a few times a day. Day, month and service are labels on instant series, so the daily
    and monthly charts are instant queries pivoted with groupingToMatrix.
    """
    L = Layout()
    usd = "currencyUSD"
    budget = 'max(aws_budget_limit_usd{period="MONTHLY"})'

    p = [
        L.row("This month"),
        L.place(stat("Month to date", "max(aws_cost_mtd_usd)", usd, decimals=2,
                     description="Unblended cost from the 1st (UTC) to now, today's partial cost included. The source lags by hours."), 4, 5),
        L.place(stat("Monthly budget", budget, usd, decimals=2,
                     description="The MONTHLY cost budget in AWS Budgets."), 4, 5),
        L.place(ratio_gauge("Month to date / budget", f"max(aws_cost_mtd_usd) / {budget}",
                            "Orange at 80% (AWSCostMonthToDateOver80), red at 100%."), 4, 5),
        L.place(stat("Forecast, month end", "max(aws_cost_month_forecast_usd)", usd, decimals=2,
                     description="In report mode the run rate of this month's complete days to month end; in Cost Explorer mode the actual "
                                 "days before the forecast's start plus Cost Explorer's forecast (fetched daily)."), 4, 5),
        L.place(ratio_gauge("Forecast / budget", f"max(aws_cost_month_forecast_usd) / {budget}",
                            "Red above 100% (AWSCostForecastOverBudget)."), 4, 5),
        L.place(stat("AWS Budgets forecast", "max(aws_budget_forecast_usd)", usd, decimals=2,
                     description="The forecast AWS Budgets calculates itself, as a cross-check of the exporter's figure."), 4, 5),

        L.place(stat("Latest day / 28-day median", "max(aws_cost_latest_day_usd) / max(aws_cost_trailing_median_day_usd)",
                     decimals=2, description="Latest UTC day before today against the median of the 28 days before it, both without domain "
                                             "renewals and tax. AWSCostDailySpike fires above 2 when the day is above USD 1."), 4, 4),
        L.place(stat("Latest day", "max(aws_cost_latest_day_usd)", usd, decimals=2,
                     description="The latest complete UTC day, without domain renewals and tax. In Cost Explorer mode it may still be an estimate."), 4, 4),
        L.place(stat("Latest day estimated", "max(aws_cost_latest_day_estimated)", "bool_yes_no",
                     description="Cost Explorer marks recent days as estimates; in report mode the latest complete day is settled by construction."), 4, 4),
        L.place(stat("Data age", 'time() - (max(aws_cost_report_delivery_timestamp_seconds) or max(aws_cost_exporter_fetch_timestamp_seconds{part="current"}))',
                     "s", decimals=0,
                     description="Time since the report's latest delivery landed in S3 (up to three times a day), or since the exporter last "
                                 "asked Cost Explorer for this month (every 12 h)."), 4, 4),
        L.place(stat("Cost Explorer API cost", "max(aws_cost_explorer_spend_usd_total)", usd, decimals=2,
                     description="What this exporter's own Cost Explorer requests have cost, at USD 0.01 each, since it started. In report "
                                 "mode it grows only while a month has no report file yet."), 4, 4),
        L.place(stat("Exporter errors", "max(aws_cost_exporter_errors_total)", decimals=0,
                     description="Failed polls since the cache was created. The cache keeps serving the last good data."), 4, 4),

        L.place(timeseries("Month to date against budget", [
            ("max(aws_cost_mtd_usd)", "month to date"),
            ("max(aws_cost_month_forecast_usd)", "forecast, month end"),
            (budget, "budget"),
            (f"0.8 * {budget}", "80% of budget"),
        ], usd, description="Recorded from the day the exporter started; Prometheus keeps 90 days."), 24, 8),

        L.row("Daily spend"),
        L.place(pivot_bars("Daily spend by service", "aws_cost_day_usd", "day", "service",
                           "Unblended cost per UTC day and service, this month and the three months before. Domain renewals "
                           "and monthly tax post as one-day spikes. The latest days may still change until the next delivery."), 24, 10),

        L.row("Services and months"),
    ]
    top = {"type": "bargauge", "title": "Top services this month", "datasource": PROM,
           "description": "Month to date by service, largest first.",
           "targets": [target("sort_desc(topk(10, aws_cost_mtd_by_service_usd))", "{{service}}", instant=True)],
           "fieldConfig": {"defaults": {"unit": usd, "decimals": 2, "min": 0}, "overrides": []},
           "options": {"orientation": "horizontal", "displayMode": "basic", "showUnfilled": True,
                       "reduceOptions": {"calcs": ["lastNotNull"]}}}
    p.append(L.place(top, 10, 10))
    months = table("Last 3 months by service", "aws_cost_month_usd",
                   description="Unblended cost per calendar month and service; the current month is to date.")
    months["transformations"] = [
        {"id": "groupingToMatrix", "options": {"columnField": "month", "rowField": "service", "valueField": "Value", "emptyValue": "zero"}}]
    months["fieldConfig"] = {"defaults": {"unit": usd, "decimals": 2}, "overrides": []}
    p.append(L.place(months, 14, 10))
    monthly = {"type": "barchart", "title": "Monthly total", "datasource": PROM,
               "description": "All services per calendar month; the current month is to date.",
               "targets": [target("sum by (month) (aws_cost_month_total_usd)", format="table", instant=True)],
               "transformations": [{"id": "organize", "options": {"excludeByName": {"Time": True},
                                                                  "renameByName": {"Value": "total"}}}],
               "fieldConfig": {"defaults": {"unit": usd, "custom": {"fillOpacity": 80, "lineWidth": 0}}, "overrides": []},
               "options": {"xField": "month", "showValue": "always", "legend": {"showLegend": False}}}
    p.append(L.place(monthly, 12, 8))
    budgets = {"type": "table", "title": "AWS Budgets", "datasource": PROM,
               "description": "Every budget in the account as AWS Budgets reports it.",
               "targets": [target("max by (budget, period, unit) (aws_budget_limit_usd)", ref="A", format="table", instant=True),
                           target("max by (budget, period, unit) (aws_budget_actual_usd)", ref="B", format="table", instant=True),
                           target("max by (budget, period, unit) (aws_budget_forecast_usd)", ref="C", format="table", instant=True)],
               "transformations": [
                   {"id": "merge", "options": {}},
                   {"id": "organize", "options": {
                       "excludeByName": {"Time": True, "__name__": True, "instance": True, "job": True},
                       "renameByName": {"Value #A": "limit", "Value #B": "actual", "Value #C": "forecast"}}}],
               # aggregated to budget, period and unit, so merge joins the three into one row per budget
               "fieldConfig": {"defaults": {"unit": usd, "decimals": 2}, "overrides": []},
               "options": {"showHeader": True}}
    p.append(L.place(budgets, 12, 8))
    steps = {"Data age": [("green", None), ("orange", 24 * 3600), ("red", 36 * 3600)],
             "Latest day / 28-day median": [("green", None), ("orange", 1.5), ("red", 2)],
             "Exporter errors": [("green", None), ("red", 1)]}
    for panel in p:  # current-value panels read the latest sample; a 30-day range step would skip a young series
        if panel["type"] in ("stat", "gauge"):
            for t in panel["targets"]:
                t.update({"instant": True, "range": False})
        if panel["type"] == "stat":  # amounts carry no colour; only the health stats have thresholds
            if panel["title"] in steps:
                panel["fieldConfig"]["defaults"]["thresholds"] = {"mode": "absolute", "steps": [
                    {"color": c, "value": v} for c, v in steps[panel["title"]]]}
            else:
                panel["options"]["colorMode"] = "none"
    p.append(L.row("Exporter"))
    p.append(L.place(timeseries("AWS API requests per day", [
        ("sum by (api) (increase(aws_cost_exporter_api_requests_total[1d]))", "{{api}}"),
    ], description="Cost Explorer requests cost USD 0.01 each; S3 requests for the report and AWS Budgets requests cost nothing at this size."), 24, 7))
    return dashboard("aws-spend", "AWS spend",
                     "AWS account spend from the cost exporter (the cost and usage report in S3, or Cost Explorer) and AWS Budgets: "
                     "month to date and forecast against the monthly budget, daily spend by service, top services, the last three "
                     "months. Internal; not shared.",
                     p, refresh="15m", time_from="now-30d")


# ---------------------------------------------------------------- finops

ANOMALY_FLOOR_USD = 0.1  # a median below this counts as this in the anomaly ratio, so cents do not read as spikes
# The fixed-costs.json item that pays for the Claude Code API calls (its `item` label on fixed_cost_month_usd).
# Once it has an amount, the bill counts it in place of the calls at list price.
CLAUDE_PLAN_ITEM = "Claude subscription"


def finops():
    """One bill in the shape of a FinOps tool (Cloudability, Finout): the AWS account from the cost
    exporter (the cost and usage report in S3, or Cost Explorer), the Claude Code API calls from Loki
    at their list price, and the fixed costs entered by hand (the Claude plan among them: while it is
    priced the bill counts the plan, not the list price, and a row shows the list price against it),
    with the account's cost split into cost centers by rule (virtual tags; the cost allocation tag named in allocation.json settles a line
    first once the account activates it), this month
    against the same days of last month, the services and cost centers that moved most, the cost per
    resource (instances, buckets, distributions: report mode only), per-service and per-resource
    anomalies, unit costs over the business metrics the stack already has, credits and tax by record
    type, and how much of the bill the rules explain.

    Internal: the panels show real spend, fixed-cost prices and resource ids, so this one is never
    shared; finops_public() is the cut to share. The default range is this month to
    date, which the Claude Code sums follow; the AWS figures are instant series with day, month and
    cost center labels, so they do not move with the range. Prometheus keeps 90 days.
    """
    L = Layout()
    r = "$__range"
    budget = 'max(aws_budget_limit_usd{period="MONTHLY"})'
    this_td = 'max(aws_cost_to_date_usd{period="this_month"})'
    prior_td = 'max(aws_cost_to_date_usd{period="prior_month"})'
    cc_cost = request_sum("cost_usd", r)
    plan = f'fixed_cost_month_usd{{item="{CLAUDE_PLAN_ITEM}"}}'
    compute7 = ('(sum(aws_cost_trailing_by_cost_center_usd{cost_center="compute"}) or vector(0)) '
                '+ (sum(aws_cost_trailing_by_cost_center_usd{cost_center="data-transfer"}) or vector(0))')

    def movers(metric, label):
        """Signed change per `label` against the same days of last month; a key on one side only counts in full."""
        this = f'sum by ({label}) ({metric}{{period="this_month"}})'
        prior = f'sum by ({label}) ({metric}{{period="prior_month"}})'
        return f"((({this}) or ({prior}) * 0) - (({prior}) or ({this}) * 0))"

    def bars(title, expr, legend, description, limit=10):
        return {"type": "bargauge", "title": title, "datasource": PROM, "description": description,
                "targets": [target(f"sort_desc(topk({limit}, {expr}))", legend, instant=True)],
                "fieldConfig": {"defaults": {"unit": USD, "decimals": 2, "min": 0}, "overrides": []},
                "options": {"orientation": "horizontal", "displayMode": "basic", "showUnfilled": True,
                            "reduceOptions": {"calcs": ["lastNotNull"]}}}

    def compare_table(title, metric, label, description):
        """This month to date, the same days of last month and the change, one row per `label`."""
        this = f'max by ({label}) ({metric}{{period="this_month"}})'
        prior = f'max by ({label}) ({metric}{{period="prior_month"}})'
        return {"type": "table", "title": title, "description": description, "datasource": PROM,
                "targets": [target(this, ref="A", format="table", instant=True),
                            target(prior, ref="B", format="table", instant=True)],
                "transformations": [
                    {"id": "merge", "options": {}},
                    {"id": "organize", "options": {"excludeByName": {"Time": True},
                                                   "renameByName": {"Value #A": "this month to date", "Value #B": "last month, same days"}}},
                    {"id": "calculateField", "options": {"mode": "binary", "alias": "change",
                                                         "binary": {"left": "this month to date", "operator": "-", "right": "last month, same days"}}},
                    {"id": "sortBy", "options": {"sort": [{"field": "this month to date", "desc": True}]}}],
                "fieldConfig": {"defaults": {"unit": USD, "decimals": 2}, "overrides": [
                    {"matcher": {"id": "byName", "options": "change"},
                     "properties": [{"id": "custom.cellOptions", "value": {"type": "color-text"}},
                                    {"id": "thresholds", "value": {"mode": "absolute", "steps": [
                                        {"color": "green", "value": None}, {"color": "text", "value": 0}, {"color": "red", "value": 1}]}}]}]},
                "options": {"showHeader": True, "footer": {"show": True, "reducer": ["sum"], "fields": ""}}}

    def anomaly_table(title, metric_prefix, label, description):
        """Latest day, 28-day median and their ratio per `label` (one name or several). The ratio is
        PromQL, with the median floored at ANOMALY_FLOOR_USD so cents do not read as spikes; a key with
        no median (first seen on the latest day) has an empty ratio cell rather than infinity."""
        by = ", ".join((label,) if isinstance(label, str) else label)
        a = f"max by ({by}) ({metric_prefix}_latest_day_usd)"
        b = f"max by ({by}) ({metric_prefix}_median_day_usd)"
        return {"type": "table", "title": title, "datasource": PROM,
                "description": description + f" Medians under USD {ANOMALY_FLOOR_USD:.2f} count as {ANOMALY_FLOOR_USD:.2f} in the ratio; "
                                             "a line first seen on the latest day has no ratio.",
                "targets": [target(a, ref="A", format="table", instant=True),
                            target(b, ref="B", format="table", instant=True),
                            target(f"{a} / clamp_min({b}, {ANOMALY_FLOOR_USD})", ref="C", format="table", instant=True)],
                "transformations": [
                    {"id": "merge", "options": {}},
                    {"id": "organize", "options": {"excludeByName": {"Time": True},
                                                   "renameByName": {"Value #A": "latest day", "Value #B": "28-day median", "Value #C": "ratio"}}},
                    {"id": "sortBy", "options": {"sort": [{"field": "ratio", "desc": True}]}}],
                "fieldConfig": {"defaults": {"unit": USD, "decimals": 2}, "overrides": [
                    {"matcher": {"id": "byName", "options": "ratio"},
                     "properties": [{"id": "unit", "value": "none"}, {"id": "decimals", "value": 2},
                                    {"id": "custom.cellOptions", "value": {"type": "color-background"}},
                                    {"id": "thresholds", "value": {"mode": "absolute", "steps": [
                                        {"color": "transparent", "value": None}, {"color": "orange", "value": 1.5}, {"color": "red", "value": 2}]}}]}]},
                "options": {"showHeader": True}}

    def text(title, md):
        return {"type": "text", "title": title, "options": {"mode": "markdown", "content": md}}

    latest_day = {"type": "stat", "title": "Latest complete day", "datasource": PROM,
                  "description": "The UTC day the anomaly tables and the latest-day stats describe: the last complete day the source has "
                                 "(before the day the report's latest delivery may still change).",
                  "targets": [target("max by (day) (aws_cost_latest_day_info)", "{{day}}", instant=True, range=False)],
                  "fieldConfig": {"defaults": {}, "overrides": []},
                  "options": {"reduceOptions": {"calcs": ["lastNotNull"]}, "textMode": "name", "colorMode": "none", "graphMode": "none"}}

    def no_colour(panel):
        """Amounts carry no colour: a stat's default thresholds turn anything above 80 red."""
        panel["options"]["colorMode"] = "none"
        return panel

    def not_priced(panel):
        """What a stat reads while the plan has no price: its query is empty."""
        panel["fieldConfig"]["defaults"]["noValue"] = "Not priced"
        return panel

    def month_pin(panel):
        """Pin a panel to the calendar month so far, in the browser's timezone, whatever range the dashboard shows."""
        panel["timeFrom"] = "now/M"
        return panel

    def projection(title, math, unit, decimals, description, plan_needed=False):
        """A number from the month's list price (Loki), the run-rate factor and, if `plan_needed`, the plan price.

        $RA is the list price since the first of the month. $RB scales it to month end: the days in the month over
        the days elapsed, with at least one day elapsed ($__range_s is the pinned panel's range, so the Loki sum and
        the day count cover the same time; days_in_month is the UTC month, which only differs in the hours around
        its turn). $RP is the plan price; with no price the query is empty, the reduce returns nothing and the
        panel reads Not priced.
        """
        parts = [("A", target(f"{cc_cost} or vector(0)", ds=LOKI, queryType="instant")),
                 ("B", target("days_in_month(vector(time())) * 86400 / clamp_min(vector($__range_s), 86400)",
                              instant=True, range=False))]
        if plan_needed:
            parts.append(("P", target(f"max({plan})", instant=True, range=False)))
        return not_priced(expr_sum(title, parts, description, unit=unit, decimals=decimals, math=math))

    # Cumulative list price per hour against the plan. The Loki frame (query A) is summed per step and the
    # transformations add it up from the first of the month; the plan line is the price now, drawn across the
    # whole range (@ the range's end in seconds; @ end() would read a step in the future, which Grafana aligns up to),
    # since the exporter's series only starts the day the price was entered.
    through_month = timeseries("List price through the month against the plan", [], USD)
    through_month.update(
        datasource=MIXED, timeFrom="now/M", interval="1h", maxDataPoints=800,
        description="The Claude Code API calls at list price, added up hour by hour from the first of the month "
                    "(the browser's timezone), against the plan price as a flat line. The day the list price crosses the "
                    "line is the day the plan has paid for itself. List price is what Claude Code reports per call, from its "
                    "api_request events in Loki. The plan line is the price as of now, and is absent until the plan has a price.",
        targets=[target(request_sum("cost_usd", "$__interval"), "List price", ds=LOKI, ref="A"),
                 target(f"max({plan} @ ${{__to:date:seconds}})", "Plan price", ref="B")],
        transformations=[
            # The first step of a Loki range query looks back one step from the start of the range, so a step at
            # or before the start would carry calls of the month before.
            {"id": "filterByValue", "filter": {"id": "byRefId", "options": "A"},
             "options": {"type": "include", "match": "all",
                         "filters": [{"fieldName": "Time", "config": {"id": "greater", "options": {"value": "${__from}"}}}]}},
            {"id": "calculateField", "filter": {"id": "byRefId", "options": "A"},
             "options": {"mode": "cumulativeFunctions", "cumulative": {"reducer": "sum"}, "alias": "List price",
                         "replaceFields": True}}])
    def cache_ratio(window):
        """Cache reads over all input tokens (fresh input, cache reads, cache writes) of the api_request events in `window`."""
        read = request_sum("cache_read_tokens", window)
        return f'{read} / ({request_sum("input_tokens", window)} + {read} + {request_sum("cache_creation_tokens", window)})'

    cache_day = loki_series("Cache hit ratio per day", [(cache_ratio("$__interval"), "Cache hit ratio")], "percentunit", draw="bars",
                            description="The same ratio per day. A drop follows a compaction, a new system prompt or a burst of fresh context; "
                                        "a high, steady value means the sessions reuse what they have already read.")
    cache_day["interval"] = "1d"
    cache_day["fieldConfig"]["defaults"].update(min=0, max=1)
    cache_day["options"]["legend"]["displayMode"] = "list"

    # Hours without a call have no row, so the line steps over them instead of breaking.
    through_month["fieldConfig"]["defaults"]["custom"].update(lineWidth=2, axisSoftMin=0, spanNulls=True,
                                                             lineInterpolation="stepAfter")
    through_month["fieldConfig"]["overrides"] = [{
        "matcher": {"id": "byName", "options": "Plan price"},
        "properties": [{"id": "color", "value": {"mode": "fixed", "fixedColor": "orange"}},
                       {"id": "custom.lineStyle", "value": {"fill": "dash", "dash": [10, 10]}}]}]
    through_month["options"]["legend"]["calcs"] = ["lastNotNull"]

    p = [
        L.row("The bill, this month"),
        L.place(expr_sum("One bill", [
            ("A", target("max(aws_cost_mtd_usd) or vector(0)", instant=True, range=False)),
            ("B", target(f"{cc_cost} or vector(0)", ds=LOKI, queryType="instant")),
            ("C", target("sum(fixed_cost_month_usd) or vector(0)", instant=True, range=False)),
            ("D", target(f"clamp_max(count({plan}), 1) or vector(0)", instant=True, range=False)),
        ], "AWS month to date, plus the fixed costs entered in fixed-costs.json, plus the Claude Code API calls in the "
           "dashboard's range at their list price only while the Claude subscription has no price. Once it is priced, the plan "
           "is one of the fixed costs and is the Claude term; the list price stays out of the bill and shows in the next row "
           "for comparison. Added server side with a Grafana expression: PromQL's or cannot reach the Loki series, and the "
           "expression keeps the three sources apart.", math="$RA + $RC + (1 - $RD) * $RB"), 5, 5),
        L.place(instant(stat("AWS, month to date", "max(aws_cost_mtd_usd)", USD, decimals=2,
                             description="Unblended cost from the 1st (UTC) to now, today's partial cost included. The source lags by hours: "
                                         "the report lands up to three times a day.")), 4, 5),
        L.place(instant(stat("Claude Code API calls, list price", cc_cost, USD, decimals=2, ds=LOKI,
                             description="What the API calls in the range would cost at list price, from Claude Code's own api_request "
                                         "events. The subscription covers them. The bill counts this amount only while the "
                                         "subscription has no price; the next row sets it against the plan.")), 5, 5),
        L.place(instant(stat("Fixed costs entered", "sum(fixed_cost_month_usd) or vector(0)", USD, decimals=2,
                             description="Monthly subscriptions and seats from fixed-costs.json, the Claude plan included once it has a price. "
                                         "Items without a price are counted on the data row below and left out here.")), 4, 5),
        L.place(instant(stat("AWS vs last month, same days", f"({this_td} - {prior_td}) / {prior_td}", "percentunit", decimals=0,
                             description="Complete days 1 to N of this month against days 1 to N of last month (N on the data row). "
                                         "Positive is up. A domain renewal or the monthly tax in one window and not the other shows here.")), 3, 5),
        L.place(ratio_gauge("Forecast / budget", f"max(aws_cost_month_forecast_usd) / {budget}",
                            "Month-end forecast against the MONTHLY budget in AWS Budgets: the run rate of this month's complete days "
                            "in report mode, Cost Explorer's forecast otherwise (the data row names the method). Red above 100%."), 3, 5),

        L.row("Claude plan against list price"),
        L.place(not_priced(no_colour(instant(stat("Plan price this month", f"max({plan})", USD, decimals=2,
                             description="The monthly price of the Claude subscription: the item named in fixed-costs.json, entered in the "
                                         "git-ignored fixed-costs.local.json. It reads Not priced until the item has an amount.")))), 4, 5),
        L.place(month_pin(no_colour(instant(stat("List price, month to date", cc_cost, USD, decimals=2, ds=LOKI,
                                       description="What the Claude Code API calls since the first of the month would cost at list price. "
                                                   "List price is what Claude Code reports per call (cost_usd on its api_request events, "
                                                   "summed in Loki). The panel is pinned to the calendar month in the browser's timezone, "
                                                   "whatever range the dashboard shows.")))), 5, 5),
        L.place(month_pin(projection("List price, month-end projection", "$RA * $RB", USD, 2,
                                     "Month-to-date list price divided by the days elapsed, times the days in the month. It is a run rate: it "
                                     "assumes the rest of the month looks like the days so far, and it counts the first day of the month as "
                                     "a whole day. List price is what Claude Code reports per call, from its api_request events in Loki.")), 5, 5),
        L.place(month_pin(projection("Projected savings this month", "$RA * $RB - $RP", USD, 2,
                                     "Projected month-end list price minus the plan price: what the plan saves against paying per call, as a "
                                     "run rate. Negative means the plan costs more than the calls would at list price. Not shown until the "
                                     "plan has a price.", plan_needed=True)), 5, 5),
        L.place(month_pin(projection("List price as a multiple of the plan", "$RA * $RB / $RP", "suffix:x", 1,
                                     "Projected month-end list price divided by the plan price. 1x means the plan costs what the calls "
                                     "would at list price; above 1x the plan is cheaper. Not shown until the plan has a price.",
                                     plan_needed=True)), 5, 5),
        L.place(through_month, 24, 9),

        L.row("Cache efficiency"),
        L.place(month_pin(no_colour(instant(stat(
            "Cache hit ratio, month to date", cache_ratio(r), "percentunit", decimals=1, ds=LOKI,
            description="Input tokens served from the prompt cache, over all input tokens (fresh input, cache reads and cache writes) of the "
                        "Claude Code API calls since the first of the month. Long sessions re-read their context cheaply, which is what "
                        "keeps the list price of a call low. Pinned to the calendar month in the browser's timezone.")))), 8, 8),
        L.place(cache_day, 16, 8),

        L.row("Cost centers (virtual tags)"),
        L.place(pivot_bars("Daily spend by cost center", "aws_cost_day_by_cost_center_usd", "day", "cost_center",
                           "Every (service, usage type) pair mapped to a cost center by the rules in allocation.json; "
                           "'unallocated' is what no rule explains. This month and the three months before."), 24, 10),
        L.place(compare_table("Cost centers, this month against last", "aws_cost_to_date_by_cost_center_usd", "cost_center",
                              "Complete days 1 to N of each month. The footer sums the columns."), 12, 9),
        L.place(bars("Top rises, by service", f"{movers('aws_cost_to_date_by_service_usd', 'service')} > 0", "{{service}}",
                     "Services that cost more than on the same days of last month, largest rise first. "
                     "A service on one side only counts in full.", limit=8), 6, 9),
        L.place(bars("Top falls, by service", f"(0 - {movers('aws_cost_to_date_by_service_usd', 'service')}) > 0", "{{service}}",
                     "Services that cost less than on the same days of last month, largest fall first, shown as the amount saved.", limit=8), 6, 9),

        L.row("Where the money goes"),
        L.place(bars("Top usage types this month", "aws_cost_mtd_by_usage_type_usd", "{{service}}: {{usage_type}}",
                     "Month to date per usage type. A usage type names the region, the instance size or the storage class; "
                     "the resources row below goes one grain finer.", limit=12), 8, 10),
        L.place(pivot_bars("Months by cost center", "aws_cost_month_by_cost_center_usd", "month", "cost_center",
                           "Calendar months by cost center; the current month is to date."), 8, 10),
        L.place(pivot_bars("Months by record type", "aws_cost_month_record_usd", "month", "record_type",
                           "Usage, tax, credits and refunds per calendar month. Credits and refunds post as negative amounts."), 8, 10),

        L.row("Resources"),
        L.place(bars("Top resources this month", "aws_cost_mtd_by_resource_usd", "{{service}}: {{resource_id}} ({{cost_center}})",
                     "Month to date per resource and cost center, from the cost and usage report's line items: an instance, a volume, "
                     "a bucket, a distribution, a hosted zone, a model. One resource can sit in two cost centers (an instance's hours "
                     "in compute, its bytes out in data-transfer). '(none)' is the lines without a resource: tax, API requests. "
                     "The data row counts the resources and sums the ones beyond the list. Empty until the report's first delivery.",
                     limit=12), 8, 10),
        L.place(pivot_bars("Daily spend by resource", "sum by (day, resource_id) (aws_cost_day_by_resource_usd)", "day", "resource_id",
                           "Per UTC day, the costliest resources of this month and last, a resource's services added up (an instance's "
                           "hours and its bytes out). Empty until the report's first delivery."), 8, 10),
        L.place(anomaly_table("Resources, latest day against their 28-day median", "aws_cost_resource", ("service", "resource_id"),
                              "The latest complete UTC day per resource against the median of the 28 days before it, zero-filled. "
                              "Orange from 1.5, red from 2. A new instance shows here first."), 8, 10),

        L.row("Anomalies"),
        L.place(anomaly_table("Services, latest day against their 28-day median", "aws_cost_service", "service",
                              "The latest complete UTC day per service against the median of the 28 days before it, days without "
                              "the service counted as zero. Orange from 1.5, red from 2. Domain renewals and tax are left out."), 12, 9),
        L.place(anomaly_table("Cost centers, latest day against their 28-day median", "aws_cost_cost_center", "cost_center",
                              "Same test per cost center; here renewals and tax stay in, so 'domains-and-dns' and 'tax' spike by design on their day."), 12, 9),
        L.place(pivot_bars("Daily total", "aws_cost_day_total_usd", "day", "estimated",
                           "All services per UTC day; the 'true' column is the days that may still change: the ones the report's "
                           "latest delivery still covers, or the ones Cost Explorer marks as estimated."), 24, 8),

        L.row("Unit costs"),
        L.place(instant(stat("AWS cost per 1,000 searches, 7 days", f"({compute7}) / (sum(increase(searxng_searches_total[7d])) / 1000)", USD, decimals=2,
                             description="Compute and data transfer of the last 7 complete UTC days (ending yesterday, or earlier when the "
                                         "source lags) over the searches of the 7 days ending now: the two windows are up to a day apart. "
                                         "Everything on the box is in the numerator, so the figure is a ceiling.")), 6, 5),
        L.place(instant(stat("Claude Code cost per API call", f"{cc_cost} / sum(count_over_time({CC_REQUESTS} [{r}]))", USD, decimals=3, ds=LOKI,
                             description="List-price cost over the number of api_request events in the range.")), 6, 5),
        L.place(instant(stat("Claude Code cost per 1M output tokens", f"{cc_cost} / ({request_sum('output_tokens', r)} / 1e6)", USD, decimals=2, ds=LOKI,
                             description="List-price cost over output tokens in the range, all models together; it moves with the model mix.")), 6, 5),
        L.place(instant(stat("Searches, 7 days", "sum(increase(searxng_searches_total[7d]))", "short", decimals=0,
                             description="The denominator of the search unit cost.")), 6, 5),

        L.row("Commitments"),
        L.place(text("Savings Plans and Reserved Instances",
                     "None in the account when this dashboard was built (the README keeps the date), so every hour is on demand "
                     "and a coverage gauge would read 0%. At this size a commitment buys nothing; when the compute cost center runs "
                     "steady for a quarter, `GetSavingsPlansPurchaseRecommendation` is the next call to add to the exporter."), 24, 4),

        L.row("Data"),
        L.place(ratio_gauge("Allocated share", "max(aws_cost_allocated_ratio)",
                            "Share of this month's AWS cost that a rule in allocation.json maps to a cost center. The rest is 'unallocated'.",
                            steps=((None, "red"), (0.9, "orange"), (0.99, "green")), max_=1), 4, 5),
        L.place(instant(stat("Days compared", "max(aws_cost_to_date_days)", "short", decimals=0,
                             description="N in the this-month-against-last panels: complete days of this month so far, capped at the "
                                         "length of last month (28 or 29 at the end of March).")), 3, 5),
        L.place(latest_day, 4, 5),
        L.place(instant(stat("Data age", 'time() - (max(aws_cost_report_delivery_timestamp_seconds) or max(aws_cost_exporter_fetch_timestamp_seconds{part="current"}))',
                             "s", decimals=0,
                             description="Time since the report's latest delivery landed in S3 (up to three times a day; orange after a day), "
                                         "or, in Cost Explorer mode, since the exporter last asked for this month (every 12 h).")), 3, 5),
        L.place(instant(stat("Latest day estimated", "max(aws_cost_latest_day_estimated)", "bool_yes_no",
                             description="Cost Explorer marks recent days as estimates until it settles them; in report mode the latest "
                                         "complete day lies before the days a delivery may still change, so this reads No.")), 3, 5),
        L.place(instant(stat("Fixed-cost items without a price", 'max(fixed_cost_items{state="unpriced"})', "short", decimals=0,
                             description="Items in the mounted fixed-costs file with amount_usd null. Enter the price and restart the exporter.")), 4, 5),
        L.place(instant(stat("Cost of measuring", "max(aws_cost_explorer_spend_usd_total)", USD, decimals=2,
                             description="What this exporter's own Cost Explorer requests have cost, at USD 0.01 each, since it started. "
                                         "In report mode it only grows while a month has no report file yet (reading S3 is free at this size).")), 3, 5),
        L.place(instant(stat("Source", "max by (source) (aws_cost_source_info)", "none",
                             description="cur = the cost and usage report in S3; cost_explorer = the Cost Explorer API.")), 3, 4),
        L.place(instant(stat("Forecast method", "max by (method) (aws_cost_forecast_info)", "none",
                             description="run_rate = this month's complete days extended to month end; prior_month_rate = last month's daily "
                                         "mean when this month has no complete day yet; cost_explorer = Cost Explorer's forecast.")), 3, 4),
        L.place(instant(stat("Resources this month", 'max(aws_cost_resources{period="this_month"})', "short", decimals=0,
                             description="Distinct (service, resource) pairs with a line this month in the report; '(none)' counts as one.")), 3, 4),
        L.place(instant(stat("Resources beyond the list", "max(aws_cost_mtd_resource_rest_usd)", USD, decimals=2,
                             description="Month to date of the resources the resource panels leave out (they show the costliest ones of this month and last).")), 3, 4),
        L.place(instant(stat("Report line items", "sum(aws_cost_report_line_items)", "short", decimals=0,
                             description="Line items read from the report's latest delivery, all billing periods in the cache.")), 3, 4),
    ]
    for panel in p:
        if panel["type"] == "stat" and panel["title"] in ("Data age", "Fixed-cost items without a price"):
            panel["options"]["colorMode"] = "value"
            panel["fieldConfig"]["defaults"]["thresholds"] = {"mode": "absolute", "steps": [
                {"color": "green", "value": None}, {"color": "orange", "value": 24 * 3600 if panel["title"] == "Data age" else 1}]}
        if panel["type"] == "stat" and panel["title"] in ("Source", "Forecast method"):
            panel["options"].update(textMode="name", colorMode="none", graphMode="none")
            panel["targets"][0]["legendFormat"] = "{{source}}" if panel["title"] == "Source" else "{{method}}"
    return dashboard("finops", "FinOps: the bill",
                     "One bill in the shape of a FinOps tool: AWS from the cost exporter, fixed costs by hand (the Claude plan "
                     "among them), Claude Code API calls from Loki at list price while the plan has no price; the plan against "
                     "list price; cost centers by rule, this month against last, movers, anomalies, unit costs, "
                     "record types. Periods: AWS is the UTC calendar month to date, the Claude Code sums follow the dashboard's "
                     "range in the browser's timezone (the plan row is pinned to the calendar month), fixed costs are per month; "
                     "with the default range (this month) they line up to within hours, with another range an unpriced plan's "
                     "bill adds that range's calls to this month's AWS. Internal; not shared.",
                     p, refresh="15m", time_from="now/M")


def finops_public():
    """Public cut of finops(): the AWS bill, the Claude plan against list price and the cache hit ratio.

    An allow-list by panel title, so a panel added to finops() stays internal until it is named here, and
    a renamed one fails the build instead of dropping out of the cut. Left out: the Resources row (its
    labels are instance ids, bucket names and ARNs), the fixed costs entered by hand and their unpriced
    items (they name other subscriptions and their prices), the resource counts in the data row and the
    commitments note. The Claude plan price is public by Stefan's decision of 2026-10-31, so the plan row
    and the cache row stay. The only fixed cost any kept panel reads is the Claude plan item: One bill is
    rebuilt as AWS plus that item (the list price of the Claude Code API calls stands in while the item has
    no price), because the internal one adds every fixed cost. The plan line of the chart reads the plan
    through max_over_time instead of an @ modifier with a Grafana time variable: a public dashboard runs
    the saved query on the server, which does not interpolate that variable.
    A few descriptions lose their pointers to the dropped panels. The labels that stay (cost center,
    service, usage type, record type, day, month) hold no identifier on 2026-10-31: the five panels that
    pass labels through name them in the query (shown_labels), the rest aggregate by label already, and
    the assertion at the end keeps the words of the dropped panels out of the JSON.
    """
    keep = {
        "The bill, this month": ["One bill", "AWS, month to date", "Claude Code API calls, list price",
                                 "AWS vs last month, same days", "Forecast / budget"],
        "Claude plan against list price": ["Plan price this month", "List price, month to date",
                                           "List price, month-end projection", "Projected savings this month",
                                           "List price as a multiple of the plan",
                                           "List price through the month against the plan"],
        "Cache efficiency": ["Cache hit ratio, month to date", "Cache hit ratio per day"],
        "Cost centers (virtual tags)": ["Daily spend by cost center", "Cost centers, this month against last",
                                        "Top rises, by service", "Top falls, by service"],
        "Where the money goes": ["Top usage types this month", "Months by cost center", "Months by record type"],
        "Anomalies": ["Services, latest day against their 28-day median",
                      "Cost centers, latest day against their 28-day median", "Daily total"],
        "Unit costs": ["AWS cost per 1,000 searches, 7 days", "Claude Code cost per API call",
                       "Claude Code cost per 1M output tokens", "Searches, 7 days"],
        "Data": ["Allocated share", "Days compared", "Latest complete day", "Data age", "Latest day estimated",
                 "Cost of measuring", "Source", "Forecast method", "Report line items"],
    }
    descriptions = {
        "Claude Code API calls, list price":
            "What the API calls in the range would cost at list price, from Claude Code's own api_request events. "
            "The plan covers them; the bill counts the plan price, and the next row sets it against this figure.",
        "Plan price this month":
            "The monthly price of the Claude plan. It reads Not priced until the plan has a price.",
        "Top usage types this month":
            "Month to date per usage type. A usage type names the region, the instance size or the storage class.",
    }
    # The panels that pass a metric's labels straight through (a bar chart column, a legend) name the labels they
    # may show, so a label added to the exporter later reaches no viewer: the label values are read at view time,
    # which the check on the JSON at the end cannot see.
    shown_labels = {
        "Daily spend by cost center": ("aws_cost_day_by_cost_center_usd", "day, cost_center"),
        "Top usage types this month": ("aws_cost_mtd_by_usage_type_usd", "service, usage_type, cost_center"),
        "Months by cost center": ("aws_cost_month_by_cost_center_usd", "month, cost_center"),
        "Months by record type": ("aws_cost_month_record_usd", "month, record_type"),
        "Daily total": ("aws_cost_day_total_usd", "day, estimated"),
    }
    plan = f'fixed_cost_month_usd{{item="{CLAUDE_PLAN_ITEM}"}}'
    one_bill = expr_sum("One bill", [
        ("A", target("max(aws_cost_mtd_usd) or vector(0)", instant=True, range=False)),
        ("B", target(f'{request_sum("cost_usd", "$__range")} or vector(0)', ds=LOKI, queryType="instant")),
        ("C", target(f"max({plan}) or vector(0)", instant=True, range=False)),
        ("D", target(f"clamp_max(count({plan}), 1) or vector(0)", instant=True, range=False)),
    ], "AWS month to date plus the Claude plan price. The plan covers the Claude Code API calls, so their list price "
       "is left out and shows in the next row for comparison; until the plan has a price, the list price stands in. "
       "Nothing else is added. Added server side with a Grafana expression: PromQL's or cannot reach the Loki series.",
       math="$RA + $RC + (1 - $RD) * $RB")
    L = Layout()
    out, wanted, seen = [], None, set()
    for p in finops()["panels"]:
        if p["type"] == "row":
            wanted = keep.get(p["title"])
            if wanted is not None:
                out.append(L.row(p["title"]))
            continue
        if wanted is None or p["title"] not in wanted:
            continue
        seen.add(p["title"])
        w, h = p["gridPos"]["w"], p["gridPos"]["h"]
        if p["title"] == "One bill":
            p = one_bill
        if p["title"] == "List price through the month against the plan":
            line = [t for t in p["targets"] if t["refId"] == "B"]
            assert len(line) == 1 and "__to" in line[0]["expr"], "finops_public: the plan line of the chart changed"
            line[0]["expr"] = f"max(max_over_time({plan}[1d]))"
        p["description"] = descriptions.get(p["title"], p.get("description", ""))
        if p["title"] in shown_labels:
            metric, labels = shown_labels[p["title"]]
            expr = p["targets"][0]["expr"]
            assert expr.count(metric) == 1, f"finops_public: {metric} not once in the query of {p['title']}"
            p["targets"][0]["expr"] = expr.replace(metric, f"max by ({labels}) ({metric})")
        out.append(L.place(p, w, h))
    missing = {t for ts in keep.values() for t in ts} - seen
    assert not missing, f"finops_public: panels not found in finops(): {sorted(missing)}"
    # A kept panel that shows per-series labels (a legend template, a table) must name them in shown_labels or
    # aggregate by label in its own query; a panel added to `keep` and forgotten fails here.
    for p in out:
        for tg in p.get("targets", []):
            if p["title"] not in shown_labels and (tg.get("format") == "table" or "{{" in tg.get("legendFormat", "")):
                assert re.search(r"\bby \(", tg["expr"]), f"finops_public: {p['title']} shows labels without naming them"
    d = dashboard("finops-public", "FinOps: the bill, public",
                  "The AWS bill in the shape of a FinOps tool, from the cost and usage report, plus the Claude plan: this "
                  "month against the same days of last month, the account's cost split into cost centers by rule, the "
                  "services that moved most, usage types, usage, tax and credits by month, anomalies against a 28-day "
                  "median, unit costs and how much of the bill the rules explain. The Claude plan is set against the list "
                  "price of the Claude Code API calls, with the prompt-cache hit ratio. AWS is the UTC calendar month to date.",
                  out, refresh="15m", time_from="now/M")
    leak = re.search(r"resource|fixed[ -]cost|local\.json|commitment|arn:|bucket|account[ _]?id", json.dumps(d), re.I)
    assert not leak, f"finops_public: '{leak.group(0)}' in the public JSON"
    return d


# ---------------------------------------------------------------- site traffic

def site_traffic(public=False):
    """Site traffic from the CloudFront access logs of the static sites (repo site-infra).

    Fed by site-infra's stats/cloudfront_to_loki.py: one Loki line per request in the stream
    {job="site-traffic", site, agent}. The logs record no IP address, and the shipper keeps no
    User-Agent string and no full referrer URL, only the agent class, the bot name and the
    referrer host. The public cut has no template variables (public dashboards ignore them)
    and leaves out what a visitor or a scanner can write: referrer hosts (they can name an
    intranet) and the paths of 404s (any request picks them); it counts 404s instead.
    """
    L = Layout()
    site = "" if public else ', site=~"$site"'
    human = f'{{job="site-traffic", agent="human"{site}}} | json'
    every = f'{{job="site-traffic"{site}}} | json'
    bots = f'{{job="site-traffic", agent="bot"{site}}} | json'
    # Totals only count lines; the stream labels select them without parsing.
    every_raw = f'{{job="site-traffic"{site}}}'
    bots_raw = f'{{job="site-traffic", agent="bot"{site}}}'

    def ranked(title, expr, columns, value, description=""):
        """Counts as a table, largest first (a Loki instant query: Time, one column per label, Value #A)."""
        return {
            "type": "table", "title": title, "description": description, "datasource": LOKI,
            "targets": [target(expr, ds=LOKI, queryType="instant")],
            "transformations": [
                {"id": "organize", "options": {"excludeByName": {"Time": True},
                                               "indexByName": {c: i for i, c in enumerate((*columns, "Value #A"))},
                                               "renameByName": {**columns, "Value #A": value}}},
                {"id": "sortBy", "options": {"sort": [{"field": value, "desc": True}]}},
            ],
            "fieldConfig": {"defaults": {"unit": "none", "decimals": 0}, "overrides": []},
            "options": {"showHeader": True, "cellHeight": "sm"},
        }

    def total(title, expr, description):
        p = stat(title, expr, unit="none", decimals=0, description=description, ds=LOKI)
        p["options"].update(colorMode="none", graphMode="none")
        return p

    pages = f'{human} | kind="page"'
    p = [L.row("Totals over the selected range")]
    p.append(L.place(total("Page views", f'sum(count_over_time({pages} [$__range]))',
                           "HTML pages served with status 200 to browsers. Bots, assets, redirects and errors excluded."), 6, 4))
    p.append(L.place(total("Requests, all", f'sum(count_over_time({every_raw} [$__range]))',
                           "Every request CloudFront logged: pages, assets, redirects, errors, bots."), 6, 4))
    p.append(L.place(total("Bot requests", f'sum(count_over_time({bots_raw} [$__range]))',
                           "Requests whose User-Agent names a crawler, a script or nothing at all."), 6, 4))
    p.append(L.place(total("Countries", f'count(sum by (country) (count_over_time({pages} [$__range])))',
                           "Countries with at least one page view. CloudFront derives the country; no IP address is stored."), 6, 4))

    p.append(L.row("Over time"))
    views = loki_series("Page views per hour", [(f'sum by (site) (count_over_time({pages} [$__interval]))', "{{site}}")],
                        draw="bars", stack=True, description="Page views by browsers, per hour and site.")
    views["interval"] = "1h"
    p.append(L.place(views, 12, 8))
    status = loki_series("Responses by status code", [(f'sum by (status) (count_over_time({every} [$__interval]))', "{{status}}")],
                         draw="bars", stack=True, description="All requests, bots included, per hour and status code.")
    status["interval"] = "1h"
    p.append(L.place(status, 12, 8))

    p.append(L.row("What and from where"))
    p.append(L.place(ranked("Top pages", f'topk(25, sum by (site, path) (count_over_time({pages} [$__range])))',
                            {"site": "Site", "path": "Page"}, "Views", "Most viewed pages over the selected range."), 12, 10))
    p.append(L.place(ranked("Countries", f'sum by (country) (count_over_time({pages} [$__range]))',
                            {"country": "Country"}, "Views", "ISO 3166 country code CloudFront derives from the viewer address."), 12 if public else 6, 10))
    if not public:
        p.append(L.place(ranked("Referrers", f'topk(25, sum by (referrer) (count_over_time({pages} | referrer!="internal" [$__range])))',
                                {"referrer": "Referrer host"}, "Views", "Host of the referring page; direct = no referrer. Internal links are left out."), 6, 10))
    p.append(L.place(ranked("Crawlers and scripts", f'sum by (bot) (count_over_time({bots} [$__range]))',
                            {"bot": "Agent"}, "Requests", "Bot requests by the agent the User-Agent names. other = a generic bot pattern."), 12, 10))
    if public:
        p.append(L.place(total("Not found", f'sum(count_over_time({every} | status="404" [$__range]))',
                               "Requests that answered 404, bots included. The paths are on the internal dashboard only: any request picks them."), 12, 10))
    else:
        p.append(L.place(ranked("Not found", f'topk(25, sum by (site, path) (count_over_time({every} | status="404" [$__range])))',
                                {"site": "Site", "path": "Path"}, "Requests", "Paths that answered 404, bots included."), 12, 10))

    variables = () if public else (query_var("site", "Site", 'label_values({job="site-traffic"}, site)', ds=LOKI),)
    uid = "site-traffic-public" if public else "site-traffic"
    title = "Site traffic (public)" if public else "Site traffic"
    d = dashboard(uid, title,
                  "Visits to the static sites on CloudFront, from the access logs. Counts only: the logs hold no IP "
                  "address, and no User-Agent string or full referrer URL reaches Loki.",
                  p, variables, refresh="5m", time_from="now-7d")
    d["tags"] += ["sites"]
    return d


# ---------------------------------------------------------------- agent tool calls

TOOL_DECISIONS = CC_EVENTS + ' | event_name="tool_decision"'
# Moves together with AgentToolDenialsSpike in prometheus/rules/alerts.yml.
TOOL_DENIALS_ALERT = 14


def agent_tool_calls():
    """Every tool call of the coding agents and who decided it: a rule, a hook, or a person.

    Built from the CTO Powersuit's /ai-ops:tool-audit spec (agent-tool-calls.json). Internal
    only, never shared publicly: with OTEL_LOG_TOOL_DETAILS=1 the trail panel shows each
    call's parameters, shell commands included. There is no public cut.
    """
    L = Layout()
    rej = TOOL_DECISIONS + ' | decision="reject"'
    asked = TOOL_DECISIONS + ' | source=~"user_.*"'

    def count(sel, window="$__range", by=(), zero=False):
        inner = f"count_over_time({sel} [{window}])"
        q = f"sum by ({', '.join(by)}) ({inner})" if by else f"sum({inner})"
        return q + " or vector(0)" if zero else q

    def counter(title, expr, description="", warn=False):
        p = stat(title, expr, unit="none", decimals=0, description=description, ds=LOKI)
        if warn:  # any denial shows orange
            p["fieldConfig"]["defaults"]["thresholds"] = {"mode": "absolute", "steps": [
                {"color": "green", "value": None}, {"color": "orange", "value": 1}]}
        else:
            p["options"]["colorMode"] = "none"
        return p

    def ranked(title, expr, columns, description=""):
        """Counts per tool as a table, largest first; a stat panel shows one tile per series and scrolls.

        Grafana turns a Loki instant query into one table frame: Time, one column per label, "Value #A".
        """
        return {
            "type": "table", "title": title, "description": description, "datasource": LOKI,
            "targets": [target(expr, ds=LOKI, queryType="instant")],
            "transformations": [
                {"id": "organize", "options": {"excludeByName": {"Time": True},
                                               "indexByName": {c: i for i, c in enumerate((*columns, "Value #A"))},
                                               "renameByName": {**columns, "Value #A": "Calls"}}},
                {"id": "sortBy", "options": {"sort": [{"field": "Calls", "desc": True}]}},
            ],
            "fieldConfig": {"defaults": {"unit": "none", "decimals": 0}, "overrides": []},
            "options": {"showHeader": True, "cellHeight": "sm"},
        }

    # 2880 points keep the step at 15 minutes or less up to a 30-day range, so the 15-minute
    # windows tile the range and no denial falls between two samples. Points, because a
    # quiet stretch has no samples and a lone value would draw no line.
    denied_15m = loki_series("Denied per 15 minutes by source", [
        (count(rej, "15m", ("source",)), "{{source}}"),
    ], description=f"Denied calls in the trailing 15 minutes, one line per source (config is a deny rule, hook a "
                   f"PreToolUse hook, user_* a person). The AgentToolDenialsSpike alert fires when one source passes "
                   f"the red line at {TOOL_DENIALS_ALERT}; the lines are not stacked because the alert reads each source.")
    denied_15m["maxDataPoints"] = 2880
    denied_15m["fieldConfig"]["defaults"]["thresholds"] = {"mode": "absolute", "steps": [
        {"color": "transparent", "value": None}, {"color": "red", "value": TOOL_DENIALS_ALERT}]}
    denied_15m["fieldConfig"]["defaults"]["custom"].update(
        thresholdsStyle={"mode": "dashed+area"}, axisSoftMax=TOOL_DENIALS_ALERT * 1.25,
        showPoints="always", pointSize=6)

    p = [
        L.row("Decisions"),
        L.place(counter("Allowed", count(TOOL_DECISIONS + ' | decision="accept"'),
                        description="Calls the settings, a hook or a person allowed in the time range."), 6, 4),
        L.place(counter("Denied", count(rej, zero=True), warn=True,
                        description="Calls a deny rule, a hook or a person refused in the time range."), 6, 4),
        L.place(counter("Asked", count(asked, zero=True),
                        description="Calls a person answered at a prompt (sources user_temporary, user_permanent, "
                                    "user_reject, user_abort). A prompt nobody answered writes no event."), 6, 4),
        L.place(counter("Blocked by hooks", count(rej + ' | source="hook"', zero=True), warn=True,
                        description="Calls a PreToolUse hook refused."), 6, 4),
        L.place(denied_15m, 12, 8),
        L.place(loki_series("Calls per hour by decision", [
            (count(TOOL_DECISIONS, "1h", ("decision",)), "{{decision}}"),
        ], stack=True), 12, 8),

        L.row("By tool"),
        L.place(ranked("Denied by tool and source", count(rej, by=("tool_name", "source")),
                       {"tool_name": "Tool", "source": "Source"}), 12, 8),
        L.place(ranked("Asked by tool", count(asked, by=("tool_name", "source")),
                       {"tool_name": "Tool", "source": "Source"}), 12, 8),
        L.place(ranked("Allowed by tool", count(TOOL_DECISIONS + ' | decision="accept"', by=("tool_name",)),
                       {"tool_name": "Tool"}), 12, 8),
        L.place(ranked("Failed by tool", count(CC_EVENTS + ' | event_name="tool_result" | success="false"',
                                               by=("tool_name",)), {"tool_name": "Tool"},
                       description="Calls that ran and failed (tool_result success false)."), 12, 8),

        L.row("Trail"),
        L.place(logs("Denied calls", rej,
                     description="One line per denied call. Open a line for tool_name, source, prompt_id and, "
                                 "with OTEL_LOG_TOOL_DETAILS=1, tool_parameters."), 24, 10),
    ]
    d = dashboard("agent-tool-calls", "Agent tool calls",
                  "Every tool call of the coding agents and who decided it: a rule in the settings, a hook, or a "
                  "person at a prompt. Counted from Claude Code's tool_decision and tool_result events in Loki. "
                  "Internal; never shared: the trail panel shows tool parameters.",
                  p, refresh="1m", time_from="now-24h")
    d["tags"] += ["ai-ops", "security"]
    return d


if __name__ == "__main__":
    OUT.mkdir(exist_ok=True)
    for d in (local_llm(), local_llm_public(), host(), host_public(), claude_code(), claude_code_public(), site_deploys(), searxng(), searxng_public(), aws_spend(), finops(), finops_public(), site_traffic(), site_traffic(public=True), agent_tool_calls()):
        path = OUT / f"{d['uid']}.json"
        path.write_text(json.dumps(d, indent=2) + "\n")
        print(f"wrote {path} ({len(d['panels'])} panels)")
