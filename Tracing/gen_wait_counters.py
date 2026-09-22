#!/usr/bin/env python3
import subprocess
import json

class GenWaitCounters:

    # counters with custom count or whose name we cannot extract directly
    counter_inc_dict = dict( 
        EV_DEC_EXPCNT_DIRECT=("expcnt", 1),
        EV_DEC_KMCNT_MESSAGE=("kmcnt", 1),
        EV_DEC_KMCNT_SMEM=("kmcnt", 1),
        EV_DEC_KMCNT_SMEM_2=("kmcnt", 2),
        EV_DEC_KMCNT_SMEM_4=("kmcnt", 4),
        EV_DEC_LOADCNT=("loadcnt", 1),
        EV_DEC_STORECNT_ATOMIC=("storecnt", 1)
    )
    
    def __init__(self):
        """
        Runs 'sp3help.pl -n "EV_DEC*CNT" --nocolor -y -description', captures stdout, and parses to extract available counter names.
        Returns a list of counter names matching the pattern.
        """

        try:
            output = subprocess.check_output(
                ['sp3help.pl', '-n', 'EV_DEC*CNT*', '-f', 'OPF_ASYNCCNT','--nocolor', '-y', '-description'],
                universal_newlines=True
            )
        except Exception as e:
            raise RuntimeError(f"Failed to run sp3help.pl: {e}")


        self.counters = {}  # Dictionary: counter name -> list of opcodes

        lines = output.splitlines()
        current_counter = None
        current_counter_inc = 1
        in_opcodes = False

        for i, line in enumerate(lines):
            # Counter name
            if line.startswith("EV_DEC_"):
                current_counter, current_counter_inc  = self.counter_name_and_inc(line.strip())
                if current_counter not in self.counters:
                    self.counters[current_counter] = {}
                continue
            if line.startswith("OPF_ASYNCCNT"):
                current_counter = "asynccnt"
                current_counter_inc = 1
                self.counters[current_counter] = {}
                continue

            line = line.strip('- ')
            if line.startswith("Opcodes ("):
                in_opcodes = True
                continue

            if in_opcodes:
                if line == '':
                    in_opcodes = False
                else:
                    self.counters[current_counter][line.strip()] = current_counter_inc

    def counter_name_and_inc(self, k):
        if k in GenWaitCounters.counter_inc_dict:
            return GenWaitCounters.counter_inc_dict[k]
        
        name = k[len('EV_DEC_'):].lower()
        return name, 1

    def __call__(self, outfile):
        with open(outfile, 'w') as f:
            json.dump(self.counters, f, indent=4)

    
if __name__ == "__main__":
    gen_wait_counters = GenWaitCounters()
    gen_wait_counters("wait_counters.json")
