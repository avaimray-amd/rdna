import csv
import math
import re
import statistics
import sys
import yaml

CLOCK = "model.gpu0.sclk"
        
class PerfCountersParser:
    def __init__(self, filename, conf_simple=None, conf_yaml=None, first_wave_start=0, last_wave_end=None, filter_repeated=True, filter_zero_only=False):
        self.__include = []
        self.__exclude = []
        self.__aliases = {}
        self.__regex_stats = {}
        self.__expr_stats = {}
        self.__defined_counters = {}
        self.__view = 'default'
        self.__filter_repeated = filter_repeated
        self.__filter_zero_only = filter_zero_only
        if conf_simple is not None:
            self.__parse_conf_simple(conf_simple)
        elif conf_yaml is not None:
            self.__parse_conf_yaml(conf_yaml)
        self.counters = self.__parse_csv(filename, first_wave_start, last_wave_end)
        

    def __parse_conf_simple(self, filename):
        with open(filename, 'r') as f:
            for l in f:
                l = l.strip()
                if l.startswith('#') or len(l) == 0:
                    continue
                if l.startswith('!'):
                    self.__exclude.append(re.compile(l[1:].strip()))
                else:
                    self.__include.append(re.compile(l))

    def __parse_conf_yaml(self, filename):
        VIEW_VALUES = ('default', 'defined_only', 'defined_first')
        errors = []
        with open(filename, 'r') as f:
            data = yaml.safe_load(f)
        if data is None:
            return
        config = data.get('config') or {}
        view = config.get('view', 'default')
        if view not in VIEW_VALUES:
            errors.append(f"config.view must be one of {VIEW_VALUES!r}, got {view!r}")
        else:
            self.__view = view
        stats = data.get('stats', [])
        if not isinstance(stats, list):
            errors.append(f"stats must be a list, got {type(stats)}")
            stats=[]
        for idx, item in enumerate(stats):
            if not isinstance(item, dict):
                continue
            if 'select' in item:
                pattern = item['select']
                if pattern is None:
                    errors.append(f"stats[{idx}]: select value is None")
                self.__include.append(re.compile(pattern))
            if 'deselect' in item:
                pattern = item['deselect']
                if pattern is None:
                    errors.append(f"stats[{idx}]: deselect value is None")
                self.__exclude.append(re.compile(pattern))
            if 'alias' in item:
                alias_key = item['alias']
                target_val = item.get('target', None)
                if alias_key is None:
                    errors.append(f"stats[{idx}]: alias key is None")
                if target_val is None:
                    errors.append(f"stats[{idx}]: target value is None for alias '{alias_key}'")
                if alias_key in self.__aliases:
                    errors.append(f"stats[{idx}]: duplicate alias key '{alias_key}'")
                self.__aliases[target_val] = alias_key
                if target_val in self.__defined_counters:
                    errors.append(f"stats[{idx}]: counter alias already defined")
                self.__defined_counters[target_val] = "alias"
            if 'stat' in item and 'regex' in item:
                stat_name = item['stat']
                regex_val = item['regex']
                if stat_name is None:
                    errors.append(f"stats[{idx}]: regex stat name is None")
                if regex_val is None:
                    errors.append(f"stats[{idx}]: regex value is None for stat '{stat_name}'")
                if stat_name in self.__regex_stats:
                    errors.append(f"stats[{idx}]: duplicate regex stat '{stat_name}'")
                op = item.get('op', 'sum')
                if op not in ['sum', 'mean', 'min', 'max']:
                    errors.append(f"stats[{idx}]: invalid operation '{op}' for regex stat '{stat_name}'")                   
                self.__regex_stats[stat_name] = {'regex': regex_val, 'op': op}
                if stat_name in self.__defined_counters:
                    errors.append(f"stats[{idx}]: counter regex already defined")
                self.__defined_counters[stat_name] = "regex"
            if 'stat' in item and 'expr' in item:
                stat_name = item['stat']
                expr_val = item['expr']
                if stat_name is None:
                    errors.append(f"stats[{idx}]: expr stat name is None")
                if expr_val is None:
                    errors.append(f"stats[{idx}]: expr value is None for stat '{stat_name}'")
                if stat_name in self.__expr_stats:
                    errors.append(f"stats[{idx}]: duplicate expr stat '{stat_name}'")
                self.__expr_stats[stat_name] = expr_val
                if stat_name in self.__defined_counters:
                    errors.append(f"stats[{idx}]: counter regex already defined")
                self.__defined_counters[stat_name] = "expr"
        if errors:
            print(f"{filename} parsing failed with {len(errors)} error(s):", file=sys.stderr)
            print("\n".join(errors), file=sys.stderr)
            sys.exit(1)


    def __parse_csv(self, filename, first_wave_start, last_wave_end):
        # Large buffer for very large counter CSV files (reduces syscalls)
        with open(filename, 'r', buffering=8*1024*1024) as csvfile:  # 8MB
            csv_data = csv.DictReader(csvfile)
            csv_fields = [ i for i in csv_data.fieldnames  if i != "row_id" and len(i) > 0]
            
            # prepare regex (get list of matching counters) and expr stats (get string sent to eval)
            counters_for_regex = self.__get_counters_for_regex(csv_fields)
            expr_str = self.__get_expr_str(csv_fields)

            # filter counters from csv
            csv_fields = self.__filter_csv_counters(csv_fields)

            # initialize empty data
            defined_counters = list(self.__defined_counters.keys())
            if self.__view == "default":
                all_counters = csv_fields + defined_counters
            else: 
                all_counters = defined_counters + csv_fields
            data = {f : [] for f in all_counters}

            # fill data
            cumulative_time = 0
            for i, row in enumerate(csv_data):
                # Stop processing if we've passed last_wave_end
                if last_wave_end is not None and cumulative_time > last_wave_end:
                    break
                    
                # counters from csv
                for f in csv_fields:
                    new_data = self._extract_data(row, f, i)
                    self.__update_data(data, new_data, f, cumulative_time, first_wave_start)

                # defined counters:
                for counter, counter_type in self.__defined_counters.items():
                    if counter_type == "alias":
                        alias = self.__aliases[counter]
                        if alias in row:
                            new_data = self._extract_data(row, alias, i, data)
                            self.__update_data(data, new_data, counter, cumulative_time, first_wave_start)
                        else:
                            print(f"Error: alias '{alias}' for {counter} not found in data", file=sys.stderr)
                            sys.exit(1)
                    elif counter_type == "regex":
                        d = [ self._extract_data(row, f, i, data) for f in counters_for_regex[counter]]
                        new_data = self.__apply_op(d, self.__regex_stats[counter]['op'])
                        self.__update_data(data, new_data, counter, cumulative_time, first_wave_start)
                        row[counter] = new_data # in case they are used in the expr
                    elif counter_type == "expr":
                        new_data = self.__eval_expr(counter, expr_str[counter], row, data, i)
                        self.__update_data(data, new_data, counter, cumulative_time, first_wave_start)
                    else:
                        raise RuntimeError (f"bad counter type {counter_type}")
                
                # clock
                try:
                    clock = int(row[CLOCK])
                except:
                    print(f"Warning: could not convert value for counter {CLOCK} to int for row {i+1}...setting to 0", file=sys.stderr)
                    clock = 0
                cumulative_time += clock

        # remove counters that don't change
        data = {i: j for i,j in data.items() if len(j) > 1}
        print(f"Using {len(data)} counters", file=sys.stderr)
        return data
    
    def __filter_csv_counters(self, csv_fields):
        if len(self.__include) > 0 or self.__view == "defined_only":
            csv_fields = [ i for i in csv_fields if any(r.match(i) is not None for r in self.__include)]
        csv_fields = [ i for i in csv_fields if not any(r.match(i) is not None for r in self.__exclude)]
        return csv_fields

    def __get_counters_for_regex(self, csv_fields):
        all_counters = csv_fields + list(self.__defined_counters.keys())
        counters_for_regex = {}
        for counter, conf in self.__regex_stats.items():
            regex = re.compile(conf['regex'])
            matching = [name for name in all_counters if regex.match(name)]
            if len(matching) > 0:
                counters_for_regex[counter] = matching
            else:
                print(f"Warning: no matching counters found for regex {regex}...removing", file=sys.stderr)
                del self.__defined_counters[counter]
        return counters_for_regex

    def __get_expr_str(self, csv_fields):
        all_counters = csv_fields + list(self.__defined_counters.keys())
        expr_str = {}

        for counter, eval_str in self.__expr_stats.items():
            # Substitute counter names with row values (boundaries prevent partial matches)
            for key in all_counters:
                # Replace whole-token occurrences only (identifier boundaries)
                src_pattern = r'(?<![a-zA-Z0-9_.])' + re.escape(key) + r'(?![a-zA-Z0-9_.])'
                dst_pattern = f'self._extract_data(row, "{key}", row_idx, parsed_data)'
                eval_str = re.sub(src_pattern, dst_pattern, eval_str)
            expr_str[counter] = eval_str
        return expr_str

    def __apply_op(self, d, op):
        if op == 'sum':
            return sum(d)
        elif op == 'mean':
            return sum(d) / len(d)
        elif op == 'min':
            return min(d)
        elif op == 'max':
            return max(d)
        assert False, "invalid operation {op}"

    def __eval_expr(self, counter, expr, row, parsed_data, row_idx):
        # Allow math module and mean (e.g. math.sqrt(a), mean(a) -> statistics.mean)
        safe_builtins = {'abs': abs, 'min': min, 'max': max, 'sum': sum, 'round': round}
        eval_ns = {'math': math, 'avg': statistics.mean, **safe_builtins}
        try:
            return eval(expr, eval_ns, locals())
        except Exception as e:
            orig_expr = self.__expr_stats[counter]
            print(f"Error: could not evaluate expression {orig_expr} for {counter} (evaluated as {expr}): {e}", file=sys.stderr)
            sys.exit(1)

    def _extract_data(self, csv_data, field_name, row_idx, parsed_data=None):
        if parsed_data is not None and field_name in parsed_data:
            return parsed_data[field_name][-1][-1]
        try:
            return float(csv_data[field_name])
        except ValueError:
            print(f"Warning: could not convert value for counter {field_name} to float for row {row_idx+1}...setting to 0", file=sys.stderr)
            return 0

    def __update_data(self, data, new_data, field_name, cumulative_time, first_wave_start):
        # Keep updating baseline value before first_wave_start
        # After first_wave_start, only track changes
        if cumulative_time < first_wave_start:
            data[field_name] = [ (cumulative_time, new_data)]  # Keep last value before first_wave_start as baseline
        elif len(data[field_name]) == 0 or data[field_name][-1][-1] != new_data or not self.__filter_repeated or (new_data != 0 and self.__filter_zero_only):
            data[field_name].append( (cumulative_time, new_data) )
    
