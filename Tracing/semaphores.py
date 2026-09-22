import  re
from enum import Enum
from itrace_parser import ItraceParser

VALID_SEMS = range(1,6)
VERBOSE_INSNS = False
VERBOSE_SEMAS = False
NR_SIMDS = 4
class Semaphore:
    flow_count = 0

    def __init__(self, sema_id, wave_coords):
        self.sema_id = sema_id
        self.wave_coords = wave_coords
        self.__wave_dbg_id = []
        self.valid = False
        self.data = []
        
    def wave_dbg_id(self, ts=None):
        if ts is None or ts == -1:
            return None if len(self.__wave_dbg_id)==0 else self.__wave_dbg_id[-1][1]
        id = None
        for t, i in self.__wave_dbg_id:
            if t <= ts:
                id = i
        return id

    def verbose(self, ts, msg):
        VERBOSE_SEMAS and print(f"    SEMA{self.sema_id}: Wave {self.wave_dbg_id(ts)} {self.wave_coords} at {ts} - {msg} - count={self.get_count()} limit={self.get_limit()} done={self.get_done()}")

    def set_wave_dbg_id(self, ts, wave_dbg_id):
        self.__wave_dbg_id.append((ts, wave_dbg_id))

    def get_last_val(self, what, err_if_empty=True):
        if len(self.data) == 0:
            if err_if_empty:
                raise ValueError(f"Semaphore {self.sema_id} of wave {self.wave_coords} is empty")
            else:
                return 0
        else:
            return self.data[-1][1][what]

    def set_val(self, what, ts, val, check_valid=True):
        assert not check_valid or self.valid, f"Semaphore {self.sema_id} of wave {self.wave_coords} is not valid for set_{what}"
        if len(self.data) > 0 and ts == self.data[-1][0]:
            self.data[-1][1][what] = val
        else:
            if len(self.data) == 0:
                new_val = {"count": 0, "limit": 0, "done": 0, "done_set": False, "flow": []}
            else:
                new_val = self.data[-1][1].copy()
                new_val["flow"] = []
            new_val[what] = val
            self.data.append((ts, new_val))

    def update_flow(self, flow_id):
        if len(self.data) == 0:
            return
        self.data[-1][1]["flow"].append(flow_id)
        ts=self.data[-1][0]

    def get_count(self, err_if_empty=True):
        return self.get_last_val("count", err_if_empty)

    def get_limit(self, err_if_empty=True):
        return self.get_last_val("limit", err_if_empty)

    def get_done(self, err_if_empty=True):
        return self.get_last_val("done", err_if_empty)

    def set_count(self, ts, val, check_valid=True):
        self.set_val("count", ts, val, check_valid)


    def set_limit(self, ts, val, check_valid=True):
        self.set_val("limit", ts, val, check_valid)

    def set_done(self, ts, val, check_valid=True):
        self.set_val("done", ts, val, check_valid)
        if val == 1:
            self.set_val("done_set", ts, True, check_valid)

    def set_flow(self, ts, flow_id, check_valid=True):
        self.set_val("flow", ts, flow_id, check_valid)

    def check_done(self, ts):
        if self.get_done() == 1:
            return
        limit = self.get_limit()
        count = self.get_count()
        if limit == 0 or count == 0:
            return
        if count >= limit:
            self.set_done(ts, 1)
            self.set_count(ts, count - limit)

    def wait_done(self, ts, insn):
        assert self.valid, f"Semaphore {self.sema_id} of wave {self.wave_coords} is not valid for wait_done"
        assert self.get_done() == 1, f"Semaphore {self.sema_id} is not done for wait_done ts={ts}, wave={insn.wave_dbg_id} insn={insn.insn_id:08x} {insn.disasm}"
        self.set_done(ts, 0)
        self.check_done(ts)

    def signal(self, ts):
        assert self.valid, f"Semaphore {self.sema_id} is not valid for signal"
        count = 1 + self.get_count()
        count %= 64
        self.set_count(ts, count)
        self.check_done(ts)

    def set_flow(self, insn):
        flow_id = Semaphore.flow_count
        self.update_flow(flow_id)        
        insn.sema_flow.append(flow_id)
        Semaphore.flow_count += 1

class SemaInsn:
    rm_comments_re = re.compile("\s*//.*")
    lit_re = re.compile("lit\((.*)\)")
    def __init__(self, wave_coords, wave_dbg_id, event, insn, prio = 100):
        self.disasm = SemaInsn.rm_comments_re.sub("", insn.disasm).strip()
        self.event = event
        self.ts = event['ts']
        self.wave_coords = wave_coords
        self.wave_dbg_id = wave_dbg_id
        self.operands = self._extract_operands()
        self.insn = insn
        self.prio = prio

    def _extract_braced_opts(self, operand_nr=0):
        assert self.operands[operand_nr][0] == "{" and self.operands[operand_nr][-1] == "}"
        opt_str = self.operands[operand_nr][1:-1]
        assert "{" not in opt_str and "}" not in opt_str
        return {k.strip(): v.strip() for k, v in [opt.split(":") for opt in opt_str.split(",")]}

    def _extract_operands(self):
        # remove insn opcode:
        try:
            operands_start = self.disasm.index(' ')
        except ValueError:
            return []
        opt_str = self.disasm[operands_start + 1:].strip()
        opts = []
        buf = []
        in_braces = False
        for ch in opt_str:
            if ch == '{':
                assert not in_braces, f"Nested braces not allowed: {opt_str}"
                in_braces = True
            elif ch == '}':
                in_braces = False
            if ch == ',' and not in_braces:
                operand = ''.join(buf).strip()
                if operand:
                    opts.append(operand)
                buf = []
                continue
            buf.append(ch)
        operand = ''.join(buf).strip()
        if operand:
            opts.append(operand)
        return opts

    def _extract_literal(self, operand):
        try:
            return int(operand, 0)
        except ValueError:
            pass
        m = SemaInsn.lit_re.match(operand)
        assert m is not None
        return int(m.group(1), 0)
                
    def __call__(self, wave_map):
        raise NotImplementedError("SemaInsn is an abstract class")

    def _get_sema(self, sema_id, what, wave_map, by_coords=True):
        wave_dict = wave_map['coord'] if by_coords else wave_map['dbg_id']
        try:
            wave = wave_dict[what]
        except:
            print (f"ERROR: {self.wave_dbg_id} {self.insn.insn_id:x} -  wave {what} not found by_coords={by_coords} ... vals={list(wave_map.keys())}")
            return None
        try:
            return wave.semas[sema_id]
        except:
            print (f"ERROR: {self.wave_dbg_id} {self.insn.insn_id:x} - sema {sema_id} not found for wave {what} by_coords={by_coords}")
            return None
        
    
class SemaSignal(SemaInsn):
    def __init__(self, wave_coords, wave_dbg_id, event, insn):
        super().__init__(wave_coords, wave_dbg_id, event, insn, 1)
        self.sema_id = event['sema_id']
        self.dst_wave_id = event['dst_wave_id']
        
    def __call__(self, wave_map):
        sema = self._get_sema(self.sema_id, self.dst_wave_id, wave_map, by_coords=False)
        if sema is None:
            return False
        VERBOSE_INSNS and print(f"SEMA_INSN: {self.disasm} Wave {self.wave_dbg_id} {self.wave_coords} insn_id={self.insn.insn_id:08x} SIGNAL to wave {self.dst_wave_id} sema_id={self.sema_id} at {self.ts}")        
        sema.signal(self.ts)
        sema.verbose(self.ts, "SIGNAL")
        sema.set_flow(self.insn)
        return True
        
class SemaWait(SemaInsn):
    def __init__(self, wave_coords, wave_dbg_id, event, insn):
        super().__init__(wave_coords, wave_dbg_id, event, insn)
        options = self._extract_braced_opts()
        self.sema_mask = int(options['sema_mask'])

    def __call__(self, wave_map):
        for i in VALID_SEMS:
            if (self.sema_mask  >> (i - 1)) & 1:
                sema = self._get_sema(i, self.wave_coords, wave_map)
                if sema is None:
                    return False
                VERBOSE_INSNS and print(f"SEMA_INSN: {self.disasm} Wave {self.wave_dbg_id} {self.wave_coords} insn_id={self.insn.insn_id:08x} WAIT on sema_id={i} at {self.ts}")
                sema.wait_done(self.ts, self.insn)
                sema.verbose(self.ts, "WAIT")
                sema.set_flow(self.insn)
        return True

class SemaSetLimit(SemaInsn):
    def __init__(self, wave_coords, wave_dbg_id, event, insn):
        super().__init__(wave_coords, wave_dbg_id, event, insn, 0)
        options = self._extract_braced_opts()
        self.limit = int(options['limit'])
        self.sema_id = int(options['sema_id'])
        assert self.sema_id == event['sema_id']

    def __call__(self, wave_map):
        sema = self._get_sema(self.sema_id, self.wave_coords, wave_map)
        if sema is None:
            return False
        VERBOSE_INSNS and print(f"SEMA_INSN: {self.disasm} Wave {self.wave_dbg_id} {self.wave_coords} insn_id={self.insn.insn_id:08x} SET LIMIT {self.limit} on sema_id={self.sema_id} at {self.ts}")
        sema.set_limit(self.ts, self.limit)
        sema.check_done(self.ts)
        sema.verbose(self.ts, "SET LIMIT")
        sema.set_flow(self.insn)
        return True

class SemaSetReg(SemaInsn):
    def __init__(self, wave_coords, wave_dbg_id, event, insn):
        super().__init__(wave_coords, wave_dbg_id, event, insn, 0)
        self.sema_id = event['sema_id']
        self.data = event['data']

    def __call__(self, wave_map):
        sema = self._get_sema(self.sema_id, self.wave_coords, wave_map)
        if sema is None:
            return False
        new_count, new_limit, new_done = self.data
        VERBOSE_INSNS and print(f"SEMA_INSN: {self.disasm} Wave {self.wave_dbg_id} {self.wave_coords} insn_id={self.insn.insn_id:08x} SET REG on sema_id={self.sema_id} at {self.ts}: count={new_count} limit={new_limit} done={new_done}")
        sema.valid = True
        sema.set_wave_dbg_id(self.ts, self.wave_dbg_id)
        sema.set_count(self.ts, new_count, check_valid=False)
        sema.set_limit(self.ts, new_limit, check_valid=False)
        sema.set_done(self.ts, new_done, check_valid=False)
        sema.check_done(self.ts)
        sema.verbose(self.ts, "SET REG")
        sema.set_flow(self.insn)
        return True

class WaveSemaphores:
    def __init__(self, wave_coords):
        self.wave_coords = wave_coords
        self.insns = []
        self.semas = {i: Semaphore(i, wave_coords) for i in VALID_SEMS}

    def __call__(self, wave_dbg_id, wave):
        wave_insns = list(wave.insns.items())
        for insn_nr, insn_entry in enumerate(wave_insns):
            insn_id, insn = insn_entry
            sema_events = insn.sema_events
            for e in sema_events:
                if e['type'] == "SETREG":
                    new_insn = SemaSetReg(self.wave_coords, wave_dbg_id, e, insn)
                elif e['type'] == "SETLIMIT":
                    new_insn = SemaSetLimit(self.wave_coords, wave_dbg_id, e, insn)
                elif e['type'] == "WAIT_DONE":
                    new_insn = SemaWait(self.wave_coords, wave_dbg_id, e, insn)
                elif e['type'] == "SIGNAL":
                    new_insn = SemaSignal(self.wave_coords, wave_dbg_id, e, insn)
                else:
                    raise RuntimeError(f"Bad sema event {e['type']}")
                
                self.insns.append(new_insn)
    
class SemaphoresParser:
    def __init__(self, parser: ItraceParser):
        self.parser = parser
        self.wave_coord_map = {}
        self.wave_dbg_id_map = {}
        self.wave_map = {'coord': self.wave_coord_map, 'dbg_id': self.wave_dbg_id_map}

    def __call__(self):
        # extract all semaphore related instructions
        self.__parse_wave_insns()

        # get all insns, sorted
        all_insns = []
        for w in self.wave_coord_map.values():
            all_insns.extend(w.insns)
        all_insns.sort(key=lambda x: (x.ts, x.prio))
        # and populate semaphore values
        for i in all_insns:
            if not i(self.wave_map):
                print(f"ERROR: {i.wave_dbg_id} {i.insn.insn_id:08x} - {i.disasm} failed to parse")
                return False
        return True

    def __parse_wave_insns(self):
        for se, sas in self.parser.se().items():
            for sa in sas:
                sa_id = (se, sa)
                for wave in sas[sa].values():
                    wave_coords = (se, sa, wave.wgp, wave.simd, wave.wave_nr)
                    if wave_coords in self.wave_coord_map:
                        w = self.wave_coord_map[wave_coords]
                    else:
                        w = WaveSemaphores(wave_coords)
                        self.wave_coord_map[wave_coords] = w
                    self.wave_dbg_id_map[wave.wave_id] = w
                    w(wave.wave_id, wave)
