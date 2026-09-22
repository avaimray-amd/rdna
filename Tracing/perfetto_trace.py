from dataclasses import dataclass, field
import re
import struct as _struct
import uuid
import yaml
from perfetto.trace_builder.proto_builder import StreamingTraceProtoBuilder
from perfetto.protos.perfetto.trace.perfetto_trace_pb2 import TrackEvent, TrackDescriptor, ProcessDescriptor, ThreadDescriptor, DebugAnnotation
from itrace_parser import ItraceParser
from gclog_parser import GclogParser
from semaphores import VALID_SEMS

# ---------------------------------------------------------------------------
# Raw binary encoder for hot-path TracePacket events.
# Bypasses Python protobuf object construction (TracePacket, DebugAnnotation,
# repeated-field appends, SerializeToString) which is the dominant cost with
# the pure-Python protobuf 3.20 backend.
# ---------------------------------------------------------------------------

def _pb_varint(n):
    buf = bytearray()
    while n > 0x7f:
        buf.append(0x80 | (n & 0x7f))
        n >>= 7
    buf.append(n)
    return bytes(buf)

def _pb_fv(fn, v):
    """Varint field: tag (wire type 0) + varint value."""
    return _pb_varint(fn << 3) + _pb_varint(v)

def _pb_fl(fn, d):
    """Length-delimited field: tag (wire type 2) + varint(len) + data."""
    return _pb_varint((fn << 3) | 2) + _pb_varint(len(d)) + d

def _pb_fs(fn, s):
    """String field."""
    b = s.encode() if isinstance(s, str) else s
    return _pb_fl(fn, b)

def _pb_f64(fn, v):
    """Fixed64 field (wire type 1): tag + 8 bytes LE."""
    return _pb_varint((fn << 3) | 1) + _struct.pack('<Q', v)

# Pre-computed constant byte fragments
_PB_TPSID          = _pb_fv(10, 12345)       # trusted_packet_sequence_id = 12345
_PB_TYPE_SLICE_BEGIN = _pb_fv(9, 1)
_PB_TYPE_SLICE_END   = _pb_fv(9, 2)
_PB_TYPE_INSTANT     = _pb_fv(9, 3)
_PB_TYPE_COUNTER     = _pb_fv(9, 4)
_PB_EVENT_TYPES = {1: _PB_TYPE_SLICE_BEGIN, 2: _PB_TYPE_SLICE_END,
                   3: _PB_TYPE_INSTANT,     4: _PB_TYPE_COUNTER}

def _pb_counter_packet(ts, track_uuid, value):
    """Raw bytes for a TYPE_COUNTER TracePacket."""
    if isinstance(value, int):
        cv = _pb_fv(30, value)                               # counter_value = 30
    else:
        cv = _pb_f64(44, _struct.unpack('<Q', _struct.pack('<d', float(value)))[0])  # double_counter_value = 44
    te = _PB_TYPE_COUNTER + _pb_fv(11, track_uuid) + cv
    return _pb_fv(8, ts) + _pb_fl(11, te) + _PB_TPSID

def _pb_slice_packet(ts, event_type, track_uuid, name=None, flow_ids=None, corr_id=None, descr=None):
    """Raw bytes for a slice/instant TracePacket (write_later=False only)."""
    parts = [_PB_EVENT_TYPES[event_type], _pb_fv(11, track_uuid)]
    if name:
        parts.append(_pb_fs(23, name))
    if flow_ids:
        for fid in flow_ids:                                  # non-packed fixed64 (wire type 1)
            parts.append(_pb_f64(47, fid))
    if corr_id is not None:
        parts.append(_pb_fv(52, corr_id))
    if descr:
        for k, v in descr.items():
            parts.append(_pb_fl(4, _pb_fs(10, k) + _pb_fs(6, str(v))))
    te = b''.join(parts)
    return _pb_fv(8, ts) + _pb_fl(11, te) + _PB_TPSID

# ---------------------------------------------------------------------------

TRUSTED_PACKET_SEQUENCE_ID  = 12345
SIMD_WAVE_CORR_ID_BASE      = 0xff000000
SIMD_WAVE_CORR_ID_EV_NR_INC = 0x1000
WAVE_SUM_CORR_ID_BASE       = 0xff800000
WAIT_COUNTERS_FLOW_BASE     = 0
SEMA_FLOW_BASE              = 0x10000000
CONNECT_EVENTS_FLOW_BASE    = 0x20000000
SEMA_CORR_ID_BASE           = 0xFFF00000
FLOW_INC_PER_FILE           = 0x1000000
MAX_SA_PER_SE               = 2

# gclog mem event channel suffix from gclog_parser (bits [9:8] of addr)
_MEM_GLOG_CH_SUFFIX = re.compile(r'_C[0-3]$')


def _mem_gclog_base_type(t: str) -> str:
    """Base mem event name: strip trailing _C0.._C3, else unchanged."""
    return _MEM_GLOG_CH_SUFFIX.sub('', t)


def _gclog_mem_slice_track_type(event_name: str, me: dict) -> str:
    """Track/slice type for a ``mem_tracks`` entry; key is ``event_name``, optional ``me['type']`` or addr channel."""
    t = me.get("type")
    if t is not None:
        return t
    addr = me.get("addr")
    if addr is not None:
        return f"{event_name}_C{GclogParser._channel_from_addr(str(addr))}"
    return event_name


def _flow_id_from_addr(addr) -> int:
    """Uint63 flow id from address: hex string (optional ``0x``) or ``int``; itrace ``addr:`` and gclog use hex strings."""
    if isinstance(addr, int):
        return addr & ((1 << 63) - 1)
    s = str(addr).strip()
    if s.lower().startswith("0x"):
        s = s[2:]
    v = int(s, 16)
    return v & ((1 << 63) - 1)


@dataclass
class TrackHier:
    uuid: int
    se_tracks: dict       = field(default_factory = lambda: {})
    sa_tracks: dict       = field(default_factory = lambda: {})
    wgp_tracks: dict      = field(default_factory = lambda: {})
    simd_tracks: dict     = field(default_factory = lambda: {})
    simd_tracks_per_wave: dict     = field(default_factory = lambda: {})
    wave_tracks: dict     = field(default_factory = lambda: {})
    sq_tracks: dict = field(default_factory = lambda: {})
    sq_stall_tracks: dict = field(default_factory = lambda: {})
    sq_stall_subtracks: dict = field(default_factory = lambda: {})
    sq_arb_tracks: dict   = field(default_factory = lambda: {})
    pc_tracks: dict       = field(default_factory = lambda: {})
    sp_tracks: dict       = field(default_factory = lambda: {})
    sp_stall_tracks: dict = field(default_factory = lambda: {})
    sp_stall_subtracks: dict = field(default_factory = lambda: {})
    sp_vdst_write_tracks: dict = field(default_factory = lambda: {})
    sp_vnbr_tracks: dict = field(default_factory = lambda: {})
    mem_tracks: dict      = field(default_factory = lambda: {})
    event_tracks: dict    = field(default_factory = lambda: {})
    event_count_tracks: dict    = field(default_factory = lambda: {})
    sema_tracks: dict    = field(default_factory = lambda: {})
    sema_subtracks: dict    = field(default_factory = lambda: {})
    # wave_id -> {(unit_name, counter_name): counter track uuid}
    itrace_unit_tracks: dict = field(default_factory = lambda: {})
    # wave_id -> { "path.event": slice track uuid } for itrace ``// path event TS=``
    path_event_tracks: dict = field(default_factory = lambda: {})

class PerfettoTrace:
    def __init__(self, parser: ItraceParser, counters=None, semas=None,
                 ev_regexs=[], ev_names=[], ev_counters_analog=False, connect_events_conf=None, events_from_file=None,
                 gclog_parser=None):
        self.ev_regexs = [re.compile(i) for i in ev_regexs]
        self.ev_names = ev_names
        self.ev_counters_analog = ev_counters_analog
        # Use the regex pattern as the default name if no event name is provided
        self.ev_names += [ev_regexs[i] for i in range(len(self.ev_names), len(self.ev_regexs))]
        assert len(self.ev_names) == len(set(self.ev_names)), "event names should be unique"
        self.ev_from_file = {}
        self.ev_names_from_file = set()        
        if events_from_file is not None:
            for f in events_from_file:
                self.__load_events_from_file(f)
        self.__ev_counts = {}
        self.__found_events = {}
        self.__connect_events_conf = connect_events_conf             
        self.parser = parser
        self.gclog_parser = gclog_parser
        self.__wave_summary_tracks = {}
        self.__counters = counters
        self.__counter_tracks = {}
        self.__waitloadcnts = 0
        self.__semas = semas
        # gclog counters mirrored under each wave (key dbg_id prefix in counter name)
        self.__dbg_to_wave_id = {}
        self.__gclog_wave_parent = {}
        self.__gclog_wave_counter_mid = {}
        self.__gclog_wave_counter_tracks = {}
        self.__gclog_next_rank = {}
        # gclog mem events not tied to an itrace instruction (see GclogParser.mem_tracks)
        self.__gclog_mem_track_map = {}
        self.__itrace_unit_tracks = {}
        self.__path_event_tracks = {}
        # (wave_id, dotted path prefix) -> folder track uuid; shared by itrace unit counters and path event slices
        self.__wave_path_mid_nodes = {}
        self.__path_unit_track_nodes = {}
        self.__path_unit_counter_tracks = {}
        self.__path_prefix_event_tracks = {}
        self.__has_top_level_path_unit_counters = False

    def __call__(self, outfile):
        with open(outfile, 'wb', buffering=128*1024*1024) as f: #128MB for large traces
            self.builder = StreamingTraceProtoBuilder(f)
            self.__pending_packet_writes = []
            self.__write_file = f
            self.__write_buf = bytearray()
            # create tracks
            self.__create_tracks()
    
            if self.parser is not None:
                # populate wave summary
                self.__populate_wave_summary()
                
                # populate exec
                self.__populate_exec()

                self.__populate_itrace_unit_counters()
                self.__populate_path_unit_counters()
                self.__populate_path_prefix_events()
                self.__populate_icache_tracks()

                #global events (from file)
                self.__populate_global_events()
    
                if self.__semas is not None:
                    self.__populate_semaphores()
    
                if self.__connect_events_conf is not None:
                    try:
                        self.__connect_events()
                    except Exception as e:
                        print (f"Warning: errors while connecting events.. stopped => {e}")

                # write packets that have not yet been written into the streaming builder
                # (e.g. events => they are written after the connections are processed)
                for i in self.__pending_packet_writes:
                    self.__write_packet(i)
            if self.__gclog_mem_track_map:
                self.__populate_gclog_mem_tracks()
            if self.__counters is not None:
                # populate counters
                self.__populate_counters()
            self.__flush_write_buf()


    def __load_events_from_file(self, filename):
        """ load events from file whose lines are one of these:
            TS WAVE_ID EVENT_NAME 
            TS GLOBAL EVENT_NAME
            blank lines and anything following '#' is ignored
        """
        rm_comments = re.compile(r'#.*')
        with open(filename, 'r') as f:
            for line in f:
                line = rm_comments.sub('', line).strip()
                if line == '':
                    continue
                try:
                    ts, wave_id, event_name = line.split()
                    ts = int(ts)
                    if wave_id != 'GLOBAL':
                        wave_id = int(wave_id, 16)
                    if wave_id not in self.ev_from_file:
                        self.ev_from_file[wave_id] = []
                    self.ev_from_file[wave_id].append({'ts': ts, 'event_name': event_name})
                    if event_name in self.ev_names:
                        print(f"ERROR: event {event_name} from {filename} is already defined by the -en cmd line option", file=sys.stderr)
                        sys.exit(1)
                    self.ev_names_from_file.add(event_name)
                except ValueError:
                    print(f"Warning: invalid line in events file: {line}")

    def __create_tracks(self):
        self.__gclog_wave_parent = {}
        self.__gclog_wave_counter_mid = {}
        self.__gclog_wave_counter_tracks = {}
        self.__gclog_next_rank = {}
        self.__itrace_unit_tracks = {}
        self.__path_event_tracks = {}
        self.__wave_path_mid_nodes = {}
        self.__path_unit_track_nodes = {}
        self.__path_unit_counter_tracks = {}
        self.__path_prefix_event_tracks = {}
        self.__has_top_level_path_unit_counters = False
        self.__icache_tracks = {}
        self.__top_level_path_unit_counters_track = None
        if self.parser is not None:
            self.__build_dbg_to_wave_id_map()
            self.__create_wave_summary_tracks(self.__new_track("Wave summary", rank=0))
            self.__wave_exec = TrackHier(self.__new_track("Wave exec", rank=1))
            self.__create_se_tracks(self.__wave_exec)
            self.__create_itrace_unit_tracks()
            self.__create_path_event_tracks()
            self.__create_path_unit_counter_tracks()
            self.__create_icache_tracks()
        else:
            self.__wave_exec = None
        if 'GLOBAL' in self.ev_from_file:
            self.__create_global_event_tracks()
        if self.__counters is not None:
            # Create a separate track group for each CSV file (rank after Wave summary/exec, optional Path unit counters, Global events)
            ctr_rank_base = 4 if self.__has_top_level_path_unit_counters else 3
            for idx, (csv_name, counter_parser) in enumerate(self.__counters):
                if isinstance(counter_parser, GclogParser) and self.parser is not None:
                    # Non-wave keys: plain global names, or unit paths from gclog
                    # (e.g. ``model.gpu0.sh0.sa0.tex0.tcp0.<counter_name>``) built as
                    # ``{location}.{counter_name}`` in ``GclogParser._add_counter_sample``.
                    # ``__create_counter_tracks`` splits on ``.`` so the model path becomes
                    # nested tracks under "Counters: {csv_name}".
                    global_counters = {
                        k: v
                        for k, v in counter_parser.counters.items()
                        if not self.__gclog_counter_key_maps_to_known_wave(k)
                    }
                    if global_counters:
                        parent_uuid = self.__new_track(f"Counters: {csv_name}", rank=ctr_rank_base + idx)
                        self.__create_counter_tracks(csv_name, parent_uuid, global_counters)
                    self.__create_gclog_counter_tracks_per_wave(counter_parser.counters)
                else:
                    parent_uuid = self.__new_track(f"Counters: {csv_name}", rank=ctr_rank_base + idx)
                    self.__create_counter_tracks(csv_name, parent_uuid, counter_parser.counters)

        self.__create_gclog_mem_tracks()

    def __create_gclog_mem_tracks(self):
        """Descriptor tracks for GclogParser.mem_tracks (no matching wave/instruction)."""
        self.__gclog_mem_track_map = {}
        gp = self.gclog_parser
        if gp is None or not getattr(gp, "mem_tracks", None):
            return
        present = set()
        for event_name, events in gp.mem_tracks.items():
            for me in events:
                present.add(_gclog_mem_slice_track_type(event_name, me))
        if not present:
            return
        n_counters = len(self.__counters) if self.__counters is not None else 0
        ctr_rank_base = 4 if self.__has_top_level_path_unit_counters else 3
        rank_base = ctr_rank_base + n_counters
        parent = self.__new_track("GCLOG MEM (unmapped)", rank=rank_base)
        bases_in_present = {_mem_gclog_base_type(t) for t in present}
        mem_bases_ordered = [t for t in GclogParser.EVENT_NAMES if t in bases_in_present]
        mem_bases_ordered.extend(
            sorted(t for t in bases_in_present if t not in GclogParser.EVENT_NAMES)
        )
        for i, b in enumerate(mem_bases_ordered):
            self.__gclog_mem_track_map[b] = self.__new_track(
                f"MEM {b}", parent=parent, rank=i + 1
            )
        channel_to_add = sorted(
            t for t in present if t != _mem_gclog_base_type(t)
        )
        for ct in channel_to_add:
            b = _mem_gclog_base_type(ct)
            if b not in self.__gclog_mem_track_map:
                continue
            base_uuid = self.__gclog_mem_track_map[b]
            existing_ch = [
                k
                for k in self.__gclog_mem_track_map
                if k != b and _mem_gclog_base_type(k) == b
            ]
            self.__gclog_mem_track_map[ct] = self.__new_track(
                f"MEM {ct}", parent=base_uuid, rank=len(existing_ch) + 1
            )

    def __populate_gclog_mem_tracks(self):
        gp = self.gclog_parser
        if gp is None or not self.__gclog_mem_track_map:
            return
        slices = []
        for event_name, events in gp.mem_tracks.items():
            for me in events:
                typ = _gclog_mem_slice_track_type(event_name, me)
                tr = self.__gclog_mem_track_map.get(typ)
                if tr is None:
                    continue
                descr = {}
                if "addr" in me:
                    descr["addr"] = me["addr"]
                if "dbg_id" in me:
                    descr["dbg_id"] = hex(me["dbg_id"])
                if "inst_id" in me:
                    descr["inst_id"] = hex(me["inst_id"])
                corr_id = me.get("dbg_id")
                addr_flow = None
                if "addr" in me:
                    addr_flow = [_flow_id_from_addr(me["addr"])]
                slices.append((me["start"], me["end"], tr, typ, descr, corr_id, addr_flow))
        for start, end, tr, typ, descr, corr_id, addr_flow in sorted(
            slices, key=lambda x: (x[0], x[1])
        ):
            self.__add_slice(
                ts=start,
                end_ts=end,
                event_track_uuid=tr,
                name=typ,
                corr_id=corr_id,
                descr=descr,
                flow_ids=addr_flow,
                flow_ids_end=addr_flow,
            )

    def __gclog_counter_key_maps_to_known_wave(self, k: str) -> bool:
        """True for ``dbg_hex.…`` keys whose first segment is a hex wave id in the itrace.

        Keys whose first segment is not hex (e.g. ``model.gpu0.…`` for unit-scoped gclog
        counters) are treated as global series, not per-wave mirrors.
        """
        if not self.__dbg_to_wave_id:
            return False
        parts = k.split('.')
        if len(parts) < 2:
            return False
        try:
            dbg_int = int(parts[0], 16)
        except ValueError:
            return False
        return dbg_int in self.__dbg_to_wave_id

    def __build_dbg_to_wave_id_map(self):
        self.__dbg_to_wave_id = {}
        for se, sas in self.parser.se().items():
            for sa, waves in sas.items():
                for wave in waves.values():
                    self.__dbg_to_wave_id[int(wave.wave_id, 16)] = (
                        se, sa, wave.wgp, wave.simd, wave.wave_nr
                    )

    def __gclog_next_sibling_rank(self, parent_uuid):
        r = self.__gclog_next_rank.get(parent_uuid, 0)
        self.__gclog_next_rank[parent_uuid] = r + 1
        return r

    def __create_gclog_counter_tracks_per_wave(self, counters_dict):
        """Counter keys are ``{dbg_id_hex}.{rest…}``; mirror leaf tracks under that wave."""
        if self.__wave_exec is None or not self.__dbg_to_wave_id:
            return
        for k in counters_dict:
            parts = k.split('.')
            if len(parts) < 2:
                continue
            try:
                dbg_int = int(parts[0], 16)
            except ValueError:
                continue
            wave_id = self.__dbg_to_wave_id.get(dbg_int)
            if wave_id is None:
                continue
            if wave_id not in self.__gclog_wave_parent:
                self.__gclog_wave_parent[wave_id] = self.__new_track(
                    'GC log counters',
                    parent=self.__wave_exec.wave_tracks[wave_id],
                    rank=6,
                )
            hier = parts[1:]
            parent = self.__gclog_wave_parent[wave_id]
            ac_hier = ''
            for i, h in enumerate(hier):
                ac_hier = h if i == 0 else f'{ac_hier}.{h}'
                mid_key = (wave_id, ac_hier)
                is_leaf = i == len(hier) - 1
                if is_leaf:
                    rank = self.__gclog_next_sibling_rank(parent)
                    self.__gclog_wave_counter_tracks[(wave_id, k)] = self.__new_track(
                        h, parent=parent, counter=True, rank=rank
                    )
                else:
                    if mid_key not in self.__gclog_wave_counter_mid:
                        rank = self.__gclog_next_sibling_rank(parent)
                        self.__gclog_wave_counter_mid[mid_key] = self.__new_track(
                            h, parent=parent, counter=False, rank=rank
                        )
                    parent = self.__gclog_wave_counter_mid[mid_key]

    def __create_itrace_unit_tracks(self):
        """Per-wave itrace unit samples: dot-split path into nested groups; leaf = counter (same idea as path-prefixed ``a.b.c: // …``)."""
        if self.__wave_exec is None:
            return
        for se, sas in self.parser.se().items():
            for sa, waves in sas.items():
                for wave in waves.values():
                    units = getattr(wave, "units", None) or {}
                    if not units:
                        continue
                    wave_id = (se, sa, wave.wgp, wave.simd, wave.wave_nr)
                    wave_uuid = self.__wave_exec.wave_tracks[wave_id]
                    mid_nodes = self.__wave_path_mid_nodes
                    children_by_parent = {}
                    for unit_name, ctr_map in units.items():
                        segs = unit_name.split(".") if unit_name else []
                        for counter_name in ctr_map:
                            hier = segs + [counter_name]
                            for i in range(len(hier)):
                                parent_ac = ".".join(hier[:i]) if i else ""
                                seg = hier[i]
                                if parent_ac not in children_by_parent:
                                    children_by_parent[parent_ac] = set()
                                children_by_parent[parent_ac].add(seg)
                    itrace_sibling_rank = {}
                    for parent_ac, segs in children_by_parent.items():
                        for idx, name in enumerate(sorted(segs)):
                            # Direct children of wave use ranks 7+ (pc/SQ/SP/MEM/Events/Sem take 0–6).
                            r = (7 + idx) if parent_ac == "" else idx
                            itrace_sibling_rank[(parent_ac, name)] = r
                    for unit_name, ctr_map in units.items():
                        segs = unit_name.split(".") if unit_name else []
                        for counter_name in ctr_map:
                            hier = segs + [counter_name]
                            parent = wave_uuid
                            ac = ""
                            for i, h in enumerate(hier):
                                ac = h if i == 0 else f"{ac}.{h}"
                                is_leaf = i == len(hier) - 1
                                parent_ac = ".".join(hier[:i]) if i else ""
                                sr = itrace_sibling_rank[(parent_ac, h)]
                                if is_leaf:
                                    tid = self.__new_track(
                                        h,
                                        parent=parent,
                                        counter=True,
                                        rank=sr,
                                    )
                                    self.__itrace_unit_tracks[(wave_id, unit_name, counter_name)] = tid
                                    if wave_id not in self.__wave_exec.itrace_unit_tracks:
                                        self.__wave_exec.itrace_unit_tracks[wave_id] = {}
                                    self.__wave_exec.itrace_unit_tracks[wave_id][(unit_name, counter_name)] = tid
                                else:
                                    mid_key = (wave_id, ac)
                                    if mid_key not in mid_nodes:
                                        mid_nodes[mid_key] = self.__new_track(
                                            h,
                                            parent=parent,
                                            counter=False,
                                            rank=sr,
                                        )
                                    parent = mid_nodes[mid_key]
                    

    def __create_icache_tracks(self):
        icache_parent = self.__new_track("icaches", parent=self.__top_level_path_unit_counters_track)
        for wgp, icache_data in self.parser.icaches().items():
            icache_nodes = {}
            wgp_parent = self.__new_track(f"wgp{wgp}", parent=icache_parent)
            for fullname in icache_data.path_events:
                parent_track = wgp_parent
                segs = fullname.split(".")
                assert len(segs) > 0
                while True:
                    if segs[0] not in icache_nodes:
                        icache_nodes[segs[0]] = self.__new_track(segs[0], parent_track)
                    parent_track = icache_nodes[segs[0]]
                    if len(segs) == 1:
                        self.__icache_tracks[(wgp, fullname)] = parent_track
                        break
                    segs = [segs[0]+"."+segs[1]] + segs[2:]
        
    def __create_path_event_tracks(self):
        """Per-wave ``Instruction.path_events``: nested path + event leaf slice tracks under the wave."""
        if self.__wave_exec is None:
            return
        self.__path_event_tracks = {}
        for se, sas in self.parser.se().items():
            for sa, waves in sas.items():
                for wave in waves.values():
                    keys = set()
                    for insn in wave.insns.values():
                        keys.update(insn.path_events.keys())
                    if not keys:
                        continue
                    wave_id = (se, sa, wave.wgp, wave.simd, wave.wave_nr)
                    wave_uuid = self.__wave_exec.wave_tracks[wave_id]
                    mid_nodes = self.__wave_path_mid_nodes
                    for composite_key in sorted(keys):
                        if "." in composite_key:
                            path, event = composite_key.rsplit(".", 1)
                        else:
                            path, event = "", composite_key
                        segs = path.split(".") if path else []
                        hier = segs + [event]
                        parent = wave_uuid
                        ac = ""
                        for i, h in enumerate(hier):
                            ac = h if i == 0 else f"{ac}.{h}"
                            is_leaf = i == len(hier) - 1
                            if is_leaf:
                                tid = self.__new_track(
                                    h,
                                    parent=parent,
                                    counter=False,
                                    rank=self.__gclog_next_sibling_rank(parent),
                                )
                                self.__path_event_tracks[(wave_id, composite_key)] = tid
                                if wave_id not in self.__wave_exec.path_event_tracks:
                                    self.__wave_exec.path_event_tracks[wave_id] = {}
                                self.__wave_exec.path_event_tracks[wave_id][composite_key] = tid
                            else:
                                mid_key = (wave_id, ac)
                                if mid_key not in mid_nodes:
                                    mid_nodes[mid_key] = self.__new_track(
                                        h,
                                        parent=parent,
                                        counter=False,
                                        rank=self.__gclog_next_sibling_rank(parent),
                                    )
                                parent = mid_nodes[mid_key]

    def __create_path_unit_counter_tracks(self):
        """Tracks under one tree for path-prefixed ``// UNIT cnt=value TS=`` (counters) and
        ``// UNIT event TS=`` (slices), from ``ItraceParser.unit_counters()`` and
        ``path_prefix_events()`` — same dot hierarchy."""
        if self.__wave_exec is None:
            return
        uc = self.parser.unit_counters()
        ppe = self.parser.path_prefix_events()
        all_keys = sorted(set(uc.keys()) | set(ppe.keys()))
        if not all_keys:
            return
        children_by_parent = {}
        for k in all_keys:
            parts = k.split(".")
            for i in range(len(parts)):
                parent_ac = ".".join(parts[:i]) if i else ""
                seg = parts[i]
                if parent_ac not in children_by_parent:
                    children_by_parent[parent_ac] = set()
                children_by_parent[parent_ac].add(seg)
        path_unit_sibling_rank = {}
        for parent_ac, segs in children_by_parent.items():
            for rank, name in enumerate(sorted(segs)):
                path_unit_sibling_rank[(parent_ac, name)] = rank
        root = self.__new_track(
            "Path unit counters & events",
            parent=None,
            rank=2,
        )
        self.__top_level_path_unit_counters_track = root
        self.__has_top_level_path_unit_counters = True
        prefix = "pathuc"
        for k in all_keys:
            hier = k.split(".")
            for i, h in enumerate(hier):
                if i == 0:
                    ac_hier = h
                    parent = root
                    parent_ac = ""
                else:
                    parent = self.__path_unit_track_nodes[f"{prefix}.{ac_hier}"]
                    ac_hier += "." + h
                    parent_ac = ".".join(hier[:i])
                track_key = f"{prefix}.{ac_hier}"
                is_leaf = i == len(hier) - 1
                if track_key not in self.__path_unit_track_nodes:
                    sr = path_unit_sibling_rank[(parent_ac, h)]
                    self.__path_unit_track_nodes[track_key] = self.__new_track(
                        h,
                        parent=parent,
                        counter=is_leaf and (k in uc),
                        rank=sr,
                    )
                if is_leaf:
                    if k in uc:
                        self.__path_unit_counter_tracks[k] = self.__path_unit_track_nodes[track_key]
                    if k in ppe:
                        self.__path_prefix_event_tracks[k] = self.__path_unit_track_nodes[track_key]

    def __populate_path_unit_counters(self):
        uc = self.parser.unit_counters()
        if not uc or not self.__path_unit_counter_tracks:
            return
        print("... populating path unit counters ...")
        for k, samples in uc.items():
            track = self.__path_unit_counter_tracks.get(k)
            if track is None:
                continue
            for value, ts in samples:
                self.__add_counter_event(ts, value, track)

    def __populate_path_prefix_events(self):
        ppe = self.parser.path_prefix_events()
        if not ppe or not self.__path_prefix_event_tracks:
            return
        print("... populating path prefix events ...")
        for k, pe_list in ppe.items():
            tr = self.__path_prefix_event_tracks.get(k)
            if tr is None:
                continue
            leaf = k.rsplit(".", 1)[-1] if "." in k else k
            name = leaf
            for pe in pe_list:
                ped = {"path": k}
                if "extra" in pe:
                    ped["extra"] = pe["extra"]
                flow_ids = None
                if "addr" in pe:
                    ped["addr"] = pe["addr"]
                    flow_ids = [_flow_id_from_addr(pe["addr"])]
                    name = pe["addr"]
                self.__add_slice(
                    ts=pe['start'],
                    end_ts=pe['end'],
                    event_track_uuid=tr,
                    name=name,
                    descr=ped,
                    flow_ids=flow_ids,
                )

    def __populate_itrace_unit_counters(self):
        if self.__wave_exec is None or not self.__itrace_unit_tracks:
            return
        print("... populating itrace unit counters ...")
        for se, sas in self.parser.se().items():
            for sa, waves in sas.items():
                for wave in waves.values():
                    units = getattr(wave, "units", None) or {}
                    if not units:
                        continue
                    wave_id = (se, sa, wave.wgp, wave.simd, wave.wave_nr)
                    for unit_name, ctr_map in units.items():
                        for counter_name, samples in ctr_map.items():
                            track = self.__itrace_unit_tracks.get((wave_id, unit_name, counter_name))
                            if track is None:
                                continue
                            for value, ts in samples:
                                self.__add_counter_event(ts, value, track)

    def __create_wave_summary_tracks(self, parent_uuid):
        ses = self.parser.se()
        for se, sas in ses.items():
            for sa in sas:
                sa_id = (se, sa)
                for wave in sas[sa].values():
                    wgp_id = sa_id + (wave.wgp,)
                    simd_id = wgp_id + (wave.simd,)
                    wave_id = simd_id + (wave.wave_nr,)
                    rank = sum(j*(32**i) for i,j in enumerate(reversed(wave_id)))
                    self.__wave_summary_tracks[wave_id] = self.__new_track(f"waves se{se} sa{sa} wgp{wave.wgp} simd{wave.simd}", parent=parent_uuid, rank=rank)

    def __create_global_event_tracks(self):
        rank = 3 if self.__has_top_level_path_unit_counters else 2
        self.__global_event_track = self.__new_track(f"Global events", rank=rank)
        self.__global_event_tracks = {}
        self.__global_event_count_tracks = {}
        for ev_idx, en in enumerate(self.ev_names_from_file):
            self.__global_event_tracks[ev_idx] = self.__new_track(en, parent=self.__global_event_track, rank=ev_idx)
            self.__global_event_count_tracks[ev_idx] = self.__new_track(f'{en} count', parent=self.__global_event_track, rank=ev_idx*2+1, counter=self.ev_counters_analog)
            
    def __create_se_tracks(self, parent):
        ses = self.parser.se()
        for se, sas in ses.items():
            se_uuid = self.__new_track(f"se{se}", parent.uuid, rank=se)
            parent.se_tracks[se] = se_uuid
            for sa in sas:
                sa_id = (se, sa)
                sa_uuid = self.__new_track(f"sa{sa}", parent=se_uuid,  rank=sa)
                parent.sa_tracks[sa_id] = sa_uuid
                wgps = set()
                simds = set()
                for wave in sas[sa].values():
                    wgp_id = sa_id + (wave.wgp,)
                    simd_id = wgp_id + (wave.simd,)
                    wave_id = simd_id + (wave.wave_nr,)
                    if wgp_id not in parent.wgp_tracks:
                        parent.wgp_tracks[wgp_id] = self.__new_track(f'wgp{wave.wgp}', parent = sa_uuid, rank=wave.wgp)

                    if simd_id not in parent.simd_tracks:
                        parent.simd_tracks[simd_id] = self.__new_track(f'simd{wave.simd}', parent=parent.wgp_tracks[wgp_id], rank=wave.simd)
                        
                    if wave_id not in parent.simd_tracks_per_wave:
                        parent.simd_tracks_per_wave[wave_id] = self.__new_track(f'simd{wave.simd}', parent=parent.wgp_tracks[wgp_id], rank=wave.simd)
                        parent.wave_tracks[wave_id] = self.__new_track(f'wave{wave.wave_nr}', parent=parent.simd_tracks[simd_id], rank=wave.wave_nr)
                        wave_uuid = parent.wave_tracks[wave_id]
                        # pc tracks
                        parent.pc_tracks[wave_id] = self.__new_track(f'pc', parent=wave_uuid, rank=0, counter=True)

                        # sq tracks
                        main_sq_arb = self.__new_track(f'SQ', parent=wave_uuid, rank=1)
                        main_sq_stall = self.__new_track(f'SQ', parent=wave_uuid, rank=1)
                        parent.sq_tracks[wave_id] = {'arb': main_sq_arb, 'stall': main_sq_stall}
                        parent.sq_stall_tracks[wave_id] = self.__new_track(f'SQ stall', parent=main_sq_stall, rank=1)
                        parent.sq_arb_tracks[wave_id] = self.__new_track(f'SQ arb', parent=main_sq_arb, rank=2)
                        parent.sq_stall_subtracks[wave_id] = {}

                        # sp tracks
                        main_sp = self.__new_track(f'SP', parent=wave_uuid, rank=2)
                        parent.sp_tracks[wave_id] = {}
                        parent.sp_stall_tracks[wave_id] = self.__new_track(f'SP stall', parent=main_sp, rank=0)
                        parent.sp_stall_subtracks[wave_id] = {}
                        
                        # mem tracks (gclog pipeline markers, subtracks added per wave below)
                        main_mem = self.__new_track(f'MEM', parent=wave_uuid, rank=3)
                        parent.mem_tracks[wave_id] = {}
                        
                        event_parent_uuid = self.__new_track(f'Events', parent=wave_uuid, rank=4)
                        event_tracks = {}
                        event_count_tracks = {}

                        # event tracks (event + counter per regex)
                        for ev_idx, en in enumerate(self.ev_names):
                            event_tracks[ev_idx] = self.__new_track(en, parent=event_parent_uuid, rank=ev_idx*2)
                            event_count_tracks[ev_idx] = self.__new_track(f'{en} count', parent=event_parent_uuid, rank=ev_idx*2+1, counter=self.ev_counters_analog)

                        # event tracks from  file
                        for i, en in enumerate(self.ev_names_from_file):
                            ev_idx = i + len(self.ev_names)
                            event_tracks[ev_idx] = self.__new_track(en, parent=event_parent_uuid, rank=ev_idx)
                            event_count_tracks[ev_idx] = self.__new_track(f'{en} count', parent=event_parent_uuid, rank=ev_idx*2+1, counter=self.ev_counters_analog)

                        parent.event_tracks[wave_id] = event_tracks
                        parent.event_count_tracks[wave_id] = event_count_tracks

                        # sema tracks
                        if self.__semas is not None:
                            sema_parent_uuid = self.__new_track(f'Semaphores', parent=wave_uuid, rank=5)
                            sema_tracks = {i: self.__new_track(f'Sema {i}', parent=sema_parent_uuid, rank=i) for i in VALID_SEMS}
                            sema_subtracks = {i: {sub: self.__new_track(f'{sub}', parent=sema_tracks[i], rank=r) for r, sub in enumerate(("count", "done"))} for i in VALID_SEMS}
                            parent.sema_tracks[wave_id] = sema_tracks
                            parent.sema_subtracks[wave_id] = sema_subtracks

                    
                    # and sub-tracks.. updating dict in case there's more than on 'waveN' in  the simd
                    sq_stall_subtracks = {}
                    stall_types = set([e['type'] for i in wave.insns.values() for e in i.sq_events if e['type'] != 'ARB'])
                    # filter out the ones already added
                    stall_types = [i for i in stall_types if i not in parent.sq_stall_subtracks[wave_id]]
                    existing = len(parent.sq_stall_subtracks[wave_id])
                    parent.sq_stall_subtracks[wave_id].update( {t: self.__new_track(t, parent=parent.sq_stall_tracks[wave_id], rank=existing + i) for i, t in enumerate(sorted(stall_types))})
                    
                    # sp tracks - create subtracks for ISSUE_ types, STALLED_ types, VDST_WRITE types, and VNBR_ types
                    issue_types = set([e['type'] for i in wave.insns.values() for e in i.sp_events if e['type'].startswith("ISSUE_")])
                    stall_types = set([e['type'] for i in wave.insns.values() for e in i.sp_events if e['type'].startswith("STALLED_")])
                    vdst_write_types = set([e['type'] for i in wave.insns.values() for e in i.sp_events if e['type'].startswith("VDST_WRITE")])
                    vnbr_types = set([e['type'] for i in wave.insns.values() for e in i.sp_events if e['type'].startswith("VNBR_")])
                    
                    # filter out the ones already added
                    issue_types = [i for i in issue_types if i not in parent.sp_tracks[wave_id]]
                    stall_types = [i for i in stall_types if i not in parent.sp_stall_subtracks[wave_id]]
                    vdst_write_types = [i for i in vdst_write_types if i not in parent.sp_vdst_write_tracks.get(wave_id, {})]
                    vnbr_types = [i for i in vnbr_types if i not in parent.sp_vnbr_tracks.get(wave_id, {})]
                    
                    existing_issue = len(parent.sp_tracks[wave_id])
                    existing_stall = len(parent.sp_stall_subtracks[wave_id])
                    existing_vdst_write = len(parent.sp_vdst_write_tracks.get(wave_id, {}))
                    existing_vnbr = len(parent.sp_vnbr_tracks.get(wave_id, {}))
                    
                    # Initialize the dictionary if it doesn't exist
                    if wave_id not in parent.sp_vdst_write_tracks:
                        parent.sp_vdst_write_tracks[wave_id] = {}
                    if wave_id not in parent.sp_vnbr_tracks:
                        parent.sp_vnbr_tracks[wave_id] = {}
                    
                    # Create issue tracks (these go directly under SP)
                    main_sp = self.__new_track(f'SP', parent=parent.wave_tracks[wave_id], rank=2)
                    for i, t in enumerate(sorted(issue_types)):
                        parent.sp_tracks[wave_id][t] = self.__new_track(f'SP {t}', parent=main_sp, rank=i+1+existing_issue)
                    
                    # Create VDST_WRITE tracks (these go directly under SP at the same level as ISSUE_)
                    for i, t in enumerate(sorted(vdst_write_types)):
                        parent.sp_vdst_write_tracks[wave_id][t] = self.__new_track(f'SP {t}', parent=main_sp, rank=len(issue_types)+existing_issue+i+1+existing_vdst_write)
                    
                    # Create VNBR_ tracks (these go directly under SP at the same level as ISSUE_)
                    for i, t in enumerate(sorted(vnbr_types)):
                        parent.sp_vnbr_tracks[wave_id][t] = self.__new_track(f'SP {t}', parent=main_sp, rank=len(issue_types)+existing_issue+len(vdst_write_types)+existing_vdst_write+i+1+existing_vnbr)

                    # Create stall subtracks (these go under SP stall)
                    parent.sp_stall_subtracks[wave_id].update({t: self.__new_track(t, parent=parent.sp_stall_tracks[wave_id], rank=existing_stall + i) for i, t in enumerate(sorted(stall_types))})
                    
                    # mem tracks — base types under MEM; _C0.._C3 channel variants nested under their base (gclog_parser)
                    present = set(me['type'] for i in wave.insns.values() for me in i.mem_events)
                    bases_in_present = {_mem_gclog_base_type(t) for t in present}
                    mem_bases_ordered = [t for t in GclogParser.EVENT_NAMES if t in bases_in_present and t not in parent.mem_tracks[wave_id]]
                    mem_bases_ordered.extend(sorted(t for t in bases_in_present if t not in GclogParser.EVENT_NAMES and t not in parent.mem_tracks[wave_id]))
                    existing_mem = len(parent.mem_tracks[wave_id])
                    main_mem = self.__new_track(f'MEM', parent=parent.wave_tracks[wave_id], rank=3)
                    for i, b in enumerate(mem_bases_ordered):
                        parent.mem_tracks[wave_id][b] = self.__new_track(f'MEM {b}', parent=main_mem, rank=i + 1 + existing_mem)
                    channel_to_add = sorted(
                        t for t in present
                        if t != _mem_gclog_base_type(t) and t not in parent.mem_tracks[wave_id]
                    )
                    for ct in channel_to_add:
                        b = _mem_gclog_base_type(ct)
                        if b not in parent.mem_tracks[wave_id]:
                            continue
                        base_uuid = parent.mem_tracks[wave_id][b]
                        existing_ch = [k for k in parent.mem_tracks[wave_id] if k != b and _mem_gclog_base_type(k) == b]
                        parent.mem_tracks[wave_id][ct] = self.__new_track(
                            f'MEM {ct}', parent=base_uuid, rank=len(existing_ch) + 1
                        )
    
    def __create_counter_tracks(self, csv_name, counters_parent_uuid, counters_dict):
        for k,v in counters_dict.items():
            hier = k.split('.')
            for i,h in enumerate(hier):
                if i == 0:
                    ac_hier = h
                    parent = counters_parent_uuid
                else:
                    parent = self.__counter_tracks[f"{csv_name}.{ac_hier}"]
                    ac_hier+="."+h
                # Prefix track key with csv_name to keep separate
                track_key = f"{csv_name}.{ac_hier}"
                if track_key not in self.__counter_tracks:
                    self.__counter_tracks[track_key] = self.__new_track(h, parent=parent, counter=(i == len(hier)-1))
              
    def __get_hier(self, h, current=None):
        if current is None:
            current = self.__hier
        if h[0] not in current:
            current[h[0]] = {}
            current = current[h[0]]
        if len(h) == 1:
            return current
        return self.__get_hier(h[1:], current)
            

        
    def __populate_wave_summary(self):
        ses = self.parser.se()
        for se, sas in ses.items():
            for sa in sas:
                sa_id = (se, sa)
                for wave in sas[sa].values():
                    wave_id = (se, sa, wave.wgp, wave.simd, wave.wave_nr)
                    track_uuid = self.__wave_summary_tracks[wave_id]
                    name = f'slot:{wave.wave_nr} - dbg_id:{wave.wave_id} - coords: {wave.get_position()} - draw_id: {wave.draw_id}'
                    corr_id =  sum(j*(32**i) for i,j in enumerate(wave_id[:-2]))+ WAVE_SUM_CORR_ID_BASE 
                    self.__add_slice(ts=wave.start, end_ts=wave.end, event_track_uuid=track_uuid,
                                     name=name, descr={"start": wave.start, "end": wave.end},
                                     corr_id=corr_id)


    def __populate_exec(self):
        ses = self.parser.se()
        for se, sas in ses.items():
            for sa in sas:
                assert sa < MAX_SA_PER_SE, "adjust MAX_SA_PER_SE"
                sa_id = (se, sa)
                wait_counters_flow_base  = WAIT_COUNTERS_FLOW_BASE + (sa + se*MAX_SA_PER_SE) * FLOW_INC_PER_FILE
                sema_flow_base = SEMA_FLOW_BASE + (sa + se*MAX_SA_PER_SE) * FLOW_INC_PER_FILE
                for wave in sas[sa].values():
                    wave_id = (se, sa, wave.wgp, wave.simd, wave.wave_nr)

                    main_sq_track = self.__wave_exec.sq_tracks[wave_id]
                    sq_stall_track = self.__wave_exec.sq_stall_tracks[wave_id]
                    sq_arb_track = self.__wave_exec.sq_arb_tracks[wave_id]
                    sq_stall_subtracks = self.__wave_exec.sq_stall_subtracks[wave_id]
                    pc_track = self.__wave_exec.pc_tracks[wave_id]
                    simd_track = self.__wave_exec.simd_tracks_per_wave[wave_id]
                    sp_tracks = self.__wave_exec.sp_tracks[wave_id]
                    sp_stall_track = self.__wave_exec.sp_stall_tracks[wave_id]
                    sp_stall_subtracks = self.__wave_exec.sp_stall_subtracks[wave_id]
                    sp_vdst_write_tracks = self.__wave_exec.sp_vdst_write_tracks.get(wave_id, {})
                    sp_vnbr_tracks = self.__wave_exec.sp_vnbr_tracks.get(wave_id, {})
                    mem_tracks = self.__wave_exec.mem_tracks.get(wave_id, {})
                    event_tracks = self.__wave_exec.event_tracks.get(wave_id, {})
                    event_count_tracks = self.__wave_exec.event_count_tracks.get(wave_id, {})
                    
                    wave_pos = f'se{se}_sa{sa}_wgp{wave.wgp}_simd{wave.simd}_wave{wave.wave_nr}'
                    wave_nr_name = f"wave{wave.wave_nr}"
                    mem_event_slices = []
                    path_event_slices = []

                    print(f"... populating exec for wave {wave.wave_id} ...")
                    for insn_id in sorted(wave.insns):
                        insn = wave.insns[insn_id]
                        # Cache formatted strings to avoid repeated formatting
                        pc_hex = f'{insn.pc:x}' if insn.pc is not None else ''
                        disasm_s = insn.disasm or ''
                        insn_name = f'{insn.insn_id:x} - {disasm_s}' if disasm_s else f'{insn.insn_id:x}'
                        extra_str = "\n".join(insn.extra_info) if insn.extra_info else ""
                        
                        wave_lsb32 = int(wave.wave_id, 16) & 0xFFFFFFFF
                        insn_lsb32 = insn.insn_id & 0xFFFFFFFF
                        insn_flow_id = (wave_lsb32 << 32) | insn_lsb32
                        
                        descr = {
                            'PC': pc_hex,
                            'pos': wave_pos,
                            'DISASM': disasm_s,
                            'EXTRA': extra_str,
                            'INSN_ID': f'{insn.insn_id:08x}',
                            'WAVE_ID': wave.wave_id,
                            'FLOW_ID': f'{insn_flow_id:016x}'
                        }
                        
                        # add sq events                        
                        for e in insn.sq_events:
                            start = e['start']
                            end = e['end']
                            typ = e['type']
                            sq_subtracks = (sq_arb_track,) if typ == 'ARB' else (sq_stall_track, sq_stall_subtracks[typ])
                            name = insn_name if typ == 'ARB' else typ
                            
                            # Create event-specific description if extra info exists
                            if 'extra' in e:
                                event_descr = {**descr, 'stall_info': e['extra']}
                            else:
                                event_descr = descr

                            # add to  sq track
                            assert all( f < FLOW_INC_PER_FILE for f in insn.wait_counters_flow_start + insn.wait_counters_flow_end)
                            flow_start =[f + wait_counters_flow_base for f in insn.wait_counters_flow_start]
                            flow_end = [f + wait_counters_flow_base for f in insn.wait_counters_flow_end]
                            flow_end.extend([f + sema_flow_base for f in insn.sema_flow])
                            flow_ids_begin = flow_start
                            if insn_flow_id is not None:
                                flow_ids_begin = flow_start + [insn_flow_id]

                            sq_track = main_sq_track['arb'] if typ == 'ARB' else main_sq_track['stall']

                            self.__add_slice(ts=start, end_ts=end, event_track_uuid=sq_track, name=name,
                                             corr_id=insn.pc, descr=event_descr, flow_ids=flow_ids_begin, flow_ids_end=flow_end)
                        
                            # add to sq detail track (either stall or arb)
                            for t in sq_subtracks:
                                self.__add_slice(ts=start, end_ts=end, event_track_uuid=t, name=name,
                                                 corr_id=insn.pc, flow_ids=(insn_flow_id,))
                            
                            # if event is ARB... update PC and add insn to simd track
                            if typ == 'ARB':
                                self.__add_counter_event(start, insn.pc - wave.lowest_pc, pc_track)
                                self.__add_slice(ts=start, end_ts=end,
                                                       event_track_uuid=simd_track, name=f"{wave_nr_name} - {name}",
                                                       corr_id=insn.pc, descr=descr)

                                # add events from regexs
                                for ev_idx, ern in enumerate(zip(self.ev_regexs, self.ev_names)):
                                    er, en = ern
                                    if er.match(insn.disasm) is not None:
                                        self.__add_event(wave, ev_idx, en, event_tracks, event_count_tracks, start, descr, write_later=True)

                        # add sp events
                        for e  in insn.sp_events:
                            start = e['start']
                            end = e['end']
                            typ = e['type']
                            if typ.startswith("STALLED_"):
                                # Add to both SP stall track and the specific stall subtrack
                                name = typ
                                self.__add_slice(ts=start, end_ts=end, event_track_uuid=sp_stall_track,
                                                 name=name,corr_id=insn.pc, descr=descr)
                                
                                # Add to specific stall subtrack
                                if typ in sp_stall_subtracks:
                                    self.__add_slice(ts=start, end_ts=end, event_track_uuid=sp_stall_subtracks[typ],
                                                     name=name, corr_id=insn.pc, descr=descr)
                            elif typ.startswith("ISSUE_") or typ.startswith('XDL_PREREAD'):
                                name = insn_name
                                if typ in sp_tracks:
                                    self.__add_slice(ts=start, end_ts=end, event_track_uuid=sp_tracks[typ],
                                                     name=name, corr_id=insn.pc, descr=descr)
                            elif typ.startswith("VDST_WRITE"):
                                name = insn_name
                                if typ in sp_vdst_write_tracks:
                                    self.__add_slice(ts=start, end_ts=end, event_track_uuid=sp_vdst_write_tracks[typ],
                                                     name=name, corr_id=insn.pc, descr=descr)
                            elif typ.startswith("VNBR_"):
                                name = insn_name
                                if typ in sp_vnbr_tracks:
                                    self.__add_slice(ts=start, end_ts=end, event_track_uuid=sp_vnbr_tracks[typ],
                                                     name=name, corr_id=insn.pc, descr=descr)
                            else:
                                print(f"BAD sp event {start} {end} {typ}")

                        # mem events (gclog pipeline markers on MEM subtracks; same slice shape as SQ arb)
                        for me in insn.mem_events:
                            start = me['start']
                            end = me['end']
                            typ = me['type']
                            if typ not in mem_tracks:
                                continue
                            mem_descr = descr.copy()
                            mem_flow_ids = [insn_flow_id]
                            if 'addr' in me:
                                mem_descr['addr'] = me['addr']
                                mem_flow_ids.append(_flow_id_from_addr(me['addr']))
                            mem_event_slices.append({
                                'start': start,
                                'end': end,
                                'track': mem_tracks[typ],
                                'name': insn_name,
                                'corr_id': insn.pc,
                                'descr': mem_descr,
                                'flow_ids': mem_flow_ids,
                            })

                        # itrace path events (// path event TS=) — nested tracks under the wave
                        for composite_key, pe_list in insn.path_events.items():
                            tr = self.__path_event_tracks.get((wave_id, composite_key))
                            if tr is None:
                                continue
                            if "." in composite_key:
                                path_pe, event_pe = composite_key.rsplit(".", 1)
                            else:
                                path_pe, event_pe = "", composite_key
                            for pe in pe_list:
                                ped = descr.copy()
                                ped["path"] = path_pe
                                ped["event"] = event_pe
                                if "extra" in pe:
                                    ped["extra"] = pe["extra"]
                                if "addr" in pe:
                                    ped["addr"] = pe["addr"]
                                pe_flow_ids = [insn_flow_id]
                                if "addr" in pe:
                                    pe_flow_ids.append(_flow_id_from_addr(pe["addr"]))
                                path_event_slices.append({
                                    'start': pe['start'],
                                    'end': pe['end'],
                                    'track': tr,
                                    'name': insn_name,
                                    'corr_id': insn.pc if insn.pc is not None else insn.insn_id,
                                    'descr': ped,
                                    'flow_ids': pe_flow_ids,
                                })

                    for item in sorted(mem_event_slices + path_event_slices, key=lambda x: x['start']):
                        self.__add_slice(
                            ts=item['start'],
                            end_ts=item['end'],
                            event_track_uuid=item['track'],
                            name=item['name'],
                            corr_id=item['corr_id'],
                            descr=item['descr'],
                            flow_ids=item['flow_ids'],
                        )

                    # add events from file
                    for ev_idx, en in enumerate(self.ev_names_from_file):
                        ev_from_file = [i for i in self.ev_from_file.get(int(wave.wave_id,16), []) if i['event_name'] == en]
                        for ev in sorted(ev_from_file, key=lambda x: x['ts']):
                            self.__add_event(wave, ev_idx + len(self.ev_names), en, event_tracks, event_count_tracks, ev['ts'], descr={ 'WAVE_ID': wave.wave_id})
                    if int(wave.wave_id,16) in self.ev_from_file:
                        del self.ev_from_file[int(wave.wave_id,16)]

    def __populate_global_events(self):
        for ev_idx, en in enumerate(self.ev_names_from_file):
            ev_from_file = [i for i in self.ev_from_file.get('GLOBAL', []) if i['event_name'] == en]
            for ev in sorted(ev_from_file, key=lambda x: x['ts']):
                self.__add_event(None, ev_idx, en, self.__global_event_tracks, self.__global_event_count_tracks, ev['ts'])
        if "GLOBAL" in self.ev_from_file:
            del self.ev_from_file["GLOBAL"]
        for i in self.ev_from_file.keys():
            print(f"Warning: events from file for wave {i:08x} not added => wave_id not found!")

    def __add_event(self, wave, ev_idx, en, event_tracks, event_count_tracks, start, descr={}, write_later=False):
        start_time = self.parser.first_wave_start()
        if wave is not None:
            wave_id = wave.wave_id
            event_corr_id = wave.wave_nr + SIMD_WAVE_CORR_ID_BASE + ev_idx * SIMD_WAVE_CORR_ID_EV_NR_INC
            event_name = f"W{wave.wave_nr} {en}"
        else:
            wave_id = 'GLOBAL'
            event_corr_id = None
            event_name = en

        if wave_id not in self.__ev_counts:
            self.__ev_counts[wave_id] = {}
        if en not in self.__ev_counts[wave_id]:
            self.__ev_counts[wave_id][en] = 0

        count = self.__ev_counts[wave_id][en]
        event_name += f" - {count}"

        self.__ev_counts[wave_id][en]+=1
        
        # If event tracks exist, use them; otherwise fallback to simd/sq tracks
        if ev_idx in event_tracks:
            event_track = event_tracks[ev_idx]
            ev_packet = self.__add_slice_event(ts=start, event_type=TrackEvent.TYPE_INSTANT,
                                             event_track_uuid=event_track, name=event_name,
                                             corr_id=event_corr_id, descr=descr, write_later=write_later)
            event_track = event_count_tracks[ev_idx]
            if wave is not None:
                if wave not in self.__found_events:
                    self.__found_events[wave] = []
                self.__found_events[wave].append({'name': en, 'packet': ev_packet, 'ts':start})
            
            if self.ev_counters_analog:
                if count == 0:
                    self.__add_counter_event(start_time, 0, event_count_tracks[ev_idx])
                self.__add_counter_event(start, count + 1, event_count_tracks[ev_idx])
            else:
                if count == 0:
                    self.__add_slice_event(ts=start_time, event_type=TrackEvent.TYPE_SLICE_BEGIN,
                                           event_track_uuid=event_track, name='0',
                                           corr_id=event_corr_id, descr=descr)
                self.__add_slice_event(ts=start, event_type=TrackEvent.TYPE_SLICE_END,
                                       event_track_uuid=event_track, corr_id=event_corr_id)
                self.__add_slice_event(ts=start, event_type=TrackEvent.TYPE_SLICE_BEGIN,
                                       event_track_uuid=event_track, name=f'{count+1}',
                                       corr_id=event_corr_id, descr=descr)                                                
        else:
            # Fallback to original behavior
            self.__add_slice_event(ts=start, event_type=TrackEvent.TYPE_INSTANT,
                                   event_track_uuid=simd_track, name=event_name + " ",
                                   corr_id=event_corr_id, descr=descr, write_later=write_later)
            self.__add_slice_event(ts=start, event_type=TrackEvent.TYPE_INSTANT,
                                   event_track_uuid=sq_track, name=event_name,
                                   corr_id=event_corr_id, descr=descr, write_later=write_later)


    def __populate_semaphores(self):
        print(f"... populating semaphores ...")
        for wave_coords, sem_data in self.__semas.wave_map['coord'].items():
            sema_flow_base = SEMA_FLOW_BASE + (wave_coords[1] + wave_coords[0]*MAX_SA_PER_SE) * FLOW_INC_PER_FILE
            sema_tracks = self.__wave_exec.sema_tracks[wave_coords]
            sema_subtracks = self.__wave_exec.sema_subtracks[wave_coords]
            for sem in sem_data.semas.values():
                if not sem.valid:
                    continue
                sema_track = sema_tracks[sem.sema_id]
                sema_count = sema_subtracks[sem.sema_id]["count"]
                sema_done = sema_subtracks[sem.sema_id]["done"]
                
                for i, sem_data in enumerate(sem.data):
                    start, val = sem_data
                    if i == len(sem.data) -1:
                        wv_dbg_id = sem.wave_dbg_id(start)
                        _, end = self.parser.wave_start_end(wv_dbg_id)
                    else:
                        end = sem.data[i+1][0]
                    flow_ids = [f + sema_flow_base for f in val["flow"]]
                    for t, v in zip( (sema_track, sema_count, sema_done),
                                     (val["count"]+ val["done"] * val['limit'], val["count"], val["done"]) ):
                        f = flow_ids if t==sema_track  else None
                        self.__add_slice_event(ts=start, event_type=TrackEvent.TYPE_SLICE_BEGIN, event_track_uuid=t, name=str(v), flow_ids=f, corr_id=SEMA_CORR_ID_BASE+sem.sema_id)
                        self.__add_slice_event(ts=end, event_type=TrackEvent.TYPE_SLICE_END, event_track_uuid=t)
                    if val['done_set']:
                        self.__add_slice_event(ts=start, event_type=TrackEvent.TYPE_INSTANT, event_track_uuid=sema_done, name="done set",  corr_id=SEMA_CORR_ID_BASE+sem.sema_id)
                            
    def __counter_track_for_series(self, csv_name, counter_parser, series_key):
        """Perfetto counter track UUID for this series, or None if no track was created.

        Gclog series keys are either ``{dbg_hex}.{name}`` (under the wave) or
        ``{model_path}.{name}`` for unit counters (under ``Counters: {csv_name}``).
        """
        if isinstance(counter_parser, GclogParser) and self.parser is not None:
            if self.__gclog_counter_key_maps_to_known_wave(series_key):
                dbg_int = int(series_key.split('.')[0], 16)
                w_id = self.__dbg_to_wave_id.get(dbg_int)
                if w_id is None:
                    return None
                return self.__gclog_wave_counter_tracks.get((w_id, series_key))
        return self.__counter_tracks.get(f"{csv_name}.{series_key}")

    def __populate_counters(self):
        print(f"... populating perf counters ...")
        for csv_name, counter_parser in self.__counters:
            for k, v in counter_parser.counters.items():
                track = self.__counter_track_for_series(csv_name, counter_parser, k)
                if track is None:
                    continue
                for ts, val in v:
                    self.__add_counter_event(ts, val, track)

    def __connect_events(self):
        print(f"... connecting events ...")
        with open(self.__connect_events_conf, "r") as f:
            connections = yaml.safe_load(f)
        flow_id = CONNECT_EVENTS_FLOW_BASE
        for connection in connections:
            match_src_wvgrp = lambda i : 'src_wvgrp' not in connection or  i.position.get('wvgrp_id', -1) == connection['src_wvgrp']
            match_src_cluster = lambda i : 'src_cluster' not in connection or all ( i.position.get(k, -1) == connection['src_wvgrp'][ki] for k,ki in ("cluster_x", "cluster_y", "cluster_z") )
            src_waves = [i for i in self.__found_events if match_src_wvgrp(i) and match_src_cluster(i) and i.position.get("wvgrp_wv_id",-1) == connection['src_wave']]
            for src_wave in src_waves:
                if 'dst_wvgrp' not in connection:
                    match_dst_wvgrp = lambda i : i.position.get('wvgrp_id', -1) == src_wave.position.get('wvgrp_id', -2)
                else:
                    match_dst_wvgrp = lambda i: i.position.get('wvgrp_id', -1) == connection['dst_wvgrp']

                if 'dst_cluster' not in connection:
                    match_dst_cluster = lambda i : all (i.position.get(k, -1) == i.position.get(k, -2) for k in ("cluster_x", "cluster_y", "cluster_z") )
                else:
                    match_dst_cluster = lambda i: all (i.position.get(k, -1) == connection['dst_cluster'][ki] for k,ki in ("cluster_x", "cluster_y", "cluster_z") )
                    
                    
                src_events = [ i for i in self.__found_events[src_wave] if i['name'] == connection['src_ev']]
                for src_ev_idx, src_event in enumerate(src_events):
                    dst_waves = [i for i in self.parser.all_waves_at_ts(src_event['ts']) if match_dst_wvgrp(i) and match_dst_cluster(i) and i.position.get("wvgrp_wv_id",-1) == connection['dst_wave']]
                    if len(dst_waves) == 0:
                        continue
                    if 'cond' not in connection:
                        cond = True
                    elif isinstance(connection['cond'], int):
                        cond = connection['cond'] != 0
                    else:
                        cond = eval(connection['cond'], {}, {'i': src_ev_idx})
                    if not cond:
                        continue
                    dst_count = eval(connection['dst_count'], {}, {'i': src_ev_idx})
                    for dst_wave in dst_waves:
                        if dst_wave not in self.__found_events:
                           continue
                        dst_events = [i for i in self.__found_events[dst_wave] if i['name'] == connection['dst_ev']]
                        if dst_count >= len(dst_events):
                            continue
                        dst_events[dst_count]['packet'].track_event.flow_ids.append(flow_id)
                        src_event['packet'].track_event.flow_ids.append(flow_id)
                        flow_id+=1

    def __populate_icache_tracks(self):
        print(f"... populating icaches ...")
        icaches = self.parser.icaches()
        for wgp, icache_data in icaches.items():
            for event, event_data in icache_data.path_events.items():
                track = self.__icache_tracks.get((wgp, event))
                if track is None:
                    continue
                name = event.split('.')
                name = name[-1] if name else '??'
                for pe in event_data:
                    ped = {}
                    for k in ('path', 'extra', 'addr'):
                        if k in pe:
                            ped[k] = pe[k]

                    self.__add_slice(
                        ts=pe['start'],
                        end_ts=pe['end'],
                        event_track_uuid=track,
                        name=name,
                        descr=ped                )


        
    def __flush_write_buf(self):
        if self.__write_buf:
            self.__write_file.write(bytes(self.__write_buf))
            self.__write_buf = bytearray()

    def __write_packet(self, packet):
        """Serialize packet directly to a bytearray buffer, bypassing StreamingTraceProtoBuilder overhead.

        StreamingTraceProtoBuilder.write_packet() re-wraps each packet in a Trace proto and calls
        SerializeToString() on the wrapper — unnecessary per-call overhead. Here we serialize the
        TracePacket directly and prepend the Perfetto wire-format field tag (field 1, wire type 2 = 0x0a)
        + varint-encoded length. Flush to disk when the buffer reaches 8 MB.
        """
        data = packet.SerializeToString()
        n = len(data)
        buf = self.__write_buf
        buf.append(0x0a)  # field 1, wire type 2
        while n > 0x7f:
            buf.append(0x80 | (n & 0x7f))
            n >>= 7
        buf.append(n)
        buf += data
        if len(buf) >= 8 * 1024 * 1024:
            self.__flush_write_buf()

    def __new_track(self, name, parent=None, rank=None, counter=False):
        track_uuid = uuid.uuid4().int & ((1 << 63) - 1)
        packet = self.builder.create_packet()
        packet.track_descriptor.uuid = track_uuid
        packet.track_descriptor.name = name
        if parent is not None:
            packet.track_descriptor.parent_uuid = parent
        packet.track_descriptor.child_ordering = TrackDescriptor.EXPLICIT
        if rank is not None:
            packet.track_descriptor.sibling_order_rank = rank
        if counter:
            packet.track_descriptor.counter.SetInParent()
        self.__write_packet(packet)
        return track_uuid

    def __add_slice(self, *, event_track_uuid, ts, end_ts=None, name=None, flow_ids=None, flow_ids_end=None, corr_id=None, descr=None, write_later=False):
        is_instant = end_ts is None or ts == end_ts
        if is_instant:
            event_type = TrackEvent.TYPE_INSTANT
            if flow_ids is None:
                flow_ids = []
            if flow_ids_end is not None:
                flow_ids=flow_ids + flow_ids_end
        else:
            event_type = TrackEvent.TYPE_SLICE_BEGIN
        
        self.__add_slice_event(ts=ts, event_type=event_type, event_track_uuid=event_track_uuid, name=name,
                               flow_ids=flow_ids, corr_id=corr_id, descr=descr, write_later=write_later)
        if not is_instant:
            self.__add_slice_event(ts=end_ts, event_type=TrackEvent.TYPE_SLICE_END,
                                   event_track_uuid=event_track_uuid, corr_id=corr_id, flow_ids=flow_ids_end, write_later=write_later) 
        
    def __add_slice_event(self, ts, event_type, event_track_uuid, name=None, flow_ids=None, corr_id=None, descr=None, write_later=False):
        if write_later:
            # write_later packets may have flow_ids appended in __connect_events — keep on protobuf API
            packet = self.builder.create_packet()
            packet.timestamp = ts
            packet.track_event.type = event_type
            packet.track_event.track_uuid = event_track_uuid
            if name:
                packet.track_event.name = name
            if flow_ids:
                packet.track_event.flow_ids.extend(flow_ids)
            if corr_id is not None:
                packet.track_event.correlation_id = corr_id
            if descr is not None:
                for k, v in descr.items():
                    debug_annotation = DebugAnnotation()
                    debug_annotation.name = k
                    debug_annotation.string_value = str(v)
                    packet.track_event.debug_annotations.append(debug_annotation)
            packet.trusted_packet_sequence_id = TRUSTED_PACKET_SEQUENCE_ID
            self.__pending_packet_writes.append(packet)
            return packet
        # Hot path: raw binary encoding — avoids TracePacket/DebugAnnotation object creation
        data = _pb_slice_packet(ts, event_type, event_track_uuid, name, flow_ids, corr_id, descr)
        buf = self.__write_buf
        n = len(data)
        buf.append(0x0a)
        while n > 0x7f:
            buf.append(0x80 | (n & 0x7f))
            n >>= 7
        buf.append(n)
        buf += data
        if len(buf) >= 8 * 1024 * 1024:
            self.__flush_write_buf()
        return None

    def __add_counter_event(self, ts, value, event_track_uuid):
        data = _pb_counter_packet(ts, event_track_uuid, value)
        buf = self.__write_buf
        n = len(data)
        buf.append(0x0a)
        while n > 0x7f:
            buf.append(0x80 | (n & 0x7f))
            n >>= 7
        buf.append(n)
        buf += data
        if len(buf) >= 8 * 1024 * 1024:
            self.__flush_write_buf()
        return None
