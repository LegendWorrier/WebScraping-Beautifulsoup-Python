import fnmatch
import math
import operator
import pandas as pd
import re

from utils import *

if optional_import("pyparsing"): from pyparsing import *

logger = logging.getLogger('bamboo')

# Various pandas utility functions

def _make_filter(filter):
    return (lambda x: True) if filter is None else filter if callable(filter) else FilterExpression.make_filter(filter) 

def _filter_columns(df, filter):
    """Filter columns using a name, list of names or name predicate."""
    return df.select(filter, axis=1) if callable(filter) else df.filter(make_iterable(filter), axis=1)

def _filter_rows(df, filter):
    """Filter rows using either a row predicate or a RecordFilter expression."""
    return df[df.apply(_make_filter(filter), axis=1)]

def _assign_rows(df, assign_if=None, **kwargs):
    """Assign or update columns using row function, with an optional row predicate condition."""
    return df.assign(**{k : (lambda df, k=k, fn=fn: [(fn(r) if callable(fn) else fn) if _make_filter(assign_if)(r) else r.get(k) for _,r in df.iterrows()]) for k,fn in kwargs.items()})

def _update_columns(df, update_if=None, **kwargs):
    """Update columns using a value function, with an optional value predicate condition, or True to update just non-nans."""
    return df.assign(**{k : (lambda df, k=k, fn=fn: [(fn(r[k]) if callable(fn) else fn) if (update_if is None or callable(update_if) and update_if(r[k]) or isinstance(update_if, bool) and update_if != none_or_nan(r[k])) else r.get(k) for _,r in df.iterrows()]) for k,fn in kwargs.items()})
    
def _groupby_rows(df, by):
    """Group rows using a row function, map, list or column name."""
    return df.groupby(lambda i: by(df.ix[i]) if callable(by) else by[i] if non_string_iterable(by) else df.ix[i].get(by))

def _split_rows(df, by):
    """Split rows by column, making one copy for each item in the column value."""
    return pd.DataFrame(pd.Series(assoc_in(row, [by], v)) for _, row in df.iterrows() for v in make_iterable(row[by]))

def _split_columns(df, columns, delimiter, converter=identity):
    """Split column string values into tuples with the given delimiter."""
    return df.update_columns(**{column : ignoring_exceptions(lambda s: tuple(converter(x) for x in s.split(delimiter)), (), (AttributeError)) for column in make_iterable(columns) })
    
pd.DataFrame.filter_columns = _filter_columns
pd.DataFrame.filter_rows = _filter_rows
pd.DataFrame.assign_rows = _assign_rows
pd.DataFrame.update_columns = _update_columns
pd.DataFrame.groupby_rows = _groupby_rows
pd.DataFrame.split_rows = _split_rows
pd.DataFrame.split_columns = _split_columns

# standalone functions

def prompt_for_value(default=math.nan, prompt=lambda r: r.to_dict()):
    """Row update function for a new field value.
    - default is either a value or a function on rows, used when no value is provided.
    - prompt is either a value or function on rows."""
    defaulter = default if callable(default) else lambda r: default
    def updater(r):
        v = input('[{}] = '.format(prompt(r)))
        if v == '': v = defaulter(r)
        return v
    return updater

# filter expressions

class FilterExpression:
    """Filter factory based on filter expressions such as "name~John and (age>18 or consent:true)".
    Filters consist of:
        - boolean expressions using and, not, or and parentheses.
        - field expressions, which consist of:
          - a key name (with optional wildcards)
          - an operator
          - a value (with type appropriate to the operator)
        - operators are one of:
          - numeric comparisons: =, !=, <, <=, >, >=
          - length comparisons: #=, #<, etc
          - string comparisons: =, !=, ~ (regex match), !~
          - containment: >> (contains), !>>
          - existence: KEY:exists (has value that's not NaN), KEY:true (has true value)
    """
    
    sign = oneOf('+ -')
    integer = Word(nums)
    number_base = (integer + Optional('.' + Optional(integer))('float')) | Literal('.')('float') + integer
    number_exponent = CaselessLiteral('E')('float') + Optional(sign) + integer
    number = Combine( Optional(sign) + number_base + Optional(number_exponent) ).setParseAction(lambda t: float(t[0]) if t.float else int(t[0]))

    def onlen(f): return lambda x,y: f(len(x), y)
    num_ops = { '<': operator.lt, '<=': operator.le,
                '=': operator.eq, '!=': operator.ne,
                '>': operator.gt, '>=': operator.ge,
                '#<': onlen(operator.lt), '#<=': onlen(operator.le),
                '#=': onlen(operator.eq), '#!=': onlen(operator.ne),
                '#>': onlen(operator.gt), '#>=': onlen(operator.ge) }
    str_ops = { '=': lambda x,y: x==str(y), '!=': lambda x,y: x!=str(y),
                '~': lambda x,y: re.search(y,str(x)), '!~': lambda x,y: not re.search(y,str(x)),
                '>>': lambda x,y: y in x, '!>>': lambda x,y: y not in x }
    exist_ops = { ':': lambda x,y: { 'exists': not none_or_nan(x), 'true': bool(x) }[y.lower()] }
                
    def oneOfOpMap(map): return oneOf(list(map.keys())).setParseAction(lambda t: ignoring_exceptions(map[t[0]], False))
    num_op = oneOfOpMap(num_ops)
    str_op = oneOfOpMap(str_ops)
    exist_op = oneOfOpMap(exist_ops)

    quoted_string = QuotedString('"', '\\') | QuotedString("'", '\\')
    key_value = Word(alphas + alphas8bit + '*?[]') | quoted_string
    str_value = Word(alphas + alphas8bit) | quoted_string
    exist_value = CaselessLiteral('True') | CaselessLiteral('Exists')

    base_expr = (key_value + ( str_op + str_value | num_op + number | exist_op + exist_value)).setParseAction(lambda t: [t])
    expr = operatorPrecedence(base_expr, [(Literal('not').setParseAction(lambda t: operator.not_), 1, opAssoc.RIGHT),
                                          (Literal('and').setParseAction(lambda t: operator.and_), 2, opAssoc.LEFT),
                                          (Literal('or').setParseAction(lambda t: operator.or_), 2, opAssoc.LEFT)])

    @classmethod
    def _eval_parse(cls, parse, d):
        if not isinstance(parse, list):
            return parse
        elif len(parse) == 1:
            return cls._eval_parse(parse[0], d)
        elif callable(parse[0]):
            return parse[0](cls._eval_parse(parse[1], d))
        else:
            x, y = cls._eval_parse(parse[0], d), cls._eval_parse(parse[2], d)
            if isinstance(x, str):
                return any(parse[1](d[k], y) for k in d.keys() if fnmatch.fnmatch(k, x))
            else:
                return parse[1](x, y)
            
    @classmethod
    def make_filter(cls, string):
        """Generates a filter function from a filter expression."""
        parse = cls.expr.parseString(string, parseAll=True).asList()
        return lambda d: cls._eval_parse(parse, d)

rs = [ { "name": "Fred", "surname": "Flintstone", "children": 2 },
       { "name": "Wilma", "surname": "Flintstone", "children": 2 },
       { "name": "Dino", "children": 15 } ]
df = pd.DataFrame(rs)