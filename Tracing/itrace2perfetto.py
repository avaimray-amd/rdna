#!/usr/bin/env python3
import sys
import argparse
import os
import traceback

from itrace_parser import ItraceParser
from gclog_parser import GclogParser
from perfetto_trace import PerfettoTrace
from perf_counters_parser import PerfCountersParser
from semaphores import SemaphoresParser


def main(argv=None):
    arg_parser = argparse.ArgumentParser()
    arg_parser.add_argument('-o', '--output', required=True, type=str)
    arg_parser.add_argument('-i', '--input', nargs='+', type=str)
    arg_parser.add_argument('-g', '--gclog', type=str, help='GC log input file')
    arg_parser.add_argument('-e', '--event-regexs', help="event if disasm matches regex", type=str, nargs='+', default=[])
    arg_parser.add_argument('-en', '--event-names', help="name of the event in the trace", type=str, nargs='+', default=[])
    arg_parser.add_argument('-ea', '--event-counters-analog', help="show ev counters as a plot", action='store_true')
    arg_parser.add_argument('-ef', '--events-from-file', help="file containing events to add to the trace", type=str, nargs='+')
    arg_parser.add_argument('-ec', '--event-connections', help="yaml file configuring connections between events", type=str)
    arg_parser.add_argument('-se', '--shader-engines', nargs='+', type=int)
    arg_parser.add_argument('-sa', '--shader-arrays',  nargs='+', type=int)
    arg_parser.add_argument('-w', '--wgps', nargs='+', type=int)
    arg_parser.add_argument('-sd', '--simds', nargs='+', type=int)
    arg_parser.add_argument('-p', '--perf-counters-csv', nargs='+', type=str)
    perf_conf_group = arg_parser.add_mutually_exclusive_group()
    perf_conf_group.add_argument('-pc', '--perf-counters-conf', type=str, help='legacy text config for perf counters')
    perf_conf_group.add_argument('-pcy', '--perf-counters-conf-yaml', type=str, help='YAML config for perf counters')
    arg_parser.add_argument("-sem", "--semaphores", action="store_true", help="Add semaphores to the trace")
    arg_parser.add_argument("--ignore-wave-boundaries", action="store_true", help="Ignore first_wave_start and last_wave_end when processing perf counters")
    arg_parser.add_argument("-ws", "--wave-summary", type=str, help="Optional filename where to save the wave summary table")
    arg_parser.add_argument("--print-unknown-lines", action="store_true", help="Print itrace lines that are not recognized")
    args = arg_parser.parse_args(argv)

    if args.input is None and args.perf_counters_csv is None and args.gclog is None:
        print("At least one option --input, --perf-counters-csv, or --gclog is required", file=sys.stderr)
        sys.exit(1)

    if len(args.event_names) > len(args.event_regexs):
        print("Too many event names", file=sys.stderr)
        sys.exit(1)
        
    if args.input is not None:
        print("Parsing itrace...", file=sys.stderr)
        itrace_parser = ItraceParser(print_unknown_lines=args.print_unknown_lines)
        # Enable parallel processing for multiple files (set parallel=False to disable)
        use_parallel = len(args.input) > 1
        itrace_parser(args.input, args.shader_engines, args.shader_arrays, args.wgps, args.simds, parallel=use_parallel, wave_summary_file=args.wave_summary)
        if args.ignore_wave_boundaries:
            first_wave_start = 0
            last_wave_end = None
        else:
            first_wave_start = itrace_parser.first_wave_start()
            last_wave_end = itrace_parser.last_wave_end()
    else:
        itrace_parser = None
        first_wave_start = 0
        last_wave_end = None
        if args.semaphores:
            print("ERROR: --semaphores is not supported when --input is not provided", file=sys.stderr)
            sys.exit(1)

    gclog_parser = None
    if args.gclog is not None:
        print("Parsing gclog...", file=sys.stderr)
        gclog_parser = GclogParser()
        gclog_parser(args.gclog, itrace_parser)

    if args.perf_counters_csv is not None:
        print("Parsing perf counters...", file=sys.stderr)
        counters = []
        for csv_file in args.perf_counters_csv:
            basename = os.path.basename(csv_file)
            # Remove .csv extension if present
            if basename.endswith('.csv'):
                basename = basename[:-4]
            parser = PerfCountersParser(csv_file, args.perf_counters_conf, args.perf_counters_conf_yaml, first_wave_start, last_wave_end)
            counters.append((basename, parser))
    else:
        counters = None

    if gclog_parser is not None and gclog_parser.counters:
        print("Parsing gclog...", file=sys.stderr)
        if counters is None:
            counters = []
        counters.append(('gclog', gclog_parser))

    if args.semaphores:
        print("Parsing semaphores...", file=sys.stderr)
        semaphores = SemaphoresParser(itrace_parser)
        try:
            ok = semaphores()
        except Exception as e:
            ok = False
            tb = traceback.extract_tb(e.__traceback__)
            last_call = tb[-1]
            print (f"xcpt while parsing sems: {e} in {last_call.filename}:{last_call.lineno}")
            
        if not ok:
            print("ERROR parsing semaphores => they won't be added to the trace")
            semaphores = None
    else:
        semaphores = None
    
    print("Creating pftrace...", file=sys.stderr)
    trace = PerfettoTrace(itrace_parser, counters, semaphores, args.event_regexs, args.event_names, args.event_counters_analog, args.event_connections, args.events_from_file, gclog_parser)
    trace(args.output)
    print(f"Created {args.output}. Open with https://ui.perfetto.dev", file=sys.stderr)


if __name__ == "__main__":
    main()
