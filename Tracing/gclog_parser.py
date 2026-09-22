import re
import subprocess
from typing import Any, Dict, List, Optional, Tuple

from itrace_parser import ItraceParser, DUAL_ISSUE_MASK


class GclogParser:

    # Per event: ``grep`` is the ``grep -F`` substring; ``pattern`` matches the full line with groups.
    EVENT_PATTERNS = {
        "START": {
            "grep": "@ExecTex Early commit VMEM type inst",
            "pattern": re.compile(
                r'(?P<ts_ns>\d+) ns \s*model\.gpu.\.sh.\.sa.\.cu.\.sq \s*\[debug@100\]\s*:\s*@ExecTex Early commit VMEM type inst: dbg_id:(?P<dbg_id>[0-9a-f]+) inst_id:(?P<inst_id>[0-9a-f]+)'
            ),
        },
        "TC_TOKEN_CREATED": {
            "grep": "TcToken Created",
            "pattern": re.compile(
                r'(?P<ts_ns>\d+) ns \s*model\.gpu.\.sh.\.sa.\.cu.\.ta_adapter. \[debug@\d+\]\s*:\s*TcToken Created -- wave_type:\d+, wave_size:\d+, debug_id:0x(?P<dbg_id>[0-9a-f]+), inst_id:0x(?P<inst_id>[0-9a-f]+)'
            ),
        },
        "LDS IN_FROM_TA": {
            "grep": "LDS in_from_ta",
            "pattern": re.compile(
                r'(?P<ts_ns>\d+) ns  model\.gpu.\.sh.\.sa.\.tex.\.tcp. \[debug@\d+\]    :  LDS in_from_ta\(\) clk:\d+ dbg_id:(?P<dbg_id>[0-9a-f]+) inst_id:(?P<inst_id>[0-9a-f]+) ta_id:\d+ simd_id:\d+ am_simd_id:\d+ wave_id:\d+ SQ-TCP Latency:\d+'
            ),
        },
        "TCP ds_process_read_out": {
            "grep": "TCP ds_process_read_out",
            "pattern": re.compile(
                r'(?P<ts_ns>\d+) ns  model\.gpu.\.sh.\.sa.\.tex.\.tcp. \[debug@\d+\]    :  TCP ds_process_read_out\(\) is_v_mcast: \d+ p_id: \d+ idx: \d+ simd_col: \d+ out_simd: \d+ is_ds_read_data: \d+ has_lds_data: \d+ tctd_data_cycles: \d+, dbgid: (?P<dbg_id>[0-9a-f]+), instid: (?P<inst_id>[0-9a-f]+)'
            ),
        },
        "IN_TEX": {
            "grep": "TEX in_from_ta",
            "pattern": re.compile(
                r'(?P<ts_ns>\d+) ns \s*model\.gpu.\.sh.\.sa.\.tex.\.tcp. \s*\[debug@100\]\s*:\s*TEX in_from_ta\(\) clk:\d+ dbg_id:(?P<dbg_id>[0-9a-f]+) inst_id:(?P<inst_id>[0-9a-f]+)'
            ),
        },
        "REQ_TO_L1": {
            "grep": "VMW: Sending request to L1 on Glx_intf 0x",
            "pattern": re.compile(
                r'(?P<ts_ns>\d+) ns \s*model\.gpu.\.sh.\.sa.\.tex.\.tcp. \s*\[debug@100\]\s*:\s*VMW: Sending request to L1 on Glx_intf 0x.* with addr (?P<addr>.+?) debug_id (?P<dbg_id>[0-9a-f]+) inst_id (?P<inst_id>[0-9a-f]+)'
            ),
        },
        "RET_FROM_L1": {
            "grep": "GL1_0: Sending return to client ",
            "pattern": re.compile(
                r'(?P<ts_ns>\d+) ns \s*model\.gpu.\.tc. \s*\[debug@100\]\s*:\s*GL1_0: Sending return to client . with addr (?P<addr>.+?) debug_id (?P<dbg_id>[0-9a-f]+) inst_id (?P<inst_id>[0-9a-f]+)'
            ),
        },
        "TCP_RETURN": {
            "grep": "Async load pending bit cleared at VCA tag ",
            "pattern": re.compile(
                r'(?P<ts_ns>\d+) ns \s*model\.gpu.\.sh.\.sa.\.tex.\.tcp. \s*\[debug@60\]\s*:\s*TCP\(\): tcp_return\(\) clk \d+ Async load pending bit cleared at VCA tag \d+ of pipe ., pending now . \(dbg_id:(?P<dbg_id>[0-9a-f]+) inst_id:(?P<inst_id>[0-9a-f]+)'
            ),
        },
        "LDS TO_TD": {
            "grep": "writing data to the TD interface dbg_id",
            "pattern": re.compile(
                r'(?P<ts_ns>\d+) ns \s*model\.gpu.\.sh.\.sa.\.tex.\.tcp. \s*\[debug@60\]\s*:\s*LDS\(\): out_to_td\(\) clk \d+ pipe_id . writing data to the TD interface dbg_id:(?P<dbg_id>[0-9a-f]+) inst_id:(?P<inst_id>[0-9a-f]+)'
            ),
        },
        "TCP TO_TD": {
            "grep": "writing data to the TD interface dbg_id",
            "pattern": re.compile(
                r'(?P<ts_ns>\d+) ns \s*model\.gpu.\.sh.\.sa.\.tex.\.tcp. \s*\[debug@60\]\s*:\s*TCP\(\): out_to_td\(\) clk \d+ pipe_id . writing data to the TD interface dbg_id:(?P<dbg_id>[0-9a-f]+) inst_id:(?P<inst_id>[0-9a-f]+)'
            ),
        },
        "ASYNC DECR_CNT": {
            "grep": "Async ack decrement async_counter for dbg_id",
            "pattern": re.compile(
                r'(?P<ts_ns>\d+) ns \s*model\.gpu.\.sh.\.sa.\.cu.\.sq \s*\[debug@100\]\s*:\s*Async ack decrement async_counter for dbg_id:(?P<dbg_id>[0-9a-f]+), inst_id:(?P<inst_id>[0-9a-f]+)'
            ),
        },
        "LDS DECR_CNT": {
            "grep": "DS_LOAD decrement ds_counter for dbg_id",
            "pattern": re.compile(
                r'(?P<ts_ns>\d+) ns \s*model\.gpu.\.sh.\.sa.\.cu.\.sq \s*\[debug@100\]\s*:\s*DS_LOAD decrement ds_counter for dbg_id:(?P<dbg_id>[0-9a-f]+) inst_id:(?P<inst_id>[0-9a-f]+)'
            ),
        },
        "TEX DECR_CNT": {
            "grep": "TEX decrement vmem counters for dbg_id",
            "pattern": re.compile(
                r'(?P<ts_ns>\d+) ns \s*model\.gpu.\.sh.\.sa.\.cu.\.sq \s*\[debug@100\]\s*:\s*TEX decrement vmem counters for dbg_id:(?P<dbg_id>[0-9a-f]+) inst_id:(?P<inst_id>[0-9a-f]+)'
            ),
        },
        "L2RequestProcess": {
            "grep": "L2RequestProcess",
            "pattern": re.compile(
                r'(?P<ts_ns>\d+) ns\s+model.gpu.\.tc.\s+\[debug@\d+\]\s+:\s+L2RequestProcess\(\) -- l1:\d+, l2:\d+, tile:.*, \(l1_port:\d+ l2_global:\d+\), tok:.* addr:(?P<addr>[0-9a-fA-F]+)'
            ),
        },
        "L2ReturnProcess": {
            "grep": "L2ReturnProcess",
            "pattern": re.compile(
                r'(?P<ts_ns>\d+) ns\s+model.gpu.\.tc.\s+\[debug@\d+\]\s+:\s+L2ReturnProcess\(\) -- l1:\d+, l2:\d+, \(l2_global:\d+\), write=\d+, addr:(?P<addr>[0-9a-fA-F]+)'
            ),
        },
    }

    EVENT_NAMES = tuple(EVENT_PATTERNS.keys())

    # Counter time series for Perfetto (same shape as ``PerfCountersParser.counters``).
    # Each entry: ``grep`` = ``grep -F`` substring; ``pattern`` = full-line regex;
    # ``per_wave``: if True, ``dbg_id`` group + ``itrace_parser`` / ``_find_wave`` select the wave;
    # track name is ``{dbg_id_hex}.{counter_name}`` where ``counter_name`` is this dict key.
    # ``per_location``: if True (and not ``per_wave``), regex must include a ``location`` group
    # (simulator hierarchy path, e.g. ``model.gpu0.sh0.sa0.tex0.tcp0``); track name is
    # ``{location}.{counter_name}`` and Perfetto nests tracks under that dot-separated path.
    COUNTER_PATTERNS: Dict[str, Dict[str, Any]] = {
        "ds_count": {
            "grep": "ds count:",
            "pattern": re.compile(
                r'(?P<ts_ns>\d+) ns\s+.*?\]\s*:\s*.*?dbg_id:(?P<dbg_id>[0-9a-fA-F]+).*?ds\s+count:\s*(?P<value>\d+)'
            ),
            "per_wave": True,
        },
        "lfifo_occ": {
            "grep": "lfifo_occ:",
            "pattern": re.compile(
                r'(?P<ts_ns>\d+) ns\s+.*?\]\s*:\s*.*?dbg_id:(?P<dbg_id>[0-9a-fA-F]+).*?lfifo_occ:\s*(?P<value>\d+)'
            ),
            "per_wave": True,
        },
        # Match before generic ``async_direct_copy_outstanding`` so lines with a unit path
        # are keyed by hierarchy, not mistaken for another pattern.
        "async_direct_copy_outstanding return": {
            "grep": "async_direct_copy_outstanding:",
            "pattern": re.compile(
                r'(?P<ts_ns>\d+) ns (?P<location>model[.a-zA-Z0-9]+)\s+.*?\]\s*:\s*.*async_direct_copy_outstanding:\s*(?P<value>\d+)'
            ),
            "per_wave": False,
            "per_location": True,
        },
        "async_direct_copy_outstanding": {
            "grep": "async_direct_copy_outstanding ",
            "pattern": re.compile(
                # Non-greedy before dbg_id: — TCP lines include req dbg_id and tracking dbg_id; greedy .* would capture tracking (zeros).
                r'(?P<ts_ns>\d+) ns\s+.*?\]\s*:\s*.*async_direct_copy_outstanding \s*(?P<value>\d+).*?dbg_id:(?P<dbg_id>[0-9a-fA-F]+)'
            ),
            "per_wave": True,
        },

    }

    def __init__(self):
        self.itrace_parser: Optional[ItraceParser] = None
        self.counters: Dict[str, List[Tuple[int, float]]] = {}
        # Mem events with no matching wave/instruction (or no dbg_id/inst_id in the line).
        self.mem_tracks: Dict[str, List[Dict[str, Any]]] = {}

    def __call__(self, filename, itrace_parser: Optional[ItraceParser] = None):
        self.itrace_parser = itrace_parser
        self.counters = {}
        self.mem_tracks = {}
        cmd = ['grep', '-F']
        for spec in self.EVENT_PATTERNS.values():
            cmd.extend(['-e', spec["grep"]])
        for spec in self.COUNTER_PATTERNS.values():
            g = spec.get("grep")
            if g is not None:
                cmd.extend(['-e', g])
        cmd.append(filename)
        print(f"Running command: {' '.join(cmd)}")
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            encoding='utf-8',
            errors='replace',
        )
        if proc.returncode not in (0, 1):
            proc.check_returncode()
        for line in proc.stdout.splitlines():
            line = line.rstrip('\n\r')
            self._try_mem_event_line(line)
            # Counters may appear on the same line as a mem event (e.g. DS_LOAD decrement … ds count:0).
            self._try_counter_line(line)

    def _try_mem_event_line(self, line: str) -> bool:
        for name, spec in self.EVENT_PATTERNS.items():
            m = spec["pattern"].match(line)

            if m is not None:
                self._add_mem_event(name, m)
                return True
        return False

    def _try_counter_line(self, line: str) -> None:
        for cname, cspec in self.COUNTER_PATTERNS.items():
            m = cspec["pattern"].match(line)
            if m is not None:
                self._add_counter_sample(cname, cspec, m)
                break

    @staticmethod
    def _channel_from_addr(addr_str: str) -> int:
        """Memory channel from address bits [9:8] (four possible channels).

        ``addr_str`` is hexadecimal (optional ``0x``); it is converted to an
        integer before shifting.
        """
        s = addr_str.strip()
        if s.lower().startswith("0x"):
            s = s[2:]
        value = int(s, 16)
        return (value >> 8) & 3

    def _find_wave(self, dbg_id: int):
        if self.itrace_parser is None:
            return None
        for sas in self.itrace_parser.se().values():
            for sa in sas.values():
                for wave in sa.values():
                    if int(wave.wave_id, 16) == dbg_id:
                        return wave
        return None

    def _add_mem_event(self, event_name, m) -> None:
        # gclog times are 10x itrace/simulator time units
        ts_ns = int(m.group("ts_ns")) // 10
        gd = m.groupdict()
        dbg_id_hex = gd.get("dbg_id")
        inst_id_hex = gd.get("inst_id")
        dbg_id = int(dbg_id_hex, 16) if dbg_id_hex is not None else None
        inst_id = (int(inst_id_hex, 16) & DUAL_ISSUE_MASK) if inst_id_hex is not None else None
        addr = gd.get("addr")

        ev: Dict[str, Any] = {"type": event_name, "start": ts_ns, "end": ts_ns + 1}
        if addr is not None:
            addr = addr.strip()
            ev["addr"] = addr
            ch = self._channel_from_addr(addr)
            ev["type"] = f"{event_name}_C{ch}"

        if (
            self.itrace_parser is not None
            and dbg_id is not None
            and inst_id is not None
        ):
            wave = self._find_wave(dbg_id)
            if wave is not None:
                insn = wave.insns.get(inst_id)
                if insn is not None:
                    insn.mem_events.append(ev)
                    return

        ev_orphan: Dict[str, Any] = dict(ev)
        if dbg_id is not None:
            ev_orphan["dbg_id"] = dbg_id
        if inst_id is not None:
            ev_orphan["inst_id"] = inst_id
        self.mem_tracks.setdefault(event_name, []).append(ev_orphan)

    def _add_counter_sample(self, spec_name: str, spec: Dict[str, Any], m) -> None:
        ts = int(m.group('ts_ns')) // 10
        value = float(int(m.group('value')))

        if spec.get('per_wave'):
            dbg_hex = m.group('dbg_id').lower()
            dbg_id = int(dbg_hex, 16)
            if self.itrace_parser is not None and self._find_wave(dbg_id) is None:
                return
            track_key = f'{dbg_hex}.{spec_name}'
        elif spec.get('per_location') and m.groupdict().get('location'):
            track_key = f'{m.group("location").strip()}.{spec_name}'
        else:
            track_key = spec_name

        series = self.counters.setdefault(track_key, [])
        if not series or series[-1][1] != value:
            series.append((ts, value))
