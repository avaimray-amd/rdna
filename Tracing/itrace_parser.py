import re
import sys
import os
import json
from concurrent.futures import ProcessPoolExecutor, as_completed
from multiprocessing import cpu_count

# Regular expressions for parsing trace files
wave_start_re = re.compile(r"WAVE_START\[WGP(\d+)_SIMD(\d+)_WAVE(\d+)\] TS=(\d+)")
wave_end_re   = re.compile(r"// ENDPGM WG_ID=\d+ TS=(\d+)")
sdata_re = re.compile(r"w \[PS(.*)\](.*) TS")
sq_arb_re = re.compile(r"(.*)\[WGP\d+_SIMD\d+_WAVE\d+\]\s+TS=(\d+)")
sq_event_re = re.compile(r"// SQ:(\w+).* TS=(\d+)")
sq_type_dep_re = re.compile(r"// SQ:TYPE_DEP\s+(\w+):?\s*.* TS=(\d+)")
sp_event_re = re.compile(r"// SP:(\w+).* TS=(\d+)")
disasm_re = re.compile(r"\s*\b(.*)\s*// ([0-9A-Fa-f]+): ")
filename_re = re.compile(r".*se(\d+)sa(\d+)")
write_reg_re = re.compile(r"w \[P?(\w+)\].*TS=(\d+)")
counter_re = re.compile(r"s_wait_(\w+)\s+(\w+)")
double_counter_re = re.compile(r"s_wait_(\w+)_dscnt\s+{\s*ds:\s*(\w+)\s*,\s*mem:\s*(\w+)\s*}")
draw_id_re = re.compile(r"// SQ:DRAW_ID:(\d+)")
# "// <path>: <counter>=<value> TS=<ts>" — <path> is one token, so dotted hierarchies
# work as a single name, e.g. "// a.b.c: cnt=45 TS=57040" (same as "// UNIT cnt=45 ...").
unit_counter_line_re = re.compile(r"^//\s+(\S+)\s*:\s*(\w+)\s*=\s*(\d+)\s+TS=(\d+)\s*$")
# "// <path>: <event> TS=<ts>" — instant event at hierarchy path (no counter=value); e.g. "// a.b.c: my_event TS=57040"
path_event_line_re = re.compile(r"^//\s+(\S+)\s*:\s*(\S+)\s+(.*)\s*TS=(\d+)\s*$")
# Optional "addr:<hex>" in path event extra text, e.g. "... addr:570f00800 ..." or "... addr:0x570f00800 ..."
path_event_addr_re = re.compile(r"\baddr:(?:0x)?([0-9a-fA-F]+)\b")
sema_setreg_re = re.compile(r"// SQ:SEMA SETREG issue, ID:(\d+), count:(\d+), limit:(\d+), wave_sema_done:(\d), sema_unit_sema_done:\d TS=(\d+)")
sema_setlimit_re = re.compile(r"// SQ:SEMA LIMIT signal done, ID:(\d+) wave:\d+, cnsmr_wave_dbg_id:[0-9a-f]+ TS=(\d+)")
sema_signal_re = re.compile(r"// SQ:SEMA signal issue, ID:(\d+), count:\d+, limit:\d+, wave_sema_done:\d, sema_unit_sema_done:\d, cnsmr_wave_dbg_id:([0-9a-f]+) TS=(\d+)")
sema_signal_sp_re = re.compile(r"// SQ:SEMA signal\(SP\), ID:(\d+), count:\d+, limit:\d+, wave_sema_done:\d, sema_unit_sema_done:\d, wave:\d, cnsmr_wave_dbg_id:([0-9a-f]+) TS=(\d+)")
sema_wait_re = re.compile(r"// SQ:SEMA WAIT done,\s*sema_mask:(.*) TS=(\d+)")
sema_wait2_re = re.compile(r"// SQ:SEMA_WAIT MODE to EXECUTING TS=(\d+)")

# Constants for instruction ID processing
DUAL_ISSUE_MASK = 0x0FFFFFFF  # Masks out dual-issue bit from instruction ID
SPECIAL_INSN_THRESHOLD = 0xFFFF0000  # Instructions above this are special/meta instructions


def _merge_unit_counters(dst, src):
    """Merge path-prefixed unit counter dicts: str -> [(value, ts), ...]."""
    for k, v in src.items():
        if k not in dst:
            dst[k] = []
        dst[k].extend(v)


def _merge_path_prefix_events(dst, src):
    """Merge path-prefixed instant/slice ranges: str -> [{start, end, extra, addr?}, ...]."""
    key_start = lambda e: e["start"]
    for k, v in src.items():
        if k not in dst:
            dst[k] = []
        if v:
            dst[k].extend(v)
            dst[k].sort(key=key_start)


def load_insn_wait_counters():
    try:
        dir_path = os.path.dirname(os.path.realpath(__file__))
        counters_path = os.path.join(dir_path, "wait_counters.json")
        with open(counters_path, "r") as f:
            return json.load(f)
    except Exception:
        return {}

class Instruction:

    wait_counters = load_insn_wait_counters()
    unsupported_wait_counters = set()

    def __init__(self, insn_id, wave_id):
        self.insn_id = insn_id
        self.wave_dbg_id = wave_id
        self.disasm = None
        self.pc = None
        self.encoding = None
        self.extra_info = []
        self.sq_events = [] #list of dicts: (start, end, type)
        self.sema_events = [] #list of dicts: (type, ts)
        self.sp_events = [] #list of dicts: (start, end, type)
        self.reg_writes = [] #list of dicts: (reg, ts)
        self.wait_counters_flow_start = []
        self.wait_counters_flow_end = []
        self.sema_flow = []
        self.mem_events = []  # from gclog: same shape as sq_events ARB — {type, start, end}
        self.path_events = {}  # itrace "// path event TS=" — key "path.event" -> [{start, end, extra, addr?}, ...]

    def add_path_event(self, path, event, ts, extra):
        ts = int(ts)
        k = f"{path}.{event}"
        end = ts + 1
        lst = self.path_events.setdefault(k, [])
        m_addr = path_event_addr_re.search(extra)
        addr = m_addr.group(1).lower() if m_addr is not None else None
        entry = {"start": ts, "end": end, "extra": extra}
        if addr is not None:
            entry["addr"] = addr
        if lst and lst[-1]["end"] == ts:
            merged = {**lst[-1], "end": end}
            if addr is not None:
                merged["addr"] = addr
            lst[-1] = merged
        else:
            lst.append(entry)

    def set_disasm(self, data):
        m = disasm_re.match(data)
        if m is not None and self.disasm is None:
            self.disasm = m.group(1)
            self.pc = int(m.group(2), 16)
            return True
        else:
            self.extra_info.append(data)
            return False

    def set_sq_arb(self, data):
        m = sq_arb_re.match(data)
        if m is None:
            return False
        self.encoding = m.group(1)
        ts = int(m.group(2))
        self.sq_events.append({ 'type': 'ARB', 'start': ts, 'end': ts+1})
        return True

    def increments_counter(self, counter):
        """ returns by how many the counter is incremented by this instruction"""

        if self.disasm is None:
            return 0

        asm = self.disasm.split()
        opcode = asm[0]
        if counter in Instruction.wait_counters:
            if opcode in Instruction.wait_counters[counter]:
                return Instruction.wait_counters[counter][opcode]
            else:
                return 0
        else:
            print (f"WARNING: unsupported counter {counter} => update wait_counters.json")

        #TODO: support combinations of counters => like storecnt_dscnt
        return 0

    def sq_event(self, data):
        # Special handling for TYPE_DEP to extract dependency type (BRA, SCA, TEX)
        extra_info = None
        if "TYPE_DEP" in data:
            m = sq_type_dep_re.match(data)
            if m is not None:
                dep_type = m.group(1)  # BRA, SCA, TEX, MAX_COUNTER, etc.
                ts = int(m.group(2))
                event = f"TYPE_DEP_{dep_type}"
                # Extract the text after "dep_type:" or "dep_type" and before " TS="
                # Try with colon first (e.g., "TYPE_DEP BRA: ")
                start_marker = f"TYPE_DEP {dep_type}: "
                start_pos = data.find(start_marker)
                if start_pos != -1:
                    start_pos += len(start_marker)
                else:
                    # Try without colon (e.g., "TYPE_DEP MAX_COUNTER ")
                    start_marker = f"TYPE_DEP {dep_type} "
                    start_pos = data.find(start_marker)
                    if start_pos != -1:
                        start_pos += len(start_marker)

                if start_pos != -1:
                    end_pos = data.find(" TS=", start_pos)
                    if end_pos != -1:
                        extra_info = data[start_pos:end_pos].strip()
                        if not extra_info:  # Empty string
                            extra_info = None
            else:
                # Fallback to regular parsing if the specific pattern doesn't match
                m = sq_event_re.match(data)
                assert m is not None
                event = m.group(1)
                ts = int(m.group(2))
        else:
            m = sq_event_re.match(data)
            assert m is not None
            event = m.group(1)
            ts = int(m.group(2))

        if event == "DRAW_ID": #ignored
            return

        if event.startswith("SEMA"):
            self.sema_event(data)
            return
        if event == "IB_NOT_RDY":
            self.sq_events.append({ 'type': "IB_NOT_RDY",
                                     'start': ts,
                                     'end': ts+1})
        elif event == "IB_RDY":
            # find preceding IB_NOT_RDY
            ib_not_rdy = [i for i in self.sq_events if i['type'] == "IB_NOT_RDY"]
            assert len(ib_not_rdy) > 0, f"IB_RDY without preceding IB_NOT_RDY at TS={ts}"
            assert len(ib_not_rdy) == 1
            ib_not_rdy[0]['end'] = ts
        else:
            # Check if we should coalesce with a previous event of the same type
            # Look backwards through recent events to find one with matching type and extra info
            # that ends at the current timestamp (consecutive)
            coalesce_idx = None
            for i in range(len(self.sq_events) - 1, -1, -1):
                prev_event = self.sq_events[i]
                if prev_event['type'] == event:
                    prev_extra = prev_event.get('extra', None)
                    if prev_extra == extra_info and prev_event['end'] == ts:
                        coalesce_idx = i
                        break
                    # If we found the same type but it's not consecutive, stop looking
                    elif prev_extra == extra_info:
                        break

            if coalesce_idx is not None:
                # Extend the existing event
                self.sq_events[coalesce_idx]['end'] = ts + 1
            else:
                # Create a new event
                event_dict = { 'type': event,
                               'start': ts,
                               'end': ts+1}
                if extra_info is not None:
                    event_dict['extra'] = extra_info
                self.sq_events.append(event_dict)

    def sema_event(self, data):
        m = sema_setreg_re.match(data)
        if m is not None:
            sema_id = int(m.group(1))
            count = int(m.group(2))
            limit = int(m.group(3))
            done =  int(m.group(4))
            ts = int(m.group(5))
            self.sema_events.append({'type': "SETREG", 'ts': ts, 'sema_id': sema_id, 'data': (count,limit,done) })
            return
        m = sema_setlimit_re.match(data)
        if m is not None:
            sema_id = int(m.group(1))
            ts = int(m.group(2))
            self.sema_events.append({'type': "SETLIMIT", 'ts': ts, 'sema_id': sema_id})
            return
        m = sema_wait_re.match(data)
        if m is not None:
            ts = int(m.group(2))
            self.sema_events.append({'type': "WAIT_DONE", 'ts': ts})
            return
        m = sema_wait2_re.match(data)
        if m is not None:
            ts = int(m.group(1))
            self.sema_events.append({'type': "WAIT_DONE", 'ts': ts})
            return
        m = sema_signal_sp_re.match(data)
        if m is not None:
            sema_id = int(m.group(1))
            dst_wave_id = m.group(2)
            ts = int(m.group(3))
            self.sema_events.append({'type': "SIGNAL", 'ts': ts, 'sema_id': sema_id, 'dst_wave_id': dst_wave_id})
            return

        m = sema_signal_re.match(data)
        if m is not None:
            sema_id = int(m.group(1))
            dst_wave_id = m.group(2)
            ts = int(m.group(3))
            # only add if not already added (from SP such as signal after might come twice)
            if not any(i['type'] == "SIGNAL" and i['sema_id'] == sema_id and i['dst_wave_id'] == dst_wave_id for i in self.sema_events):
                self.sema_events.append({'type': "SIGNAL", 'ts': ts, 'sema_id': sema_id, 'dst_wave_id': dst_wave_id})

    def sp_event(self, data):
        m = sp_event_re.match(data)
        assert m is not None
        event = m.group(1)
        ts = int(m.group(2))
        if any (event.startswith(i) for i in ('ISSUE_', 'STALLED_', 'VDST_WRITE', 'XDL_PREREAD', 'VNBR_')):
            if len(self.sp_events) == 0 or self.sp_events[-1]['type'] != event:
                self.sp_events.append({ 'type': event,
                                        'start': ts,
                                        'end': ts+1})
            else:
                self.sp_events[-1]['end'] = ts+1

        elif event.startswith("SEMA"):
            pass
        else:
            assert False, f"unknown event {data}"

    def write_reg(self, data):
        m = write_reg_re.match(data)
        assert m is not None
        reg=m.group(1)
        ts=int(m.group(2))
        self.reg_writes.append(dict(reg=reg, ts=ts))

    def is_valu(self):
        return not self.is_vmem() and self.encoding.startswith("V")

    def is_vmem(self):
        return self.encoding in ("VBUFFER", "VIMAGE", "VSAMPLE", "VGLOBAL", "VDDS", "VSCRATCH", "SCRATCH", "GLOBAL", "DDS")

    def is_lds(self, only_vect=False):
        return self.encoding == "LDS" and (not only_vect or any(i in self.disasm for i in ("_b8", "_b16", "_b32", "_b64", "_b128")))

class Wave:
    nr_wait_counters = 0
    def __init__(self, wave_id, wgp, simd, wave_nr, sdata, start, end=None):
        self.wave_id = wave_id
        self.wgp = wgp
        self.simd = simd
        self.wave_nr = wave_nr
        self.__sdata =  sdata
        self.start = start
        self.end = end
        self.insns = {}
        self.last_sq_arb = None
        self.lowest_pc = 0xFFFFFFFFFFFF
        self.position = {}
        self.draw_id = None
        # unit_name -> counter_name -> [(value, timestamp), ...]
        self.units = {}

    def add_unit_counter(self, unit, counter, value, ts):
        if unit not in self.units:
            self.units[unit] = {}
        if counter not in self.units[unit]:
            self.units[unit][counter] = []
        self.units[unit][counter].append((value, ts))

    def add_path_event(self, insn_id, path, event, ts, extra):
        self.add_insn(insn_id)
        self.insns[insn_id].add_path_event(path, event, ts, extra)

    def set_end(self, data):
        m = wave_end_re.match(data)
        assert m is not None
        self.end = int(m.group(1))

    def add_insn(self, insn_id):
        if insn_id not in self.insns:
            self.insns[insn_id] = Instruction(insn_id, self.wave_id)

    def set_disasm(self, insn_id, data):
        insn = self.insns[insn_id]
        if not insn.set_disasm(data.strip()):
            return

        if insn.pc is not None:
            self.lowest_pc = min(self.lowest_pc, insn.pc)

        counter_names = None
        m = counter_re.match(insn.disasm)
        if m is not None:
            counter_names = [m.group(1)]
            counts = [int(m.group(2),0) +1]
        else:
            m = double_counter_re.match(insn.disasm)
            if m is not None:
                counter_names = ["dscnt", m.group(1)]
                counts = [int(m.group(2),0) +1, int(m.group(3),0) +1]

        if counter_names is None:
            if "s_wait" in insn.disasm and not insn.disasm.startswith("s_wait_alu"):
                assert False, insn.disasm
            return
        # a s_wait_count insn => get previous

        prev_insns = reversed(self.insns.keys())
        for c, cname in zip(counts, counter_names):
            if cname not in Instruction.wait_counters:
                if cname not in Instruction.unsupported_wait_counters:
                    Instruction.unsupported_wait_counters.add(cname)
                    print(f"WARNING: unsupported counter {cname}; dependency arrows omitted")
                continue
            for i in prev_insns:
                inc = self.insns[i].increments_counter(cname)
                if inc > 0:
                    c-=inc
                    if c <= 0:
                        insn.wait_counters_flow_start.append(Wave.nr_wait_counters)
                        self.insns[i].wait_counters_flow_end.append(Wave.nr_wait_counters)
                        Wave.nr_wait_counters+=1
                        break

    def sq_arb(self, insn_id, data):
        if self.insns[insn_id].set_sq_arb(data):
            self.last_sq_arb = insn_id
            return True
        else:
            return False

    def sq_event(self, insn_id, data):
        self.insns[insn_id].sq_event(data)

    def sp_event(self, insn_id, data):
        self.insns[insn_id].sp_event(data)

    def write_reg(self, insn_id, data):
        self.insns[insn_id].write_reg(data)
        if self.last_sq_arb is None:
            return

    def parse_sdata(self):
        in_cluster = 114 in self.__sdata # ttmp6 is only initialized if there are clusters
        if in_cluster:
            ttmp6 = self.__sdata[114]
            self.position['cluster_x'] = ttmp6 & 0xF
            self.position['cluster_y'] = (ttmp6 >> 4) & 0xF
            self.position['cluster_z'] = (ttmp6 >> 8) & 0xF

        if 117 in self.__sdata:
            ttmp9 = self.__sdata[117]
            self.position['wg_x'] = ttmp9

        if 115 in self.__sdata:
            ttmp7 = self.__sdata[115]
            self.position['wg_y'] = ttmp7 & 0xFFFF
            self.position['wg_z'] = ttmp7 >> 16

        if 116 in self.__sdata:
            ttmp8 = self.__sdata[116]
            self.position["wvgrp_id"] = (ttmp8 >> 25) & 3
            self.position["wvgrp_wv_id"] = (ttmp8 >> 27) & 7


    def __position_group(self, group_name, items):
        values = [ str(self.position[i]) for i in items if i in self.position]
        if len(values) == 0:
            return ""
        return f"{group_name}=(" + ",".join(values) + ")"

    def get_position(self):
        pos = [ self.__position_group("cluster", ('cluster_x', 'cluster_y', 'cluster_z')),
                self.__position_group("wg", ('wg_x','wg_y', 'wg_z')),
                self.__position_group("wvgrp_id", ('wvgrp_id',)),
                self.__position_group("wvgrp_wv_id", ('wvgrp_wv_id',))
                ]
        return " ".join(pos)

class Icache:
    def __init__(self, wgp):
        self.__wgp = wgp
        # unit_name -> counter_name -> [(value, timestamp), ...]
        self.path_events = {}

    def add_path_event(self, path, event, ts, extra):
        k = f'{path}.{event}'
        lst = self.path_events.setdefault(k, [])
        end = ts + 1
        entry = {"start": ts, "end": end, "extra": extra}
        m_addr = path_event_addr_re.search(extra)
        if m_addr is not None:
            entry["addr"] = m_addr.group(1).lower()
        lst.append(entry)

    def merge(self, other):
        for event, data in other.path_events.items():
            if event not in self.path_events:
                self.path_events[event] = []
            self.path_events[event] += data
            self.path_events[event].sort(key=lambda x: x['start'])

class ItraceParser:
    def __init__(self, print_unknown_lines=False):
        self.__se = {}
        self.print_unknown_lines = print_unknown_lines
        self.__first_wave_start = None
        self.__last_wave_end = None
        self.__sdata = {}
        self.__wave_dbg_dict = {}
        self.__icache_dict = {}
        # Dot-separated name (path + unit + counter, e.g. gpu0.sh0.sa1.tex1.UNIT.cntr) -> [(value, ts), ...]
        self.__unit_counters = {}
        # Same dotted hierarchy as __unit_counters, for "path: // UNIT event TS=" (no =value) -> [{start, end, extra}, ...]
        self.__path_prefix_events = {}

    def se(self):
        return self.__se

    def icaches(self):
        return self.__icache_dict

    def first_wave_start(self):
        return self.__first_wave_start

    def last_wave_end(self):
        return self.__last_wave_end

    def unit_counters(self):
        return self.__unit_counters

    def path_prefix_events(self):
        return self.__path_prefix_events

    def __add_unit_counter(self, path_parts, unit, counter, value, ts):
        parts = [p for p in path_parts if p] + [unit, counter]
        key = ".".join(parts)
        if key not in self.__unit_counters:
            self.__unit_counters[key] = []
        self.__unit_counters[key].append((value, ts))

    def __add_path_prefix_event(self, path_parts, unit, event, ts, extra):
        """Prefix column hierarchy + // line (path_event_line_re), same key shape as unit counters."""
        parts = [p for p in path_parts if p] + [unit, event]
        key = ".".join(parts)
        ts = int(ts)
        end = ts + 1
        lst = self.__path_prefix_events.setdefault(key, [])
        entry = {"start": ts, "end": end, "extra": extra}
        m_addr = path_event_addr_re.search(extra)
        if m_addr is not None:
            entry["addr"] = m_addr.group(1).lower()
        lst.append(entry)

    # Wave dbg id / instruction id used for global (non-wave) hierarchy lines; full path is in the // column.
    _GLOBAL_PATH_WAVE_INSN_IDS = frozenset(("00000000",))

    def __parse_global_path_prefix_payload(self, data, is_icache, icache_idx):
        """Parse // lines where the dotted hierarchy + unit name is in group(1), e.g.
        // sh0.sa0.wgp0.tex0.p0: async_direct_copy_outstanding=74 TS=...
        Returns True if a line was recognized and recorded."""
        ds = data.strip()
        m_uc = None if is_icache else unit_counter_line_re.match(ds)

        if m_uc is not None:
            dotted = m_uc.group(1)
            segs = dotted.split(".")
            unit = segs[-1]
            path_parts = segs[:-1]
            self.__add_unit_counter(
                path_parts,
                unit,
                m_uc.group(2),
                int(m_uc.group(3)),
                int(m_uc.group(4)),
            )
            return True

        m_pe = path_event_line_re.match(ds)
        if m_pe is not None and "=" not in m_pe.group(2):
            dotted = m_pe.group(1)
            segs = dotted.split(".")
            unit = segs[-1]
            path_parts = segs[:-1]
            if not is_icache:
                self.__add_path_prefix_event(
                    path_parts,
                    unit,
                    m_pe.group(2),
                    int(m_pe.group(4)),
                    m_pe.group(3).strip(),
                )
            else:
                if icache_idx not in self.__icache_dict:
                    self.__icache_dict[icache_idx] = Icache(icache_idx)
                self.__icache_dict[icache_idx].add_path_event(m_pe.group(1), m_pe.group(2), int(m_pe.group(4)), m_pe.group(3).strip())
            return True
        return False

    def __call__(self, filenames, shader_engines=None, shader_arrays=None, wgps=None, simds=None, parallel=True, wave_summary_file=None):
        Wave.nr_wait_counters = 0

        # Use parallel processing for multiple files if enabled
        if parallel and len(filenames) > 1:
            err = False
            num_workers = min(len(filenames), cpu_count())
            print(f"Parsing {len(filenames)} files using {num_workers} workers...", file=sys.stderr)

            # Parse files in parallel
            with ProcessPoolExecutor(max_workers=num_workers) as executor:
                # Submit all parsing jobs
                future_to_file = {
                    executor.submit(self._parse_file_wrapper, fname, shader_engines, shader_arrays, wgps, simds, self.print_unknown_lines): fname
                    for fname in filenames
                }

                # Collect results as they complete
                for future in as_completed(future_to_file):
                    fname = future_to_file[future]
                    try:
                        se, sa, waves, first_wave_start, last_wave_end, unit_counters, path_prefix_events, icaches = future.result()
                        _merge_unit_counters(self.__unit_counters, unit_counters)
                        _merge_path_prefix_events(self.__path_prefix_events, path_prefix_events)
                        if se is not None:  # File wasn't filtered out
                            if se not in self.__se:
                                self.__se[se] = {}
                            if sa not in self.__se[se]:
                                self.__se[se][sa] = {}
                            self.__se[se][sa].update(waves)
                            self.__wave_dbg_dict.update({wid: w for wid, w in waves.items()})

                            if first_wave_start is not None:
                                if self.__first_wave_start is None:
                                    self.__first_wave_start = first_wave_start
                                else:
                                    self.__first_wave_start = min(first_wave_start, self.__first_wave_start)

                            if last_wave_end is not None:
                                if self.__last_wave_end is None:
                                    self.__last_wave_end = last_wave_end
                                else:
                                    self.__last_wave_end = max(last_wave_end, self.__last_wave_end)

                        for i in icaches:
                            if i not in self.__icache_dict:
                                self.__icache_dict[i] = Icache(i)
                            self.__icache_dict[i].merge(icaches[i])


                    except Exception as e:
                        print(f"Error parsing {fname}: {e}", file=sys.stderr)
                        err = True
            if err:
                raise RuntimeError ("Exceptions raised while parsing files")
        else:
            # Sequential parsing for single file or when parallel is disabled
            for i in filenames:
                self.__parse_file(i, shader_engines, shader_arrays, wgps, simds)
                for sas in self.__se.values():
                    for sa in sas.values():
                        self.__wave_dbg_dict.update({w.wave_id: w for w in sa.values()})

        self.__finalize()
        if wave_summary_file is not None:
            self.__save_wave_summary(wave_summary_file)


    def wave_start_end(self, wv_dbg_id):
        assert wv_dbg_id in self.__wave_dbg_dict
        w = self.__wave_dbg_dict[wv_dbg_id]
        return w.start, w.end

    def all_waves_at_ts(self, ts):
        return [ w for w in self.__wave_dbg_dict.values() if  w.start <= ts <= w.end]

    @staticmethod
    def _parse_file_wrapper(filename, shader_engines, shader_arrays, wgps, simds, print_unknown_lines=False):
        """Wrapper for parallel file parsing - returns (se, sa, waves_dict)"""
        # This is a static method that can be pickled for multiprocessing
        # We need to do the parsing in a way that can be serialized
        parser = ItraceParser(print_unknown_lines=print_unknown_lines)
        parser._ItraceParser__parse_file(filename, shader_engines, shader_arrays, wgps, simds)

        # Return the parsed data for merging
        unit_counters = parser._ItraceParser__unit_counters
        path_prefix_events = parser._ItraceParser__path_prefix_events
        if parser._ItraceParser__se:
            # Extract the single SE/SA that was parsed
            for se, sas in parser._ItraceParser__se.items():
                for sa, waves in sas.items():
                    return (se, sa, waves, parser.__first_wave_start, parser.__last_wave_end, unit_counters, path_prefix_events, parser.__icache_dict)
        return (None, None, {}, None, None, unit_counters, path_prefix_events)

    def __finalize(self):
        # add end ts to waves if not read from trace,
        for sas in self.__se.values():
            for sa in sas.values():
                for wave in sa.values():
                    wave.parse_sdata()
                    if wave.end is None:
                        find_end = True
                        wave.end = 0
                    else:
                        find_end = False
                    insns = list(wave.insns.keys())
                    bad_insns = []
                    for i in sorted(insns):
                        insn = wave.insns[i]
                        if insn.pc is None or insn.disasm is None:
                            if insn.path_events:
                                if find_end:
                                    for pe_list in insn.path_events.values():
                                        for pe in pe_list:
                                            wave.end = max(wave.end, pe["end"])
                                continue
                            print(f"Warning: removing bad insn {wave.wave_id} {insn.insn_id:08x} pc={insn.pc} disasm={insn.disasm}")
                            bad_insns.append(i)
                            continue
                        # make s_wait_xxx longer, until the next instruction
                        is_s_wait = insn.disasm.startswith("s_wait_")
                        if is_s_wait:
                            start = insn.sq_events[-1]['end']
                            if i+1 not in wave.insns:
                                print(f"Warning: {wave.wave_id} {insn.insn_id:08x} => it's a wait and the last insn")
                                insn.sq_events.append({'type': f'waiting forever {insn.disasm}', 'start': start, 'end':start + 1000})
                            else:
                                next_insn = wave.insns[i+1]
                                end = start + 1

                                for n in next_insn.sq_events:
                                    if n['type'] != "IB_NOT_RDY":
                                        end = n['start']
                                        break

                                if start < end:
                                    wait_name = insn.disasm.split('(')[0].split()[0]
                                    insn.sq_events.append({'type': f'waiting {wait_name}', 'start': start, 'end':end})

                        # make s_sema_wait longer
                        is_s_sema_wait = insn.disasm.startswith("s_sema_wait")
                        if is_s_sema_wait:
                            start = insn.sq_events[-1]['end']
                            if i+1 not in wave.insns:
                                insn.sq_events.append({'type': f'waiting forever {insn.disasm}', 'start': start, 'end':start + 1000})
                            else:
                                end_wait = [i for i in insn.sema_events if i['type'] == "WAIT_DONE"]

                                assert len(end_wait) <= 1
                                end = end_wait[0]['ts'] if len(end_wait) > 0 else start
                                if start < end:
                                    insn.sq_events.append({'type': f'waiting {insn.disasm}', 'start': start, 'end':end})


                        # if no wave.end, set to the last event
                        if find_end:
                            if insn.sq_events:
                                wave.end = max(wave.end, insn.sq_events[-1]['end'])
                            if insn.sp_events:
                                wave.end = max(wave.end, insn.sp_events[-1]['end'])
                            for pe_list in insn.path_events.values():
                                for pe in pe_list:
                                    wave.end = max(wave.end, pe["end"])
                    for i in bad_insns:
                        del wave.insns[i]

                    assert len(wave.insns) > 0

    def __parse_file(self, filename, shader_engines, shader_arrays, wgps, simds):
        ignored = set()
        m = filename_re.match(os.path.basename(filename))
        if m is None:
            print(f"Error: bad filename {filename}, cannot extract se/sa => should be something like se3sa0_itrace_emu.mon")
            sys.exit(1)
        se = int(m.group(1))
        sa = int(m.group(2))
        if (shader_engines is not None and se not in shader_engines) or (shader_arrays is not None and sa not in shader_arrays):
            print(f"Ignoring {filename} because of -se, -sa options")
            return

        if se not in self.__se:
            self.__se[se] = {}
        if sa not in self.__se[se]:
            self.__se[se][sa] = {}
        waves_dict = self.__se[se][sa]

        # Pre-compile constants for faster lookup
        skip_prefixes = ("//  TS=", "// SIGNAL", "// WAIT WG_ID", "fp_excp", "// INIT", "// JOIN", "//  src=sema", "// S_SET_FRAMES")
        skip_insn_ids = frozenset(("_vdata_",))

        # Use larger buffer for faster I/O on large files
        with open(filename, 'r', buffering=8*1024*1024) as f:  # 8MB buffer
            lno = 0
            for l in f:
                lno+=1
                line = l.rstrip('\n\r')
                if not line:
                    continue
                try:
                    ids, data = line.split(': ', 1)
                except ValueError:
                    continue

                id_parts = ids.split()

                if len(id_parts) != 2:
                    if self.print_unknown_lines:
                        print(f"{filename}:{lno}: {l.strip()}", file=sys.stderr)
                    continue

                wave_id, insn_id = id_parts[0], id_parts[1]

                # Global hierarchy tracks: wave dbg id and insn id are all zeros; dotted path lives in // ...
                # e.g. "00000000 00000000: // sh0.sa0.wgp0.tex0.p0: cnt=1 TS=57000"
                if (
                    wave_id in ItraceParser._GLOBAL_PATH_WAVE_INSN_IDS
                    and (insn_id in ItraceParser._GLOBAL_PATH_WAVE_INSN_IDS or int(insn_id,16) >= SPECIAL_INSN_THRESHOLD)
                ):
                    icache_idx = int(insn_id,16) - SPECIAL_INSN_THRESHOLD
                    if self.__parse_global_path_prefix_payload(data, is_icache=icache_idx>=0, icache_idx=icache_idx):
                        continue
                    if self.print_unknown_lines:
                        print(f"{filename}:{lno}: {l.strip()}", file=sys.stderr)
                    continue

                if wave_id in ignored:
                    continue
                if insn_id in skip_insn_ids:
                    continue
                if insn_id == "_sdata_":
                     m = sdata_re.match(data)
                     assert m is not None
                     if wave_id not in self.__sdata:
                         self.__sdata[wave_id] = {}
                     self.__sdata[wave_id][int(m.group(1))] = int(m.group(2), 16)
                     continue

                # WAVE start and end
                if insn_id == "_new_wv":
                    m = wave_start_re.match(data)
                    if m is None:
                        continue
                    wgp = int(m.group(1))
                    simd = int(m.group(2), 2)
                    if wgps is not None and wgp not in wgps or simds is not None and simd not in simds:
                        ignored.add(wave_id)
                        continue
                    wave_nr = int(m.group(3))
                    start = int(m.group(4))
                    self.__first_wave_start = self.__first_wave_start or start
                    assert wave_id not in waves_dict
                    if wave_id not in self.__sdata:
                        self.__sdata[wave_id] = {}
                    waves_dict[wave_id] = Wave(wave_id, wgp, simd, wave_nr, self.__sdata[wave_id], start)
                    continue

                if data.startswith("// ENDPGM"):
                    waves_dict[wave_id].set_end(data)
                    wave_end = waves_dict[wave_id].end
                    if self.__last_wave_end is None or wave_end > self.__last_wave_end:
                        self.__last_wave_end = wave_end
                    continue

                if data.startswith(skip_prefixes):
                    continue

                # get insn id
                insn_id = int(insn_id, 16)

                # wave draw id
                if insn_id == 0xffffffff:
                    m = draw_id_re.match(data)
                    if m is not None:
                        draw_id = int(m.group(1))
                        waves_dict[wave_id].draw_id = draw_id
                    continue


                if insn_id > SPECIAL_INSN_THRESHOLD:
                    continue
                insn_id &= DUAL_ISSUE_MASK  # Mask out dual-issue bit

                # add instruction to wave if not already there
                if wave_id not in waves_dict:
                    if self.print_unknown_lines:
                        print(f"Warning: unknown wave_id {wave_id} in {filename}:{lno}... ignoring line", file=sys.stderr)
                    continue
                wave = waves_dict[wave_id]

                wave.add_insn(insn_id)

                if "TS=" not in data:
                    wave.set_disasm(insn_id, data)
                    continue
                if data.startswith('r '):
                    #TODO:
                    continue
                if data.startswith('w '):
                    wave.write_reg(insn_id, data)
                    continue
                if data.startswith('// SQ:'):
                    wave.sq_event(insn_id, data)
                    continue
                if data.startswith('// SP:'):
                    wave.sp_event(insn_id, data)
                    continue
                if wave.sq_arb(insn_id, data):
                    continue

                m_uc = unit_counter_line_re.match(data)
                if m_uc is not None:
                    wave.add_unit_counter(
                        m_uc.group(1),
                        m_uc.group(2),
                        int(m_uc.group(3)),
                        int(m_uc.group(4)),
                    )
                    continue

                m_pe = path_event_line_re.match(data)
                if m_pe is not None and "=" not in m_pe.group(2):
                    wave.add_path_event(
                        insn_id,
                        m_pe.group(1),
                        m_pe.group(2),
                        int(m_pe.group(4)),
                        m_pe.group(3).strip(),
                    )
                    continue


    def __save_wave_summary(self, filename):
        columns = ['wave dbg id', 'start ts', 'end ts', 'duration', 'se', 'sa', 'wgp', 'simd', 'draw_id', 'cluster_x', 'cluster_y', 'cluster_z', 'wg_x', 'wg_y', 'wg_z', 'wvgrp_id', 'wvgrp_wv_id']
        table = {i: [] for i in columns}
        nr_rows = 0
        for se, sas in self.__se.items():
            for sa in sas:
                for wave in sas[sa].values():
                    table['start ts'].append(wave.start)
                    table['end ts'].append(wave.end)
                    table['duration'].append(wave.end-wave.start+1)
                    table['se'].append(se)
                    table['sa'].append(sa)
                    table['wgp'].append(wave.wgp)
                    table['simd'].append(wave.simd)
                    table['wave dbg id'].append(wave.wave_id)
                    table['draw_id'].append(wave.draw_id)
                    for p in  ('cluster_x', 'cluster_y', 'cluster_z', 'wg_x', 'wg_y', 'wg_z', 'wvgrp_id', 'wvgrp_wv_id'):
                        table[p].append(wave.position[p] if p in wave.position else '')
                    nr_rows+=1
        # convert to string
        for c in columns:
            table[c] = [str(i) for i in table[c]]

        # remove columns with no data and prepend header
        for c in columns:
            if all(i == '' for i in table[c]):
                del table[c]
            else:
                table[c] = [c] + table[c]

        columns = list (table.keys())

        # get max widths
        max_widths = { c: max(len(i) for i in table[c]) + 2  for c in columns}
        # and output
        separator = '+' + '+'.join( ["-"*max_widths[c] for c in columns]) + "+\n"
        with open(filename, 'w') as f:
            f.write(separator)
            for row_nr in range(nr_rows):
                row = '|' .join( [f"{table[c][row_nr]:^{max_widths[c]}s}" for c in columns])
                f.write(f"|{row}|\n")
                if row_nr == 0:
                    f.write(separator)
            f.write(separator)


