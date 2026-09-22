#!/usr/bin/env python3
import argparse
import re
from perf_counters_parser import PerfCountersParser


if __name__ == "__main__":
    arg_parser = argparse.ArgumentParser()
    arg_parser.add_argument('-i', '--input', required=True, type=str,)
    arg_parser.add_argument('-c', '--counters-regexs', help="counters to extract match this regex", type=str, nargs='+', required=True)
    arg_parser.add_argument('-nf', '--no-filter-repeated', help="do not filter repeated values", action="store_false")
    arg_parser.add_argument('-fz', '--filter-zero-only', help="filter values = 0... overrides filter-repeated=False", action="store_true")
    args = arg_parser.parse_args()

    c_re = [re.compile(i) for i in args.counters_regexs]
    parser = PerfCountersParser(args.input, filter_repeated=args.no_filter_repeated, filter_zero_only=args.filter_zero_only)
    counters = {i: j for i,j in parser.counters.items() if any(j.search(i) for j in c_re) }

    for cname, data in counters.items():
        print(f"#{cname}")
        for t, v in data:
            print(f"{t:010d} {v}")
