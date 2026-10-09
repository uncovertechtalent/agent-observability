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

        L.place(loki_series("Cost per hour by model", [
            (request_sum("cost_usd", "1h", ("model",)), "{{model}}"),
        ], "currencyUSD", stack=True, description="Cost of the API calls in the trailing hour."), 12, 8),
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



def aws_spend():
    """AWS account spend from Cost Explorer and AWS Budgets (aws-cost-exporter).

    Internal only: the panels show real spend and are never shared publicly. The exporter
    fetches this month every 12 hours and Cost Explorer lags by up to a day, so this dashboard moves
    a few times a day. Day, month and service are labels on instant series, so the daily
    and monthly charts are instant queries pivoted with groupingToMatrix.
    """
    L = Layout()
    usd = "currencyUSD"
    budget = 'max(aws_budget_limit_usd{period="MONTHLY"})'

    def ratio_gauge(title, expr, description):
        return {"type": "gauge", "title": title, "description": description, "datasource": PROM,
                "targets": [target(expr)],
                "fieldConfig": {"defaults": {"unit": "percentunit", "decimals": 0, "min": 0, "max": 1.2,
                                             "thresholds": {"mode": "absolute", "steps": [
                                                 {"color": "green", "value": None}, {"color": "orange", "value": 0.8},
                                                 {"color": "red", "value": 1}]}}, "overrides": []},
                "options": {"reduceOptions": {"calcs": ["lastNotNull"]}, "showThresholdMarkers": True}}

    def pivot_bars(title, expr, row, col, description, stack=True):
        t = target(expr, format="table", instant=True)
        return {"type": "barchart", "title": title, "description": description, "datasource": PROM, "targets": [t],
                "transformations": [
                    {"id": "groupingToMatrix", "options": {"columnField": col, "rowField": row, "valueField": "Value",
                                                           "emptyValue": "zero"}},
                    {"id": "sortBy", "options": {"sort": [{"field": f"{row}\\{col}"}]}}],
                "fieldConfig": {"defaults": {"unit": usd, "custom": {"fillOpacity": 80, "lineWidth": 0}}, "overrides": []},
                "options": {"xField": f"{row}\\{col}", "stacking": "normal" if stack else "none", "xTickLabelRotation": -45,
                            "xTickLabelSpacing": 100, "legend": {"displayMode": "table", "placement": "right", "calcs": ["sum"]},
                            "tooltip": {"mode": "multi"}}}

    p = [
        L.row("This month"),
        L.place(stat("Month to date", "max(aws_cost_mtd_usd)", usd, decimals=2,
                     description="Unblended cost from the 1st (UTC) to now, today's partial cost included. Cost Explorer lags by up to a day."), 4, 5),
        L.place(stat("Monthly budget", budget, usd, decimals=2,
                     description="The MONTHLY cost budget in AWS Budgets."), 4, 5),
        L.place(ratio_gauge("Month to date / budget", f"max(aws_cost_mtd_usd) / {budget}",
                            "Orange at 80% (AWSCostMonthToDateOver80), red at 100%."), 4, 5),
        L.place(stat("Forecast, month end", "max(aws_cost_month_forecast_usd)", usd, decimals=2,
                     description="Actual days before the forecast's start plus Cost Explorer's forecast to the end of the month (forecast fetched daily)."), 4, 5),
        L.place(ratio_gauge("Forecast / budget", f"max(aws_cost_month_forecast_usd) / {budget}",
                            "Red above 100% (AWSCostForecastOverBudget)."), 4, 5),
        L.place(stat("AWS Budgets forecast", "max(aws_budget_forecast_usd)", usd, decimals=2,
                     description="The forecast AWS Budgets calculates itself, as a cross-check of the Cost Explorer figure."), 4, 5),

        L.place(stat("Latest day / 28-day median", "max(aws_cost_latest_day_usd) / max(aws_cost_trailing_median_day_usd)",
                     decimals=2, description="Latest UTC day before today against the median of the 28 days before it, both without domain "
                                             "renewals and tax. AWSCostDailySpike fires above 2 when the day is above USD 1."), 4, 4),
        L.place(stat("Latest day", "max(aws_cost_latest_day_usd)", usd, decimals=2,
                     description="May be partial: Cost Explorer marks recent days as estimated."), 4, 4),
        L.place(stat("Latest day estimated", "max(aws_cost_latest_day_estimated)", "bool_yes_no"), 4, 4),
        L.place(stat("Data age", 'time() - max(aws_cost_exporter_fetch_timestamp_seconds{part="current"})', "s", decimals=0,
                     description="Time since the exporter last asked Cost Explorer for this month (every 12 h)."), 4, 4),
        L.place(stat("Cost Explorer API cost", "max(aws_cost_explorer_spend_usd_total)", usd, decimals=2,
                     description="What this exporter's own Cost Explorer requests have cost, at USD 0.01 each, since it started."), 4, 4),
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
                           "Unblended cost per UTC day and service, this month and the three months before. "
                           "Bedrock carries the evals, EC2 the reverse proxy box, Route 53 the DNS zones; data transfer "
                           "shows under the service that caused it. The latest days are estimates until Cost Explorer settles them."), 24, 10),

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
    steps = {"Data age": [("green", None), ("orange", 13 * 3600), ("red", 25 * 3600)],
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
    ], description="Cost Explorer requests cost USD 0.01 each; AWS Budgets requests are free."), 24, 7))
    return dashboard("aws-spend", "AWS spend",
                     "AWS account spend from Cost Explorer and AWS Budgets: month to date and forecast against the "
                     "monthly budget, daily spend by service, top services, the last three months. Internal; not shared.",
                     p, refresh="15m", time_from="now-30d")

if __name__ == "__main__":
    OUT.mkdir(exist_ok=True)
    for d in (local_llm(), local_llm_public(), host(), host_public(), claude_code(), claude_code_public(), site_deploys(), searxng(), searxng_public(), aws_spend()):
        path = OUT / f"{d['uid']}.json"
        path.write_text(json.dumps(d, indent=2) + "\n")
        print(f"wrote {path} ({len(d['panels'])} panels)")
